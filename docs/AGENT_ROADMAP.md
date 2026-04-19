# AGENT_ROADMAP.md

Reactive-robot → agent. End state: voice-in / tool-calling / spatial-memory
loop that can accept "find the blue cup and come back" and replan when a
primitive fails. Guiding principle: **ship agent behavior by gluing adopted
libraries, not by fine-tuning a VLA**. Source ranking:
`docs/RESEARCH_AGENT_STACK.md` §11.

**Scope note (2026-04-20):** the original plan allotted 13–15 days across
4 weeks. Parallel-agent execution collapsed Weeks 1 and parts of 2–3 into
roughly 8 hours of wall time over 2026-04-19 → 2026-04-20. What remains is
hardware-gated and cannot be compressed by dispatching more agents. The
honest schedule is below.

## ✅ Done (2026-04-19 → 2026-04-20)

Code-only items — all landed and pushed to `origin/main`:

| Layer | Shipped | Commit |
|---|---|---|
| Voice | Pipecat real pipeline (Silero VAD + faster-whisper + Piper) behind `--voice pipecat` | `7706189` |
| Voice | Basic-loop fallback (arecord + webrtcvad + openai-whisper + Piper + OpenWakeWord) | `0825002` |
| Planner | LLM tool-calling loop with 9 tools (pose, walk, stop, jump, look, look_for, say, wait, finish) behind `--planner` | `0825002` |
| Planner | Eval harness (21 cases, 5 groups) + baseline CI checkpoint | `37ee93b` |
| Planner | Prompt fixes + walk/stop schema resolution; 16/20 → 19/20 | `7706189` |
| Planner | LLM bakeoff across 6 models; default → Qwen-2.5-72B (passes C2 "no fabricated greeting" case) | `35cfded` |
| Vision | CLIP open-vocab `look_for(query)` tool on top of existing YOLO path | `930e5d7` |
| UI | `/tmp/robot_state.json` atomic snapshot (decouples UI from log regex) | `b87bc13` |
| UI | HTTP + SSE server on `127.0.0.1:5556` (GET /state, /events, /health, POST /stop); web UI becomes thin EventSource client | `b6d40e3` |
| Decisions | `docs/DECISIONS_PENDING.md` (Termux port feasibility, depth sensor verdict) | `659cebe` |
| Docs | Honest Edge TPU framing (no more "10× faster" headline on models we don't run) | *this commit* |

**Eval score, current baseline:** 20/21 (Qwen-2.5-72B, median 3.07 s per step,
71.7 s total). One remaining failure (C3-find-keys) is a harness quirk where
smarter models pick `look_for` over `look` — correct behavior, wrong
expectation.

## 🚧 In flight (as of 2026-04-20)

Three parallel agents dispatched after roast round 2:

- `scripts/hw_stress_test.py` — real motor + IMU stress cycle, no walking
- `demo/goal_keeper.py` — persistent-goal + re-plan-on-event (the
  reactive → agent transition)
- State-server auth: bearer token, CORS allowlist, TLS — so `--state-bind
  0.0.0.0` stops being negligent

## Week 2 — remaining scope (2026-04-21 → 2026-04-26)

**Goal:** spatial memory + reflex. Everything left on the "code-only"
ledger. Roughly 2–3 days of work, not a calendar week.

| Commit title | What |
|---|---|
| `demo/occupancy_map.py` — 2D grid, 10 cm cells, 8×8 m | Dead-reckoning pose from walk cmd duration + IMU yaw integration |
| `demo/imu_reflex.py` — pitch/roll > 20° → stop + speak | Runs in daemon thread; pre-empts in-flight tool calls |
| Vision event → occupancy integration | Each detection places a bearing-tagged landmark in the map |
| New planner tools: `where_am_i`, `what_did_i_see_where` | Memory queryable from the planner |

**Success:** After 5 min of pose + jump + (simulated) walk commands, the
occupancy map shows a coherent trail. Tilt robot to 25° → stop within 150 ms.
**Risk:** Yaw drift without a magnetometer — accept it; add a `set_origin`
reset command. No real distance without depth; use "near/far" coarse bin.

## Week 3 — phone migration or VLA (decision point)

This is the fork in the road. Two viable paths, pick one.

**Path A: Phone-as-brain Termux port.** Per `docs/DECISIONS_PENDING.md`,
~3 days: write `TermuxAudioTransport` that shells to
`termux-microphone-record` + `play-audio`; port planner + state server to
Termux; wire ADB-free USB to ESP32 via a small Kotlin companion app.
Killer feature: the phone IS the robot brain, the laptop goes away.

**Path B: SmolVLM scene caption.** Rich visual observations instead of
class IDs. `describe_scene()` as a 10th planner tool. ~2 days. Makes the
robot much more expressive but doesn't change its physical capability.

**Recommendation:** Path A if the "phone as brain" story is real. Path B
if the reality is this stays a demo on a laptop. The architecture doc is
committed to Path A; I'd pick it.

## Week 4 — LeRobot adapter (firmware-gated, DO NOT SCHEDULE YET)

**Blocked on the ESP32 firmware dump.** Without source access, we cannot
expose the raw servo-angle channel LeRobot expects. `lerobot_adapter/`
remains as design-only until the BOOT-button flash dump happens (physical
access required; user action, not agent-dispatchable).

If unblocked, ~3 days: robot config + action space, teleop record script,
replay smoke test. Unlocks data collection for any future VLA fine-tune.

## What's NOT compressible by agents

- **Physical testing.** The robot has never walked on a floor. 30 min of
  walking under human supervision is hours, not dispatched.
- **Firmware access.** Requires holding BOOT during reset. User's keyboard,
  not mine.
- **VLA fine-tune.** Needs 500+ teleop episodes we don't have.
- **Outdoor / noise tuning.** Environmental iteration, not code.

Roughly 60% of the original 4-week calendar was actually "physical work
disguised as code work." That's why parallel agents crushed weeks 1–2 but
can't touch weeks 3–4 without hardware.

## Key decisions still pending (human input)

- **Phone-as-brain:** Path A or defer? Week 3 pivots on this.
- **ESP32 firmware dump:** schedule it? Blocks Week 4.
- **Depth sensor:** per decisions doc, skip 2026. Confirm and close.
- **VLA strategy:** fine-tune vs stay prompt-engineered for the year.

## Month 2+ candidates

Unchanged from original roadmap; none scheduled until Week 3 path is
picked:

- Wheeled base swap (removes balance from the critical path)
- Better servos (STS3215 torque vs current STS3032)
- SmolVLA or π0 fine-tune on recorded data
- Multi-turn dialogue memory
- Outdoor test with full wake-word stack
