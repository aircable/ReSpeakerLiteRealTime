#pragma once

#include "esphome/components/audio_dac/audio_dac.h"
#include "esphome/components/i2c/i2c.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/core/component.h"
#include "esphome/components/realtime_companion/mic_gain_control.h"

#include <cstdint>
#include <array>
#include <atomic>

namespace esphome::xvf3610_audio {

// Board-specific adapter for the stock XVF3610-INT image on ReSpeaker Lite.
// The ESP32 sends playback to both AIC3204 main DIN and the XVF AEC reference
// on GPIO44, and receives processed microphone audio on GPIO43.
class Xvf3610Audio : public Component, public i2c::I2CDevice,
                     public realtime_companion::MicGainControl {
 public:
  void set_audio_dac(audio_dac::AudioDac *dac) { audio_dac_ = dac; }
  void set_microphone(microphone::Microphone *microphone) { microphone_ = microphone; }
  void set_aec_diagnostics(bool enabled) { aec_diagnostics_ = enabled; }
  void set_ffva_mode(bool enabled) { ffva_mode_ = enabled; }
  void request_ch0_gain(float gain) override;
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::LATE; }

 protected:
  bool configure_codec_primary_();
  bool set_xmos_gpo_(uint8_t pin, bool high);
  bool set_speaker_amp_(bool enabled);
  bool read_xmos_u8_(uint8_t command, uint8_t &value);
  void report_xmos_status_();
  enum class ProbeResult : uint8_t { DONE, WAIT, ERROR };
  ProbeResult read_dsp_field_(uint8_t resource, uint8_t command, uint8_t data_bytes,
                              std::array<uint32_t, 4> &values);
  void probe_dsp_();
  void log_dsp_snapshot_() const;
  void apply_requested_gain_();
  bool write_agc_channel_(uint8_t command, uint32_t value, uint32_t channel);

  audio_dac::AudioDac *audio_dac_{nullptr};
  microphone::Microphone *microphone_{nullptr};
  i2c::I2CDevice codec_i2c_{};
  bool codec_ready_{false};
  bool output_ready_{false};
  bool amp_ready_{false};
  bool led_line_high_requested_{false};
  bool wait_for_clocks_logged_{false};
  uint32_t next_retry_ms_{3500};
  uint32_t next_led_retry_ms_{0};
  uint32_t next_status_ms_{6000};
  uint8_t status_reports_{0};
  static constexpr uint8_t DSP_FIELD_COUNT = 8;
  static constexpr uint8_t DSP_QUALITY_FIELD_COUNT = 12;
  std::array<std::array<uint32_t, 4>, DSP_QUALITY_FIELD_COUNT> dsp_values_{};
  uint16_t dsp_valid_mask_{0};
  uint8_t dsp_field_{0};
  uint8_t dsp_attempts_{0};
  bool dsp_probe_active_{false};
  bool aec_diagnostics_{false};
  bool ffva_mode_{false};
  uint32_t next_dsp_probe_ms_{8000};
  std::atomic<uint32_t> requested_gain_q16_{0};  // 0 means adaptive AGC.
  std::atomic<uint32_t> gain_request_sequence_{0};
  uint32_t applied_gain_request_sequence_{0};
  uint32_t active_gain_request_sequence_{0};
  uint32_t active_gain_target_q16_{0};
  enum class GainStage : uint8_t {
    READ_ADAPT,
    READ_GAIN,
    RESTORE_CH1_ADAPT,
    WRITE_ADAPT,
    WRITE_GAIN,
    VERIFY_ADAPT,
    VERIFY_GAIN,
  };
  GainStage gain_stage_{GainStage::READ_ADAPT};
  std::array<uint32_t, 2> gain_adaptation_{};
  std::array<uint32_t, 2> gain_values_{};
  uint8_t gain_wait_attempts_{0};
  uint32_t next_gain_attempt_ms_{0};
};

}  // namespace esphome::xvf3610_audio
