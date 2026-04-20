#include "imu.h"

#include <Wire.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

namespace robot {

static Adafruit_MPU6050 g_mpu;

bool IMU::begin(uint8_t sda_pin, uint8_t scl_pin) {
  Wire.begin(sda_pin, scl_pin);
  Wire.setClock(400000);
  ok_ = g_mpu.begin();
  if (ok_) {
    g_mpu.setAccelerometerRange(MPU6050_RANGE_4_G);
    g_mpu.setGyroRange(MPU6050_RANGE_500_DEG);
    g_mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
  }
  return ok_;
}

ImuSample IMU::read6() {
  ImuSample s = {};
  if (!ok_) return s;
  sensors_event_t a, g, t;
  if (!g_mpu.getEvent(&a, &g, &t)) return s;
  // Scale accel m/s^2 -> g so the daemon sees ~0.98 at rest (matches live fw).
  s.ax = a.acceleration.x / 9.80665f;
  s.ay = a.acceleration.y / 9.80665f;
  s.az = a.acceleration.z / 9.80665f;
  // Gyro rad/s -> deg/s.
  s.gx = g.gyro.x * 57.2957795f;
  s.gy = g.gyro.y * 57.2957795f;
  s.gz = g.gyro.z * 57.2957795f;
  s.temp_c = t.temperature;
  s.ok = true;
  return s;
}

int16_t IMU::readTempCelsius() {
  if (!ok_) return 0;
  sensors_event_t a, g, t;
  if (!g_mpu.getEvent(&a, &g, &t)) return 0;
  return (int16_t)t.temperature;
}

} // namespace robot
