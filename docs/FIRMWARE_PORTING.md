# Porting `firmware/ble_controller` to a new robot

This firmware is intentionally split into modules so that adapting it to a
different-sized robot (or a different servo bus / IMU) is mostly a
configuration edit, not a rewrite. The layout:

| File | Role |
|---|---|
| `src/main.cpp` | top-level `setup()`/`loop()` wiring only |
| `src/robot_profile.{h,cpp}` | **per-robot compile-time constants** (edit me) |
| `src/robot_config.{h,cpp}` | NVS-backed runtime pin/ID config |
| `src/servo_bus.{h,cpp}` | servo driver abstraction |
| `src/servo_bus_feetech_sts.cpp` | Feetech SMS_STS implementation |
| `src/imu.{h,cpp}` | MPU-6050 wrapper |
| `src/battery.{h,cpp}` | ADC-based voltage sense |
| `src/poses.{h,cpp}` | name -> pose table lookup + apply |
| `src/gait.{h,cpp}` | alternating-trot state machine |
| `src/jump.{h,cpp}` | crouch+extend maneuver |
| `src/safety.{h,cpp}` | disconnect watchdog + command-timeout hold |
| `src/ble_wire.{h,cpp}` | NimBLE + NUS GATT + JSON wire protocol |

## Checklist: adapt to a different robot

1. **Edit `src/robot_profile.h` / `robot_profile.cpp`.** This is usually the
   only file you need to touch. Specifically:
   - `N_SERVOS` — number of servos on the bus.
   - `NEUTRAL_POS` — servo center step (STS3032 mid-range = 2048 for 12-bit).
   - `kPoses[]` — the pose table (name -> per-servo positions).
   - `kDefaultGait` — stride, step period, speed, acceleration defaults.
   - `kPhaseA` / `kPhaseB` — which servos swing together in the trot.
   - `kJump` — crouch/extend positions + timing.

2. **Edit `src/gait.cpp`** only if the locomotion pattern differs from the
   alternating-trot default (e.g. a bipedal or hexapod robot). The state
   machine itself is ~30 lines.

3. **Swap the servo driver** if the robot uses something other than Feetech
   STS/SMS. Add a sibling `servo_bus_<driver>.cpp`, guard it on
   `#ifdef SERVO_DRIVER_<NAME>`, and flip `-DSERVO_DRIVER_*` in
   `platformio.ini`. The header `servo_bus.h` is the contract.

4. **(optional)** Adjust the pin defaults in
   `robot_config.cpp::kDefaultConfig` for your board. These are
   overridable over the air via the `{"cfg":{...}}` BLE command, so
   getting them wrong on first flash is not fatal.

5. **Build + flash + verify:**
   ```sh
   cd firmware/ble_controller
   pio run
   pio run --target upload
   python3 scripts/ble_smoke.py     # optional: from laptop with BLE
   ```
   Boot logs on `/dev/ttyACM0` should show:
   ```
   [ble_controller] boot
   [servo] Serial1 up: baud=1000000 rx=18 tx=17 dir=255
   [servo] probe id=1 ReadPos=20XX err=0   # repeated for each ID
   [ble_controller] adv.start() -> OK
   ```

## What you must NOT change

- The NUS service / characteristic UUIDs in `ble_wire.cpp` — the Android
  companion app and `scripts/ble_smoke.py` match on them.
- The JSON wire protocol shape (keys and values). Add new commands by all
  means, but don't rename `p`/`v`/`tmp`/`ms`/`imu` in state packets.
- The BLE device name `PhoneWalker-BLE` — scanners on the phone daemon
  filter by it.

## Size budget

Post-refactor footprint (2026-04-20, Waveshare ESP32-S3 Zero):

| | Used | Budget |
|---|---|---|
| Flash | 578 933 B | ~700 KB |
| RAM   |  30 608 B | ~60 KB |
