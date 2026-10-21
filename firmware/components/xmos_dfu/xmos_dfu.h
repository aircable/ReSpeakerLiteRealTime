#pragma once

#include "esphome/components/i2c/i2c.h"
#include "esphome/core/automation.h"
#include "esphome/core/component.h"

#include <cstddef>
#include <cstdint>

namespace esphome::xmos_dfu {

enum class Phase : uint8_t {
  IDLE,
  DISCOVER,
  SEND_BLOCK,
  POLL_BLOCK,
  SEND_FINAL,
  POLL_FINAL,
  REBOOT,
  VERIFY,
  COMPLETE,
  ERROR,
};

class XmosDfu : public Component, public i2c::I2CDevice {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  bool can_proceed() override { return !auto_update_ || phase_ == Phase::COMPLETE; }
  float get_setup_priority() const override { return auto_update_ ? setup_priority::BUS - 50.0f : setup_priority::LATE; }

  void set_firmware(const uint8_t *data, uint32_t size, const char *name) {
    firmware_ = data;
    firmware_size_ = size;
    image_name_ = name;
  }
  void start_update();
  void set_auto_update(bool enabled) { auto_update_ = enabled; }
  void set_source_version(uint8_t major, uint8_t minor, uint8_t patch) {
    source_version_[0] = major; source_version_[1] = minor; source_version_[2] = patch;
  }
  void set_target_version(uint8_t major, uint8_t minor, uint8_t patch) {
    target_version_[0] = major; target_version_[1] = minor; target_version_[2] = patch;
  }
  bool is_updating() const { return phase_ != Phase::IDLE && phase_ != Phase::COMPLETE && phase_ != Phase::ERROR; }

 protected:
  bool read_at_(uint8_t address, uint8_t resource, uint8_t command, uint8_t *data, size_t size);
  bool write_at_(uint8_t address, const uint8_t *data, size_t size);
  bool read_seeed_version_(uint8_t *version);
  bool version_matches_(const uint8_t *actual, const uint8_t *expected);
  bool discover_(bool log_result);
  bool seeed_ready_();
  bool send_block_();
  bool send_final_();
  void fail_(const char *message);

  const uint8_t *firmware_{nullptr};
  uint32_t firmware_size_{0};
  const char *image_name_{nullptr};
  bool auto_update_{false};
  uint8_t source_version_[3]{};
  uint8_t target_version_[3]{};
  Phase phase_{Phase::IDLE};
  uint32_t offset_{0};
  uint32_t next_action_ms_{0};
  uint32_t started_ms_{0};
  uint32_t last_progress_ms_{0};
  uint32_t verify_started_ms_{0};
};

template<typename... Ts> class XmosDfuFlashAction : public Action<Ts...> {
 public:
  explicit XmosDfuFlashAction(XmosDfu *parent) : parent_(parent) {}
  void play(Ts... x) override { this->parent_->start_update(); }
 protected:
  XmosDfu *parent_;
};

}  // namespace esphome::xmos_dfu
