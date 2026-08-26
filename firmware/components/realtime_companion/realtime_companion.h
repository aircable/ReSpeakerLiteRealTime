#pragma once

#include "esphome/components/microphone/microphone.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/component.h"

#include <esp_websocket_client.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace esphome::realtime_companion {

static constexpr size_t INPUT_FRAME_BYTES = 960;    // 20 ms, 24 kHz, mono, PCM16
static constexpr size_t OUTPUT_FRAME_BYTES = 1920;  // expanded to stereo before ESPHome resampler

enum class CompanionState : uint8_t {
  IDLE,
  CONNECTING,
  LISTENING,
  THINKING,
  SPEAKING,
  MUTED,
  ERROR,
};

struct InputFrame {
  std::array<uint8_t, INPUT_FRAME_BYTES> data;
};

struct OutputFrame {
  std::array<uint8_t, OUTPUT_FRAME_BYTES> data;
  uint16_t offset{0};
};

class FirDecimator2 {
 public:
  void push(int16_t sample, int16_t *output, bool *ready);
  void reset();

 protected:
  static constexpr size_t TAPS = 31;
  static const int16_t COEFFICIENTS[TAPS];
  int16_t history_[TAPS]{};
  size_t position_{0};
  bool phase_{false};
};

class RealtimeCompanion : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  void set_microphone(microphone::Microphone *microphone) { microphone_ = microphone; }
  void set_speaker(speaker::Speaker *speaker) { speaker_ = speaker; }
  void set_url(const std::string &url) { url_ = url; }
  void set_token(const std::string &token) { token_ = token; }
  void set_device_id(const std::string &device_id) { device_id_ = device_id; }

  void start_session();
  void stop_session(const char *reason = "button");
  void toggle_session();
  void set_muted(bool muted);
  CompanionState get_state() const { return state_; }

 protected:
  static void websocket_event(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data);
  void handle_websocket_event(int32_t event_id, esp_websocket_event_data_t *event);
  void handle_microphone_data(const std::vector<uint8_t> &data);
  void handle_text(const char *data, size_t length);
  void connect();
  bool send_json(const std::string &json);
  void flush_playback();
  void update_state(CompanionState state);

  microphone::Microphone *microphone_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  esp_websocket_client_handle_t client_{nullptr};
  std::string url_;
  std::string token_;
  std::string device_id_;
  std::string stream_id_;
  FirDecimator2 decimator_;

  StaticQueue_t capture_queue_struct_{};
  StaticQueue_t playback_queue_struct_{};
  uint8_t capture_queue_storage_[6 * sizeof(InputFrame)]{};
  uint8_t playback_queue_storage_[10 * sizeof(OutputFrame)]{};
  QueueHandle_t capture_queue_{nullptr};
  QueueHandle_t playback_queue_{nullptr};
  InputFrame building_frame_{};
  size_t building_samples_{0};
  std::atomic<uint32_t> played_frames_{0};
  uint32_t expected_duration_ms_{0};
  uint32_t last_progress_ms_{0};
  uint32_t last_connect_attempt_ms_{0};
  CompanionState state_{CompanionState::IDLE};
  std::atomic<bool> auth_pending_{false};
  std::atomic<bool> session_start_pending_{false};
  bool authenticated_{false};
  bool session_active_{false};
  bool muted_{false};
};

}  // namespace esphome::realtime_companion
