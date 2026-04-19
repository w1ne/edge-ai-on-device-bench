# AGENT_ROADMAP.md

Reactive-robot → agent in four weeks. Today (2026-04-19): the daemon runs a regex intent matcher with an optional one-shot Llama fallback over a reactive `BehaviorEngine`. End state (2026-05-17): voice-in / tool-calling / spatial-memory loop that can accept "find the blue cup and come back" and replan when a primitive fails. Guiding principle: **ship agent behavior this month by gluing adopted libraries, not by fine-tuning a VLA**. Source ranking: `docs/RESEARCH_AGENT_STACK.md` §11 (13-15 days total). Calendar uses Sunday-Saturday weeks.

## Today (2026-04-19)

In flight right now (dispatched in parallel with this plan):
- `demo/voice_pipecat.py` — Pipecat pipeline (VAD + Whisper + Piper + barge-in).
- `demo/robot_planner.py` — LLM tool-calling loop around a `RobotTools` class wrapping existing wire cmds.

Already shipped this session (see `STATUS.md`): regex intent path, `BehaviorEngine` (idle/walking/paused/following), Piper TTS, USB recovery, battery alert, vision supervisor, wake-word self-trigger mute, web dashboard STOP, IMU verified live at 10 Hz, Whisper TFLite encoder path correctness-unblocked (wire pending), reliability audit doc.

## Week 1 (Apr 20 – Apr 26) — voice + planner wired

**Goal**: Pipecat replaces `arecord`+`whisper-cli`+Piper shell-out; planner loop replaces the regex+one-shot-Llama path behind a `--planner` flag.

| Commit title | What |
|---|---|
| Wire voice_pipecat.py into robot_daemon (--voice pipecat) | Daemon delegates mic→STT→TTS to Pipecat; keeps legacy path behind `--voice legacy` |
| Wire robot_planner.py into robot_daemon (--planner) | Planner replaces `parse_intent`; BehaviorEngine becomes one tool among many |
| Add RobotTools schema (pose/walk/stop/jump/look/say) | Thin wrapper returning `{success, observation, error}` dicts for tool-call replies |
| Fix barge-in vs wake-word self-trigger interaction | Pipecat mute event must respect existing OpenWakeWord gate |

**Success**: `scripts/run_robot.sh --planner --voice pipecat` handles "walk forward, then stop after 3 seconds" as a **two-tool plan** (walk + stop), not one intent. Barge-in during TTS cuts audio < 200 ms. `stress_test.py` runs 30 min with planner enabled, 0 daemon crashes, ≤ 5 re-plans per goal.
**Risks**: Pipecat's Piper plugin may not exist → fall back to our shell-out TTSService. Planner may loop if primitives don't return crisp observations — fix by forcing max 4 tool calls per user turn.
**Estimate**: 5 days (2 in flight today + 3 glue).

## Week 2 (Apr 27 – May 3) — 2D spatial memory

**Goal**: Planner sees "where I've been" and "what I last saw where."

| Commit title | What |
|---|---|
| Add occupancy_map.py (numpy 2D grid, 10 cm cells, 8×8 m) | Dead-reckoning pose from `walk` cmd duration + stride; IMU yaw integration |
| Add detection_log.py (ring buffer of last 32 YOLO hits w/ bearing) | Each hit: `{class, bearing_deg, distance_est, t}` from vision_watcher frames |
| Expose where_am_i / what_did_i_see tools to planner | Planner can query memory before re-exploring |
| Add web_ui /map endpoint rendering occupancy as PNG | Debug visibility; same Flask app |

**Success**: Starting from a known pose, after 5 min of `walk`+`stop`+`turn` the map shows a contiguous free-cell trail matching observed floor. "What did you see?" answers with the last 3 distinct classes and rough bearings.
**Risks**: Yaw drift without a magnetometer — accept it for now, reset pose on user command "you're at origin." No real distance estimate without depth fusion; use Depth V2 monocular confidence only as "near/far" tag.
**Estimate**: 2 days.

## Week 3 (May 4 – May 10) — reflex + scene caption

**Goal**: Robot stops walking off tables; planner gets rich scene descriptions instead of class IDs.

| Commit title | What |
|---|---|
| Add imu_reflex.py watchdog (pitch/roll > 20° → stop + speak) | Runs in daemon thread; pre-empts any in-flight tool call |
| Add reflex override log line + web UI badge | Visibility for the human watching |
| Add scene_caption.py (SmolVLM on "interesting frame" trigger) | Trigger = new class enters frame OR planner calls `describe_scene()` |
| Wire describe_scene tool into planner | Caption returned as observation text, not bbox list |

**Success**: Tilt robot to 25° by hand → `stop` wire cmd within 150 ms, TTS says "stopping, I'm tipping." `describe_scene` returns a ≤ 40-token sentence on the Pixel 6 in ≤ 3 s (SmolVLM already bench'd at this in `STATUS.md`).
**Risks**: SmolVLM cold-load is ~8 s on Pixel 6 — keep it resident or accept one-shot latency only on explicit ask. Reflex false positives when user picks robot up — add 1 s debounce.
**Estimate**: 3 days.

## Week 4 (May 11 – May 17) — LeRobot primitive adapter

**Goal**: Our primitives expose a LeRobot-compatible action interface so future π0/SmolVLA experiments can roll out on this hardware without a rewrite.

| Commit title | What |
|---|---|
| Add lerobot_adapter/ (robot config + action space) | Maps pose/walk/stop/jump into LeRobot's `RobotConfig` / `action` dict |
| Add recording script (teleop trace → LeRobotDataset parquet) | Uses existing wire protocol; logs IMU + detections as obs |
| Smoke-test: replay a recorded trace through the adapter | No learning yet — just prove the loop closes |

**Success**: 5-minute teleop session writes a valid LeRobotDataset folder. Replay reproduces the servo trajectory within ± 1 servo tick. `lerobot-eval` CLI at least loads our robot config without error.
**Risks**: **Blocked if firmware dump still pending** — no source means we can't expose the raw servo-angle channel LeRobot expects; must synthesize it from state packets. Fallback: ship only the action side, skip observation parquet until firmware is dumped. Research doc §7 already flags this.
**Estimate**: 3-5 days.

## After week 4

Month 2-3 candidates, not committed:
- Hardware: wheeled base swap (quadruped gait is the weakest link; a diff-drive base removes balance from the critical path). Better servos (STS3215 torque vs current STS3032).
- Fine-tune SmolVLA on ~1 k teleop episodes once the LeRobot adapter is ingesting data.
- Multi-turn dialogue memory (short summary rolled into planner system prompt).
- Outdoor test with wake-word + phone-brain migration (ARCHITECTURE.md §3 Week 2-4).

Decision checkpoints:
- **End of W2**: if planner+memory work on Llama-3.1-8B, stay. If replan count > 5/goal on clean tasks, upgrade to Llama-3.3-70B via DeepInfra.
- **End of W3**: if reflex + caption make the robot feel "aware," defer SLAM. If robot still gets lost in 3 min, invest in a real 2D SLAM (Nav2 or Cartographer-lite).
- **End of W4**: if LeRobot adapter lands clean, start a data-collection sprint. If firmware-dump still blocking, pivot to wheeled base (no legacy firmware).

## Key decisions still pending (human user input needed)

- **Phone-as-brain**: does the laptop-free runtime actually land in Month 2, or does the laptop stay as the runtime host? Affects whether we port Pipecat to Termux or keep it laptop-side.
- **ESP32 firmware**: dump it (BOOT-button reset, physical access, re-flash OSS) or keep the black box? Blocks LeRobot observation parquet and any closed-loop balance.
- **VLA strategy**: fine-tune a small VLA on our robot (needs ≥ 500 teleop episodes we don't have yet) vs stay prompt-engineered on Llama-tool-calling for the year? Affects whether W4 effort goes into data pipeline or planner polish.

## Underscoped items flagged from the research doc

Calling these out — parent agent may want to revise:
- **Depth sensor**: doc §6 concludes 2D memory is "sufficient" given Depth V2 monocular at 7.9 FPS. That's a judgment call; if we want to pick objects off a table later, monocular depth will not be enough. Adding this as a hardware decision checkpoint, not a week item.
- **Pipecat on Pixel 6**: adoption list assumes laptop-hosted Pipecat. Porting to Termux (ARCHITECTURE.md §3 step 1-2) is a separate ~3-day effort not in the 13-15 day count. Roadmap treats Pipecat as laptop-side for now.
- **Planner eval harness**: ranking has no "how do we measure planner quality" item. A small regression set (20 voice commands → expected tool-call sequences) is arguably a W1 prerequisite, not a W4 nice-to-have.
