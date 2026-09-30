#include "xvf3610_diag.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <cstring>

namespace esphome::xvf3610_diag {

static const char *const TAG = "xvf3610_diag";

// XVF3610 5.7.3 host_control.h: special control, GPIO, and AEC resources.
static constexpr uint8_t CONTROL_RESOURCE = 0x00;
static constexpr uint8_t CONTROL_GET_VERSION = 0x80;
static constexpr uint8_t GPIO_RESOURCE = 0xE0;
static constexpr uint8_t GPIO_GET_I2S_RATE = 0x8F;
static constexpr uint8_t GPIO_GET_I2S_STARTED = 0x90;
static constexpr uint8_t GPIO_GET_MIC_STARTED = 0x9E;
static constexpr uint8_t GPIO_GET_RUN_STATUS = 0x98;
static constexpr uint8_t AEC_RESOURCE = 0x11;
static constexpr uint8_t AEC_GET_BYPASS = 0x80;
static constexpr uint8_t AEC_GET_Y_CHANNELS = 0x8B;
static constexpr uint8_t AEC_GET_X_CHANNELS = 0x8C;

void Xvf3610Diag::setup() {
  if (microphone_ == nullptr) {
    ESP_LOGE(TAG, "No microphone configured");
    this->mark_failed();
    return;
  }
  microphone_->add_data_callback([this](const std::vector<uint8_t> &data) { on_audio_(data); });
  // Starting the ESP I2S master produces the clocks required by the XVF3610-INT.
  // The first control probe is delayed so XMOS has time to start processing frames.
  microphone_->start();
  ESP_LOGI(TAG, "Started ESP I2S master; waiting 6 s before XVF3610 control reads");
}

void Xvf3610Diag::dump_config() {
  ESP_LOGCONFIG(TAG, "XVF3610-INT read-only audio diagnostic:");
  LOG_I2C_DEVICE(this);
  ESP_LOGCONFIG(TAG, "  Microphone clock source: ESP32 I2S master, 48 kHz");
  ESP_LOGCONFIG(TAG, "  No XMOS flash, codec, or AEC writes");
}

bool Xvf3610Diag::read_command_(uint8_t resource, uint8_t command, uint8_t *value,
                                size_t value_size) {
  if (value_size == 0 || value_size > 4)
    return false;

  // XVF GET requests include the control-status byte in their payload length.
  const uint8_t request[] = {resource, command, static_cast<uint8_t>(value_size + 1)};
  uint8_t response[5]{};
  for (unsigned attempt = 0; attempt < 4; attempt++) {
    if (this->write_read(request, sizeof(request), response, value_size + 1) != i2c::ERROR_OK)
      return false;
    if (response[0] == 0) {  // CTRL_DONE
      std::memcpy(value, response + 1, value_size);
      return true;
    }
    if (response[0] != 1)  // CTRL_WAIT can be retried after an audio frame.
      return false;
    delay(20);
  }
  return false;
}

bool Xvf3610Diag::read_u8_(uint8_t resource, uint8_t command, uint8_t &value) {
  return read_command_(resource, command, &value, 1);
}

bool Xvf3610Diag::read_u32_(uint8_t resource, uint8_t command, uint32_t &value) {
  uint8_t bytes[4]{};
  if (!read_command_(resource, command, bytes, sizeof(bytes)))
    return false;
  // XVF control parameters use network byte order, not ESP32 little endian.
  value = (uint32_t(bytes[0]) << 24) | (uint32_t(bytes[1]) << 16) |
          (uint32_t(bytes[2]) << 8) | uint32_t(bytes[3]);
  return true;
}

void Xvf3610Diag::report_control_() {
  uint8_t version = 0;
  uint8_t run_status = 0;
  uint8_t i2s_started = 0;
  uint8_t mic_started = 0;
  uint32_t rate = 0;
  uint32_t aec_bypass = 0;
  uint32_t y_channels = 0;
  uint32_t x_channels = 0;

  const bool version_ok = read_u8_(CONTROL_RESOURCE, CONTROL_GET_VERSION, version);
  if (!version_ok) {
    ESP_LOGW(TAG, "XVF3610 control at 0x2C did not respond; check MCLK/BCLK/LRCLK");
    return;
  }
  const bool run_ok = read_u8_(GPIO_RESOURCE, GPIO_GET_RUN_STATUS, run_status);
  const bool rate_ok = read_u32_(GPIO_RESOURCE, GPIO_GET_I2S_RATE, rate);
  const bool i2s_ok = read_u8_(GPIO_RESOURCE, GPIO_GET_I2S_STARTED, i2s_started);
  const bool mic_ok = read_u8_(GPIO_RESOURCE, GPIO_GET_MIC_STARTED, mic_started);
  ESP_LOGI(TAG, "XVF control=0x%02X run=%s%u i2s_rate=%s%u i2s_start=%s%u mic_start=%s%u",
           version, run_ok ? "" : "?", run_status, rate_ok ? "" : "?", rate,
           i2s_ok ? "" : "?", i2s_started, mic_ok ? "" : "?", mic_started);

  const bool bypass_ok = read_u32_(AEC_RESOURCE, AEC_GET_BYPASS, aec_bypass);
  const bool y_ok = read_u32_(AEC_RESOURCE, AEC_GET_Y_CHANNELS, y_channels);
  const bool x_ok = read_u32_(AEC_RESOURCE, AEC_GET_X_CHANNELS, x_channels);
  if (bypass_ok || y_ok || x_ok)
    ESP_LOGI(TAG, "AEC bypass=%s%u Y_channels=%s%u X_channels=%s%u",
             bypass_ok ? "" : "?", aec_bypass, y_ok ? "" : "?", y_channels,
             x_ok ? "" : "?", x_channels);
}

void Xvf3610Diag::on_audio_(const std::vector<uint8_t> &data) {
  callbacks_.fetch_add(1, std::memory_order_relaxed);
  if (data.empty()) {
    empty_callbacks_.fetch_add(1, std::memory_order_relaxed);
    return;
  }
  // formatBCE's pinned microphone driver decimates 48 kHz stereo PCM32 to 16 kHz.
  const size_t frames = data.size() / 8;
  sample_frames_.fetch_add(frames, std::memory_order_relaxed);
  uint32_t block_peak = 0;
  for (size_t i = 0; i < frames; i++) {
    int32_t sample = 0;
    std::memcpy(&sample, data.data() + i * 8, sizeof(sample));
    const int32_t pcm16 = sample >> 16;
    const uint32_t magnitude = pcm16 < 0 ? uint32_t(-pcm16) : uint32_t(pcm16);
    if (magnitude > block_peak)
      block_peak = magnitude;
  }
  uint32_t previous = peak_.load(std::memory_order_relaxed);
  while (block_peak > previous &&
         !peak_.compare_exchange_weak(previous, block_peak, std::memory_order_relaxed)) {}
}

void Xvf3610Diag::update() {
  const uint32_t frames = sample_frames_.exchange(0, std::memory_order_relaxed);
  const uint32_t callbacks = callbacks_.exchange(0, std::memory_order_relaxed);
  const uint32_t empty = empty_callbacks_.exchange(0, std::memory_order_relaxed);
  const uint32_t peak = peak_.exchange(0, std::memory_order_relaxed);
  ESP_LOGI(TAG, "Mic/2s: callbacks=%u frames=%u expected_about=32000 empty=%u peak=%u running=%s",
           callbacks, frames, empty, peak, microphone_->is_running() ? "yes" : "no");

  if (millis() >= 6000 && (last_control_ms_ == 0 || millis() - last_control_ms_ >= 10000)) {
    last_control_ms_ = millis();
    report_control_();
  }
}

}  // namespace esphome::xvf3610_diag
