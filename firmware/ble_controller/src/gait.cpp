#include "gait.h"
#include "robot_profile.h"

namespace robot {

void Gait::start(uint16_t stride, uint16_t step_ms) {
  stride_       = stride;
  step_ms_      = step_ms;
  walking_      = true;
  last_step_ms_ = 0;   // fire immediately on next tick
  phase_        = 0;
}

void Gait::stop() {
  walking_ = false;
}

void Gait::tick(uint32_t now_ms, ServoBus& bus, const Config& cfg) {
  if (!walking_) return;
  if (step_ms_ == 0) return;
  if ((now_ms - last_step_ms_) < step_ms_) return;
  last_step_ms_ = now_ms;

  // Alternating trot.  Convention (matching the original firmware's live
  // telemetry, captured 2026-04-20):
  //   In the active phase's pair, the FIRST entry goes DOWN (-stride) and
  //   the SECOND entry goes UP (+stride).  The resting pair holds NEUTRAL.
  //
  //   Phase A  (phase_ even) -> swing {1, 2}:
  //       servo 1 -> NEUTRAL - stride   (pulls forward)
  //       servo 2 -> NEUTRAL + stride   (diagonal counter-swing)
  //       servos 0, 3 -> NEUTRAL
  //   Phase B  (phase_ odd)  -> swing {0, 3}:
  //       servo 0 -> NEUTRAL - stride
  //       servo 3 -> NEUTRAL + stride
  //       servos 1, 2 -> NEUTRAL
  //
  // For different leg topologies, swap kPhaseA / kPhaseB in robot_profile.h.
  const int16_t off   = (int16_t)stride_;
  const uint16_t spd  = kDefaultGait.speed;
  const uint8_t  acc  = kDefaultGait.acc;

  const uint8_t* swing = (phase_ & 1) ? kPhaseB : kPhaseA;
  const uint8_t* rest  = (phase_ & 1) ? kPhaseA : kPhaseB;

  uint8_t k = 0;
  if (swing[k] < N_SERVOS) {
    bus.writePosition(cfg.ids[swing[k]], NEUTRAL_POS - off, spd, acc);  // DOWN
    ++k;
  }
  if (k < N_SERVOS && swing[k] < N_SERVOS) {
    bus.writePosition(cfg.ids[swing[k]], NEUTRAL_POS + off, spd, acc);  // UP
  }
  for (uint8_t r = 0; r < N_SERVOS && rest[r] < N_SERVOS; ++r) {
    bus.writePosition(cfg.ids[rest[r]], NEUTRAL_POS, spd, acc);
  }
  phase_++;
}

} // namespace robot
