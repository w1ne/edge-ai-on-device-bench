# PhoneWalker Firmware IMU Audit

**Date:** 2026-04-19
**Audit target:** `/home/andrii/Projects/PhoneWalker/` (`w1ne/PhoneWalker`, branch `main`, HEAD `b62edfb`)
**Scope:** Trace the `"imu":[0,0,0,0,0,0]` pin-to-zero bug back to source.
**Constraints honored:** No commits, no pushes, no flashing. Investigation only.

---

## TL;DR

**The live ESP32 is not running the firmware in this source tree.** Stop before
writing any driver: the patch would land in a codebase that has no path to the
device currently on the bench.

- The `imu` field is **not** emitted anywhere in `/home/andrii/Projects/PhoneWalker/`.
  Not by C++ firmware, not by any Python simulator, not hardcoded, not via any
  `firmware/lib/` subproject.
- The firmware in `firmware/src/` (`main.cpp`, `command_handler.cpp`,
  `servo_manager.cpp`) is a **text-based CLI** emitting emoji strings over USB
  CDC (`"🤖 PHONEWALKER SERVO CONTROL SYSTEM"`, `"📡 Pinging all detected
  servos…"`). It does not speak the `{"c":"ping"}` → `{"t":"state",...}` JSON
  wire protocol at all.
- The only reference in the repo to a JSON-telemetry emitter is
  `brain/tools/sim_server.py`, which shells out to `firmware/simulation/sim.py`
  — and that file **does not exist** in the tree. The brain-side schema in
  `brain/wire.py` documents the wire protocol but there is no firmware-side
  implementation of it in this repo.
- No MPU-6050 driver, no `Adafruit_MPU6050`, no `Wire.begin()`, no I²C code of
  any kind. `platformio.ini` lists `Wire` as a lib dep, but nothing `#include`s
  or uses it.

Confidence that running firmware ≠ this source tree: **HIGH.**

---

## Evidence

### 1. Where `"imu"` appears in the repo

Full-repo search for `imu|IMU|mpu|MPU|Wire\.begin|MPU6050` returns 25 files,
none of them C++:

- Docs: `telemetry_protocol.md`, `BRAIN_ARCHITECTURE.md`, various `*.md`.
- Brain Python: `brain/transport.py`, `brain/tools/sim_server.py`,
  `brain/schema/*`, `brain/tests/*`.
- Zero hits under `firmware/src/`, `firmware/lib/`, or any `.cpp` / `.h` /
  `.ino` anywhere in the tree.

String search for the literal `"imu"` in code returns exactly one hit:
`telemetry_protocol.md:29` — a protocol design document, not an emitter.

### 2. What `firmware/src/main.cpp` actually is

```
firmware/src/
├── command_handler.{cpp,h}   # text CLI dispatcher (h/s/p/e/m/r/c/i/x/d/q/g/z)
├── config.h
├── main.cpp                  # Serial.println("\n🤖 PHONEWALKER SERVO CONTROL SYSTEM")
└── servo_manager.{cpp,h}     # STS3032 bus only
```

`main.cpp` calls `servoManager.begin()`, `cmdHandler.begin()`, and then in
`loop()` forwards chars to/from `Serial1` (STS3032 bus). No IMU. No JSON. No
periodic state packet.

### 3. What the laptop daemon expects

`/home/andrii/Projects/edge-ai-on-device-bench/demo/robot_daemon.py` sends
`{"c":"ping"}` over USB CDC and parses `{"t":"state","imu":[…],"v":…,"tmp":…,
"p":[…],"ms":…}` back. Some firmware on the wire does emit this — but its
source is not in this repo. Candidates:

- A hand-built prototype firmware flashed once and never committed.
- A stale binary left on the ESP32 from before the `50f3d1a Refactor to
  modular architecture` rewrite (which eliminated whatever was there).
- A separate unpushed branch or local working tree elsewhere on this machine.

### 4. `sim_server.py` references a non-existent simulator

`brain/tools/sim_server.py:24`:
```python
SIM_PATH = Path(__file__).resolve().parents[2] / "firmware" / "simulation" / "sim.py"
```

`firmware/simulation/` does not exist. `brain/README.md` and `brain/transport.py`
both reference this path. The JSON wire protocol has a spec (`brain/wire.py`) and
a brain-side client, but no server-side implementation checked in anywhere.

---

## Why I'm not writing the diff

The task spec's step 3 ("produce a diff that enables IMU read from an existing
but broken driver, or adds a minimal MPU-6050 driver") assumes the source tree
matches the running binary. It doesn't. Consequences:

1. A new `imu_manager.{cpp,h}` added here would not affect the live device — it
   would need to be flashed, and the existing `main.cpp` doesn't even speak the
   wire protocol the daemon is talking to, so flashing this tree would break
   the demo rather than fix the IMU.
2. The real fix has to happen in whatever source actually produced the running
   binary. Without that source, we'd be guessing at its structure (where does
   it call `mpu.getEvent()`? where does it build the state packet? does it use
   Adafruit's driver or a hand-rolled I²C implementation?).
3. Writing a plausible-looking patch against the wrong codebase would create
   the illusion of progress while leaving the bug untouched. That's worse than
   nothing.

Honoring the explicit constraint in the task: *"If the current running firmware
is clearly not this source tree, say so and stop — don't chase ghosts."*

---

## What needs to happen before a real fix

In priority order:

1. **Locate the source of the running binary.** Check for:
   - Other clones of `w1ne/PhoneWalker` on this laptop (different worktree,
     different branch).
   - Unpushed commits on the device's last-known flasher machine.
   - A sibling repo (anything with `telemetry`, `firmware_v2`, `esp32_ai`,
     etc. in the name).
   - `find ~ -name "*.cpp" -exec grep -l '"imu"' {} \;` — if it's on disk it
     will surface here.
   Quick probe command:
   ```sh
   grep -rln --include='*.cpp' --include='*.h' --include='*.ino' \
       -e 'Adafruit_MPU6050' -e 'MPU6050' -e '"imu"' \
       ~/Projects ~/src ~/code 2>/dev/null
   ```
2. **If no source is found:** dump the running firmware off the ESP32
   (`esptool.py read_flash 0x0 0x400000 dump.bin`) and treat that as the new
   ground truth — either re-derive the source or replace it wholesale with a
   fresh build from this tree *after* the JSON telemetry path is actually
   implemented here.
3. **If the JSON-emitting firmware is to be rewritten into this tree:** that's
   a significantly larger piece of work than an IMU fix. It needs: USB-CDC JSON
   state-packet emitter, `{"c":"ping"|"pose"|"walk"|"stop"|"jump"|"estop"}`
   command parser (`brain/wire.py` is the spec), periodic 10 Hz task using the
   existing `servoManager` for servo feedback, battery ADC read, MPU-6050
   driver with the watchdog/re-init pattern from `docs/FIRMWARE_TODO.md` P0,
   and a graceful fallback that emits `{"t":"err","msg":"imu_dead"}` once
   rather than silent zeros.

---

## Sketched IMU driver (for whenever the right codebase is found)

This is the shape the fix should take; **do not apply to this repo** until the
JSON telemetry path exists. Written against `Adafruit_MPU6050` because
`platformio.ini` already has `Wire` as a dep and ESP32-S3 projects typically
use this library.

**`firmware/src/imu_manager.h`** (new file):
```cpp
#pragma once
#include <Arduino.h>
#include <Adafruit_MPU6050.h>

class ImuManager {
public:
  bool begin(uint8_t sda, uint8_t scl);
  // Returns true on good read; fills ax,ay,az (m/s^2) and gx,gy,gz (rad/s).
  // On N consecutive all-zero reads triggers re-init attempt.
  bool read(float& ax, float& ay, float& az,
            float& gx, float& gy, float& gz);
  bool isDead() const { return dead_; }
private:
  Adafruit_MPU6050 mpu_;
  uint8_t sda_ = 0, scl_ = 0;
  uint8_t zero_streak_ = 0;
  uint8_t retry_count_ = 0;
  bool    dead_ = false;
  bool    reinit();
};
extern ImuManager imuManager;
```

**`firmware/src/imu_manager.cpp`** (new file, sketch — untested):
```cpp
#include "imu_manager.h"
#include <Wire.h>

ImuManager imuManager;

static constexpr uint8_t  ZERO_STREAK_LIMIT = 10;   // ~1 s at 10 Hz
static constexpr uint8_t  MAX_RETRIES       = 3;

bool ImuManager::begin(uint8_t sda, uint8_t scl) {
  sda_ = sda; scl_ = scl;
  Wire.begin(sda_, scl_);
  Wire.setClock(400000);
  if (!mpu_.begin()) { dead_ = true; return false; }
  mpu_.setAccelerometerRange(MPU6050_RANGE_4_G);
  mpu_.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu_.setFilterBandwidth(MPU6050_BAND_21_HZ);
  return true;
}

bool ImuManager::reinit() {
  Wire.end();
  delay(5);
  Wire.begin(sda_, scl_);
  Wire.setClock(400000);
  if (mpu_.begin()) { zero_streak_ = 0; retry_count_ = 0; return true; }
  if (++retry_count_ >= MAX_RETRIES) dead_ = true;
  return false;
}

bool ImuManager::read(float& ax, float& ay, float& az,
                      float& gx, float& gy, float& gz) {
  if (dead_) { ax=ay=az=gx=gy=gz=0.0f; return false; }
  sensors_event_t a, g, t;
  if (!mpu_.getEvent(&a, &g, &t)) {
    ax=ay=az=gx=gy=gz=0.0f;
    if (++zero_streak_ >= ZERO_STREAK_LIMIT) reinit();
    return false;
  }
  ax = a.acceleration.x; ay = a.acceleration.y; az = a.acceleration.z;
  gx = g.gyro.x;         gy = g.gyro.y;         gz = g.gyro.z;
  const bool all_zero =
      ax == 0.0f && ay == 0.0f && az == 0.0f &&
      gx == 0.0f && gy == 0.0f && gz == 0.0f;
  if (all_zero) {
    if (++zero_streak_ >= ZERO_STREAK_LIMIT) reinit();
  } else {
    zero_streak_ = 0;
  }
  return true;
}
```

**Hookup** (`setup()`): `imuManager.begin(IMU_SDA_PIN, IMU_SCL_PIN);` after
`servoManager.begin()`. **Telemetry path** (in whatever task builds the state
packet): call `imuManager.read(...)` and either emit the floats as the `imu`
array, or, if `isDead()` is true, emit `{"t":"err","msg":"imu_dead"}` once and
continue sending the rest of the state packet with the six zeros (daemon-side
already tolerates them).

Lib deps to add in `platformio.ini`:
```ini
lib_deps =
    bblanchon/ArduinoJson@^7.0.4
    Wire
    adafruit/Adafruit NeoPixel@^1.12.0
    adafruit/Adafruit MPU6050@^2.2.6
    adafruit/Adafruit Unified Sensor@^1.1.14
```

**Loop-latency impact:** `mpu_.getEvent()` over 400 kHz I²C reads 14 bytes —
~350 µs. At 10 Hz telemetry cadence this is negligible next to the
`servoManager` serial polling budget.

---

## Acceptance test (for the eventual real fix)

Run from the laptop daemon; device on USB CDC.

1. Boot robot, start `python3 demo/robot_daemon.py --mode text`.
2. Issue `{"c":"ping"}`; confirm returned state packet's `"imu"` array
   contains at least one non-zero value and the magnitude of
   `sqrt(ax^2+ay^2+az^2)` is within ±1.5 m/s² of 9.81 when the robot is
   stationary and upright.
3. Physically wiggle the MPU ribbon for 5 s. Expect IMU values to return to
   plausible non-zero within ~1.5 s of cable reseat, with at most one
   `{"t":"err","msg":"imu_dead"}` packet per dropout episode and no reboot.
4. Tilt the robot 30° left/right and verify `ay` / `ax` track the tilt
   direction, confirming the driver is reading real accelerometer data not
   just non-zero garbage.

---

## Files touched by this audit

- **Read, not modified:** everything under `/home/andrii/Projects/PhoneWalker/`.
- **Created:** this file, `/home/andrii/Projects/edge-ai-on-device-bench/docs/FIRMWARE_IMU_AUDIT.md`.
- **No changes to the firmware repo working tree.** `git status` on
  `/home/andrii/Projects/PhoneWalker/` is clean post-audit.
