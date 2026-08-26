#include "realtime_companion.h"

#include "esphome/components/audio/audio.h"
#include "esphome/core/log.h"

#include <cJSON.h>
#include <esp_timer.h>

#include <algorithm>
#include <cstring>

namespace esphome::realtime_companion {

static const char *const TAG = "realtime_companion";

const int16_t FirDecimator2::COEFFICIENTS[TAPS] = {
    2, 9, -12, -54, 16, 180, 45, -426, -303, 787, 991, -1191, -2691, 1514, 10144, 14746,
    10144, 1514, -2691, -1191, 991, 787, -303, -426, 45, 180, 16, -54, -12, 9, 2,
};

void FirDecimator2::reset() {
  memset(this->history_, 0, sizeof(this->history_));
  this->position_ = 0;
  this->phase_ = false;
}

void FirDecimator2::push(int16_t sample, int16_t *output, bool *ready) {
  this->history_[this->position_] = sample;
  this->position_ = (this->position_ + 1) % TAPS;
  this->phase_ = !this->phase_;
  *ready = this->phase_;
  if (!*ready)
    return;
  int64_t accumulator = 0;
  size_t cursor = this->position_;
  for (size_t tap = 0; tap < TAPS; tap++) {
    cursor = cursor == 0 ? TAPS - 1 : cursor - 1;
    accumulator += static_cast<int32_t>(this->history_[cursor]) * COEFFICIENTS[tap];
  }
  accumulator = (accumulator + (1 << 14)) >> 15;
  *output = static_cast<int16_t>(std::clamp<int64_t>(accumulator, INT16_MIN, INT16_MAX));
}

void RealtimeCompanion::setup() {
  this->capture_queue_ = xQueueCreateStatic(6, sizeof(InputFrame), this->capture_queue_storage_,
                                             &this->capture_queue_struct_);
  this->playback_queue_ = xQueueCreateStatic(10, sizeof(OutputFrame), this->playback_queue_storage_,
                                              &this->playback_queue_struct_);
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 2, 24000));
  this->speaker_->add_audio_output_callback([this](uint32_t frames, int64_t) {
    if (!this->stream_id_.empty())
      this->played_frames_.fetch_add(frames, std::memory_order_relaxed);
  });
  this->microphone_->add_data_callback(
      [this](const std::vector<uint8_t> &data) { this->handle_microphone_data(data); });
  this->microphone_->start();
  this->connect();
}

void RealtimeCompanion::connect() {
  if (this->client_ != nullptr)
    esp_websocket_client_destroy(this->client_);
  esp_websocket_client_config_t config{};
  config.uri = this->url_.c_str();
  config.network_timeout_ms = 5000;
  config.reconnect_timeout_ms = 2000;
  config.disable_auto_reconnect = false;
  config.buffer_size = 2048;
  this->client_ = esp_websocket_client_init(&config);
  if (this->client_ == nullptr) {
    this->update_state(CompanionState::ERROR);
    return;
  }
  esp_websocket_register_events(this->client_, WEBSOCKET_EVENT_ANY, &RealtimeCompanion::websocket_event, this);
  esp_websocket_client_start(this->client_);
  this->last_connect_attempt_ms_ = millis();
}

void RealtimeCompanion::websocket_event(void *handler_args, esp_event_base_t, int32_t event_id, void *event_data) {
  static_cast<RealtimeCompanion *>(handler_args)
      ->handle_websocket_event(event_id, static_cast<esp_websocket_event_data_t *>(event_data));
}

void RealtimeCompanion::handle_websocket_event(int32_t event_id, esp_websocket_event_data_t *event) {
  if (event_id == WEBSOCKET_EVENT_CONNECTED) {
    this->authenticated_ = false;
    this->auth_pending_.store(true, std::memory_order_release);
  } else if (event_id == WEBSOCKET_EVENT_DISCONNECTED) {
    this->authenticated_ = false;
    this->auth_pending_.store(false, std::memory_order_release);
    this->session_start_pending_.store(false, std::memory_order_release);
    if (this->session_active_)
      this->update_state(CompanionState::CONNECTING);
  } else if (event_id == WEBSOCKET_EVENT_DATA && event->payload_offset == 0 &&
             event->data_len == event->payload_len) {
    if (event->op_code == 0x2 && event->data_len == INPUT_FRAME_BYTES) {
      OutputFrame output{};
      auto *mono = reinterpret_cast<const int16_t *>(event->data_ptr);
      auto *stereo = reinterpret_cast<int16_t *>(output.data.data());
      for (size_t i = 0; i < INPUT_FRAME_BYTES / 2; i++)
        stereo[2 * i] = stereo[2 * i + 1] = mono[i];
      if (xQueueSend(this->playback_queue_, &output, 0) != pdTRUE)
        ESP_LOGW(TAG, "Playback queue full; dropping frame");
    } else if (event->op_code == 0x1) {
      this->handle_text(event->data_ptr, event->data_len);
    }
  }
}

void RealtimeCompanion::handle_text(const char *data, size_t length) {
  std::string message(data, length);
  cJSON *root = cJSON_ParseWithLength(message.data(), message.size());
  if (root == nullptr)
    return;
  cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
  const char *kind = cJSON_IsString(type) ? type->valuestring : "";
  if (strcmp(kind, "auth.ok") == 0) {
    this->authenticated_ = true;
    if (this->session_active_)
      this->session_start_pending_.store(true, std::memory_order_release);
  } else if (strcmp(kind, "session.started") == 0) {
    this->update_state(this->muted_ ? CompanionState::MUTED : CompanionState::LISTENING);
  } else if (strcmp(kind, "session.ended") == 0) {
    this->session_active_ = false;
    this->flush_playback();
    this->update_state(this->muted_ ? CompanionState::MUTED : CompanionState::IDLE);
  } else if (strcmp(kind, "state") == 0) {
    cJSON *state = cJSON_GetObjectItemCaseSensitive(root, "state");
    if (cJSON_IsString(state)) {
      if (strcmp(state->valuestring, "thinking") == 0) this->update_state(CompanionState::THINKING);
      if (strcmp(state->valuestring, "speaking") == 0) this->update_state(CompanionState::SPEAKING);
      if (strcmp(state->valuestring, "listening") == 0) this->update_state(CompanionState::LISTENING);
      if (strcmp(state->valuestring, "error") == 0) this->update_state(CompanionState::ERROR);
    }
  } else if (strcmp(kind, "playback.start") == 0) {
    cJSON *stream = cJSON_GetObjectItemCaseSensitive(root, "stream_id");
    this->stream_id_ = cJSON_IsString(stream) ? stream->valuestring : "";
    this->played_frames_.store(0, std::memory_order_relaxed);
    this->expected_duration_ms_ = 0;
    this->speaker_->start();
  } else if (strcmp(kind, "playback.end") == 0) {
    cJSON *duration = cJSON_GetObjectItemCaseSensitive(root, "duration_ms");
    this->expected_duration_ms_ = cJSON_IsNumber(duration) ? duration->valueint : 0;
  } else if (strcmp(kind, "playback.flush") == 0) {
    this->flush_playback();
  }
  cJSON_Delete(root);
}

void RealtimeCompanion::handle_microphone_data(const std::vector<uint8_t> &data) {
  if (!this->session_active_ || this->muted_)
    return;
  // XMOS stream is interleaved stereo, signed little-endian PCM32. Channel 0 carries AEC speech.
  for (size_t offset = 0; offset + 7 < data.size(); offset += 8) {
    int32_t input;
    memcpy(&input, data.data() + offset, sizeof(input));
    int16_t output;
    bool ready;
    this->decimator_.push(static_cast<int16_t>(input >> 16), &output, &ready);
    if (!ready)
      continue;
    this->building_frame_.data[2 * this->building_samples_] = static_cast<uint8_t>(output);
    this->building_frame_.data[2 * this->building_samples_ + 1] = static_cast<uint8_t>(output >> 8);
    if (++this->building_samples_ == INPUT_FRAME_BYTES / 2) {
      if (xQueueSend(this->capture_queue_, &this->building_frame_, 0) != pdTRUE)
        ESP_LOGW(TAG, "Capture queue full; dropping 20 ms frame");
      this->building_samples_ = 0;
    }
  }
}

void RealtimeCompanion::loop() {
  if (this->client_ == nullptr && millis() - this->last_connect_attempt_ms_ > 2000)
    this->connect();
  if (this->auth_pending_.load(std::memory_order_acquire)) {
    const std::string auth = "{\"v\":1,\"type\":\"auth\",\"token\":\"" + this->token_ +
                             "\",\"device_id\":\"" + this->device_id_ +
                             "\",\"capabilities\":{\"aec\":true,\"frame_ms\":20}}";
    if (this->send_json(auth))
      this->auth_pending_.store(false, std::memory_order_release);
  }
  if (this->session_start_pending_.load(std::memory_order_acquire) && this->authenticated_ &&
      this->session_active_ && this->send_json("{\"v\":1,\"type\":\"session.start\"}")) {
    this->session_start_pending_.store(false, std::memory_order_release);
  }
  InputFrame captured;
  if (this->authenticated_ && this->session_active_ && !this->muted_ &&
      xQueueReceive(this->capture_queue_, &captured, 0) == pdTRUE) {
    esp_websocket_client_send_bin(this->client_, reinterpret_cast<const char *>(captured.data.data()),
                                  captured.data.size(), 0);
  }
  OutputFrame playback;
  if (xQueueReceive(this->playback_queue_, &playback, 0) == pdTRUE) {
    const size_t remaining = playback.data.size() - playback.offset;
    size_t written = this->speaker_->play(playback.data.data() + playback.offset, remaining, 0);
    playback.offset += written;
    if (playback.offset != playback.data.size()) {
      xQueueSendToFront(this->playback_queue_, &playback, 0);
    } else {
      // Progress is counted by the DAC-output callback, not by accepted input bytes.
    }
  }
  if (!this->stream_id_.empty() && millis() - this->last_progress_ms_ >= 100) {
    this->last_progress_ms_ = millis();
    const uint32_t played_ms = this->played_frames_.load(std::memory_order_relaxed) / 24;
    this->send_json("{\"v\":1,\"type\":\"playback.progress\",\"stream_id\":\"" +
                    this->stream_id_ + "\",\"played_ms\":" + std::to_string(played_ms) + "}");
    if (this->expected_duration_ms_ != 0 && played_ms >= this->expected_duration_ms_) {
      this->stream_id_.clear();
      this->expected_duration_ms_ = 0;
    }
  }
}

void RealtimeCompanion::start_session() {
  if (this->muted_)
    return;
  this->session_active_ = true;
  this->decimator_.reset();
  this->building_samples_ = 0;
  xQueueReset(this->capture_queue_);
  this->update_state(CompanionState::CONNECTING);
  if (this->authenticated_)
    this->send_json("{\"v\":1,\"type\":\"session.start\"}");
}

void RealtimeCompanion::stop_session(const char *reason) {
  if (!this->session_active_)
    return;
  this->send_json("{\"v\":1,\"type\":\"session.stop\",\"reason\":\"" + std::string(reason) + "\"}");
  this->session_active_ = false;
  this->flush_playback();
  this->update_state(this->muted_ ? CompanionState::MUTED : CompanionState::IDLE);
}

void RealtimeCompanion::toggle_session() {
  if (this->session_active_)
    this->stop_session();
  else
    this->start_session();
}

void RealtimeCompanion::set_muted(bool muted) {
  this->muted_ = muted;
  this->microphone_->set_mute_state(muted);
  if (muted) {
    xQueueReset(this->capture_queue_);
    this->flush_playback();
    this->update_state(CompanionState::MUTED);
  } else {
    this->update_state(this->session_active_ ? CompanionState::LISTENING : CompanionState::IDLE);
  }
  this->send_json("{\"v\":1,\"type\":\"state\",\"state\":\"" +
                  std::string(muted ? "muted" : "idle") + "\",\"muted\":" +
                  std::string(muted ? "true" : "false") + "}");
}

void RealtimeCompanion::flush_playback() {
  xQueueReset(this->playback_queue_);
  this->speaker_->stop();
  this->stream_id_.clear();
  this->played_frames_.store(0, std::memory_order_relaxed);
  this->expected_duration_ms_ = 0;
}

bool RealtimeCompanion::send_json(const std::string &json) {
  if (this->client_ == nullptr || !esp_websocket_client_is_connected(this->client_))
    return false;
  const int sent = esp_websocket_client_send_text(this->client_, json.c_str(), json.size(), pdMS_TO_TICKS(20));
  if (sent != static_cast<int>(json.size())) {
    ESP_LOGW(TAG, "WebSocket control send failed: sent %d of %u bytes", sent,
             static_cast<unsigned>(json.size()));
    return false;
  }
  return true;
}

void RealtimeCompanion::update_state(CompanionState state) { this->state_ = state; }

void RealtimeCompanion::dump_config() {
  ESP_LOGCONFIG(TAG, "Realtime Thinking Companion:");
  ESP_LOGCONFIG(TAG, "  Gateway: %s", this->url_.c_str());
  ESP_LOGCONFIG(TAG, "  Device ID: %s", this->device_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Capture queue: 6 x 20 ms; playback queue: 10 x 20 ms");
}

}  // namespace esphome::realtime_companion
