#include "robot_profile.h"

namespace robot {

const Pose kPoses[] = {
  { "neutral",    { 2048, 2048, 2048, 2048 } },
  { "lean_left",  { 1700, 1700, 1700, 1700 } },
  { "lean_right", { 2400, 2400, 2400, 2400 } },
  { "bow_front",  { 1501, 2501, 2597, 1595 } },
};
const uint8_t kPoseCount = sizeof(kPoses) / sizeof(kPoses[0]);

const GaitParams kDefaultGait = {
  /*stride */ 150,
  /*step_ms*/ 400,
  /*speed  */ 2000,
  /*acc    */ 100,
};

// Jump is NOT symmetric in the original firmware; it's a quadruped crouch
// where servos 0,1 go DOWN and servos 2,3 go UP in parallel.  Positions
// captured from the original binary via USB CDC replay on 2026-04-20:
//   crouch peak: [1845, 1838, 2253, 2259]  (deltas ≈ ±210 from neutral)
const JumpParams kJump = {
  /*crouch_pos  */ 1840,   // servos 0,1 target  (the 2,3 pair uses NEUTRAL+(NEUTRAL-crouch_pos))
  /*extend_pos  */ 2048,   // settle target for all 4 after crouch phase (unused right now; gait.jump uses NEUTRAL_POS)
  /*phase_ms    */ 250,    // captured cycle is ~250 ms crouch-in then settle
  /*speed_fast  */ 3500,
  /*acc_fast    */ 150,
  /*speed_settle*/ 2000,
  /*acc_settle  */ 100,
};

} // namespace robot
