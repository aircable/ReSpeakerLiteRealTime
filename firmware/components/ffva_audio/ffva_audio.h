#pragma once

#include "esphome/components/audio_dac/audio_dac.h"
#include "esphome/components/i2c/i2c.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/core/component.h"

#include <cstdint>

namespace esphome::ffva_audio {

// Configures the ReSpeaker Lite codec for FFVA's XMOS-master I2S path.
// FFVA itself controls the speaker amplifier and WS2812 gate.
class FfvaAudio : public Component, public i2c::I2CDevice {
 public:
  void set_audio_dac(audio_dac::AudioDac *dac) { audio_dac_ = dac; }
  void set_microphone(microphone::Microphone *microphone) { microphone_ = microphone; }
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;
  float get_setup_priority() const override { return setup_priority::LATE; }

 protected:
  bool configure_codec_primary_();

  audio_dac::AudioDac *audio_dac_{nullptr};
  microphone::Microphone *microphone_{nullptr};
  bool codec_ready_{false};
  bool output_ready_{false};
  bool wait_for_clocks_logged_{false};
  uint32_t next_retry_ms_{3500};
};

}  // namespace esphome::ffva_audio
