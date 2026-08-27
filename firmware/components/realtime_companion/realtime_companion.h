#pragma once

#include "esphome/components/microphone/microphone.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/component.h"

#include <esp_websocket_client.h>
#include <resampler.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include <array>
#include <atomic>
#include <cstdint>
#include <mutex>
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
  void set_output_volume(float volume) { output_volume_ = volume; }
  void set_dac_volume(float volume) { dac_volume_ = volume; }

  void start_session();
  void stop_session(const char *reason = "button");
  void toggle_session();
  void set_muted(bool muted);
  CompanionState get_state() const { return state_.load(std::memory_order_acquire); }
  bool is_authenticated() const { return authenticated_.load(std::memory_order_acquire); }
  bool has_authenticated_once() const { return authenticated_once_.load(std::memory_order_acquire); }
  bool is_network_ready() const;

 protected:
  static void websocket_event(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data);
  static void audio_sender_task(void *parameter);
  void handle_websocket_event(int32_t event_id, esp_websocket_event_data_t *event);
  void handle_microphone_data(const std::vector<uint8_t> &data);
  void append_capture_sample(int16_t sample);
  void run_audio_sender();
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
  // esp_websocket_client has its own mutex, but short competing sends can time out before
  // acquiring it. Serialize our audio and control producers before entering the client.
  std::mutex websocket_send_mutex_;
  std::mutex playback_mutex_;
  float output_volume_{0.5f};
  float dac_volume_{0.85f};
  static constexpr size_t RESAMPLER_INPUT_FRAMES = 256;
  static constexpr size_t RESAMPLER_OUTPUT_FRAMES = 384;
  static constexpr UBaseType_t PLAYBACK_PREBUFFER_FRAMES = 6;
  esp_audio_libs::resampler::Resampler input_resampler_{RESAMPLER_INPUT_FRAMES,
                                                         RESAMPLER_OUTPUT_FRAMES};
  std::array<int16_t, RESAMPLER_INPUT_FRAMES> resampler_input_{};
  std::array<int16_t, RESAMPLER_OUTPUT_FRAMES> resampler_output_{};

  StaticQueue_t capture_queue_struct_{};
  StaticQueue_t playback_queue_struct_{};
  uint8_t capture_queue_storage_[6 * sizeof(InputFrame)]{};
  uint8_t playback_queue_storage_[10 * sizeof(OutputFrame)]{};
  QueueHandle_t capture_queue_{nullptr};
  QueueHandle_t playback_queue_{nullptr};
  // ESP-IDF's Xtensa StackType_t is uint8_t, so this count is bytes, not 32-bit words.
  static constexpr uint32_t AUDIO_SENDER_STACK_BYTES = 8192;
  StaticTask_t audio_sender_task_struct_{};
  StackType_t audio_sender_task_stack_[AUDIO_SENDER_STACK_BYTES]{};
  TaskHandle_t audio_sender_task_handle_{nullptr};
  InputFrame building_frame_{};
  size_t building_samples_{0};
  uint16_t building_peak_{0};
  bool capture_running_{false};
  std::atomic<uint32_t> captured_frames_{0};
  std::atomic<uint32_t> sent_frames_{0};
  std::atomic<uint32_t> dropped_frames_{0};
  std::atomic<uint16_t> capture_peak_{0};
  std::atomic<uint32_t> played_frames_{0};
  std::atomic<bool> playback_active_{false};
  std::atomic<bool> playback_prebuffering_{false};
  std::atomic<bool> playback_end_received_{false};
  uint32_t expected_duration_ms_{0};
  uint32_t last_progress_ms_{0};
  uint32_t last_audio_stats_ms_{0};
  uint32_t last_connect_attempt_ms_{0};
  std::atomic<CompanionState> state_{CompanionState::IDLE};
  std::atomic<bool> auth_pending_{false};
  std::atomic<bool> session_start_pending_{false};
  std::atomic<bool> authenticated_{false};
  std::atomic<bool> authenticated_once_{false};
  std::atomic<bool> session_active_{false};
  std::atomic<bool> stream_ready_{false};
  std::atomic<bool> muted_{false};
};

}  // namespace esphome::realtime_companion
