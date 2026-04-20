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

const JumpParams kJump = {
  /*crouch_pos  */ 1700,
  /*extend_pos  */ 2400,
  /*phase_ms    */ 150,
  /*speed_fast  */ 3500,
  /*acc_fast    */ 150,
  /*speed_settle*/ 2000,
  /*acc_settle  */ 100,
};

} // namespace robot
