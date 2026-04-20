# REAL_ROBOT_ROADMAP

From "tool-calling demo on a phone" to "thing that walks around, talks, and
does stuff by itself." Two-week concrete plan. Written 2026-04-20, after
commit `f61735e` (button panel) shipped on top of `eff79d7` (native Kotlin app
replacing Termux).

Inputs: `AGENT_ROADMAP.md` (prior plan, mostly cashed), `RESEARCH_AGENT_STACK.md`
(field), `STATUS.md` (ground truth), recent commits.

## 1. What's missing to be "real"

A real robot DOES things unprompted. Ours waits to be told. Specific gaps:

1. **No autonomous behavior loop.** Nothing in `Orchestrator.kt` / `GoalKeeper.kt`
   fires without a voice goal or explicit button press. No heartbeat, no idle
   patrol, no reactive triggers. `goalState = idle` means the robot literally
   sits there.
2. **Walking gait is uncalibrated for the floor.** Servo trajectory matches
   stock firmware byte-for-byte (commit `f8d7a0d`) — but we have never measured
   forward displacement. Stride=150 / step=400 may or may not translate the
   chassis. Likely it cycles legs in place.
3. **No spatial awareness.** No odometry, no yaw integration, no map, no
   "where was that person I saw 20 s ago." `look_for` returns boolean seen +
   score; nothing persists. `recentSeen` in `Orchestrator.buildTools()` is a
   stale list of strings with no bearings.
4. **Voice→action latency ≈ 16 s.** SpeechRecognizer ~2 s + Qwen-72B planner
   turn ~3 s + `look_for` via DeepInfra vision ~4 s + BLE ack ~100 ms + second
   planner turn + TTS. Not conversational.
5. **Cloud-dependent brain.** Planner (Qwen-72B) and vision (Llama-3.2-11B-V)
   both on DeepInfra. Airplane mode = brick.
6. **No continuous perception.** Camera captures only when `look_for` is
   called. Vision is pull, not push. The robot cannot "notice" anything.
7. **Wake word is a substring match.** `VoiceListener.kt` filters on the
   string `"hey robot"`. Under noise or across a room: dead.
8. **No memory across restarts.** Kill the app, lose everything seen.
9. **IMU is read but not acted on.** State packet exposes 6-axis IMU at 10 Hz.
   Nothing consumes it. No tilt reflex, no fall detection, no step counter.
10. **No self-speech.** Robot never initiates. A real robot greets, prompts,
    complains about battery.

## 2. Concrete features, ranked by value/effort

| # | Name | User sees | We build | Effort | Depends on |
|---|---|---|---|---|---|
| F1 | **Idle autonomous loop** | Robot glances around every 30 s while idle; announces new objects | `IdleLoop` coroutine in `Orchestrator`; when `goalKeeper.state in INERT` for >30 s, enqueue `look_for("person, cup, laptop")`; dedupe against last-hour `recentSeen`; TTS the delta | S | none |
| F2 | **Tilt reflex (IMU watchdog)** | Tilt robot 25° → it stops + says "whoa" | Parse `imu` field from BLE state packet in `BleRobotService.kt`; daemon thread; on `pitch>20° or roll>20°` send `{"c":"stop"}` + `tts.say("whoa")`; mute for 2 s | S | IMU live (confirmed per STATUS.md) |
| F3 | **Calibrate walk on floor** | Robot actually moves across the room | Set robot on floor, run 10 × `walk(stride=s, step=p)` for grid s∈{100,125,150,175,200}, p∈{300,400,500}; phone IMU yaw+accel integration or tape-measure; pick best; bake defaults into `Orchestrator.walk` | M | hardware time |
| F4 | **Battery-aware behavior** | Under 6.8 V, robot says "battery low, parking" and refuses walk | Plumb `v` field from state packet; `Orchestrator` wraps walk/jump tools in `if (battery < threshold) return ok=false, error="low battery"`; TTS once per session | S | parsing state packet |
| F5 | **Persistent memory (SQLite)** | "What did you see today?" works across restarts | Room DB `seen_objects(ts, label, score, yaw_at_sight, bearing)`; write from `look_for` success; new tool `what_did_i_see_where` | M | F6 for yaw |
| F6 | **Dead-reckoning pose** | `where_am_i` tool returns `(x, y, theta)` | Integrate walk-command duration × calibrated stride (from F3) into (dx, dy); integrate `imu.gz` for yaw at 10 Hz; `RobotState.pose`; reset via `set_origin()` | M | F3 |
| F7 | **Continuous perception watcher** | Robot spontaneously says "oh, hi Andrii" when you enter the room | Background `CameraQuery` poll at 0.5 Hz when idle, 2 Hz when actively searching; emit `VisionEvent` into `GoalKeeper.onEvent`; add standing "greet new people" goal by default | M | F1 |
| F8 | **Face follow** | "Follow me" → camera points toward user, robot walks short bursts | New tools `turn_to_face(bearing)` and `walk_short(ms)`; `look_for("person")` with bbox returned by VLM (need prompt tweak to request `{"x":0-1,"y":0-1}`); planner composes; goal-keeper standing | L | F3, F7 |
| F9 | **Local wake word + barge-in** | Say "hey robot" from 3 m away; interrupt TTS mid-sentence | Swap substring filter in `VoiceListener.kt` for OpenWakeWord `.tflite` (already cross-built per `STATUS.md`); add TTS ducking when VAD fires | M | none |
| F10 | **Patrol mode** | Goal-keeper empty >5 min → robot walks a short loop, looks around, returns | `IdleLoop` extension: after 5 min idle, plan `walk 2s → stop → look_for(room objects) → walk 2s → stop`; respects battery | M | F3, F6 |
| F11 | **Local planner fallback (offline)** | Pull WiFi, robot still answers "sit," "walk," "jump" | Port `scripts/local_rule_planner.py` (commit `ac81501`) to Kotlin; `Planner.run()` falls through when `config.apiKey.isBlank()` or 3× DeepInfra 5xx | M | none |
| F12 | **Sound source localization (crude)** | Loud bang → robot turns camera that way, captures, narrates | Pixel 6 has stereo mics; AudioRecord 2-channel; peak cross-correlation → left/right; map to servo pose `lean_left` / `lean_right`; `look_for("what happened")` via VLM | L | F7 |
| F13 | **Proactive check-in** | Every hour of idle, robot asks "still there?" | Hooked on F1; after N idle glances with a person seen, TTS a short question; skip if silent hours set | S | F1 |

**Top-3 rationale:** F1 + F2 + F3 together are the difference between "demo"
and "robot." F1 gives autonomy (user's literal ask). F2 is 50 lines of code
and prevents self-destruction. F3 is the single thing that currently makes
the robot look fake — legs move, body doesn't.

## 3. Hardware gaps

| Item | State | Verdict |
|---|---|---|
| Servo torque (4× STS3032) | Running stock gait; floor walking unverified | **Need NOW:** test F3 first. If stride <5 cm at 150/400, upgrade to STS3215 (~$25 × 4). |
| IMU | Live per `STATUS.md` open items — `[-0.01,-0.01,0.98,…]` | Works. Keep. |
| Battery (2S Li-ion, 7 V via ADC) | Reports voltage; lifetime unmeasured under walking | **Need NOW:** one-hour walk loop, log `v`. If <30 min, add 2nd pack. |
| Camera (Pixel 6 back, ~80° FOV) | CameraX working | Fine for v1. |
| Speaker | Phone speaker under TTS — audible, not loud | **v2:** small USB-C or BT speaker if demos are outdoor. |
| Microphone | Phone mic, push-to-talk effective <1 m | **Need for F9:** test wake word from 3 m before buying. |
| Depth | None | **Nice-to-have only.** RealSense D405 (~$350) unlocks grasping — not in this plan. |
| Compute | Pixel 6 only. DeepInfra for heavy LLM/VLM | **v2:** on-device Qwen-2.5-3B for planner fallback. |
| Firmware source | Still flash-only; BOOT dump pending | Blocks LeRobot adapter only — not this plan. |

## 4. Software gaps (prioritized cuts)

- **Voice→action 16 s.** Cut: F9 (local wake, faster VAD), drop initial
  `look_for` in first planner turn (use cached `recentSeen`), use
  Qwen-2.5-7B instead of 72B for simple goals (DeepInfra supports both).
  Target: 5 s.
- **Vision polling fixed 2 Hz / 4 s latency.** Make goal-aware: idle 0.5 Hz,
  active search 2 Hz, explicit `look_for` immediate. Swap Llama-3.2-11B-V
  for 3B-V when goal is simple bbox (3× faster).
- **No local LLM.** F11 lands a rule-based fallback (hands-off offline
  degrade). On-device Qwen-3B stays as v2.
- **No persistent memory.** F5.
- **Wake word brittle.** F9.
- **No continuous awareness.** F7.

## 5. Autonomous loop — the heartbeat

In `Orchestrator.kt`, a new `AutonomyLoop` coroutine ticks at 1 Hz:

```
every tick:
  if battery < 6.5 V: say_once("battery low, parking"); refuse motion; return
  if imu.tilt > 20°: stop(); say("whoa"); mute 2 s
  if goalKeeper.state == active: return   // human goal wins
  if idle for > 30 s:
      glance (look_for list of common objects)
      diff vs last-hour recentSeen → announce new
  if idle for > 5 min:
      enter patrol: walk 2 s → stop → look_around → return
  if loud sound (peak dB > threshold):
      turn_to_source(L/R) → look_for("what happened") → say
  if new face detected (F7 watcher):
      if not seen in last 10 min: greet
```

## 6. Day-by-day (2026-04-21 → 2026-05-04, 14 days)

| Day | Date | Items |
|---|---|---|
| 1 | Mon 04-21 | F2 IMU tilt reflex (parse state packet, daemon thread, 1-line TTS); F4 battery threshold in `Orchestrator` |
| 2 | Tue 04-22 | F1 `AutonomyLoop` skeleton + idle glance every 30 s; wire into `Orchestrator.init` |
| 3 | Wed 04-23 | **HARDWARE DAY** F3 walk calibration: 15-point grid on floor; tape-measure or phone IMU dx; bake default stride/step |
| 4 | Thu 04-24 | F6 dead reckoning: yaw from `imu.gz` integration; walk-duration → (dx,dy); `RobotState.pose`; `where_am_i` tool |
| 5 | Fri 04-25 | F5 Room DB `seen_objects`; `what_did_i_see_where` tool; write path from `look_for` success |
| 6 | Sat 04-26 | **HARDWARE DAY** 1-hour walk loop for battery lifetime; log `v` every 10 s; decide F3 torque question |
| 7 | Sun 04-27 | F11 local rule planner fallback (port `ac81501` to Kotlin); unit tests for 14 canned commands |
| 8 | Mon 04-28 | F9a OpenWakeWord .tflite integration (replace substring filter in `VoiceListener.kt`) |
| 9 | Tue 04-29 | F9b TTS ducking + barge-in: pause `TtsPlayer` when VAD triggers mid-speech |
| 10 | Wed 04-30 | F7 continuous perception watcher: CameraX poll 0.5/2 Hz; emit `VisionEvent`; dedupe |
| 11 | Thu 05-01 | F13 proactive check-in; F10 patrol mode in `AutonomyLoop`; tune idle thresholds |
| 12 | Fri 05-02 | F8a `turn_to_face`: prompt VLM for bbox; map x∈[0,1] to `lean_left/right`; short walk burst |
| 13 | Sat 05-03 | **HARDWARE DAY** F8b full follow-me integration test; record video; note failure modes |
| 14 | Sun 05-04 | Buffer: latency cuts (smaller Qwen, skip-first-look_for), F12 only if time, write `STATUS.md` update |

Hardware days: 3, 6, 13. Others are laptop/phone.

## 7. Explicitly out of scope

- LeRobot adapter (firmware source blocked).
- VLA fine-tune (no data).
- Depth sensor / grasping.
- Wheeled base swap.
- Outdoor / crowd demos.
- SLAM. Dead-reckoning is good enough for room-scale.

## 8. Success criteria at end of 14 days

1. Place robot on floor, say nothing. Within 60 s it glances around, says
   what it sees.
2. Tilt it 25° — it stops and speaks within 200 ms.
3. "Walk to me" command results in measurable forward motion (>30 cm in
   2 s).
4. Kill WiFi. Say "stand up." Robot does the neutral pose via F11 fallback.
5. Restart app. Ask "what did you see today?" — at least one entry per prior
   `look_for` success comes back.
6. From 3 m: "hey robot, jump." It jumps.
7. "Follow me" across a room: robot keeps its camera on you for at least
   three walk bursts.

If 5/7 pass, this is no longer a demo.
