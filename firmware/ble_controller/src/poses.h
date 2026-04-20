// poses.h — look up a named pose in the profile and drive the bus.
#pragma once

#include <Arduino.h>
#include "servo_bus.h"
#include "robot_config.h"

namespace robot {

// Apply the named pose. `dur_ms` controls the servo speed (shorter = faster).
// Returns true if the pose name was found, false otherwise.
bool applyPose(const char* name, uint16_t dur_ms,
               ServoBus& bus, const Config& cfg);

// Drive all servos to NEUTRAL_POS using the default speed. Logs per-servo
// WritePosEx errors at boot so the operator can diagnose bus failures.
void applyNeutral(ServoBus& bus, const Config& cfg, bool log = true);

} // namespace robot
