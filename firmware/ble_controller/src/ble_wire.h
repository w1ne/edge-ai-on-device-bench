// ble_wire.h — NimBLE setup + NUS GATT chars + JSON wire protocol.
//
// NUS service/characteristic UUIDs are IDENTICAL to the legacy USB-CDC
// firmware so the companion app on the Pixel 6 (`dev.robot.companion`)
// and scripts/ble_smoke.py match without changes. Do not change them.
//
//   Service        6E400001-B5A3-F393-E0A9-E50E24DCCA9E  (Nordic UART)
//   RX  (write)    6E400002-...
//   TX  (notify)   6E400003-...
//   CFG (write/R)  6E400004-...
//
// Wire protocol (preserved byte-for-byte from the pre-refactor firmware):
//   RX: newline-delimited JSON
//     {"c":"ping"}
//     {"c":"pose","n":"<name>","d":<ms>}
//     {"c":"walk","on":true,"stride":150,"step":400}
//     {"c":"stop"} / {"c":"jump"}
//     {"cfg":{"tx":N,"rx":N,"dir":N,"sda":N,"scl":N,"vbat":N,"ids":[1,2,3,4]}}
//   TX: newline-delimited JSON
//     {"t":"state","p":[...],"v":<cV>,"tmp":<C>,"ms":<uptime>,"imu":[...]}
//     {"t":"ack","c":"<cmd>","ok":true|false}
//     {"t":"err","msg":"..."}
//     {"t":"info","msg":"..."}
#pragma once

#include <Arduino.h>
#include "servo_bus.h"
#include "imu.h"
#include "battery.h"
#include "gait.h"
#include "jump.h"
#include "robot_config.h"
#include "safety.h"

namespace robot {

// All the subsystems ble_wire needs to dispatch commands + emit state.
struct WireContext {
  ServoBus*           bus;
  IMU*                imu;
  Battery*            battery;
  Gait*               gait;
  Jump*               jump;
  Config*             cfg;
  ConnectionWatchdog* watchdog;
};

// Initialize NimBLE, register GATT, start advertising. Idempotent-ish:
// call once from setup() after ctx subsystems are already begun().
void bleBegin(const char* device_name, WireContext ctx);

// Is a BLE central currently connected? Used by loop() to decide whether
// to emit state packets (avoids wasting CPU when nobody is listening).
bool bleConnected();

// Push a {"t":"state",...} packet based on live bus/imu/battery state.
void bleEmitState(uint32_t now_ms);

// Push a {"t":"info","msg":...} notify (used by watchdog for timeout msg).
void bleNotifyInfo(const char* msg);

} // namespace robot
