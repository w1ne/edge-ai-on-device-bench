// robot_profile.h — per-robot COMPILE-TIME constants.
//
// This is the file you edit to adapt the firmware to a different robot
// (different number of servos, different pose table, different gait). See
// docs/FIRMWARE_PORTING.md for the checklist.
#pragma once

#include <Arduino.h>

namespace robot {

// ------------ 1. Mechanical shape ---------------------------------------
// Number of servos on the bus. If you change this you must also update the
// pose table below and review gait.cpp (alternating-trot assumes 4 legs).
static constexpr uint8_t  N_SERVOS      = 4;

// Neutral/center position in servo steps. STS3032 is 12-bit -> mid=2048.
static constexpr int16_t  NEUTRAL_POS   = 2048;

// Feetech half-duplex bus baud. 1 Mbit is the factory default for STS/SMS.
static constexpr uint32_t SERVO_BAUD    = 1000000;

// ------------ 2. Pose table ---------------------------------------------
// Positions derived from the live wire protocol log in
// docs/FIRMWARE_IMU_AUDIT.md §"Verified command surface". Names are matched
// case-sensitively by poses.cpp::applyPose(). Order of entries is irrelevant.
struct Pose {
  const char* name;
  int16_t     pos[N_SERVOS];
};

extern const Pose kPoses[];
extern const uint8_t kPoseCount;

// ------------ 3. Gait parameters (alternating trot) ---------------------
// Stride: how far each leg swings from neutral, in servo steps.
// Step period: milliseconds per half-cycle (shorter = faster).
struct GaitParams {
  uint16_t stride;
  uint16_t step_ms;
  uint16_t speed;   // Feetech speed units (steps/sec)
  uint8_t  acc;     // Feetech accel units
};
extern const GaitParams kDefaultGait;

// Which servos belong to the "diagonal A" and "diagonal B" pair. The gait
// tick alternates swinging A forward, then B forward. For a quadruped this
// is typically {0,2} and {1,3}. For a different topology, edit these.
// phase_a[] entries that are >= N_SERVOS are ignored (sentinel).
static constexpr uint8_t kPhaseA[N_SERVOS] = {0, 2, 255, 255};
static constexpr uint8_t kPhaseB[N_SERVOS] = {1, 3, 255, 255};

// ------------ 4. Jump sequence ------------------------------------------
// Two-step "crouch then extend" timing, tuned on hardware later.
struct JumpParams {
  int16_t  crouch_pos;
  int16_t  extend_pos;
  uint16_t phase_ms;
  uint16_t speed_fast;
  uint8_t  acc_fast;
  uint16_t speed_settle;
  uint8_t  acc_settle;
};
extern const JumpParams kJump;

} // namespace robot
