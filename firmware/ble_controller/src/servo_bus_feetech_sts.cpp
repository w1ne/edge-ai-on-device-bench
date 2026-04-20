// servo_bus_feetech_sts.cpp — Feetech SMS_STS implementation of ServoBus.
//
// Compiled when SERVO_DRIVER_FEETECH_STS is defined (default in
// platformio.ini). To port to a different driver, add another _*.cpp with
// its own #ifdef and flip the build flag.
#include "servo_bus.h"

#ifndef SERVO_DRIVER_FEETECH_STS
#define SERVO_DRIVER_FEETECH_STS 1
#endif

#if SERVO_DRIVER_FEETECH_STS

#include <SCServo.h>

namespace robot {

static SMS_STS g_sms;

void ServoBus::begin(uint8_t tx_pin, uint8_t rx_pin, uint8_t dir_pin, uint32_t baud) {
  // Feetech library needs the HardwareSerial started before use.
  Serial1.begin(baud, SERIAL_8N1, rx_pin, tx_pin);
  g_sms.pSerial = &Serial1;
  if (dir_pin != 255) {
    pinMode(dir_pin, OUTPUT);
    digitalWrite(dir_pin, HIGH);
  }
  delay(20);
  Serial.printf("[servo] Serial1 up: baud=%u rx=%u tx=%u dir=%u\n",
                baud, rx_pin, tx_pin, dir_pin);
  for (int i = 0; i < 16; ++i) last_pos_[i] = NEUTRAL_POS;
}

int ServoBus::writePosition(uint8_t id, int16_t pos, uint16_t speed, uint8_t acc) {
  g_sms.WritePosEx(id, pos, speed, acc);
  rememberPosition(id, pos);
  return g_sms.getLastError();
}

ServoRead ServoBus::readPosition(uint8_t id) {
  int raw = g_sms.ReadPos(id);
  if (raw >= 0) {
    last_pos_[id & 0x0F] = (int16_t)raw;
    return { (int16_t)raw, true };
  }
  return { last_pos_[id & 0x0F], false };
}

void ServoBus::rememberPosition(uint8_t id, int16_t pos) {
  last_pos_[id & 0x0F] = pos;
}

int16_t ServoBus::lastPosition(uint8_t id) const {
  return last_pos_[id & 0x0F];
}

void ServoBus::stopAll(const uint8_t* cfg_ids) {
  // Writing the current position with speed=0 latches the servo in place.
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    uint8_t id = cfg_ids[i];
    g_sms.WritePosEx(id, last_pos_[id & 0x0F], 0, 50);
  }
}

void ServoBus::probe(const uint8_t* cfg_ids) {
  // Emits one line per servo at boot. Format preserved from pre-refactor
  // main.cpp so the operator's grep still matches.
  Serial.printf("[servo] ids=[%u,%u,%u,%u]\n",
                cfg_ids[0], cfg_ids[1], cfg_ids[2], cfg_ids[3]);
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    int p = g_sms.ReadPos(cfg_ids[i]);
    int e = g_sms.getLastError();
    Serial.printf("[servo] probe id=%u ReadPos=%d err=%d\n", cfg_ids[i], p, e);
    if (p >= 0) last_pos_[cfg_ids[i] & 0x0F] = (int16_t)p;
  }
}

} // namespace robot

#endif // SERVO_DRIVER_FEETECH_STS
