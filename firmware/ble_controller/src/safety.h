// safety.h — fail-safes: disconnect watchdog + command-timeout hold.
#pragma once

#include <Arduino.h>
#include "servo_bus.h"
#include "gait.h"
#include "robot_config.h"

namespace robot {

class ConnectionWatchdog {
 public:
  // Wake a fresh watchdog (call on connect).
  void arm(uint32_t now_ms);

  // Record that a command just arrived.
  void touch(uint32_t now_ms);

  // If connected and we haven't heard a command in >timeout_ms, stop gait.
  // Returns true exactly once on the transition to timed-out.
  bool checkTimeout(uint32_t now_ms, uint32_t timeout_ms, Gait& gait);

  // Hard stop everything (call on BLE disconnect).
  void trip(Gait& gait, ServoBus& bus, const Config& cfg);

 private:
  uint32_t last_cmd_ms_ = 0;
  bool     timed_out_   = false;
};

} // namespace robot
