#pragma once

#include "esphome/components/i2c/i2c.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/core/component.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace esphome::xvf3610_diag {

class Xvf3610Diag : public PollingComponent, public i2c::I2CDevice {
 public:
  void set_microphone(microphone::Microphone *microphone) { microphone_ = microphone; }
  void setup() override;
  void update() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::LATE; }

 protected:
  bool read_command_(uint8_t resource, uint8_t command, uint8_t *value, size_t value_size);
  bool read_u8_(uint8_t resource, uint8_t command, uint8_t &value);
  bool read_u32_(uint8_t resource, uint8_t command, uint32_t &value);
  void report_control_();
  void on_audio_(const std::vector<uint8_t> &data);

  microphone::Microphone *microphone_{nullptr};
  std::atomic<uint32_t> sample_frames_{0};
  std::atomic<uint32_t> callbacks_{0};
  std::atomic<uint32_t> empty_callbacks_{0};
  std::atomic<uint32_t> peak_{0};
  uint32_t last_control_ms_{0};
};

}  // namespace esphome::xvf3610_diag
