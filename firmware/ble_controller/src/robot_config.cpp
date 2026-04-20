#include "robot_config.h"
#include <Preferences.h>

namespace robot {

// Pin defaults for Waveshare ESP32-S3 Zero + generic Feetech STS board.
// Verified against firmware_backup/original_20260420-104957.bin.
const Config kDefaultConfig = {
  /*servo_tx */ 17,
  /*servo_rx */ 18,
  /*servo_dir*/ 255,   // disabled; original firmware uses no DIR pin
  /*i2c_sda  */  8,
  /*i2c_scl  */  9,
  /*adc_vbat */  8,    // confirmed via battery_scan.cpp sweep on 2026-04-20
                       // (2:1 divider tap; see logs/hw_adc_scan_2026-04-20.log).
                       // NOTE: GPIO 8 is also listed as i2c_sda above. The
                       // ADC sweep showed GPIO 8 holds a real analog level
                       // (~3150 mV ≈ 6.3 V battery via 2:1) while GPIO 9 is
                       // saturated to raw=4095 (I2C SCL pull-up). I2C and
                       // VBAT likely aren't both on 8 in hardware — one of
                       // these defaults is inherited from the guess and
                       // should be re-verified separately.
  /*ids      */ {1, 2, 3, 4},
};

static constexpr const char* kNamespace = "bleconf";

void loadFromNVS(Config& cfg) {
  Preferences p;
  if (!p.begin(kNamespace, true)) return;
  cfg.servo_tx  = p.getUChar("tx",   cfg.servo_tx);
  cfg.servo_rx  = p.getUChar("rx",   cfg.servo_rx);
  cfg.servo_dir = p.getUChar("dir",  cfg.servo_dir);
  cfg.i2c_sda   = p.getUChar("sda",  cfg.i2c_sda);
  cfg.i2c_scl   = p.getUChar("scl",  cfg.i2c_scl);
  cfg.adc_vbat  = p.getUChar("vbat", cfg.adc_vbat);
  uint8_t ids_buf[N_SERVOS];
  for (uint8_t i = 0; i < N_SERVOS; ++i) ids_buf[i] = cfg.ids[i];
  p.getBytes("ids", ids_buf, N_SERVOS);
  for (uint8_t i = 0; i < N_SERVOS; ++i) cfg.ids[i] = ids_buf[i];
  p.end();
}

void saveToNVS(const Config& cfg) {
  Preferences p;
  if (!p.begin(kNamespace, false)) return;
  p.putUChar("tx",   cfg.servo_tx);
  p.putUChar("rx",   cfg.servo_rx);
  p.putUChar("dir",  cfg.servo_dir);
  p.putUChar("sda",  cfg.i2c_sda);
  p.putUChar("scl",  cfg.i2c_scl);
  p.putUChar("vbat", cfg.adc_vbat);
  p.putBytes("ids",  cfg.ids, N_SERVOS);
  p.end();
}

bool patch(Config& cfg, const CfgPatch& pp) {
  bool changed = false;
  if (pp.has_tx)  { cfg.servo_tx  = pp.tx;  changed = true; }
  if (pp.has_rx)  { cfg.servo_rx  = pp.rx;  changed = true; }
  if (pp.has_dir) { cfg.servo_dir = pp.dir; changed = true; }
  if (pp.has_sda) { cfg.i2c_sda   = pp.sda; changed = true; }
  if (pp.has_scl) { cfg.i2c_scl   = pp.scl; changed = true; }
  if (pp.has_vbat){ cfg.adc_vbat  = pp.vbat;changed = true; }
  if (pp.has_ids) {
    for (uint8_t i = 0; i < N_SERVOS; ++i) cfg.ids[i] = pp.ids[i];
    changed = true;
  }
  return changed;
}

} // namespace robot
