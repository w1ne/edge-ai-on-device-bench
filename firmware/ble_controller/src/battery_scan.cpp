// battery_scan.cpp — diagnostic ADC sweep.
//
// Only compiled when BATTERY_SCAN_DIAG=1 is defined. Enabled via the
// `esp32s3_zero_batscan` PIO environment. At boot, prints each ADC1 channel's
// raw count, mv, and estimated battery voltage (assuming a 2:1 divider) for
// ~5 seconds over USB CDC, then spins forever so we don't start BLE or touch
// the servo bus while diagnosing.
//
// Purpose: identify which GPIO the original firmware used for VBAT sense.
// Pins 8,9 (I2C), 17,18 (servo UART), 19,20 (USB CDC) are already assigned
// by the stock firmware, but we read them anyway for completeness so the
// operator can rule them out from the log.
#ifdef BATTERY_SCAN_DIAG

#include <Arduino.h>

namespace {

// ADC1 pins on ESP32-S3: GPIO 1..10.
constexpr uint8_t kAdc1Pins[] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};

void scanOnce() {
  for (uint8_t pin : kAdc1Pins) {
    int raw = analogRead(pin);
    uint32_t mv = analogReadMilliVolts(pin);
    // 2:1 divider -> battery voltage = mv * 2 / 1000 (volts).
    float est_bat_v = (float)mv * 2.0f / 1000.0f;
    Serial.printf("[adc_scan] GPIO=%-2u  raw=%-5d  mv=%-4u  est_bat_v=%.3f\n",
                  (unsigned)pin, raw, (unsigned)mv, est_bat_v);
  }
  Serial.println("[adc_scan] ---");
}

} // namespace

void setup() {
  Serial.begin(115200);
  // Give USB-CDC some settle time so the first line is not dropped.
  delay(1500);
  Serial.println("[adc_scan] boot: BATTERY_SCAN_DIAG=1");
  Serial.println("[adc_scan] scanning ADC1 (GPIO 1..10) for ~10 seconds");

  analogReadResolution(12);
  // ADC_11db is the Arduino-ESP32 default; explicit for clarity.
  // Max attenuation so we can measure up to ~3.1V at the pin.
#if defined(ADC_11db)
  for (uint8_t pin : kAdc1Pins) {
    analogSetPinAttenuation(pin, ADC_11db);
  }
#endif

  const uint32_t kStartMs = millis();
  while (millis() - kStartMs < 10000) {
    scanOnce();
    delay(500);
  }
  Serial.println("[adc_scan] done; halting (no BLE, no servos). Reflash normal firmware next.");
}

void loop() {
  // Halt so nothing else runs in this diagnostic build.
  delay(1000);
}

#endif  // BATTERY_SCAN_DIAG
