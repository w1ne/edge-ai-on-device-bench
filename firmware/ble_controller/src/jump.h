// jump.h — two-phase "crouch then extend" maneuver.
//
// Blocking for the duration of the sequence (~2 * phase_ms). Parameters
// come from robot_profile.h::kJump.
#pragma once

#include <Arduino.h>
#include "servo_bus.h"
#include "robot_config.h"

namespace robot {

class Jump {
 public:
  void trigger(ServoBus& bus, const Config& cfg);
};

} // namespace robot
