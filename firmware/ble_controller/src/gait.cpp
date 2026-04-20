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

  // Alternate left/right half-step. This is a placeholder gait; the real
  // per-leg phase angles need to be ported from the original firmware or
  // tuned on the bench.
  const int16_t off = (int16_t)stride_;
  const uint16_t speed = kDefaultGait.speed;
  const uint8_t  acc   = kDefaultGait.acc;

  // Phase even: swing pair A (first member -off, second +off). Phase odd:
  // swing pair B (first member +off, second -off), matching the pre-refactor
  // alternating-trot pattern ({0:-off, 2:+off} / {1:+off, 3:-off}).
  const uint8_t* swing = (phase_ & 1) ? kPhaseB : kPhaseA;
  const uint8_t* rest  = (phase_ & 1) ? kPhaseA : kPhaseB;
  const int16_t sign0  = (phase_ & 1) ? +1 : -1;
  for (uint8_t k = 0; k < N_SERVOS; ++k) {
    if (swing[k] >= N_SERVOS) break;
    int16_t delta = ((k == 0) ? sign0 : -sign0) * off;
    bus.writePosition(cfg.ids[swing[k]], NEUTRAL_POS + delta, speed, acc);
  }
  for (uint8_t k = 0; k < N_SERVOS; ++k) {
    if (rest[k] >= N_SERVOS) break;
    bus.writePosition(cfg.ids[rest[k]], NEUTRAL_POS, speed, acc);
  }
  phase_++;
}

} // namespace robot
