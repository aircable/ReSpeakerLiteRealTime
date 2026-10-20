#include "realtime_companion.h"
#include "wake_chime_pcm.h"

#include "esphome/components/audio/audio.h"
#include "esphome/components/json/json_util.h"
#include "esphome/components/network/util.h"
#include "esphome/core/log.h"
#include "esphome/core/helpers.h"

#include <esp_timer.h>

#include <algorithm>
#include <cstring>

namespace esphome::realtime_companion {

static const char *const TAG = "realtime_companion";

void RealtimeCompanion::setup() {
  ESP_LOGI(TAG, "Initializing transport; WebSocket will wait for network readiness");
  this->volume_preference_ = global_preferences->make_preference<float>(
      fnv1_hash(this->volume_preference_key_.c_str()));
  float saved_volume;
  if (this->volume_preference_.load(&saved_volume) && saved_volume >= 0.0f && saved_volume <= 1.0f) {
    this->output_volume_ = saved_volume;
    ESP_LOGI(TAG, "Restored assistant output volume: %.0f%%", this->output_volume_ * 100.0f);
  }
  this->capture_queue_ = xQueueCreateStatic(6, sizeof(InputFrame), this->capture_queue_storage_,
                                             &this->capture_queue_struct_);
  this->control_queue_ = xQueueCreateStatic(8, sizeof(ControlFrame), this->control_queue_storage_,
                                             &this->control_queue_struct_);
  this->progress_queue_ = xQueueCreateStatic(1, sizeof(ControlFrame), this->progress_queue_storage_,
                                              &this->progress_queue_struct_);
  this->playback_queue_ = xQueueCreateStatic(PLAYBACK_QUEUE_FRAMES, sizeof(OutputFrame), this->playback_queue_storage_,
                                              &this->playback_queue_struct_);
  if (this->capture_queue_ == nullptr || this->control_queue_ == nullptr ||
      this->progress_queue_ == nullptr || this->playback_queue_ == nullptr) {
    ESP_LOGE(TAG, "Could not create static audio transport queues");
    this->mark_failed();
    return;
  }
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
      &RealtimeCompanion::audio_sender_task, "realtime_tx", AUDIO_SENDER_STACK_BYTES, this,
      tskIDLE_PRIORITY + 3, this->audio_sender_task_stack_, &this->audio_sender_task_struct_);
  if (this->audio_sender_task_handle_ == nullptr) {
    ESP_LOGE(TAG, "Could not create audio sender task");
    this->mark_failed();
    return;
  }
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 2, 24000));
  this->speaker_->add_audio_output_callback([this](uint32_t frames, int64_t) {
    if (this->playback_active_.load(std::memory_order_acquire))
      this->played_frames_.fetch_add(frames, std::memory_order_relaxed);
    else if (this->wake_ack_pending_.load(std::memory_order_acquire))
      this->wake_ack_played_frames_.fetch_add(frames, std::memory_order_relaxed);
  });
  this->microphone_->add_data_callback(
      [this](const std::vector<uint8_t> &data) { this->handle_microphone_data(data); });
  if (this->startup_guard_ == nullptr || this->startup_guard_->can_proceed()) {
    this->microphone_->start();
    this->microphone_started_ = true;
  } else {
    ESP_LOGE(TAG, "XMOS firmware check failed; microphone and gateway transport remain disabled");
  }
}

void RealtimeCompanion::connect() {
  if (this->client_ != nullptr)
    esp_websocket_client_destroy(this->client_);
  esp_websocket_client_config_t config{};
  config.uri = this->url_.c_str();
  config.network_timeout_ms = 5000;
  config.reconnect_timeout_ms = 2000;
  config.disable_auto_reconnect = false;
  // Docker/Uvicorn normally performs a clean WebSocket close during a gateway update. ESP-IDF
  // does not reconnect after a clean close unless this is explicitly enabled.
  config.enable_close_reconnect = true;
  // Detect a silently replaced or stale NAT connection while idle, before the wake word needs it.
  config.ping_interval_sec = 5;
  config.pingpong_timeout_sec = 10;
  config.disable_pingpong_discon = false;
  config.task_stack = 8192;
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
  ControlFrame control;
  InputFrame captured;
  while (true) {
    // All device-to-gateway writes happen in this task. Send at most one item from each class per
    // pass so a slow class cannot starve microphone audio. Playback progress is coalesced in its
    // own one-slot queue because only the newest played duration is useful to the gateway.
    if (xQueueReceive(this->control_queue_, &control, 0) == pdTRUE) {
      if (this->client_ != nullptr && esp_websocket_client_is_connected(this->client_)) {
        const int sent = esp_websocket_client_send_text(this->client_, control.data.data(),
                                                         control.length, WEBSOCKET_SEND_TIMEOUT);
        if (sent != control.length) {
          ESP_LOGW(TAG, "Control WebSocket send failed: sent %d of %u bytes", sent,
                   static_cast<unsigned>(control.length));
        }
      }
    }
    if (xQueueReceive(this->capture_queue_, &captured, pdMS_TO_TICKS(10)) == pdTRUE) {
      if (this->authenticated_.load(std::memory_order_acquire) &&
          this->session_active_.load(std::memory_order_acquire) &&
          this->stream_ready_.load(std::memory_order_acquire) &&
          !this->muted_.load(std::memory_order_acquire) && this->client_ != nullptr &&
          esp_websocket_client_is_connected(this->client_)) {
        const int sent = esp_websocket_client_send_bin(
            this->client_, reinterpret_cast<const char *>(captured.data.data()), captured.data.size(),
            WEBSOCKET_SEND_TIMEOUT);
        if (sent == static_cast<int>(captured.data.size())) {
          this->sent_frames_.fetch_add(1, std::memory_order_relaxed);
        } else {
          ESP_LOGW(TAG, "Audio WebSocket send failed: sent %d of %u bytes", sent,
                   static_cast<unsigned>(captured.data.size()));
          // A lock timeout can fail without disconnecting the client. Drop only that frame in
          // this case; the disconnect callback owns stream shutdown and queue reset.
          if (!esp_websocket_client_is_connected(this->client_)) {
            this->stream_ready_.store(false, std::memory_order_release);
            xQueueReset(this->capture_queue_);
            if (this->session_active_.load(std::memory_order_acquire))
              this->update_state(CompanionState::CONNECTING);
          }
        }
      }
    }
    if (xQueueReceive(this->progress_queue_, &control, 0) == pdTRUE && this->client_ != nullptr &&
        esp_websocket_client_is_connected(this->client_)) {
      const int sent = esp_websocket_client_send_text(this->client_, control.data.data(),
                                                       control.length, WEBSOCKET_SEND_TIMEOUT);
      if (sent != control.length) {
        ESP_LOGW(TAG, "Playback progress send failed: sent %d of %u bytes", sent,
                 static_cast<unsigned>(control.length));
      }
    }
  }
}

void RealtimeCompanion::handle_websocket_event(int32_t event_id, esp_websocket_event_data_t *event) {
  if (event_id == WEBSOCKET_EVENT_ERROR) {
    ESP_LOGW(TAG,
             "Gateway WebSocket error: type=%d esp_tls=%s tls_stack=%d socket_errno=%d "
             "handshake_status=%d",
             static_cast<int>(event->error_handle.error_type),
             esp_err_to_name(event->error_handle.esp_tls_last_esp_err),
             event->error_handle.esp_tls_stack_err, event->error_handle.esp_transport_sock_errno,
             event->error_handle.esp_ws_handshake_status_code);
  } else if (event_id == WEBSOCKET_EVENT_CONNECTED) {
    ESP_LOGI(TAG, "Gateway WebSocket connected; authentication pending");
    xQueueReset(this->control_queue_);
    xQueueReset(this->progress_queue_);
    this->authenticated_ = false;
    this->auth_pending_.store(true, std::memory_order_release);
  } else if (event_id == WEBSOCKET_EVENT_DISCONNECTED) {
    ESP_LOGW(TAG, "Gateway WebSocket disconnected; close_status=%d; pausing microphone transport",
             event->close_status_code);
    this->authenticated_ = false;
    this->stream_ready_.store(false, std::memory_order_release);
    this->auth_pending_.store(false, std::memory_order_release);
    this->session_start_pending_.store(false, std::memory_order_release);
    this->waiting_for_ready_.store(false, std::memory_order_release);
    this->ready_signal_sent_.store(false, std::memory_order_release);
    this->barge_in_seen_.store(false, std::memory_order_release);
    xQueueReset(this->capture_queue_);
    xQueueReset(this->control_queue_);
    xQueueReset(this->progress_queue_);
    this->flush_playback();
    if (this->session_active_)
      this->update_state(CompanionState::CONNECTING);
  } else if (event_id == WEBSOCKET_EVENT_DATA && event->payload_offset == 0 &&
             event->data_len == event->payload_len) {
    if (event->op_code == 0x2 && event->data_len == INPUT_FRAME_BYTES) {
      OutputFrame output{};
      auto *mono = reinterpret_cast<const int16_t *>(event->data_ptr);
      auto *stereo = reinterpret_cast<int16_t *>(output.data.data());
      // Treat the runtime value as a human-facing volume position rather than a
      // linear PCM multiplier. Linear amplitude made almost the entire useful
      // range fit below 2% on this amplifier. Squaring provides useful low-end
      // resolution while retaining full scale at 100%.
      const float output_gain = this->output_volume_ * this->output_volume_;
      uint16_t input_peak = 0;
      uint16_t output_peak = 0;
      for (size_t i = 0; i < INPUT_FRAME_BYTES / 2; i++) {
        const int16_t scaled = static_cast<int16_t>(mono[i] * output_gain);
        stereo[2 * i] = stereo[2 * i + 1] = scaled;
        const int32_t input_sample = mono[i];
        const int32_t output_sample = scaled;
        input_peak = std::max<uint16_t>(input_peak, static_cast<uint16_t>(input_sample < 0 ? -input_sample : input_sample));
        output_peak = std::max<uint16_t>(output_peak, static_cast<uint16_t>(output_sample < 0 ? -output_sample : output_sample));
      }
      uint16_t prior = this->playback_input_peak_.load(std::memory_order_relaxed);
      while (prior < input_peak && !this->playback_input_peak_.compare_exchange_weak(
                                     prior, input_peak, std::memory_order_relaxed)) {}
      prior = this->playback_output_peak_.load(std::memory_order_relaxed);
      while (prior < output_peak && !this->playback_output_peak_.compare_exchange_weak(
                                      prior, output_peak, std::memory_order_relaxed)) {}
      if (xQueueSend(this->playback_queue_, &output, 0) != pdTRUE)
        this->playback_dropped_frames_.fetch_add(1, std::memory_order_relaxed);
    } else if (event->op_code == 0x1) {
      this->handle_text(event->data_ptr, event->data_len);
    }
  }
}

void RealtimeCompanion::handle_text(const char *data, size_t length) {
  auto document = json::parse_json(reinterpret_cast<const uint8_t *>(data), length);
  if (document.isNull())
    return;
  JsonObject root = document.as<JsonObject>();
  const char *kind = root["type"] | "";
  if (strcmp(kind, "auth.ok") == 0) {
    ESP_LOGI(TAG, "Gateway authentication accepted");
    this->authenticated_ = true;
    this->authenticated_once_ = true;
    if (this->session_active_)
      this->session_start_pending_.store(true, std::memory_order_release);
    this->report_volume();
  } else if (strcmp(kind, "session.started") == 0) {
    ESP_LOGI(TAG, "Gateway session started; microphone audio transport active");
    this->stream_ready_.store(true, std::memory_order_release);
    this->waiting_for_ready_.store(false, std::memory_order_release);
    this->ready_signal_sent_.store(false, std::memory_order_release);
    this->paused_.store(false, std::memory_order_release);
    this->update_state(this->muted_ ? CompanionState::MUTED : CompanionState::LISTENING);
  } else if (strcmp(kind, "session.ended") == 0) {
    ESP_LOGI(TAG, "Gateway session ended");
    this->session_active_ = false;
    this->stream_ready_.store(false, std::memory_order_release);
    this->waiting_for_ready_.store(false, std::memory_order_release);
    this->ready_signal_sent_.store(false, std::memory_order_release);
    this->paused_.store(true, std::memory_order_release);
    this->barge_in_seen_.store(false, std::memory_order_release);
    xQueueReset(this->capture_queue_);
    this->flush_playback();
    this->update_state(this->muted_ ? CompanionState::MUTED : CompanionState::IDLE);
  } else if (strcmp(kind, "state") == 0) {
    const char *state = root["state"] | "";
    if (strcmp(state, "thinking") == 0) this->update_state(CompanionState::THINKING);
    if (strcmp(state, "speaking") == 0) this->update_state(CompanionState::SPEAKING);
    if (strcmp(state, "listening") == 0) this->update_state(CompanionState::LISTENING);
    if (strcmp(state, "error") == 0) this->update_state(CompanionState::ERROR);
  } else if (strcmp(kind, "playback.start") == 0) {
    const char *stream = root["stream_id"] | "";
    ESP_LOGI(TAG, "Playback started stream=%s", stream);
    xQueueReset(this->playback_queue_);
    xQueueReset(this->progress_queue_);
    {
      std::lock_guard<std::mutex> lock(this->playback_mutex_);
      this->stream_id_ = stream;
      this->expected_duration_ms_ = 0;
    }
    this->played_frames_.store(0, std::memory_order_relaxed);
    this->playback_end_received_.store(false, std::memory_order_release);
    this->playback_prebuffering_.store(true, std::memory_order_release);
    this->playback_active_.store(true, std::memory_order_release);
  } else if (strcmp(kind, "playback.end") == 0) {
    std::lock_guard<std::mutex> lock(this->playback_mutex_);
    this->expected_duration_ms_ = root["duration_ms"] | 0;
    this->playback_end_received_.store(true, std::memory_order_release);
    ESP_LOGI(TAG, "Playback end received stream=%s expected_ms=%u played_ms=%u queued=%u",
             this->stream_id_.c_str(), static_cast<unsigned>(this->expected_duration_ms_),
             static_cast<unsigned>(this->played_frames_.load(std::memory_order_relaxed) / 24),
             static_cast<unsigned>(uxQueueMessagesWaiting(this->playback_queue_)));
  } else if (strcmp(kind, "playback.flush") == 0) {
    ESP_LOGI(TAG, "Playback flushed stream=%s played_ms=%u queued=%u state=%u",
             this->stream_id_.c_str(),
             static_cast<unsigned>(this->played_frames_.load(std::memory_order_relaxed) / 24),
             static_cast<unsigned>(uxQueueMessagesWaiting(this->playback_queue_)),
             static_cast<unsigned>(this->get_state()));
    // A flush while active playback is speaking is normally a user barge-in.
    // Keep this as a short LED-only indication; the gateway owns the real state.
    if (this->session_active_.load(std::memory_order_acquire) &&
        this->playback_active_.load(std::memory_order_acquire) &&
        this->get_state() == CompanionState::SPEAKING) {
      this->last_barge_in_ms_.store(millis(), std::memory_order_release);
      this->barge_in_seen_.store(true, std::memory_order_release);
    }
    this->flush_playback();
  } else if (strcmp(kind, "timer.alarm") == 0) {
    const int32_t timer_id = root["timer_id"] | 0;
    if (timer_id > 0) {
      this->timer_alarm_id_.store(timer_id, std::memory_order_release);
      ESP_LOGI(TAG, "Timer alarm received id=%ld", static_cast<long>(timer_id));
    }
  } else if (strcmp(kind, "timer.stop") == 0) {
    const int32_t timer_id = root["timer_id"] | 0;
    if (timer_id > 0 && timer_id == this->timer_alarm_id_.load(std::memory_order_acquire)) {
      this->timer_alarm_id_.store(0, std::memory_order_release);
      ESP_LOGI(TAG, "Timer alarm stopped id=%ld", static_cast<long>(timer_id));
    }
  } else if (strcmp(kind, "volume.set") == 0) {
    JsonVariant level = root["level"];
    if (level.is<float>() || level.is<double>() || level.is<int>())
      this->set_runtime_volume(level.as<float>(), true);
  } else if (strcmp(kind, "mic_gain.set") == 0 && this->mic_gain_controller_ != nullptr) {
    JsonVariant gain = root["ch0_gain"];
    if (gain.is<float>() || gain.is<double>() || gain.is<int>())
      this->mic_gain_controller_->request_ch0_gain(gain.as<float>());
  } else if (strcmp(kind, "keyword.mode") == 0 && this->ready_keyword_enabled_) {
    const char *mode = root["mode"] | "";
    const bool ready = strcmp(mode, "ready") == 0;
    this->waiting_for_ready_.store(ready, std::memory_order_release);
    this->ready_signal_sent_.store(false, std::memory_order_release);
    xQueueReset(this->capture_queue_);
    ESP_LOGI(TAG, "Keyword mode changed to %s", ready ? "ready" : "wake");
  }
}

void RealtimeCompanion::handle_microphone_data(const std::vector<uint8_t> &data) {
  const bool capture_enabled = this->session_active_.load(std::memory_order_acquire) &&
                               this->stream_ready_.load(std::memory_order_acquire) &&
                               !this->waiting_for_ready_.load(std::memory_order_acquire) &&
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
  if (sample == INT16_MIN || sample == INT16_MAX)
    this->capture_rail_samples_.fetch_add(1, std::memory_order_relaxed);
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
    }
    this->building_samples_ = 0;
    this->building_peak_ = 0;
  }
}

void RealtimeCompanion::loop() {
  if (this->startup_guard_ != nullptr && !this->startup_guard_->can_proceed())
    return;
  if (!this->microphone_started_) {
    this->microphone_->start();
    this->microphone_started_ = true;
  }
  if (this->client_ == nullptr && network::is_connected() &&
      millis() - this->last_connect_attempt_ms_ > 2000) {
    ESP_LOGI(TAG, "Network is ready; starting WebSocket client");
    this->connect();
  }
  if (this->auth_pending_.load(std::memory_order_acquire)) {
    const std::string auth = json::build_json([this](JsonObject root) {
      root["v"] = 1;
      root["type"] = "auth";
      root["token"] = this->token_;
      root["device_id"] = this->device_id_;
      if (!this->device_name_.empty())
        root["name"] = this->device_name_;
      JsonObject capabilities = root["capabilities"].to<JsonObject>();
      capabilities["aec"] = true;
      capabilities["frame_ms"] = 20;
      capabilities["playback_queue_ms"] = PLAYBACK_QUEUE_FRAMES * 20;
      capabilities["runtime_volume"] = true;
      capabilities["ready_keyword"] = this->ready_keyword_enabled_;
      capabilities["timer_alarm"] = true;
      capabilities["xvf_agc_ch0"] = this->mic_gain_controller_ != nullptr;
    });
    if (this->send_json(auth))
      this->auth_pending_.store(false, std::memory_order_release);
  }
  this->process_timer_alarm();
  this->process_wake_ack();
  if (this->session_start_pending_.load(std::memory_order_acquire) && this->authenticated_ &&
      this->session_active_ && !this->wake_ack_pending_.load(std::memory_order_acquire) &&
      this->send_json("{\"v\":1,\"type\":\"session.start\"}")) {
    // The gateway buffers subsequent WebSocket frames while it establishes the OpenAI session.
    // Enabling capture here preserves speech that begins immediately after the wake word.
    this->stream_ready_.store(true, std::memory_order_release);
    this->session_start_pending_.store(false, std::memory_order_release);
    ESP_LOGI(TAG, "Session start sent; accepting microphone frames for transport");
  }
  if (this->session_active_.load(std::memory_order_acquire) &&
      millis() - this->last_audio_stats_ms_ >= this->audio_stats_interval_ms_) {
    this->last_audio_stats_ms_ = millis();
    const uint32_t captured = this->captured_frames_.exchange(0, std::memory_order_relaxed);
    const uint32_t sent = this->sent_frames_.exchange(0, std::memory_order_relaxed);
    const uint32_t dropped = this->dropped_frames_.exchange(0, std::memory_order_relaxed);
    const uint32_t rail_samples = this->capture_rail_samples_.exchange(0, std::memory_order_relaxed);
    const uint32_t playback_dropped =
        this->playback_dropped_frames_.exchange(0, std::memory_order_relaxed);
    const uint16_t peak = this->capture_peak_.exchange(0, std::memory_order_relaxed);
    const uint16_t playback_input_peak = this->playback_input_peak_.exchange(0, std::memory_order_relaxed);
    const uint16_t playback_output_peak = this->playback_output_peak_.exchange(0, std::memory_order_relaxed);
    const unsigned queued = this->capture_queue_ == nullptr ? 0 : uxQueueMessagesWaiting(this->capture_queue_);
    const unsigned control_queued =
        this->control_queue_ == nullptr ? 0 : uxQueueMessagesWaiting(this->control_queue_);
    const unsigned progress_pending =
        this->progress_queue_ == nullptr ? 0 : uxQueueMessagesWaiting(this->progress_queue_);
    const unsigned playback_queued =
        this->playback_queue_ == nullptr ? 0 : uxQueueMessagesWaiting(this->playback_queue_);
    ESP_LOGI(TAG,
             "Audio stats/%ums: captured=%u sent=%u dropped=%u queued=%u/6 peak=%u rail_samples=%u "
             "playback_in_peak=%u playback_out_peak=%u volume=%.0f%% "
             "control_queued=%u/8 progress_pending=%u/1 playback_queued=%u/%u "
             "playback_dropped=%u tx_stack_free=%u ready=%s connected=%s",
             static_cast<unsigned>(this->audio_stats_interval_ms_),
             static_cast<unsigned>(captured), static_cast<unsigned>(sent),
             static_cast<unsigned>(dropped), queued, static_cast<unsigned>(peak),
             static_cast<unsigned>(rail_samples),
             static_cast<unsigned>(playback_input_peak), static_cast<unsigned>(playback_output_peak),
             this->output_volume_ * 100.0f,
             control_queued, progress_pending, playback_queued,
             static_cast<unsigned>(PLAYBACK_QUEUE_FRAMES),
             static_cast<unsigned>(playback_dropped),
             static_cast<unsigned>(uxTaskGetStackHighWaterMark(this->audio_sender_task_handle_)),
             this->stream_ready_.load(std::memory_order_acquire) ? "yes" : "no",
             this->client_ != nullptr && esp_websocket_client_is_connected(this->client_) ? "yes" : "no");
  }
  if (this->playback_prebuffering_.load(std::memory_order_acquire)) {
    const UBaseType_t queued = uxQueueMessagesWaiting(this->playback_queue_);
    if (queued >= this->playback_prebuffer_frames_ ||
        (queued > 0 && this->playback_end_received_.load(std::memory_order_acquire))) {
      ESP_LOGI(TAG, "Playback prebuffer ready: %u frames (%u ms)", static_cast<unsigned>(queued),
               static_cast<unsigned>(queued * 20));
      this->speaker_->start();
      this->playback_prebuffering_.store(false, std::memory_order_release);
    }
  }
  OutputFrame playback;
  if (!this->playback_prebuffering_.load(std::memory_order_acquire) &&
      xQueueReceive(this->playback_queue_, &playback, 0) == pdTRUE) {
    const size_t remaining = playback.data.size() - playback.offset;
    size_t written = this->speaker_->play(playback.data.data() + playback.offset, remaining, 0);
    playback.offset += written;
    if (playback.offset != playback.data.size()) {
      if (xQueueSendToFront(this->playback_queue_, &playback, 0) != pdTRUE) {
        this->playback_dropped_frames_.fetch_add(1, std::memory_order_relaxed);
        ESP_LOGW(TAG, "Playback queue full while retrying a partial frame");
      }
    } else {
      // Progress is counted by the DAC-output callback, not by accepted input bytes.
    }
  }
  if (millis() - this->last_progress_ms_ >= 100) {
    this->last_progress_ms_ = millis();
    std::string stream_id;
    uint32_t expected_duration_ms;
    {
      std::lock_guard<std::mutex> lock(this->playback_mutex_);
      stream_id = this->stream_id_;
      expected_duration_ms = this->expected_duration_ms_;
    }
    if (!stream_id.empty()) {
      const uint32_t played_ms = this->played_frames_.load(std::memory_order_relaxed) / 24;
      const bool progress_sent =
          this->send_playback_progress(
              "{\"v\":1,\"type\":\"playback.progress\",\"stream_id\":\"" + stream_id +
              "\",\"played_ms\":" + std::to_string(played_ms) + "}");
      // Resampler/DAC accounting can finish fractionally below the PCM duration. Requiring exact
      // equality leaves the stream active forever and keeps generating stale progress messages.
      // One 20 ms transport frame is an inaudible and safe completion tolerance.
      if (progress_sent && expected_duration_ms != 0 && played_ms + 20 >= expected_duration_ms) {
        std::lock_guard<std::mutex> lock(this->playback_mutex_);
        if (this->stream_id_ == stream_id) {
          ESP_LOGI(TAG, "Playback locally complete stream=%s played_ms=%u expected_ms=%u queued=%u",
                   stream_id.c_str(), static_cast<unsigned>(played_ms),
                   static_cast<unsigned>(expected_duration_ms),
                   static_cast<unsigned>(uxQueueMessagesWaiting(this->playback_queue_)));
          this->stream_id_.clear();
          this->expected_duration_ms_ = 0;
          this->playback_active_.store(false, std::memory_order_release);
          this->speaker_->finish();
        }
      }
    }
  }
}

void RealtimeCompanion::process_wake_ack() {
  if (!this->wake_ack_pending_.load(std::memory_order_acquire))
    return;
  if (!this->wake_ack_started_) {
    if (!this->speaker_->is_stopped()) {
      ESP_LOGW(TAG, "Wake chime skipped because speaker is busy");
      this->wake_ack_pending_.store(false, std::memory_order_release);
      return;
    }
    this->wake_ack_started_ms_ = millis();
    this->wake_ack_started_ = true;
    this->speaker_->start();
  }
  if (static_cast<uint32_t>(millis() - this->wake_ack_started_ms_) > 2500) {
    ESP_LOGW(TAG, "Local chime timed out");
    this->speaker_->stop();
    this->wake_ack_pending_.store(false, std::memory_order_release);
    if (this->is_timer_ringing()) this->timer_alarm_repeat_after_ms_ = millis() + 1500;
    return;
  }
  static constexpr uint32_t SAMPLES_PER_FRAME = OUTPUT_FRAME_BYTES / (2 * sizeof(int16_t));
  static constexpr uint32_t TOTAL_SAMPLES = kWakeChimePcm_len / sizeof(int16_t);
  static constexpr uint32_t TOTAL_FRAMES = (TOTAL_SAMPLES + SAMPLES_PER_FRAME - 1) / SAMPLES_PER_FRAME;
  if (this->wake_ack_draining_) {
    if (this->speaker_->is_stopped() ||
        this->wake_ack_played_frames_.load(std::memory_order_relaxed) + 24 >= TOTAL_SAMPLES) {
      if (this->is_timer_ringing()) {
        this->timer_alarm_repeat_after_ms_ = millis() + 1500;
      } else {
        ESP_LOGI(TAG, "Wake chime complete; starting gateway session");
      }
      this->wake_ack_pending_.store(false, std::memory_order_release);
    }
    return;
  }
  if (!this->speaker_->is_running())
    return;

  if (this->wake_ack_frame_index_ < TOTAL_FRAMES) {
    if (this->wake_ack_frame_offset_ == 0) {
      // The original Voice PE / ReSpeaker wake sound is mono 48 kHz FLAC, converted
      // once to 24 kHz PCM16. Keep this gain independent of the voice's squared
      // volume curve: the notification should remain gentle but audible.
      const float gain = 0.15f + 0.55f * this->output_volume_;
      for (uint32_t i = 0; i < SAMPLES_PER_FRAME; i++) {
        const uint32_t position = this->wake_ack_frame_index_ * SAMPLES_PER_FRAME + i;
        int16_t sample = 0;
        if (position < TOTAL_SAMPLES) {
          const uint32_t byte_offset = 2 * position;
          const uint16_t raw = static_cast<uint16_t>(kWakeChimePcm[byte_offset]) |
                               (static_cast<uint16_t>(kWakeChimePcm[byte_offset + 1]) << 8);
          sample = static_cast<int16_t>(static_cast<int16_t>(raw) * gain);
        }
        this->wake_ack_pcm_[2 * i] = sample;
        this->wake_ack_pcm_[2 * i + 1] = sample;
      }
    }
    const size_t remaining = OUTPUT_FRAME_BYTES - this->wake_ack_frame_offset_;
    const auto *pcm = reinterpret_cast<const uint8_t *>(this->wake_ack_pcm_.data());
    this->wake_ack_frame_offset_ += this->speaker_->play(pcm + this->wake_ack_frame_offset_, remaining, 0);
    if (this->wake_ack_frame_offset_ == OUTPUT_FRAME_BYTES) {
      this->wake_ack_frame_offset_ = 0;
      this->wake_ack_frame_index_++;
    }
    return;
  }
  this->speaker_->finish();
  this->wake_ack_draining_ = true;
}

void RealtimeCompanion::process_timer_alarm() {
  const int32_t timer_id = this->timer_alarm_id_.load(std::memory_order_acquire);
  if (timer_id != this->timer_alarm_handled_id_) {
    this->flush_playback();
    this->timer_alarm_handled_id_ = timer_id;
    this->timer_alarm_started_ms_ = millis();
    this->timer_alarm_repeat_after_ms_ = millis();
  }
  if (timer_id > 0) {
    if (static_cast<uint32_t>(millis() - this->timer_alarm_started_ms_) >= 60000) {
      ESP_LOGI(TAG, "Timer alarm timed out after 60 seconds id=%ld", static_cast<long>(timer_id));
      this->dismiss_timer_alarm();
    } else if (!this->wake_ack_pending_.load(std::memory_order_acquire) &&
               !this->session_active_.load(std::memory_order_acquire) &&
               this->output_volume_ > 0.0f &&
               this->speaker_->is_stopped() &&
               static_cast<int32_t>(millis() - this->timer_alarm_repeat_after_ms_) >= 0) {
      this->wake_ack_played_frames_.store(0, std::memory_order_relaxed);
      this->wake_ack_frame_index_ = 0;
      this->wake_ack_frame_offset_ = 0;
      this->wake_ack_started_ = false;
      this->wake_ack_draining_ = false;
      this->wake_ack_pending_.store(true, std::memory_order_release);
    }
  }
  const int32_t dismissed = this->timer_dismiss_pending_id_.load(std::memory_order_acquire);
  if (dismissed > 0 && this->send_json("{\"v\":1,\"type\":\"timer.dismissed\",\"timer_id\":" +
                                      std::to_string(dismissed) + "}")) {
    this->timer_dismiss_pending_id_.store(0, std::memory_order_release);
    ESP_LOGI(TAG, "Timer dismissal sent id=%ld", static_cast<long>(dismissed));
  }
}

void RealtimeCompanion::dismiss_timer_alarm() {
  const int32_t timer_id = this->timer_alarm_id_.exchange(0, std::memory_order_acq_rel);
  if (timer_id <= 0) return;
  this->timer_dismiss_pending_id_.store(timer_id, std::memory_order_release);
  this->wake_ack_pending_.store(false, std::memory_order_release);
  this->speaker_->stop();
  ESP_LOGI(TAG, "Timer alarm dismissed id=%ld", static_cast<long>(timer_id));
}

void RealtimeCompanion::start_session() {
  if (this->muted_ || this->session_active_ || this->is_timer_ringing())
    return;
  this->session_active_ = true;
  this->stream_ready_.store(false, std::memory_order_release);
  this->waiting_for_ready_.store(false, std::memory_order_release);
  this->ready_signal_sent_.store(false, std::memory_order_release);
  this->paused_.store(false, std::memory_order_release);
  this->barge_in_seen_.store(false, std::memory_order_release);
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

void RealtimeCompanion::wake_word_detected() {
  if (this->is_timer_ringing()) {
    this->dismiss_timer_alarm();
    return;
  }
  if (this->muted_ || this->session_active_)
    return;
  this->start_session();
  if (this->output_volume_ <= 0.0f)
    return;  // Zero output volume also silences the wake notification.
  this->wake_ack_played_frames_.store(0, std::memory_order_relaxed);
  this->wake_ack_frame_index_ = 0;
  this->wake_ack_frame_offset_ = 0;
  this->wake_ack_started_ = false;
  this->wake_ack_draining_ = false;
  this->wake_ack_pending_.store(true, std::memory_order_release);
  ESP_LOGI(TAG, "Wake word accepted; playing original 947 ms ReSpeaker chime before microphone transport");
}

void RealtimeCompanion::stop_session(const char *reason) {
  if (!this->session_active_)
    return;
  this->send_json("{\"v\":1,\"type\":\"session.stop\",\"reason\":\"" + std::string(reason) + "\"}");
  this->session_active_ = false;
  this->stream_ready_.store(false, std::memory_order_release);
  this->waiting_for_ready_.store(false, std::memory_order_release);
  this->ready_signal_sent_.store(false, std::memory_order_release);
  this->paused_.store(true, std::memory_order_release);
  this->barge_in_seen_.store(false, std::memory_order_release);
  xQueueReset(this->capture_queue_);
  this->flush_playback();
  this->update_state(this->muted_ ? CompanionState::MUTED : CompanionState::IDLE);
}

void RealtimeCompanion::ready_detected() {
  if (!this->ready_keyword_enabled_ ||
      !this->waiting_for_ready_.load(std::memory_order_acquire) ||
      this->ready_signal_sent_.exchange(true, std::memory_order_acq_rel))
    return;
  if (!this->send_json("{\"v\":1,\"type\":\"ready.detected\"}")) {
    this->ready_signal_sent_.store(false, std::memory_order_release);
    ESP_LOGW(TAG, "Could not queue ready-word notification");
    return;
  }
  ESP_LOGI(TAG, "Ready word detected; waiting for gateway acknowledgement");
}

void RealtimeCompanion::toggle_session() {
  if (this->is_timer_ringing()) {
    this->dismiss_timer_alarm();
    return;
  }
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
  this->wake_ack_pending_.store(false, std::memory_order_release);
  xQueueReset(this->playback_queue_);
  xQueueReset(this->progress_queue_);
  this->speaker_->stop();
  this->played_frames_.store(0, std::memory_order_relaxed);
  this->playback_active_.store(false, std::memory_order_release);
  this->playback_prebuffering_.store(false, std::memory_order_release);
  this->playback_end_received_.store(false, std::memory_order_release);
  std::lock_guard<std::mutex> lock(this->playback_mutex_);
  this->stream_id_.clear();
  this->expected_duration_ms_ = 0;
}

bool RealtimeCompanion::send_json(const std::string &json) {
  if (this->client_ == nullptr || !esp_websocket_client_is_connected(this->client_))
    return false;
  if (json.size() >= CONTROL_FRAME_BYTES) {
    ESP_LOGE(TAG, "WebSocket control frame is too large: %u bytes",
             static_cast<unsigned>(json.size()));
    return false;
  }
  ControlFrame frame{};
  frame.length = json.size();
  memcpy(frame.data.data(), json.data(), json.size());
  if (xQueueSend(this->control_queue_, &frame, 0) != pdTRUE) {
    ESP_LOGW(TAG, "WebSocket control queue full; dropping %u-byte message",
             static_cast<unsigned>(json.size()));
    return false;
  }
  return true;
}

bool RealtimeCompanion::send_playback_progress(const std::string &json) {
  if (this->client_ == nullptr || !esp_websocket_client_is_connected(this->client_))
    return false;
  if (json.size() >= CONTROL_FRAME_BYTES) {
    ESP_LOGE(TAG, "Playback progress frame is too large: %u bytes",
             static_cast<unsigned>(json.size()));
    return false;
  }
  ControlFrame frame{};
  frame.length = json.size();
  memcpy(frame.data.data(), json.data(), json.size());
  return xQueueOverwrite(this->progress_queue_, &frame) == pdTRUE;
}

bool RealtimeCompanion::is_network_ready() const { return network::is_connected(); }

bool RealtimeCompanion::is_barge_in_recent() const {
  return this->barge_in_seen_.load(std::memory_order_acquire) &&
         static_cast<uint32_t>(millis() - this->last_barge_in_ms_.load(std::memory_order_acquire)) < 1200;
}

void RealtimeCompanion::set_runtime_volume(float volume, bool persist) {
  const float bounded = std::max(0.0f, std::min(1.0f, volume));
  this->output_volume_ = bounded;
  if (persist && !this->volume_preference_.save(&this->output_volume_))
    ESP_LOGW(TAG, "Could not persist assistant output volume");
  ESP_LOGI(TAG, "Assistant output volume set to %.0f%% (effective PCM gain %.2f%%)",
           this->output_volume_ * 100.0f,
           this->output_volume_ * this->output_volume_ * 100.0f);
  this->report_volume();
}

void RealtimeCompanion::report_volume() {
  this->send_json("{\"v\":1,\"type\":\"volume.changed\",\"level\":" +
                  std::to_string(this->output_volume_) + "}");
}

void RealtimeCompanion::update_state(CompanionState state) {
  this->state_.store(state, std::memory_order_release);
}

void RealtimeCompanion::dump_config() {
  ESP_LOGCONFIG(TAG, "Realtime Thinking Companion:");
  ESP_LOGCONFIG(TAG, "  Gateway: %s", this->url_.c_str());
  ESP_LOGCONFIG(TAG, "  Device ID: %s", this->device_id_.c_str());
  ESP_LOGCONFIG(TAG, "  Assistant output volume: %.0f%% (effective PCM gain %.2f%%)",
                this->output_volume_ * 100.0f,
                this->output_volume_ * this->output_volume_ * 100.0f);
  ESP_LOGCONFIG(TAG, "  Ready keyword support: %s", this->ready_keyword_enabled_ ? "yes" : "no");
  ESP_LOGCONFIG(TAG, "  Playback prebuffer: %u x 20 ms (%u ms)",
                static_cast<unsigned>(this->playback_prebuffer_frames_),
                static_cast<unsigned>(this->playback_prebuffer_frames_) * 20);
  ESP_LOGCONFIG(TAG, "  Audio stats interval: %u ms", static_cast<unsigned>(this->audio_stats_interval_ms_));
  ESP_LOGCONFIG(TAG, "  XVF3610 channel-0 gain control: %s", this->mic_gain_controller_ != nullptr ? "yes" : "no");
  ESP_LOGCONFIG(TAG,
                "  TX queues: 8 control + 1 coalesced progress + 6 x 20 ms audio; playback queue: "
                "%u x 20 ms", static_cast<unsigned>(PLAYBACK_QUEUE_FRAMES));
}

}  // namespace esphome::realtime_companion
