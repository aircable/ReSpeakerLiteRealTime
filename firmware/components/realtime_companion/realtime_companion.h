#pragma once

#include "esphome/components/microphone/microphone.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/component.h"
#include "esphome/core/preferences.h"

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
static constexpr size_t PLAYBACK_QUEUE_FRAMES = 16;  // 320 ms of 20 ms output frames
static constexpr size_t CONTROL_FRAME_BYTES = 512;
// A short Wi-Fi/TCP backpressure interval must not make esp_websocket_client abort the connection.
// This is a network-write timeout, not an audio pacing delay; the sender remains queue-driven.
static constexpr TickType_t WEBSOCKET_SEND_TIMEOUT = pdMS_TO_TICKS(1000);

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

struct ControlFrame {
  std::array<char, CONTROL_FRAME_BYTES> data;
  uint16_t length{0};
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
  void set_device_name(const std::string &device_name) { device_name_ = device_name; }
  void set_output_volume(float volume) { output_volume_ = volume; }
  void set_volume_preference_key(const std::string &key) { volume_preference_key_ = key; }
  void set_ready_keyword_enabled(bool enabled) { ready_keyword_enabled_ = enabled; }
  void set_playback_prebuffer_frames(uint8_t frames) { playback_prebuffer_frames_ = frames; }
  void set_audio_stats_interval_ms(uint32_t interval_ms) { audio_stats_interval_ms_ = interval_ms; }
  void set_startup_guard(Component *guard) { startup_guard_ = guard; }

  void start_session();
  void wake_word_detected();
  void stop_session(const char *reason = "button");
  void toggle_session();
  void set_muted(bool muted);
  void ready_detected();
  bool is_waiting_for_ready() const {
    return waiting_for_ready_.load(std::memory_order_acquire);
  }
  bool is_session_paused() const {
    return paused_.load(std::memory_order_acquire) &&
           !session_active_.load(std::memory_order_acquire);
  }
  bool is_barge_in_recent() const;
  bool is_timer_ringing() const { return timer_alarm_id_.load(std::memory_order_acquire) > 0; }
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
  bool send_playback_progress(const std::string &json);
  void flush_playback();
  void process_wake_ack();
  void process_timer_alarm();
  void dismiss_timer_alarm();
  void update_state(CompanionState state);
  void set_runtime_volume(float volume, bool persist);
  void report_volume();

  microphone::Microphone *microphone_{nullptr};
  Component *startup_guard_{nullptr};
  bool microphone_started_{false};
  speaker::Speaker *speaker_{nullptr};
  esp_websocket_client_handle_t client_{nullptr};
  std::string url_;
  std::string token_;
  std::string device_id_;
  std::string device_name_;
  std::string stream_id_;
  std::mutex playback_mutex_;
  float output_volume_{0.5f};
  std::string volume_preference_key_{"realtime_companion_output_volume"};
  ESPPreferenceObject volume_preference_;
  static constexpr size_t RESAMPLER_INPUT_FRAMES = 256;
  static constexpr size_t RESAMPLER_OUTPUT_FRAMES = 384;
  uint8_t playback_prebuffer_frames_{6};
  uint32_t audio_stats_interval_ms_{2000};
  esp_audio_libs::resampler::Resampler input_resampler_{RESAMPLER_INPUT_FRAMES,
                                                         RESAMPLER_OUTPUT_FRAMES};
  std::array<int16_t, RESAMPLER_INPUT_FRAMES> resampler_input_{};
  std::array<int16_t, RESAMPLER_OUTPUT_FRAMES> resampler_output_{};

  StaticQueue_t capture_queue_struct_{};
  StaticQueue_t control_queue_struct_{};
  StaticQueue_t progress_queue_struct_{};
  StaticQueue_t playback_queue_struct_{};
  uint8_t capture_queue_storage_[6 * sizeof(InputFrame)]{};
  uint8_t control_queue_storage_[8 * sizeof(ControlFrame)]{};
  uint8_t progress_queue_storage_[sizeof(ControlFrame)]{};
  uint8_t playback_queue_storage_[PLAYBACK_QUEUE_FRAMES * sizeof(OutputFrame)]{};
  QueueHandle_t capture_queue_{nullptr};
  QueueHandle_t control_queue_{nullptr};
  QueueHandle_t progress_queue_{nullptr};
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
  std::atomic<uint32_t> capture_rail_samples_{0};
  std::atomic<uint32_t> playback_dropped_frames_{0};
  std::atomic<uint16_t> capture_peak_{0};
  std::atomic<uint16_t> playback_input_peak_{0};
  std::atomic<uint16_t> playback_output_peak_{0};
  std::atomic<uint32_t> played_frames_{0};
  // A local chime is played before session.start, so its acoustic pickup is
  // never sent to the gateway as the user's first utterance.
  std::array<int16_t, OUTPUT_FRAME_BYTES / sizeof(int16_t)> wake_ack_pcm_{};
  std::atomic<bool> wake_ack_pending_{false};
  std::atomic<uint32_t> wake_ack_played_frames_{0};
  uint32_t wake_ack_started_ms_{0};
  uint16_t wake_ack_frame_index_{0};
  uint16_t wake_ack_frame_offset_{0};
  bool wake_ack_started_{false};
  bool wake_ack_draining_{false};
  std::atomic<int32_t> timer_alarm_id_{0};
  std::atomic<int32_t> timer_dismiss_pending_id_{0};
  uint32_t timer_alarm_repeat_after_ms_{0};
  uint32_t timer_alarm_started_ms_{0};
  int32_t timer_alarm_handled_id_{0};
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
  std::atomic<bool> paused_{false};
  std::atomic<bool> barge_in_seen_{false};
  std::atomic<uint32_t> last_barge_in_ms_{0};
  std::atomic<bool> stream_ready_{false};
  std::atomic<bool> muted_{false};
  bool ready_keyword_enabled_{false};
  std::atomic<bool> waiting_for_ready_{false};
  std::atomic<bool> ready_signal_sent_{false};
};

}  // namespace esphome::realtime_companion
