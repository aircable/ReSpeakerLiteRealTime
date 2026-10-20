#include "xvf3610_audio.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <cmath>

namespace esphome::xvf3610_audio {

static const char *const TAG = "xvf3610_audio";
static constexpr uint8_t CODEC_ADDRESS = 0x18;
static constexpr uint8_t CODEC_PAGE = 0x00;
static constexpr uint8_t CODEC_NDAC = 0x0B;
static constexpr uint8_t CODEC_INTERFACE = 0x1B;
static constexpr uint8_t CODEC_AUDIO_IF_4 = 0x1F;
static constexpr uint8_t CODEC_DATA_IN_SELECT = 0x20;
static constexpr uint8_t CODEC_DIN_PIN_FUNCTION = 0x36;
static constexpr uint8_t CODEC_SCLK_PIN_FUNCTION = 0x38;
static constexpr uint8_t GPIO_RESOURCE = 0xE0;
static constexpr uint8_t GPIO_SET_GPO_PIN = 0x07;
static constexpr uint8_t GPIO_GET_RUN_STATUS = 0x98;
static constexpr uint8_t GPIO_GET_I2S_STARTED = 0x90;
static constexpr uint8_t GPIO_GET_MIC_STARTED = 0x9E;
static constexpr uint8_t AGC_RESOURCE = 0x31;
static constexpr uint8_t AGC_SET_GAIN = 0x00;
static constexpr uint8_t AGC_SET_ADAPT = 0x02;
static constexpr uint8_t STAGE_A_RESOURCE = 0x10;
static constexpr uint8_t AEC_RESOURCE = 0x11;
static constexpr uint8_t PIPELINE_CONTROL_RESOURCE = 0xF0;

struct DspField {
  uint8_t resource;
  uint8_t command;
  uint8_t data_bytes;
};

// XVF3610-INT 5.7.3 host_control.h. AGC GETs return both channels, even when
// the vfctrl command is named CH0; the host tool selects the requested half.
static constexpr std::array<DspField, 12> DSP_FIELDS{{
    {AGC_RESOURCE, 0x80, 8},               // GET_GAIN_CH0/CH1_AGC, Q16.16
    {AGC_RESOURCE, 0x81, 8},               // GET_MAX_GAIN_CH0/CH1_AGC, Q16.16
    {AGC_RESOURCE, 0x82, 8},               // GET_ADAPT_CH0/CH1_AGC
    {AGC_RESOURCE, 0x87, 8},               // GET_LC_ENABLED_CH0/CH1_AGC
    {STAGE_A_RESOURCE, 0x8E, 4},           // GET_ADEC_ENABLED
    {STAGE_A_RESOURCE, 0x8F, 4},           // GET_ADEC_MODE
    {PIPELINE_CONTROL_RESOURCE, 0x82, 4}, // GET_DELAY_SAMPLES
    {AEC_RESOURCE, 0x80, 4},               // GET_BYPASS_AEC
    {STAGE_A_RESOURCE, 0x8A, 4},           // GET_DELAY_DIRECTION
    {AEC_RESOURCE, 0x84, 4},               // GET_ADAPTATION_CONFIG_AEC
    {AEC_RESOURCE, 0x88, 16},              // GET_ERLE_CH0_AEC: mic and residual energies
    {STAGE_A_RESOURCE, 0x97, 8},           // GET_AEC_PEAK_TO_AVERAGE_RATIO
}};

// XMOS TYPE_ENERGY is a network-order 64-bit mantissa/exponent pair. See
// XVF3610-INT host/dsp_control/src/host.c:convert_uint64_to_energy().
static double decode_energy(const std::array<uint32_t, 4> &words, size_t start) {
  return std::ldexp(static_cast<double>(words[start]), static_cast<int32_t>(words[start + 1]));
}

void Xvf3610Audio::setup() {
  // Board experiment: OP_1 is the only unused OP_1..OP_3 output. The Seeed
  // schematic shows an XMOS WS2812 input but does not identify its XMOS pin.
  // A high level is the non-clamping state for the shared LED data circuit.
  if (!ffva_mode_) {
    if (set_xmos_gpo_(1, true)) {
      led_line_high_requested_ = true;
      ESP_LOGI(TAG, "XMOS OP_1 high request sent for shared WS2812 test (physical connection unverified)");
    } else {
      ESP_LOGW(TAG, "XMOS OP_1 high request failed at setup; retrying");
    }
  }
  codec_i2c_.set_i2c_bus(this->bus_);
  codec_i2c_.set_i2c_address(CODEC_ADDRESS);
  codec_ready_ = configure_codec_primary_();
  if (!codec_ready_) {
    ESP_LOGE(TAG, "AIC3204 main-DIN configuration failed; speaker will stay disabled");
    this->mark_failed();
    return;
  }
  ESP_LOGI(TAG, "AIC3204 configured for GPIO44 main DIN and %s MCLK; waiting for DAC power-up and I2S clocks",
           ffva_mode_ ? "24.576-MHz XMOS" : "12.288-MHz ESP32");
  if (ffva_mode_)
    ESP_LOGI(TAG, "Physical microphone mute is manual; release MUTE before audio testing");
  else
    ESP_LOGW(TAG, "XVF3610 physical microphone mute is manual; check MUTE before wake-word testing");
}

void Xvf3610Audio::loop() {
  if (ffva_mode_) {
    // FFVA controls the amplifier and LED gate internally. Its I2C telemetry
    // is not the XVF3610 0x2C control protocol, so do not poll/write it here.
    if (!codec_ready_ || output_ready_ || millis() < next_retry_ms_)
      return;
    if (!microphone_->is_running()) {
      if (!wait_for_clocks_logged_ && millis() >= 5000) {
        wait_for_clocks_logged_ = true;
        ESP_LOGW(TAG, "FFVA microphone driver not running; check XMOS RAM image and I2S clocks");
      }
      return;
    }
    if (!audio_dac_->set_volume(0.65f) || !audio_dac_->set_mute_off()) {
      next_retry_ms_ = millis() + 1000;
      ESP_LOGW(TAG, "FFVA codec volume/unmute failed; retrying");
      return;
    }
    output_ready_ = true;
    ESP_LOGI(TAG, "FFVA startup path ready: codec configured, mic driver running, RX GPIO43, TX GPIO44; verify audio peaks during session");
    return;
  }
  if (!led_line_high_requested_ && millis() >= next_led_retry_ms_) {
    next_led_retry_ms_ = millis() + 1000;
    if (set_xmos_gpo_(1, true)) {
      led_line_high_requested_ = true;
      ESP_LOGI(TAG, "XMOS OP_1 high request sent for shared WS2812 test (physical connection unverified)");
    }
  }
  if (status_reports_ < 3 && millis() >= next_status_ms_) {
    report_xmos_status_();
    status_reports_++;
    next_status_ms_ = millis() + 10000;
  }
  if (millis() >= next_dsp_probe_ms_)
    probe_dsp_();
  if (gain_request_sequence_.load(std::memory_order_acquire) != applied_gain_request_sequence_ &&
      millis() >= next_gain_attempt_ms_ && microphone_->is_running())
    apply_requested_gain_();
  if (!codec_ready_ || amp_ready_ || millis() < next_retry_ms_)
    return;
  next_retry_ms_ = millis() + 1000;
  if (!microphone_->is_running()) {
    if (!wait_for_clocks_logged_ && millis() >= 5000) {
      wait_for_clocks_logged_ = true;
      ESP_LOGW(TAG, "Waiting for microphone I2S clock before enabling XMOS speaker amp");
    }
    return;
  }
  if (!output_ready_) {
    // AIC3204's own 2.5-second power-up callback has finished by this point.
    // This DAC setting is the audible, unclipped value from the duplex test;
    // the assistant's separate squared PCM volume control remains in force.
    if (!audio_dac_->set_volume(0.65f) || !audio_dac_->set_mute_off()) {
      ESP_LOGW(TAG, "AIC3204 volume/unmute failed; retrying in 1 s");
      return;
    }
    output_ready_ = true;
  }
  if (!set_speaker_amp_(true)) {
    ESP_LOGW(TAG, "XVF3610 OP_2 amp enable failed; retrying in 1 s");
    return;
  }
  amp_ready_ = true;
  ESP_LOGI(TAG, "XVF3610 live audio ready: microphone RX GPIO43, speaker/AEC reference TX GPIO44");
}

void Xvf3610Audio::dump_config() {
  if (ffva_mode_) {
    ESP_LOGCONFIG(TAG, "FFVA ReSpeaker Lite audio adapter:");
    ESP_LOGCONFIG(TAG, "  Codec: AIC3204 at 0x18, main DIN, NDAC=2, XMOS 24.576-MHz MCLK");
    ESP_LOGCONFIG(TAG, "  Speaker amplifier and LED gate: controlled by XMOS image");
    ESP_LOGCONFIG(TAG, "  XVF3610 I2C AGC/AEC controls: disabled");
    return;
  }
  ESP_LOGCONFIG(TAG, "XVF3610-INT ReSpeaker Lite audio adapter:");
  LOG_I2C_DEVICE(this);
  ESP_LOGCONFIG(TAG, "  Codec: AIC3204 at 0x18, main DIN, NDAC=1, 12.288-MHz MCLK");
  ESP_LOGCONFIG(TAG, "  Speaker amplifier: XMOS OP_2 runtime enable");
  ESP_LOGCONFIG(TAG, "  Shared WS2812 test: request XMOS OP_1 high (board connection unverified)");
  ESP_LOGCONFIG(TAG, "  Microphone mute: physical/manual; no blind boot-time toggle");
  ESP_LOGCONFIG(TAG, "  Read-only AEC diagnostics: %s", aec_diagnostics_ ? "enabled" : "disabled");
  ESP_LOGCONFIG(TAG, "  Channel-0 AGC: gateway-controlled fixed gain or adaptive; channel 1 unchanged");
}

void Xvf3610Audio::request_ch0_gain(float gain) {
  if (!std::isfinite(gain) || gain < 0.0f || gain > 1000.0f) {
    ESP_LOGW(TAG, "Rejected channel-0 AGC gain %.2f; use 0 (adaptive) or 0..1000", gain);
    return;
  }
  requested_gain_q16_.store(static_cast<uint32_t>(std::lround(gain * 65536.0f)),
                            std::memory_order_release);
  gain_request_sequence_.fetch_add(1, std::memory_order_acq_rel);
  ESP_LOGI(TAG, "Channel-0 AGC requested: %s%.1f", gain == 0.0f ? "adaptive " : "fixed gain ", gain);
}

bool Xvf3610Audio::write_agc_channel_(uint8_t command, uint32_t value, uint32_t channel) {
  // XVF3610-INT 5.7.3 host.c appends the channel index after the value for
  // SET_*_CH0/CH1_AGC. GETs instead return both channel values.
  const uint8_t request[] = {
      AGC_RESOURCE, command, 8,
      uint8_t(value >> 24), uint8_t(value >> 16), uint8_t(value >> 8), uint8_t(value),
      uint8_t(channel >> 24), uint8_t(channel >> 16), uint8_t(channel >> 8), uint8_t(channel),
  };
  return this->write(request, sizeof(request)) == i2c::ERROR_OK;
}

void Xvf3610Audio::apply_requested_gain_() {
  const uint32_t sequence = gain_request_sequence_.load(std::memory_order_acquire);
  if (sequence != active_gain_request_sequence_) {
    active_gain_request_sequence_ = sequence;
    active_gain_target_q16_ = requested_gain_q16_.load(std::memory_order_acquire);
    gain_stage_ = GainStage::READ_ADAPT;
    gain_wait_attempts_ = 0;
  }
  next_gain_attempt_ms_ = millis() + 20;
  std::array<uint32_t, 4> values{};
  ProbeResult result;
  switch (gain_stage_) {
    case GainStage::READ_ADAPT:
    case GainStage::VERIFY_ADAPT:
      result = read_dsp_field_(AGC_RESOURCE, 0x82, 8, values);
      if (result != ProbeResult::DONE) {
        if (result == ProbeResult::ERROR || ++gain_wait_attempts_ >= 10) {
          gain_wait_attempts_ = 0;
          next_gain_attempt_ms_ = millis() + 500;
        }
        return;
      }
      gain_wait_attempts_ = 0;
      gain_adaptation_ = {values[0], values[1]};
      if (gain_adaptation_[1] != 1) {
        // The old pair-format bug could disable channel 1. Its intended
        // wake-word path is adaptive; repair it before accepting this request.
        gain_stage_ = GainStage::RESTORE_CH1_ADAPT;
        return;
      }
      gain_stage_ = gain_stage_ == GainStage::READ_ADAPT ? GainStage::READ_GAIN : GainStage::VERIFY_GAIN;
      return;
    case GainStage::READ_GAIN:
    case GainStage::VERIFY_GAIN:
      result = read_dsp_field_(AGC_RESOURCE, 0x80, 8, values);
      if (result != ProbeResult::DONE) {
        if (result == ProbeResult::ERROR || ++gain_wait_attempts_ >= 10) {
          gain_wait_attempts_ = 0;
          next_gain_attempt_ms_ = millis() + 500;
        }
        return;
      }
      gain_wait_attempts_ = 0;
      gain_values_ = {values[0], values[1]};
      if ((active_gain_target_q16_ == 0 && gain_adaptation_[0] == 1) ||
          (active_gain_target_q16_ != 0 && gain_adaptation_[0] == 0 &&
           gain_values_[0] == active_gain_target_q16_)) {
        applied_gain_request_sequence_ = sequence;
        ESP_LOGI(TAG, "XMOS channel-0 AGC applied: %s gain=%.1f; channel-1 adapt=%u gain=%.1f",
                 active_gain_target_q16_ == 0 ? "adaptive" : "fixed",
                 gain_values_[0] / 65536.0f, gain_adaptation_[1], gain_values_[1] / 65536.0f);
      } else if (gain_stage_ == GainStage::VERIFY_GAIN) {
        ESP_LOGW(TAG, "XMOS channel-0 AGC readback mismatch: target=%s%.1f gain=%.1f adapt_0/1=%u/%u; retrying",
                 active_gain_target_q16_ == 0 ? "adaptive " : "fixed ",
                 active_gain_target_q16_ / 65536.0f, gain_values_[0] / 65536.0f,
                 gain_adaptation_[0], gain_adaptation_[1]);
        gain_stage_ = GainStage::READ_ADAPT;
        next_gain_attempt_ms_ = millis() + 500;
      } else {
        gain_stage_ = active_gain_target_q16_ == 0 || gain_adaptation_[0] != 0
                          ? GainStage::WRITE_ADAPT : GainStage::WRITE_GAIN;
      }
      return;
    case GainStage::RESTORE_CH1_ADAPT:
      if (!write_agc_channel_(AGC_SET_ADAPT, 1, 1)) {
        ESP_LOGW(TAG, "XMOS channel-1 AGC adaptation restore failed; retrying");
        next_gain_attempt_ms_ = millis() + 500;
        return;
      }
      ESP_LOGW(TAG, "Restoring XMOS channel-1 AGC adaptation after legacy gain-command bug");
      gain_stage_ = GainStage::READ_ADAPT;
      next_gain_attempt_ms_ = millis() + 500;
      return;
    case GainStage::WRITE_ADAPT:
      // Freeze channel 0 before setting its gain, so AGC cannot move the
      // value during the 20 ms between I2C control transactions.
      if (!write_agc_channel_(AGC_SET_ADAPT, active_gain_target_q16_ == 0 ? 1 : 0, 0)) {
        ESP_LOGW(TAG, "XMOS channel-0 AGC adaptation write failed; retrying");
        next_gain_attempt_ms_ = millis() + 500;
        return;
      }
      gain_stage_ = active_gain_target_q16_ == 0 ? GainStage::VERIFY_ADAPT : GainStage::WRITE_GAIN;
      return;
    case GainStage::WRITE_GAIN:
      if (!write_agc_channel_(AGC_SET_GAIN, active_gain_target_q16_, 0)) {
        ESP_LOGW(TAG, "XMOS channel-0 AGC gain write failed; retrying");
        next_gain_attempt_ms_ = millis() + 500;
        return;
      }
      gain_stage_ = GainStage::VERIFY_ADAPT;
      return;
  }
}

void Xvf3610Audio::on_shutdown() {
  if (audio_dac_ != nullptr)
    audio_dac_->set_mute_on();
  if (amp_ready_)
    set_speaker_amp_(false);
}

bool Xvf3610Audio::configure_codec_primary_() {
  // The pinned AIC3204 driver sets the analog outputs and secondary input.
  // GPIO44 is wired to the codec's main DIN. XVF3610 mode uses ESP32's
  // 12.288-MHz MCLK (NDAC=1); FFVA mode uses XMOS's 24.576-MHz MCLK (NDAC=2).
  const uint8_t ndac_target = ffva_mode_ ? 0x82 : 0x81;
  if (!codec_i2c_.write_byte(CODEC_PAGE, 0x00) ||
      !codec_i2c_.write_byte(CODEC_NDAC, ndac_target) ||
      !codec_i2c_.write_byte(CODEC_DATA_IN_SELECT, 0x00))
    return false;
  if (ffva_mode_ &&
      (!codec_i2c_.write_byte(CODEC_DIN_PIN_FUNCTION, 0x02) ||
       !codec_i2c_.write_byte(CODEC_SCLK_PIN_FUNCTION, 0x00) ||
       !codec_i2c_.write_byte(CODEC_AUDIO_IF_4, 0x00) ||
       !codec_i2c_.write_byte(CODEC_DATA_IN_SELECT, 0x00)))
    return false;
  uint8_t ndac = 0;
  uint8_t input_select = 0;
  uint8_t din_function = 0;
  uint8_t format = 0;
  const bool readback = codec_i2c_.read_byte(CODEC_NDAC, &ndac) &&
                        codec_i2c_.read_byte(CODEC_DATA_IN_SELECT, &input_select) &&
                        codec_i2c_.read_byte(CODEC_DIN_PIN_FUNCTION, &din_function) &&
                        codec_i2c_.read_byte(CODEC_INTERFACE, &format);
  if (readback)
    ESP_LOGI(TAG, "AIC3204 readback: NDAC=0x%02X data_in=0x%02X DIN_pin=0x%02X I2S_format=0x%02X",
             ndac, input_select, din_function, format);
  return readback && ndac == ndac_target && input_select == 0x00 &&
         (din_function & 0x06) == 0x02 && format == 0x30;
}

bool Xvf3610Audio::set_xmos_gpo_(uint8_t pin, bool high) {
  // XVF3610 5.7.3 host_control.h: resource E0, SET_GPO_PIN, [port,pin,level].
  const uint8_t request[] = {GPIO_RESOURCE, GPIO_SET_GPO_PIN, 3, 0, pin, uint8_t(high)};
  return this->write(request, sizeof(request)) == i2c::ERROR_OK;
}

bool Xvf3610Audio::set_speaker_amp_(bool enabled) {
  // On this ReSpeaker Lite, OP_2 / X0D31 drives AUDIO_PA_EN.
  return set_xmos_gpo_(2, enabled);
}

bool Xvf3610Audio::read_xmos_u8_(uint8_t command, uint8_t &value) {
  // XVF3610 5.7.3 control GET reply: status byte (0=done), then one value.
  // A single read keeps the audio loop responsive if the XMOS is not ready.
  const uint8_t request[] = {GPIO_RESOURCE, command, 2};
  uint8_t response[2]{};
  if (this->write_read(request, sizeof(request), response, sizeof(response)) != i2c::ERROR_OK ||
      response[0] != 0)
    return false;
  value = response[1];
  return true;
}

void Xvf3610Audio::report_xmos_status_() {
  uint8_t run = 0;
  uint8_t i2s = 0;
  uint8_t mic = 0;
  const bool run_ok = read_xmos_u8_(GPIO_GET_RUN_STATUS, run);
  const bool i2s_ok = read_xmos_u8_(GPIO_GET_I2S_STARTED, i2s);
  const bool mic_ok = read_xmos_u8_(GPIO_GET_MIC_STARTED, mic);
  ESP_LOGI(TAG, "XMOS startup status: run=%s%u i2s_start=%s%u mic_start=%s%u esp_mic_running=%s codec_ready=%s amp_ready=%s uptime_ms=%u",
           run_ok ? "" : "?", run, i2s_ok ? "" : "?", i2s, mic_ok ? "" : "?", mic,
           microphone_->is_running() ? "yes" : "no", codec_ready_ ? "yes" : "no",
           amp_ready_ ? "yes" : "no", millis());
}

Xvf3610Audio::ProbeResult Xvf3610Audio::read_dsp_field_(
    uint8_t resource, uint8_t command, uint8_t data_bytes, std::array<uint32_t, 4> &values) {
  // Exactly one transaction per loop pass. An unanswered GET must not block
  // microphone capture or speaker playback while the XMOS is busy.
  if (data_bytes == 0 || data_bytes > 16 || data_bytes % 4 != 0)
    return ProbeResult::ERROR;
  const uint8_t request[] = {resource, command, uint8_t(data_bytes + 1)};
  uint8_t response[17]{};
  if (this->write_read(request, sizeof(request), response, data_bytes + 1) != i2c::ERROR_OK)
    return ProbeResult::ERROR;
  if (response[0] == 1 || response[0] == 2) // CTRL_WAIT or CTRL_QUEUE_FULL
    return ProbeResult::WAIT;
  if (response[0] != 0) // CTRL_INVALID
    return ProbeResult::ERROR;
  auto decode_be32 = [](const uint8_t *bytes) {
    return (uint32_t(bytes[0]) << 24) | (uint32_t(bytes[1]) << 16) |
           (uint32_t(bytes[2]) << 8) | uint32_t(bytes[3]);
  };
  values.fill(0);
  for (uint8_t i = 0; i < data_bytes / 4; i++)
    values[i] = decode_be32(response + 1 + i * 4);
  return ProbeResult::DONE;
}

void Xvf3610Audio::probe_dsp_() {
  if (!dsp_probe_active_) {
    dsp_probe_active_ = true;
    dsp_field_ = 0;
    dsp_attempts_ = 0;
    dsp_valid_mask_ = 0;
  }
  const auto &field = DSP_FIELDS[dsp_field_];
  const auto result = read_dsp_field_(field.resource, field.command, field.data_bytes,
                                      dsp_values_[dsp_field_]);
  if (result == ProbeResult::WAIT && ++dsp_attempts_ < 10) {
    next_dsp_probe_ms_ = millis() + 20;
    return;
  }
  if (result == ProbeResult::DONE)
    dsp_valid_mask_ |= uint16_t(1U << dsp_field_);
  dsp_attempts_ = 0;
  if (++dsp_field_ == (aec_diagnostics_ ? DSP_QUALITY_FIELD_COUNT : DSP_FIELD_COUNT)) {
    log_dsp_snapshot_();
    dsp_probe_active_ = false;
    next_dsp_probe_ms_ = millis() + (aec_diagnostics_ ? 2000 : 5000);
  } else {
    next_dsp_probe_ms_ = millis() + 20;
  }
}

void Xvf3610Audio::log_dsp_snapshot_() const {
  auto valid = [this](uint8_t field) { return (dsp_valid_mask_ & (1U << field)) != 0; };
  ESP_LOGI(TAG, "XMOS DSP AGC: gain_0/1=%s%.1f/%.1f max_0/1=%s%.1f/%.1f adapt_0/1=%s%u/%u loss_0/1=%s%u/%u",
           valid(0) ? "" : "?", dsp_values_[0][0] / 65536.0f, dsp_values_[0][1] / 65536.0f,
           valid(1) ? "" : "?", dsp_values_[1][0] / 65536.0f, dsp_values_[1][1] / 65536.0f,
           valid(2) ? "" : "?", dsp_values_[2][0], dsp_values_[2][1],
           valid(3) ? "" : "?", dsp_values_[3][0], dsp_values_[3][1]);
  ESP_LOGI(TAG, "XMOS DSP AEC: bypass=%s%u adec_enabled=%s%u adec_mode=%s%u delay_samples=%s%u",
           valid(7) ? "" : "?", dsp_values_[7][0],
           valid(4) ? "" : "?", dsp_values_[4][0],
           valid(5) ? "" : "?", dsp_values_[5][0],
           valid(6) ? "" : "?", dsp_values_[6][0]);
  if (!aec_diagnostics_)
    return;

  // ERLE is meaningful while far-end audio is playing and ADEC is in normal
  // mode. ALT_ARCH only provides valid ERLE for channel 0.
  const double mic_energy = decode_energy(dsp_values_[10], 0);
  const double residual_energy = decode_energy(dsp_values_[10], 2);
  const bool erle_valid = valid(10) && mic_energy > 0.0 && residual_energy > 0.0 &&
                          std::isfinite(mic_energy) && std::isfinite(residual_energy);
  const double erle_db = erle_valid ? 10.0 * (std::log10(mic_energy) - std::log10(residual_energy)) : 0.0;
  const double peak_to_average = decode_energy(dsp_values_[11], 0);
  const bool ratio_valid = valid(11) && std::isfinite(peak_to_average) && peak_to_average >= 0.0;
  ESP_LOGI(TAG,
           "XMOS DSP AEC quality: ERLE_ch0=%s%.1f dB peak_to_average=%s%.2f "
           "delay_direction=%s%u adapt_config=%s%u",
           erle_valid ? "" : "?", erle_db,
           ratio_valid ? "" : "?", peak_to_average,
           valid(8) ? "" : "?", dsp_values_[8][0],
           valid(9) ? "" : "?", dsp_values_[9][0]);
}

}  // namespace esphome::xvf3610_audio
