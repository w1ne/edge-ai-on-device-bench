// gait.h — alternating-trot state machine (placeholder for real tuning).
//
// Tune on hardware: the stride/step_ms parameters come from the BLE wire
// protocol and default to robot_profile.h::kDefaultGait. The phase topology
// (which servos belong to diagonal A vs B) comes from kPhaseA/kPhaseB.
#pragma once

#include <Arduino.h>
#include "servo_bus.h"
#include "robot_config.h"

namespace robot {

class Gait {
 public:
  // Begin walking with the given stride (servo steps from neutral) and
  // step period (ms per half-cycle).
  void start(uint16_t stride, uint16_t step_ms);
  void stop();
  bool walking() const { return walking_; }

  // Advance the gait if it's time. No-op if not walking or if the period
  // hasn't elapsed since the last tick. Call once per main-loop iteration.
  void tick(uint32_t now_ms, ServoBus& bus, const Config& cfg);

  uint16_t stride() const { return stride_; }
  uint16_t stepMs() const { return step_ms_; }

 private:
  bool     walking_      = false;
  uint16_t stride_       = 0;
  uint16_t step_ms_      = 0;
  uint32_t last_step_ms_ = 0;
  uint8_t  phase_        = 0;
};

} // namespace robot
