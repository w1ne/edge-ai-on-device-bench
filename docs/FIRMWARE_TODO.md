# PhoneWalker Firmware — Next Iteration Planning

**Target repo:** `w1ne/PhoneWalker` (ESP32-S3 firmware). This doc lives in
`edge-ai-on-device-bench` because that is where the laptop-side daemon, the
vision stack, and the benchmark harness consuming firmware telemetry live.

## Why this matters

The benchmarks in this repo assume a physical robot that (a) tells us the
truth about its sensors, (b) responds to safety commands deterministically,
and (c) emits telemetry fast enough to close a visual loop. Right now the
firmware fails all three: the IMU dies silently, `stop` is non-sticky, and
10 Hz state packets are too slow for balance work. Every higher-level
experiment we want to run — obstacle avoidance, gait tuning, fall detection
— is bottlenecked on these gaps. This iteration is scoped to close them.

## Prioritized checklist

- [ ] **P0** — Revive MPU-6050 IMU (watchdog + re-init + `imu_dead` err packet)
- [ ] **P1a** — Add `estop_latch` / `estop_clear` sticky emergency-stop
- [ ] **P1b** — Add `mode` field to `state` packets (`idle|walking|pose|jumping`)
- [ ] **P2a** — Add `set_trim` per-servo offset command
- [ ] **P2b** — Add `telemetry_rate` command; support 10 Hz / 50 Hz
- [ ] **P2c** — Add `probe_sensors` one-shot diagnostic command
- [ ] **P3** — Read STS3032 status registers; include `fault_mask` in telemetry

---

## P0 — IMU revive

**Symptom.** Steady stream of
`{"t":"state",...,"imu":[0,0,0,0,0,0]}` packets over USB CDC after the first
few seconds post-boot. The IMU occasionally wakes up mid-walk for a
fraction of a second and then flatlines again. VERIFY: "first few seconds"
is from recollection — confirm against a fresh boot log before coding.

**Root cause / hypothesis.** Two candidates, neither confirmed:
1. Loose MPU-6050 ribbon. LED flicker correlated with IMU dropout ≈
   brownout on the sensor rail. *Not verified* — nobody has scoped VCC on
   the MPU while it drops.
2. `mpu.begin()` is called exactly once in `setup()`. Any transient I²C
   stall after that (ESD, bus contention with the servo bus if they share
   pins, brownout recovery) leaves the Adafruit driver in a wedged state
   and `getEvent()` silently returns stale zeros. *Hypothesis, not
   verified.*

**Proposed fix.** Add an IMU health watchdog in the main loop:

```
every 100 ms:
  read imu -> (ax, ay, az, gx, gy, gz)
  if all six == 0.0 exactly:
    zero_streak += 1
  else:
    zero_streak = 0
  if zero_streak >= 10:           // 1 s of dead reads
    Wire.end(); delay(5); Wire.begin();
    ok = mpu.begin();
    if !ok and retry_count < 3:   // back off, try again next window
      retry_count++
    if !ok and retry_count >= 3:
      emit {"t":"err","msg":"imu_dead"} once
      stop retrying until next "probe_sensors" or reboot
    else:
      retry_count = 0
      zero_streak = 0
```

Also: document MPU-6050 ribbon wire gauge and crimp spec in the PhoneWalker
hardware README (separate PR, hardware-side).

**Acceptance test** (runs from laptop daemon):
1. Boot the robot, start telemetry capture.
2. Physically wiggle the MPU ribbon for 5 s to force dropouts.
3. Expect: IMU values return to non-zero within ~1.5 s of cable reseat,
   no reboot needed, and at most one `{"t":"err","msg":"imu_dead"}` per
   dropout episode.

---

## P1a — Sticky emergency stop

**Symptom.** `{"c":"stop"}` halts servos for exactly one tick, then the
next queued motion command resumes motion. No way for the laptop to say
"stay stopped until I say otherwise."

**Root cause.** By design — `stop` just clears the current motion target.
There is no latched fault state.

**Proposed fix.** Add firmware state `estop_latched: bool`. While true:
- all motion commands (`walk`, `pose`, `jump`, `set_servo`) are dropped
  and acked with `{"t":"err","msg":"estop_latched"}`.
- servos are held at safe pose (VERIFY: is "safe pose" the current pose,
  or a defined neutral? Pick one and document).
- `mode` field (see P1b) reports `"idle"`.

Commands:
- `{"c":"estop_latch"}` → sets the flag, stops motion immediately.
- `{"c":"estop_clear"}` → clears the flag, returns to `idle`.
- Existing `{"c":"stop"}` remains non-sticky (back-compat).

**Acceptance test.** From daemon: send `estop_latch`, then send `walk`,
confirm robot does not move and an `estop_latched` err is returned. Send
`estop_clear`, then `walk`, confirm motion resumes.

---

## P1b — `mode` field in state packets

**Symptom.** Daemon has to infer whether the robot is walking by watching
servo deltas. This is brittle and races with the firmware's own motion
planner.

**Proposed fix.** Add `"mode":"idle"|"walking"|"pose"|"jumping"` to every
`state` packet. Firmware sets this directly from its current motion-FSM
state — it already has this info internally.

**Acceptance test.** Send `walk`, confirm `mode` flips to `"walking"`
within one packet; send `stop`, confirm it returns to `"idle"` within one
packet.

---

## P2a — Per-servo trim

**Symptom.** To level the robot we currently edit firmware constants and
reflash.

**Proposed fix.** `{"c":"set_trim","n":<0-3>,"us":<offset>}` stores an
int16 microsecond offset per servo, applied to every outgoing servo write.
Persist to NVS so trims survive reboot. Offsets clamped to ±200 µs
(VERIFY — need to confirm STS3032 safe range; 200 µs is a guess).
Add `{"c":"get_trim"}` → `{"t":"trim","us":[a,b,c,d]}` for readback.

**Acceptance test.** Set trim for servo 0 to +50, read back, power-cycle,
read back again. Value persists.

---

## P2b — Configurable telemetry rate

**Symptom.** 10 Hz state packets. Too slow for any closed-loop balance
work — at 10 Hz the control loop is already a full tick behind on
anything that moves.

**Proposed fix.** `{"c":"telemetry_rate","hz":<10|25|50>}`. Default
stays 10 Hz. At 50 Hz, USB CDC bandwidth is fine (~5 KB/s at current
packet size) but we should measure JSON serialization overhead on the
ESP32-S3 under load. VERIFY — nobody has profiled the serializer yet.

**Acceptance test.** Send `telemetry_rate hz=50`, measure inter-packet
gap at laptop, confirm 20 ms ± 5 ms. Drop back to 10 Hz, confirm
100 ms ± 5 ms.

---

## P2c — `probe_sensors` diagnostic

**Symptom.** No way to ask "is everything alive right now?" without
watching telemetry for 10+ seconds.

**Proposed fix.** `{"c":"probe_sensors"}` → one-shot reply:
```
{"t":"sensors","imu":"ok"|"dead","servos":[s0,s1,s2,s3],
 "voltage":V,"tmp":T}
```
Each `servos[i]` is the STS3032 ping result (1 = alive, 0 = no response).
VERIFY — `tmp` source: board temp via ESP32 internal sensor, or an
external NTC? Confirm before implementing.

**Acceptance test.** Unplug one servo, send `probe_sensors`, confirm
that servo's entry is 0 and all others are 1.

---

## P3 — Servo fault reporting

**Symptom.** If an STS3032 stalls or trips over-current, the firmware
keeps commanding it and the laptop has no idea.

**Proposed fix.** Low-rate (2 Hz) polling loop reads the Feetech status
register for each of the 4 servos. Aggregate into a single `fault_mask`
byte (1 bit per servo; 1 = fault) and include in every `state` packet.
Faults of interest: overload, overheat, voltage out of range. VERIFY —
exact register / bit layout: check the STS3032 datasheet against the
Feetech Arduino library constants; do not trust memory here.

**Acceptance test.** Stall a servo manually for ~2 s, confirm the
corresponding bit in `fault_mask` flips to 1 within ~1 s. Release,
confirm it clears within ~1 s.

---

## Not in scope (intentionally punted)

- **OTA firmware updates.** USB-C reflash is fine for a bench robot.
- **Wi-Fi / BLE.** USB CDC is deterministic and we control both ends;
  wireless adds latency variance we don't want in a control loop.
- **Second IMU / sensor fusion.** Fix the one we have first.
- **On-chip gait planner / inverse kinematics.** The whole point of the
  laptop tether is that the laptop plans. Firmware stays dumb.
- **Battery fuel gauge.** `voltage` in telemetry is enough for now.
- **Per-servo PID tuning over the wire.** Defer until we actually have
  closed-loop balance working.
- **Logging to SD card on the robot.** Laptop captures everything.
