#include "ffva_audio.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome::ffva_audio {

static const char *const TAG = "ffva_audio";
static constexpr uint8_t CODEC_PAGE = 0x00;
static constexpr uint8_t CODEC_NDAC = 0x0B;
static constexpr uint8_t CODEC_INTERFACE = 0x1B;
static constexpr uint8_t CODEC_AUDIO_IF_4 = 0x1F;
static constexpr uint8_t CODEC_DATA_IN_SELECT = 0x20;
static constexpr uint8_t CODEC_DIN_PIN_FUNCTION = 0x36;
static constexpr uint8_t CODEC_SCLK_PIN_FUNCTION = 0x38;

void FfvaAudio::setup() {
  codec_ready_ = configure_codec_primary_();
  if (!codec_ready_) {
    ESP_LOGE(TAG, "AIC3204 main-DIN configuration failed; speaker will stay disabled");
    this->mark_failed();
    return;
  }
  ESP_LOGI(TAG, "AIC3204 configured for GPIO44 main DIN and 24.576-MHz XMOS MCLK; waiting for DAC power-up and I2S clocks");
  ESP_LOGI(TAG, "Physical microphone mute is manual; release MUTE before audio testing");
}

void FfvaAudio::loop() {
  if (!codec_ready_ || output_ready_ || millis() < next_retry_ms_)
    return;
  if (!microphone_->is_running()) {
    if (!wait_for_clocks_logged_ && millis() >= 5000) {
      wait_for_clocks_logged_ = true;
      ESP_LOGW(TAG, "FFVA microphone driver not running; check XMOS image and I2S clocks");
    }
    return;
  }
  if (!audio_dac_->set_volume(0.65f) || !audio_dac_->set_mute_off()) {
    next_retry_ms_ = millis() + 1000;
    ESP_LOGW(TAG, "FFVA codec volume/unmute failed; retrying");
    return;
  }
  output_ready_ = true;
  ESP_LOGI(TAG, "FFVA startup path ready: codec configured, mic driver running, RX GPIO43, TX GPIO44");
}

void FfvaAudio::dump_config() {
  ESP_LOGCONFIG(TAG, "FFVA ReSpeaker Lite audio adapter:");
  LOG_I2C_DEVICE(this);
  ESP_LOGCONFIG(TAG, "  Codec: AIC3204 at 0x18, main DIN, NDAC=2, XMOS 24.576-MHz MCLK");
  ESP_LOGCONFIG(TAG, "  Speaker amplifier and LED gate: controlled by XMOS image");
}

void FfvaAudio::on_shutdown() {
  if (audio_dac_ != nullptr)
    audio_dac_->set_mute_on();
}

bool FfvaAudio::configure_codec_primary_() {
  // The pinned AIC3204 driver configures the analog output. GPIO44 is wired
  // to its main DIN; NDAC=2 divides FFVA's 24.576-MHz MCLK for 48-kHz audio.
  if (!this->write_byte(CODEC_PAGE, 0x00) ||
      !this->write_byte(CODEC_NDAC, 0x82) ||
      !this->write_byte(CODEC_DATA_IN_SELECT, 0x00) ||
      !this->write_byte(CODEC_DIN_PIN_FUNCTION, 0x02) ||
      !this->write_byte(CODEC_SCLK_PIN_FUNCTION, 0x00) ||
      !this->write_byte(CODEC_AUDIO_IF_4, 0x00) ||
      !this->write_byte(CODEC_DATA_IN_SELECT, 0x00))
    return false;

  uint8_t ndac = 0;
  uint8_t input_select = 0;
  uint8_t din_function = 0;
  uint8_t format = 0;
  const bool readback = this->read_byte(CODEC_NDAC, &ndac) &&
                        this->read_byte(CODEC_DATA_IN_SELECT, &input_select) &&
                        this->read_byte(CODEC_DIN_PIN_FUNCTION, &din_function) &&
                        this->read_byte(CODEC_INTERFACE, &format);
  if (readback)
    ESP_LOGI(TAG, "AIC3204 readback: NDAC=0x%02X data_in=0x%02X DIN_pin=0x%02X I2S_format=0x%02X",
             ndac, input_select, din_function, format);
  return readback && ndac == 0x82 && input_select == 0x00 &&
         (din_function & 0x06) == 0x02 && format == 0x30;
}

}  // namespace esphome::ffva_audio
