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
  //
  // `divider_ratio_x100` is the real-world divider ratio * 100 so we can
  // encode non-integer ratios like 2.25 (pass 225). For a plain 2:1
  // divider, pass 200. Defaults to 225 to match the observed behavior of
  // the Waveshare ESP32-S3 Zero board: at the ADC pin the measured reading
  // (~2981 mV) times 2.25 yields 6.7 V which matches the stock firmware's
  // `v=67` state packet (centivolts).
  void configure(uint8_t adc_pin, uint16_t divider_ratio_x100 = 225);

  // Raw mV at the ADC (pre-divider compensation).
  int readMilliVolts() const;

  // Centi-volts of the battery after multiplying by divider ratio.
  // Matches the legacy `v` field in state packets (e.g. 72 = 7.2 V).
  uint16_t readCentiVolts() const;

 private:
  uint8_t  pin_           = 255;
  uint16_t ratio_x100_    = 225;
};

} // namespace robot
