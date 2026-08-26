#include "realtime_companion.h"

#include "esphome/components/audio/audio.h"
#include "esphome/components/network/util.h"
#include "esphome/core/log.h"

#include <cJSON.h>
#include <esp_timer.h>

#include <algorithm>
#include <cstring>

namespace esphome::realtime_companion {

static const char *const TAG = "realtime_companion";

void RealtimeCompanion::setup() {
  ESP_LOGI(TAG, "Initializing transport; WebSocket will wait for network readiness");
  this->capture_queue_ = xQueueCreateStatic(6, sizeof(InputFrame), this->capture_queue_storage_,
                                             &this->capture_queue_struct_);
  this->playback_queue_ = xQueueCreateStatic(10, sizeof(OutputFrame), this->playback_queue_storage_,
                                              &this->playback_queue_struct_);
  esp_audio_libs::resampler::ResamplerConfiguration resampler_config = {
      .source_sample_rate = 16000.0f,
      .target_sample_rate = 24000.0f,
      .source_bits_per_sample = 16,
      .target_bits_per_sample = 16,
      .channels = 1,
      .use_pre_or_post_filter = false,
      .subsample_interpolate = false,
      .number_of_taps = 32,
      .number_of_filters = 16,
  };
  if (!this->input_resampler_.initialize(resampler_config)) {
    ESP_LOGE(TAG, "Could not initialize the 16-to-24 kHz microphone resampler");
    this->mark_failed();
    return;
  }
  this->audio_sender_task_handle_ = xTaskCreateStatic(
      &RealtimeCompanion::audio_sender_task, "realtime_tx", AUDIO_SENDER_STACK_WORDS, this,
      tskIDLE_PRIORITY + 3, this->audio_sender_task_stack_, &this->audio_sender_task_struct_);
  if (this->audio_sender_task_handle_ == nullptr) {
    ESP_LOGE(TAG, "Could not create audio sender task");
    this->mark_failed();
    return;
  }
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 2, 24000));
  this->speaker_->add_audio_output_callback([this](uint32_t frames, int64_t) {
    if (!this->stream_id_.empty())
      this->played_frames_.fetch_add(frames, std::memory_order_relaxed);
  });
  this->microphone_->add_data_callback(
      [this](const std::vector<uint8_t> &data) { this->handle_microphone_data(data); });
  this->microphone_->start();
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
  const esp_err_t result = esp_websocket_client_start(this->client_);
  if (result != ESP_OK) {
    ESP_LOGE(TAG, "Could not start WebSocket client: %s", esp_err_to_name(result));
    esp_websocket_client_destroy(this->client_);
    this->client_ = nullptr;
    this->update_state(CompanionState::ERROR);
    this->last_connect_attempt_ms_ = millis();
    return;
  }
  ESP_LOGI(TAG, "WebSocket client started for %s", this->url_.c_str());
  this->last_connect_attempt_ms_ = millis();
}

void RealtimeCompanion::websocket_event(void *handler_args, esp_event_base_t, int32_t event_id, void *event_data) {
  static_cast<RealtimeCompanion *>(handler_args)
      ->handle_websocket_event(event_id, static_cast<esp_websocket_event_data_t *>(event_data));
}

void RealtimeCompanion::audio_sender_task(void *parameter) {
  static_cast<RealtimeCompanion *>(parameter)->run_audio_sender();
}

void RealtimeCompanion::run_audio_sender() {
  InputFrame captured;
  while (true) {
    if (xQueueReceive(this->capture_queue_, &captured, portMAX_DELAY) != pdTRUE)
      continue;
    if (!this->authenticated_.load(std::memory_order_acquire) ||
        !this->session_active_.load(std::memory_order_acquire) ||
        !this->stream_ready_.load(std::memory_order_acquire) ||
        this->muted_.load(std::memory_order_acquire) || this->client_ == nullptr ||
        !esp_websocket_client_is_connected(this->client_)) {
      continue;
    }
    const int sent = esp_websocket_client_send_bin(
        this->client_, reinterpret_cast<const char *>(captured.data.data()), captured.data.size(),
        pdMS_TO_TICKS(50));
    if (sent == static_cast<int>(captured.data.size())) {
      this->sent_frames_.fetch_add(1, std::memory_order_relaxed);
    } else {
      ESP_LOGW(TAG, "Audio WebSocket send failed: sent %d of %u bytes", sent,
               static_cast<unsigned>(captured.data.size()));
      // A short lock-contention timeout can fail without disconnecting the client. Drop only
      // that frame in this case; the disconnect callback owns stream shutdown and queue reset.
      if (!esp_websocket_client_is_connected(this->client_)) {
        this->stream_ready_.store(false, std::memory_order_release);
        xQueueReset(this->capture_queue_);
        if (this->session_active_.load(std::memory_order_acquire))
          this->update_state(CompanionState::CONNECTING);
      }
    }
  }
}

void RealtimeCompanion::handle_websocket_event(int32_t event_id, esp_websocket_event_data_t *event) {
  if (event_id == WEBSOCKET_EVENT_CONNECTED) {
    ESP_LOGI(TAG, "Gateway WebSocket connected; authentication pending");
    this->authenticated_ = false;
    this->auth_pending_.store(true, std::memory_order_release);
  } else if (event_id == WEBSOCKET_EVENT_DISCONNECTED) {
    ESP_LOGW(TAG, "Gateway WebSocket disconnected; pausing microphone transport");
    this->authenticated_ = false;
    this->stream_ready_.store(false, std::memory_order_release);
    this->auth_pending_.store(false, std::memory_order_release);
    this->session_start_pending_.store(false, std::memory_order_release);
    xQueueReset(this->capture_queue_);
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
    ESP_LOGI(TAG, "Gateway authentication accepted");
    this->authenticated_ = true;
    this->authenticated_once_ = true;
    if (this->session_active_)
      this->session_start_pending_.store(true, std::memory_order_release);
  } else if (strcmp(kind, "session.started") == 0) {
    ESP_LOGI(TAG, "Gateway session started; microphone audio transport active");
    this->stream_ready_.store(true, std::memory_order_release);
    this->update_state(this->muted_ ? CompanionState::MUTED : CompanionState::LISTENING);
  } else if (strcmp(kind, "session.ended") == 0) {
    ESP_LOGI(TAG, "Gateway session ended");
    this->session_active_ = false;
    this->stream_ready_.store(false, std::memory_order_release);
    xQueueReset(this->capture_queue_);
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
  const bool capture_enabled = this->session_active_.load(std::memory_order_acquire) &&
                               this->stream_ready_.load(std::memory_order_acquire) &&
                               !this->muted_.load(std::memory_order_acquire);
  if (!capture_enabled) {
    if (this->capture_running_) {
      ESP_LOGI(TAG, "Microphone frame capture paused");
      this->building_samples_ = 0;
      this->building_peak_ = 0;
      this->capture_running_ = false;
    }
    return;
  }
  if (!this->capture_running_) {
    ESP_LOGI(TAG, "Microphone frame capture started");
    this->building_samples_ = 0;
    this->building_peak_ = 0;
    this->capture_running_ = true;
  }
  // The pinned formatBCE microphone reads the 48 kHz XMOS bus, keeps every third frame, and
  // advertises its callback as 16 kHz PCM32 stereo. Channel 0 is the AEC speech channel.
  const size_t source_frames = data.size() / 8;
  for (size_t base = 0; base < source_frames;) {
    const size_t frames = std::min(RESAMPLER_INPUT_FRAMES, source_frames - base);
    for (size_t i = 0; i < frames; i++) {
      int32_t input;
      memcpy(&input, data.data() + (base + i) * 8, sizeof(input));
      this->resampler_input_[i] = static_cast<int16_t>(input >> 16);
    }
    const auto result = this->input_resampler_.resample(
        reinterpret_cast<const uint8_t *>(this->resampler_input_.data()),
        reinterpret_cast<uint8_t *>(this->resampler_output_.data()), frames,
        RESAMPLER_OUTPUT_FRAMES, 0.0f);
    if (result.frames_used != frames) {
      ESP_LOGW(TAG, "Microphone resampler consumed %u of %u input frames",
               static_cast<unsigned>(result.frames_used), static_cast<unsigned>(frames));
    }
    for (size_t i = 0; i < result.frames_generated; i++)
      this->append_capture_sample(this->resampler_output_[i]);
    base += result.frames_used;
    if (result.frames_used == 0)
      break;
  }
}

void RealtimeCompanion::append_capture_sample(int16_t sample) {
  const uint16_t magnitude = sample < 0 ? static_cast<uint16_t>(-static_cast<int32_t>(sample))
                                        : static_cast<uint16_t>(sample);
  this->building_peak_ = std::max(this->building_peak_, magnitude);
  this->building_frame_.data[2 * this->building_samples_] = static_cast<uint8_t>(sample);
  this->building_frame_.data[2 * this->building_samples_ + 1] = static_cast<uint8_t>(sample >> 8);
  if (++this->building_samples_ == INPUT_FRAME_BYTES / 2) {
    this->captured_frames_.fetch_add(1, std::memory_order_relaxed);
    uint16_t previous_peak = this->capture_peak_.load(std::memory_order_relaxed);
    while (previous_peak < this->building_peak_ &&
           !this->capture_peak_.compare_exchange_weak(previous_peak, this->building_peak_,
                                                      std::memory_order_relaxed)) {}
    if (xQueueSend(this->capture_queue_, &this->building_frame_, 0) != pdTRUE) {
      this->dropped_frames_.fetch_add(1, std::memory_order_relaxed);
      ESP_LOGW(TAG, "Capture queue full; dropping 20 ms frame");
    }
    this->building_samples_ = 0;
    this->building_peak_ = 0;
  }
}

void RealtimeCompanion::loop() {
  if (this->client_ == nullptr && network::is_connected() &&
      millis() - this->last_connect_attempt_ms_ > 2000) {
    ESP_LOGI(TAG, "Network is ready; starting WebSocket client");
    this->connect();
  }
  if (this->auth_pending_.load(std::memory_order_acquire)) {
    const std::string auth = "{\"v\":1,\"type\":\"auth\",\"token\":\"" + this->token_ +
                             "\",\"device_id\":\"" + this->device_id_ +
                             "\",\"capabilities\":{\"aec\":true,\"frame_ms\":20}}";
    if (this->send_json(auth))
      this->auth_pending_.store(false, std::memory_order_release);
  }
  if (this->session_start_pending_.load(std::memory_order_acquire) && this->authenticated_ &&
      this->session_active_ && this->send_json("{\"v\":1,\"type\":\"session.start\"}")) {
    // The gateway buffers subsequent WebSocket frames while it establishes the OpenAI session.
    // Enabling capture here preserves speech that begins immediately after the wake word.
    this->stream_ready_.store(true, std::memory_order_release);
    this->session_start_pending_.store(false, std::memory_order_release);
    ESP_LOGI(TAG, "Session start sent; accepting microphone frames for transport");
  }
  if (this->session_active_.load(std::memory_order_acquire) &&
      millis() - this->last_audio_stats_ms_ >= 2000) {
    this->last_audio_stats_ms_ = millis();
    const uint32_t captured = this->captured_frames_.exchange(0, std::memory_order_relaxed);
    const uint32_t sent = this->sent_frames_.exchange(0, std::memory_order_relaxed);
    const uint32_t dropped = this->dropped_frames_.exchange(0, std::memory_order_relaxed);
    const uint16_t peak = this->capture_peak_.exchange(0, std::memory_order_relaxed);
    const unsigned queued = this->capture_queue_ == nullptr ? 0 : uxQueueMessagesWaiting(this->capture_queue_);
    ESP_LOGI(TAG,
             "Audio stats/2s: captured=%u sent=%u dropped=%u queued=%u/6 peak=%u ready=%s connected=%s",
             static_cast<unsigned>(captured), static_cast<unsigned>(sent),
             static_cast<unsigned>(dropped), queued, static_cast<unsigned>(peak),
             this->stream_ready_.load(std::memory_order_acquire) ? "yes" : "no",
             this->client_ != nullptr && esp_websocket_client_is_connected(this->client_) ? "yes" : "no");
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
  this->stream_ready_.store(false, std::memory_order_release);
  this->captured_frames_.store(0, std::memory_order_relaxed);
  this->sent_frames_.store(0, std::memory_order_relaxed);
  this->dropped_frames_.store(0, std::memory_order_relaxed);
  this->capture_peak_.store(0, std::memory_order_relaxed);
  this->last_audio_stats_ms_ = millis();
  xQueueReset(this->capture_queue_);
  this->update_state(CompanionState::CONNECTING);
  ESP_LOGI(TAG, "Wake activation requested a Realtime session (authenticated=%s)",
           this->authenticated_.load(std::memory_order_acquire) ? "yes" : "no");
  if (this->authenticated_)
    this->session_start_pending_.store(true, std::memory_order_release);
}

void RealtimeCompanion::stop_session(const char *reason) {
  if (!this->session_active_)
    return;
  this->send_json("{\"v\":1,\"type\":\"session.stop\",\"reason\":\"" + std::string(reason) + "\"}");
  this->session_active_ = false;
  this->stream_ready_.store(false, std::memory_order_release);
  xQueueReset(this->capture_queue_);
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

bool RealtimeCompanion::is_network_ready() const { return network::is_connected(); }

void RealtimeCompanion::update_state(CompanionState state) {
  this->state_.store(state, std::memory_order_release);
}

void RealtimeCompanion::dump_config() {
  ESP_LOGCONFIG(TAG, "Realtime Thinking Companion:");
  ESP_LOGCONFIG(TAG, "  Gateway: %s", this->url_.c_str());
  ESP_LOGCONFIG(TAG, "  Device ID: %s", this->device_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Capture queue: 6 x 20 ms; playback queue: 10 x 20 ms");
}

}  // namespace esphome::realtime_companion
