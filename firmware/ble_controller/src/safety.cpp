#include "safety.h"

namespace robot {

void ConnectionWatchdog::arm(uint32_t now_ms) {
  last_cmd_ms_ = now_ms;
  timed_out_   = false;
}

void ConnectionWatchdog::touch(uint32_t now_ms) {
  last_cmd_ms_ = now_ms;
  timed_out_   = false;
}

bool ConnectionWatchdog::checkTimeout(uint32_t now_ms, uint32_t timeout_ms, Gait& gait) {
  if (!gait.walking()) return false;
  if ((now_ms - last_cmd_ms_) <= timeout_ms) return false;
  if (timed_out_) return false;
  gait.stop();
  timed_out_ = true;
  return true;
}

void ConnectionWatchdog::trip(Gait& gait, ServoBus& bus, const Config& cfg) {
  gait.stop();
  bus.stopAll(cfg.ids);
}

} // namespace robot
