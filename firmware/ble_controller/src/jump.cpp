#include "jump.h"
#include "robot_profile.h"

namespace robot {

void Jump::trigger(ServoBus& bus, const Config& cfg) {
  const JumpParams& j = kJump;
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    bus.writePosition(cfg.ids[i], j.crouch_pos, j.speed_fast, j.acc_fast);
  }
  delay(j.phase_ms);
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    bus.writePosition(cfg.ids[i], j.extend_pos, j.speed_fast, j.acc_fast);
  }
  delay(j.phase_ms);
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    bus.writePosition(cfg.ids[i], NEUTRAL_POS, j.speed_settle, j.acc_settle);
  }
}

} // namespace robot
