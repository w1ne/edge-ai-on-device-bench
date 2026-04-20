// robot_config.h — NVS-backed pin/ID configuration.
//
// This is *runtime* configuration that can be patched over BLE (see ble_wire)
// and survives reboots via NVS. It is distinct from robot_profile.h, which is
// *compile-time* per-robot constants (servo count, pose table, gait params).
#pragma once

#include <Arduino.h>
#include "robot_profile.h"

namespace robot {

struct Config {
  uint8_t servo_tx;
  uint8_t servo_rx;
  uint8_t servo_dir;   // 255 = none
  uint8_t i2c_sda;
  uint8_t i2c_scl;
  uint8_t adc_vbat;    // 255 = none
  uint8_t ids[N_SERVOS];
};

// Default values used before NVS is consulted. Verified against the stock
// firmware backup (2026-04-20). See main.cpp comments in the old monolithic
// source for how these were recovered.
extern const Config kDefaultConfig;

// Load persisted values from NVS (key="bleconf"). Fields missing in NVS keep
// their current in-struct values, so call this after copying kDefaultConfig in.
void loadFromNVS(Config& cfg);

// Persist the full struct to NVS.
void saveToNVS(const Config& cfg);

// Apply a partial JSON-derived patch. Missing keys are left unchanged.
// Accepts the same {"tx":N,"rx":N,...,"ids":[...]} shape seen on the wire.
// Returns true if any field was modified.
struct CfgPatch {
  bool has_tx  = false; uint8_t tx  = 0;
  bool has_rx  = false; uint8_t rx  = 0;
  bool has_dir = false; uint8_t dir = 0;
  bool has_sda = false; uint8_t sda = 0;
  bool has_scl = false; uint8_t scl = 0;
  bool has_vbat= false; uint8_t vbat= 0;
  bool has_ids = false; uint8_t ids[N_SERVOS] = {0};
};
bool patch(Config& cfg, const CfgPatch& p);

} // namespace robot
