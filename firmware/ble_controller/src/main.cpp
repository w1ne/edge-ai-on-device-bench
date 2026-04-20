// PhoneWalker BLE controller — top-level wiring only.
//
// Board:  Waveshare ESP32-S3 Zero
// Servos: 4x Feetech STS3032 on half-duplex UART bus (default IDs 1..4)
// IMU:    MPU-6050 on I2C
//
// This file intentionally stays thin. To port to a new robot:
//   1. edit src/robot_profile.{h,cpp}  (N_SERVOS, pose table, gait params)
//   2. edit src/gait.cpp              (if the locomotion pattern differs)
//   3. swap servo_bus driver          (#define a different SERVO_DRIVER_*)
//   4. build + flash + verify via scripts/ble_smoke.py
// See docs/FIRMWARE_PORTING.md for the full checklist.
//
// Wire protocol, NUS UUIDs, and BLE name are preserved byte-for-byte from
// the pre-refactor monolith; see ble_wire.cpp.

#include <Arduino.h>

#include "robot_config.h"
#include "robot_profile.h"
#include "servo_bus.h"
#include "imu.h"
#include "battery.h"
#include "gait.h"
#include "jump.h"
#include "poses.h"
#include "safety.h"
#include "ble_wire.h"
#include "led.h"

static constexpr uint32_t STATE_RATE_HZ  = 10;
static constexpr uint32_t CMD_TIMEOUT_MS = 10 * 1000;
static const     char*    BLE_NAME       = "PhoneWalker-BLE";

static robot::Config             g_cfg;
static robot::ServoBus           g_bus;
static robot::IMU                g_imu;
static robot::Battery            g_battery;
static robot::Gait               g_gait;
static robot::Jump               g_jump;
static robot::ConnectionWatchdog g_watchdog;
static robot::Led                g_led;
static constexpr uint16_t LOW_BATT_CENTIVOLTS = 620;  // 6.20 V cutoff

void setup() {
  Serial.begin(115200);
  delay(50);
  Serial.println("[ble_controller] boot");

  g_led.begin();
  g_led.setState(robot::LedState::BOOT);

  // 1. Config: defaults overlaid by anything persisted in NVS.
  g_cfg = robot::kDefaultConfig;
  robot::loadFromNVS(g_cfg);

  // 2. Hardware subsystems.
  g_bus.begin(g_cfg.servo_tx, g_cfg.servo_rx, g_cfg.servo_dir, robot::SERVO_BAUD);
#if DEBUG_SERVO_PROBE
  g_bus.probe(g_cfg.ids);
#endif
  g_imu.begin(g_cfg.i2c_sda, g_cfg.i2c_scl);
  // divider_ratio_x100 = 225 -> real divider ≈ 2.25:1, matched against the
  // stock firmware's `v=67` (6.7 V) state-packet sample with ADC pin reading
  // ~2981 mV on GPIO 8 (see logs/hw_adc_scan_2026-04-20.log).
  g_battery.configure(g_cfg.adc_vbat, /*divider_ratio_x100=*/225);
  Serial.printf("[battery] adc_pin=%u centiV=%u\n",
                (unsigned)g_cfg.adc_vbat,
                (unsigned)g_battery.readCentiVolts());

  // 3. Power-on pose (no autonomous motion after this).
  robot::applyNeutral(g_bus, g_cfg, /*log=*/true);

  // 4. BLE + wire protocol. Do this last so all dependencies are ready
  //    when the first command arrives.
  robot::WireContext ctx = {
    &g_bus, &g_imu, &g_battery, &g_gait, &g_jump, &g_cfg, &g_watchdog,
  };
  robot::bleBegin(BLE_NAME, ctx);
  g_led.setState(robot::LedState::IDLE);
}

void loop() {
  static uint32_t last_state_ms = 0;
  const uint32_t now = millis();

  // 10 Hz state packet stream while a central is connected.
  if (robot::bleConnected() && (now - last_state_ms) >= (1000 / STATE_RATE_HZ)) {
    last_state_ms = now;
    robot::bleEmitState(now);
  }

  // 10 s command timeout -> hold last pose (don't move autonomously).
  if (g_watchdog.checkTimeout(now, CMD_TIMEOUT_MS, g_gait)) {
    robot::bleNotifyInfo("cmd timeout; holding pose");
  }

  g_gait.tick(now, g_bus, g_cfg);

  // LED state follows the most-urgent condition first.
  const uint16_t cv = g_battery.readCentiVolts();
  robot::LedState desired;
  if (cv > 0 && cv < LOW_BATT_CENTIVOLTS)  desired = robot::LedState::LOW_BATT;
  else if (g_gait.walking())               desired = robot::LedState::WALKING;
  else if (robot::bleConnected())          desired = robot::LedState::CONNECTED;
  else                                     desired = robot::LedState::IDLE;
  g_led.setState(desired);
  g_led.tick(now);

  delay(2);
}
