#include "battery.h"

namespace robot {

void Battery::configure(uint8_t adc_pin, uint16_t divider_ratio_x100) {
  pin_ = adc_pin;
  ratio_x100_ = divider_ratio_x100 == 0 ? 100 : divider_ratio_x100;
}

int Battery::readMilliVolts() const {
  if (pin_ == 255) return 0;
  return (int)analogReadMilliVolts(pin_);
}

uint16_t Battery::readCentiVolts() const {
  if (pin_ == 255) return 0;
  uint32_t mv_adc  = analogReadMilliVolts(pin_);
  // mv_batt = mv_adc * (ratio_x100 / 100); centiV = mv_batt / 100.
  //         = mv_adc * ratio_x100 / 10000
  uint32_t centiV  = (mv_adc * (uint32_t)ratio_x100_) / 10000u;
  return (uint16_t)centiV;
}

} // namespace robot
