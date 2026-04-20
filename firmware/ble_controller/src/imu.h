// imu.h — IMU abstraction. Today: MPU-6050 via Adafruit lib.
#pragma once

#include <Arduino.h>

namespace robot {

struct ImuSample {
  float ax, ay, az;  // g (gravity = ~1.0 at rest)
  float gx, gy, gz;  // deg/s
  float temp_c;
  bool  ok;
};

class IMU {
 public:
  // Open I2C on the given pins and initialize the sensor. Returns true on
  // success; subsequent read6()/readTempCelsius() calls will then supply
  // real data. On failure read6() returns a zeroed sample with ok=false.
  bool begin(uint8_t sda_pin, uint8_t scl_pin);

  ImuSample read6();
  int16_t   readTempCelsius();

  bool available() const { return ok_; }

 private:
  bool ok_ = false;
};

} // namespace robot
