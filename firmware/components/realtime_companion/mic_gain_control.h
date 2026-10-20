#pragma once

namespace esphome::realtime_companion {

// Optional board-specific microphone gain control. The transport does not
// depend on the XVF3610 component when used with the Seeed XMOS image.
class MicGainControl {
 public:
  virtual ~MicGainControl() = default;
  virtual void request_ch0_gain(float gain) = 0;  // 0 restores adaptive AGC.
};

}  // namespace esphome::realtime_companion
