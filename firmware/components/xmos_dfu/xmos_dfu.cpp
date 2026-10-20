#include "xmos_dfu.h"

#include "esphome/core/application.h"
#include "esphome/core/log.h"
#include "esphome/core/hal.h"

#include <algorithm>
#include <cstring>

#ifdef USE_RESPEAKER_LITE
// formatBCE's component exposes no runtime switch for its tight-loop 0xF1 mute poll. Its
// translation unit uses this flag to gate that poll, so turn it off for non-Seeed images.
namespace esphome::respeaker_lite {
extern bool initialized;
}
#endif

namespace esphome::xmos_dfu {

static const char *const TAG = "xmos_dfu";
static constexpr uint8_t SEEED_ADDRESS = 0x42;
static constexpr uint8_t SEEED_RESOURCE = 0xF0;
static constexpr uint8_t XVF_ADDRESS = 0x2C;
static constexpr uint8_t XVF_RESOURCE = 0xD0;
static constexpr size_t BLOCK_SIZE = 128;

static uint32_t load_u32_le(const uint8_t *p) {
  return uint32_t(p[0]) | (uint32_t(p[1]) << 8) | (uint32_t(p[2]) << 16) | (uint32_t(p[3]) << 24);
}

void XmosDfu::setup() {
  if (this->auto_update_) {
    // ESPHome waits for can_proceed() before setting up audio and gateway
    // components. The I2C bus has already been initialized at BUS priority.
    this->phase_ = Phase::DISCOVER;
    this->started_ms_ = millis();
    this->next_action_ms_ = millis() + 3000;
    ESP_LOGI(TAG, "Checking XMOS firmware before enabling ESP32 audio pins");
    return;
  }
#ifdef USE_RESPEAKER_LITE
  // The legacy component performs its version check after three seconds. Inspect the resulting
  // application just afterwards and stop its Seeed-only mute polling when another image is active.
  this->set_timeout(3200, [this]() { this->disable_legacy_polling_if_incompatible_(); });
#else
  this->set_timeout(5000, [this]() { this->discover_(true); });
#endif
}

void XmosDfu::disable_legacy_polling_if_incompatible_() {
#ifdef USE_RESPEAKER_LITE
  uint8_t version[4]{};
  if (!this->read_at_(SEEED_ADDRESS, SEEED_RESOURCE, 0xD8, version, sizeof(version)) ||
      version[0] != 0)
    return;
  if (version[1] == 1 && version[2] == 1 && version[3] == 0)
    return;
  esphome::respeaker_lite::initialized = false;
  ESP_LOGW(TAG,
           "XMOS version %u.%u.%u is not Seeed 1.1.0; disabled legacy 0xF1 mute polling. "
           "Physical mute reporting is unavailable during this experiment.",
           version[1], version[2], version[3]);
#endif
}

void XmosDfu::dump_config() {
  ESP_LOGCONFIG(TAG, "XMOS dual-protocol I2C updater:");
  ESP_LOGCONFIG(TAG, "  Embedded image: %s (%u bytes)", this->image_name_, this->firmware_size_);
  ESP_LOGCONFIG(TAG, "  Discovery: Seeed 0x42/F0 and XVF3610 0x2C/D0");
  if (this->auto_update_)
    ESP_LOGCONFIG(TAG, "  Auto-upgrade: %u.%u.%u -> %u.%u.%u (stock Seeed 0x42/F0 only)",
                  source_version_[0], source_version_[1], source_version_[2],
                  target_version_[0], target_version_[1], target_version_[2]);
}

bool XmosDfu::read_at_(uint8_t address, uint8_t resource, uint8_t command, uint8_t *data, size_t size) {
  if (size == 0 || size > 255) return false;
  const uint8_t request[] = {resource, command, static_cast<uint8_t>(size)};
  this->set_i2c_address(address);
  return this->write_read(request, sizeof(request), data, size) == i2c::ERROR_OK;
}

bool XmosDfu::write_at_(uint8_t address, const uint8_t *data, size_t size) {
  this->set_i2c_address(address);
  return this->write(data, size) == i2c::ERROR_OK;
}

bool XmosDfu::read_seeed_version_(uint8_t *version) {
  uint8_t reply[4]{};
  if (!this->read_at_(SEEED_ADDRESS, SEEED_RESOURCE, 0xD8, reply, sizeof(reply)) || reply[0] != 0)
    return false;
  std::memcpy(version, reply + 1, 3);
  return true;
}

bool XmosDfu::version_matches_(const uint8_t *actual, const uint8_t *expected) {
  return std::memcmp(actual, expected, 3) == 0;
}

Protocol XmosDfu::discover_(bool log_result) {
  const uint8_t addresses[] = {SEEED_ADDRESS, XVF_ADDRESS};
  for (const uint8_t address : addresses) {
    uint8_t version[4]{};
    if (this->read_at_(address, SEEED_RESOURCE, 0xD8, version, sizeof(version)) && version[0] == 0) {
      this->protocol_address_ = address;
      if (log_result)
        ESP_LOGI(TAG, "DFU available: Seeed protocol address=0x%02X resource=0xF0 version=%u.%u.%u",
                 address, version[1], version[2], version[3]);
      return Protocol::SEEED;
    }
  }
  // Prefer the XMOS default, but also accept a data partition that retains Seeed's 0x42 address.
  const uint8_t xvf_addresses[] = {XVF_ADDRESS, SEEED_ADDRESS};
  for (const uint8_t address : xvf_addresses) {
    uint8_t control_version[1]{};
    uint8_t state_raw[4]{};
    if (this->read_at_(address, 0x00, 0x80, control_version, sizeof(control_version)) &&
        this->read_at_(address, XVF_RESOURCE, 0x86, state_raw, sizeof(state_raw))) {
      const uint32_t state = load_u32_le(state_raw);
      if (control_version[0] == 0x10 && state <= 10) {
        this->protocol_address_ = address;
        if (log_result)
          ESP_LOGI(TAG, "DFU available: XVF protocol address=0x%02X resource=0xD0 control=%u state=%u",
                   address, control_version[0], state);
        return Protocol::XVF;
      }
    }
  }
  if (log_result) ESP_LOGE(TAG, "No supported XMOS I2C DFU service found at 0x42 or 0x2C");
  return Protocol::NONE;
}

void XmosDfu::start_update() {
  if (this->is_updating() && this->phase_ != Phase::DISCOVER) {
    ESP_LOGW(TAG, "XMOS update already in progress");
    return;
  }
  if (this->firmware_ == nullptr || this->firmware_size_ == 0) {
    this->fail_("No embedded XMOS image");
    return;
  }
  this->protocol_ = this->discover_(true);
  if (this->protocol_ == Protocol::NONE) {
    this->fail_("No supported XMOS DFU service; upgrade not started");
    return;
  }
  if (this->auto_update_ && (this->protocol_ != Protocol::SEEED || this->protocol_address_ != SEEED_ADDRESS)) {
    this->fail_("Automatic stock upgrade requires Seeed DFU at 0x42/F0");
    return;
  }
  this->offset_ = 0;
  this->block_number_ = 0;
  this->started_ms_ = millis();
  this->last_progress_ms_ = 0;
  ESP_LOGW(TAG, "Beginning XMOS update with %s (%u bytes); do not remove power",
           this->image_name_, this->firmware_size_);

  if (this->protocol_ == Protocol::SEEED) {
    const uint8_t select_upgrade[] = {SEEED_RESOURCE, 0x40, 1, 1};
    if (!this->write_at_(this->protocol_address_, select_upgrade, sizeof(select_upgrade))) {
      this->fail_("Seeed SETALTERNATE failed");
      return;
    }
    this->phase_ = Phase::SEND_BLOCK;
  } else {
    this->phase_ = Phase::XVF_DETACH;
  }
  this->next_action_ms_ = millis();
}

bool XmosDfu::seeed_ready_() {
  uint8_t response[6]{};
  if (!this->read_at_(this->protocol_address_, SEEED_RESOURCE, 0x83, response, sizeof(response)) || response[0] != 0)
    return false;
  const uint8_t status = response[1];
  const uint8_t state = response[5];
  const uint32_t delay_ms = uint32_t(response[2]) | (uint32_t(response[3]) << 8) | (uint32_t(response[4]) << 16);
  this->next_action_ms_ = millis() + delay_ms;
  if (status != 0) {
    ESP_LOGE(TAG, "Seeed DFU error status=%u state=%u", status, state);
    return false;
  }
  return state == 2 || state == 5 || state == 8;
}

bool XmosDfu::xvf_status_(uint32_t &state, uint32_t &status, uint32_t &timeout_ms) {
  uint8_t response[12]{};
  if (!this->read_at_(this->protocol_address_, XVF_RESOURCE, 0x87, response, sizeof(response))) return false;
  status = load_u32_le(response);
  state = load_u32_le(response + 4);
  timeout_ms = load_u32_le(response + 8);
  this->next_action_ms_ = millis() + timeout_ms;
  return status == 0 && state <= 10;
}

bool XmosDfu::send_block_() {
  const size_t count = std::min<size_t>(BLOCK_SIZE, this->firmware_size_ - this->offset_);
  if (this->protocol_ == Protocol::SEEED) {
    uint8_t request[BLOCK_SIZE + 5] = {SEEED_RESOURCE, 0x01, 0x82, 0, 0};
    request[3] = static_cast<uint8_t>(count);
    std::memcpy(request + 5, this->firmware_ + this->offset_, count);
    if (!this->write_at_(this->protocol_address_, request, sizeof(request))) return false;
  } else {
    uint8_t request[3 + 4 + BLOCK_SIZE]{};
    request[0] = XVF_RESOURCE;
    request[1] = 0x03;
    request[2] = static_cast<uint8_t>(count + 4);
    request[3] = static_cast<uint8_t>(this->block_number_);
    request[4] = static_cast<uint8_t>(this->block_number_ >> 8);
    std::memcpy(request + 7, this->firmware_ + this->offset_, count);
    if (!this->write_at_(this->protocol_address_, request, count + 7)) return false;
  }
  this->offset_ += count;
  this->block_number_++;
  return true;
}

bool XmosDfu::send_final_() {
  if (this->protocol_ == Protocol::SEEED) {
    uint8_t request[BLOCK_SIZE + 5] = {SEEED_RESOURCE, 0x01, 0x82, 0, 0};
    return this->write_at_(this->protocol_address_, request, sizeof(request));
  }
  const uint8_t request[] = {XVF_RESOURCE, 0x03, 4, 0, 0, 0, 0};
  return this->write_at_(this->protocol_address_, request, sizeof(request));
}

void XmosDfu::fail_(const char *message) {
  ESP_LOGE(TAG, "%s", message);
  this->phase_ = Phase::ERROR;
  if (this->auto_update_)
    this->mark_failed();
}

void XmosDfu::loop() {
  if (!this->is_updating() || static_cast<int32_t>(millis() - this->next_action_ms_) < 0) return;
  if (millis() - this->started_ms_ > 10 * 60 * 1000UL) {
    this->fail_("XMOS update timed out");
    return;
  }

  switch (this->phase_) {
    case Phase::DISCOVER: {
      uint8_t version[3]{};
      if (!this->read_seeed_version_(version)) {
        if (millis() - this->started_ms_ > 15000)
          this->fail_("XMOS did not expose Seeed-compatible DFU at 0x42; voice startup blocked");
        else
          this->next_action_ms_ = millis() + 500;
        break;
      }
      ESP_LOGI(TAG, "XMOS firmware reported %u.%u.%u", version[0], version[1], version[2]);
      if (this->version_matches_(version, this->target_version_)) {
        ESP_LOGI(TAG, "Expected FFVA image already active; XMOS write skipped");
        this->phase_ = Phase::COMPLETE;
      } else if (this->version_matches_(version, this->source_version_)) {
        ESP_LOGW(TAG, "Stock XMOS version matched; beginning one-time FFVA upgrade");
        this->start_update();
      } else {
        ESP_LOGE(TAG, "Unsupported XMOS version %u.%u.%u; expected stock %u.%u.%u or FFVA %u.%u.%u",
                 version[0], version[1], version[2],
                 source_version_[0], source_version_[1], source_version_[2],
                 target_version_[0], target_version_[1], target_version_[2]);
        this->fail_("Automatic XMOS upgrade refused; voice startup blocked");
      }
      break;
    }
    case Phase::XVF_DETACH: {
      uint8_t state_raw[4]{};
      if (!this->read_at_(this->protocol_address_, XVF_RESOURCE, 0x86, state_raw, sizeof(state_raw)) ||
          load_u32_le(state_raw) != 0) { this->fail_("XVF is not in APP_IDLE"); break; }
      const uint8_t detach[] = {XVF_RESOURCE, 0x01, 0};
      if (!this->write_at_(this->protocol_address_, detach, sizeof(detach))) { this->fail_("XVF DETACH failed"); break; }
      this->phase_ = Phase::XVF_BUS_RESET;
      this->next_action_ms_ = millis() + 10;
      break;
    }
    case Phase::XVF_BUS_RESET: {
      uint8_t state_raw[4]{};
      if (!this->read_at_(this->protocol_address_, XVF_RESOURCE, 0x86, state_raw, sizeof(state_raw)) ||
          load_u32_le(state_raw) != 1) { this->fail_("XVF did not enter APP_DETACH"); break; }
      const uint8_t reset[] = {XVF_RESOURCE, 0x02, 0};
      if (!this->write_at_(this->protocol_address_, reset, sizeof(reset))) { this->fail_("XVF BUS_RESET failed"); break; }
      this->phase_ = Phase::SEND_BLOCK;
      this->next_action_ms_ = millis() + 10;
      break;
    }
    case Phase::SEND_BLOCK:
      if (this->offset_ >= this->firmware_size_) { this->phase_ = Phase::SEND_FINAL; break; }
      if (this->protocol_ == Protocol::SEEED && !this->seeed_ready_()) { this->next_action_ms_ = millis() + 5; break; }
      if (!this->send_block_()) { this->fail_("XMOS DNLOAD write failed"); break; }
      this->phase_ = Phase::POLL_BLOCK;
      this->next_action_ms_ = millis() + 1;
      break;
    case Phase::POLL_BLOCK: {
      bool ready = false;
      if (this->protocol_ == Protocol::SEEED) ready = this->seeed_ready_();
      else {
        uint32_t state, status, timeout_ms;
        if (!this->xvf_status_(state, status, timeout_ms)) { this->fail_("XVF GETSTATUS failed"); break; }
        ready = state == 5;
      }
      if (!ready) break;
      if (millis() - this->last_progress_ms_ > 1000 || this->offset_ == this->firmware_size_) {
        this->last_progress_ms_ = millis();
        ESP_LOGI(TAG, "XMOS update progress %.1f%%", 100.0f * this->offset_ / this->firmware_size_);
      }
      this->phase_ = this->offset_ < this->firmware_size_ ? Phase::SEND_BLOCK : Phase::SEND_FINAL;
      break;
    }
    case Phase::SEND_FINAL:
      if (!this->send_final_()) { this->fail_("Final XMOS DNLOAD failed"); break; }
      this->phase_ = Phase::POLL_FINAL;
      this->next_action_ms_ = millis() + 5;
      break;
    case Phase::POLL_FINAL: {
      bool ready = false;
      if (this->protocol_ == Protocol::SEEED) ready = this->seeed_ready_();
      else {
        uint32_t state, status, timeout_ms;
        if (!this->xvf_status_(state, status, timeout_ms)) { this->fail_("XVF final GETSTATUS failed"); break; }
        ready = state == 2;
      }
      if (ready) this->phase_ = Phase::REBOOT;
      break;
    }
    case Phase::REBOOT: {
      const uint8_t seeed_reboot[] = {SEEED_RESOURCE, 0x59, 1, 0};
      const uint8_t xvf_reboot[] = {XVF_RESOURCE, 0x05, 0};
      const bool ok = this->protocol_ == Protocol::SEEED
                          ? this->write_at_(this->protocol_address_, seeed_reboot, sizeof(seeed_reboot))
                          : this->write_at_(this->protocol_address_, xvf_reboot, sizeof(xvf_reboot));
      if (!ok) { this->fail_("XMOS reboot command failed"); break; }
      ESP_LOGI(TAG, "XMOS image written; waiting for reboot and post-flash DFU discovery");
      this->phase_ = Phase::VERIFY;
      this->verify_started_ms_ = millis();
      this->next_action_ms_ = millis() + 3000;
      break;
    }
    case Phase::VERIFY: {
      if (this->auto_update_) {
        uint8_t version[3]{};
        if (!this->read_seeed_version_(version)) {
          if (millis() - this->verify_started_ms_ > 20000)
            this->fail_("XMOS did not report its version after upgrade; voice startup blocked");
          else
            this->next_action_ms_ = millis() + 500;
          break;
        }
        if (!this->version_matches_(version, this->target_version_)) {
          ESP_LOGE(TAG, "XMOS booted version %u.%u.%u, not expected FFVA %u.%u.%u",
                   version[0], version[1], version[2],
                   target_version_[0], target_version_[1], target_version_[2]);
          this->fail_("XMOS post-flash version mismatch; voice startup blocked");
          break;
        }
      } else if (this->discover_(true) == Protocol::NONE) {
        this->fail_("Image write completed, but no supported DFU service appeared after reboot");
        break;
      }
      ESP_LOGI(TAG, "XMOS update complete in %.1f seconds", (millis() - this->started_ms_) / 1000.0f);
      this->disable_legacy_polling_if_incompatible_();
      this->phase_ = Phase::COMPLETE;
      if (this->auto_update_) {
        ESP_LOGI(TAG, "Rebooting ESP32 to initialize I2S and codec against the new XMOS image");
        App.reboot();
      }
      break;
    }
    default:
      break;
  }
}

}  // namespace esphome::xmos_dfu
