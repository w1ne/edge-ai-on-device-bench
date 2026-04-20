// battery.h — simple ADC-based voltage sense.
//
// The VBAT ADC pin on this board is still not confirmed (see main.cpp
// comments in the original firmware). Until it is, the default reading is
// likely junk. configure(pin=255) to disable entirely; callers should treat
// a value of 0 as "unknown".
#pragma once

#include <Arduino.h>

namespace robot {

class Battery {
 public:
  // Set the ADC pin. 255 = disabled (reads return 0).
  void configure(uint8_t adc_pin, uint8_t divider_ratio = 2);

  // Raw mV at the ADC (pre-divider compensation).
  int readMilliVolts() const;

  // Centi-volts of the battery after multiplying by divider ratio.
  // Matches the legacy `v` field in state packets (e.g. 72 = 7.2 V).
  uint16_t readCentiVolts() const;

 private:
  uint8_t pin_ = 255;
  uint8_t ratio_ = 2;
};

} // namespace robot
