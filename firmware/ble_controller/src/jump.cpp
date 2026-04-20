#include "jump.h"
#include "robot_profile.h"

namespace robot {

// Asymmetric jump pattern matching the original firmware (captured 2026-04-20):
//   Servos 0,1 go DOWN to ~crouch_pos; servos 2,3 go UP to a mirror position
//   (NEUTRAL + (NEUTRAL - crouch_pos)).  Then hold, then settle back to
//   NEUTRAL for all four.  The original never actually "launches" — it's a
//   visual squat.  We replicate that behavior here.
void Jump::trigger(ServoBus& bus, const Config& cfg) {
  const JumpParams& j = kJump;
  const int16_t delta = NEUTRAL_POS - j.crouch_pos;   // positive, ≈ 208
  const int16_t up_pos = NEUTRAL_POS + delta;
  // Phase 1: crouch.  First two legs drop, back two rise.
  bus.writePosition(cfg.ids[0], j.crouch_pos, j.speed_fast, j.acc_fast);
  bus.writePosition(cfg.ids[1], j.crouch_pos, j.speed_fast, j.acc_fast);
  bus.writePosition(cfg.ids[2], up_pos,       j.speed_fast, j.acc_fast);
  bus.writePosition(cfg.ids[3], up_pos,       j.speed_fast, j.acc_fast);
  delay(j.phase_ms);
  // Phase 2: settle all four to neutral.
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    bus.writePosition(cfg.ids[i], NEUTRAL_POS, j.speed_settle, j.acc_settle);
  }
}

} // namespace robot
