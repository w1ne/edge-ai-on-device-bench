#include "poses.h"
#include "robot_profile.h"
#include <string.h>

namespace robot {

static const Pose* findPose(const char* name) {
  for (uint8_t i = 0; i < kPoseCount; ++i) {
    if (!strcmp(kPoses[i].name, name)) return &kPoses[i];
  }
  return nullptr;
}

bool applyPose(const char* name, uint16_t dur_ms,
               ServoBus& bus, const Config& cfg) {
  const Pose* p = findPose(name);
  if (!p) return false;
  // Map duration -> speed on STS3032: full travel ~4096 steps; target
  // roughly (|delta| / dur_ms) * 1000, clamped.
  uint16_t speed = dur_ms > 0
      ? (uint16_t)constrain(4096000UL / (uint32_t)dur_ms, 100UL, 3400UL)
      : 1000;
  uint8_t acc = 50;
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    bus.writePosition(cfg.ids[i], p->pos[i], speed, acc);
  }
  return true;
}

void applyNeutral(ServoBus& bus, const Config& cfg, bool log) {
  // speed=0 in FTServo means "use current limits" which on a fresh unit is
  // often ~0, resulting in no motion. Use an explicit speed (matches the
  // WritePos example in the FTServo repo).
  const uint16_t speed = 1500;
  const uint8_t  acc   = 50;
  for (uint8_t i = 0; i < N_SERVOS; ++i) {
    int err = bus.writePosition(cfg.ids[i], NEUTRAL_POS, speed, acc);
    if (log) {
      Serial.printf("[servo] WritePosEx id=%u pos=%d -> err=%d\n",
                    cfg.ids[i], (int)NEUTRAL_POS, err);
    }
  }
}

} // namespace robot
