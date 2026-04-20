#include "battery.h"

namespace robot {

void Battery::configure(uint8_t adc_pin, uint8_t divider_ratio) {
  pin_ = adc_pin;
  ratio_ = divider_ratio == 0 ? 1 : divider_ratio;
}

int Battery::readMilliVolts() const {
  if (pin_ == 255) return 0;
  return (int)analogReadMilliVolts(pin_);
}

uint16_t Battery::readCentiVolts() const {
  if (pin_ == 255) return 0;
  uint32_t raw = analogReadMilliVolts(pin_);
  uint32_t mv_batt = raw * ratio_;
  return (uint16_t)(mv_batt / 100);
}

} // namespace robot
