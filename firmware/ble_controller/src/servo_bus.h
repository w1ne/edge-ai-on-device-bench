// servo_bus.h — abstraction over the servo driver IC.
//
// Today: Feetech SMS_STS (half-duplex, 1 Mbit). Tomorrow: Dynamixel or PWM.
// Select the implementation via preprocessor (SERVO_DRIVER_FEETECH_STS is
// the default). Only one .cpp compiles at a time; the header is shared.
#pragma once

#include <Arduino.h>
#include "robot_profile.h"

namespace robot {

struct ServoRead {
  int16_t pos;
  bool    ok;
};

class ServoBus {
 public:
  // Open the underlying UART/serial bus and reset internal state. `dir_pin`
  // = 255 means no DE/RE direction pin.
  void begin(uint8_t tx_pin, uint8_t rx_pin, uint8_t dir_pin, uint32_t baud);

  // Send a position command. `speed` in driver-specific steps/s; `acc` in
  // driver-specific accel units. Returns the driver's last-error code
  // (0 == success on Feetech).
  int writePosition(uint8_t id, int16_t pos, uint16_t speed, uint8_t acc);

  // Read position of `id`. If the bus read fails, ok=false and pos is the
  // last *commanded* position for that ID (or 0 if none yet).
  ServoRead readPosition(uint8_t id);

  // Per-ID helpers to maintain "last known" position without a bus read.
  void rememberPosition(uint8_t id, int16_t pos);
  int16_t lastPosition(uint8_t id) const;

  // Drive-by-ID convenience: iterate `cfg_ids[]` (length N_SERVOS).
  void stopAll(const uint8_t* cfg_ids);

  // Boot-time probe: read each configured ID and log the result to Serial.
  // Emits lines matching the regex `\[servo\] probe id=\d+ ReadPos=.* err=\d+`,
  // which the smoke test + operator use to verify the bus is alive.
  void probe(const uint8_t* cfg_ids);

 private:
  int16_t last_pos_[16] = {0};  // keyed by ID, supports IDs 0..15
};

} // namespace robot
