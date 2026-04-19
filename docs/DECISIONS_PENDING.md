# DECISIONS_PENDING.md

Scope for the two underscoped items flagged at the end of `AGENT_ROADMAP.md`
(2026-04-19). Planner eval harness is being written by a parallel agent and is
deliberately omitted here.

---

## 1. Pipecat → Termux port

**Verdict (2026-04-19): feasible in ~3 focused days, but only by bypassing
Pipecat's default `LocalAudioTransport` (pyaudio/portaudio). Not structurally
blocked — blocked on a glue layer we have to write.**

### Why it's not trivial

- Pipecat's stock local path is `LocalAudioTransport` → `pyaudio` → `portaudio`.
  Termux has `portaudio` in `its-pointless`, but pyaudio wheels fail to compile
  against it reliably on aarch64 (open discussions going back to
  `termux/termux-packages#7349`, still not clean in 2026).
- Termux has no ALSA, no PulseAudio, no PipeWire. The only sanctioned mic path
  is `termux-microphone-record` (writes a WAV; not a stream), via the
  `termux-api` package plus the Termux:API companion APK.
- Pipecat itself is pure Python aside from its transport plugins — the
  frame-based pipeline, VAD, WhisperSTTService, and (our) shell-out Piper are
  fine on aarch64 Python 3.11 in Termux.

### Concrete path (what we actually write)

1. `pkg install python python-pip termux-api ffmpeg` + install the Termux:API
   APK; grant mic permission once. ~10 min.
2. `pip install pipecat-ai` without the `[local]` extra (skips pyaudio). Install
   `[whisper]` for `WhisperSTTService` (faster-whisper wheels exist for
   aarch64). ~30 min.
3. Write `TermuxAudioTransport` (~150 LOC) implementing Pipecat's
   `BaseTransport` / `BaseInputTransport` / `BaseOutputTransport`:
   - Input: shell out to `termux-microphone-record -l 0 -d -f /tmp/chunk.wav -r 16000 -c 1`
     in 200 ms rolling chunks, feed bytes into a Pipecat `AudioRawFrame` queue.
   - Output: accept `AudioRawFrame`s, concatenate to WAV, shell to
     `play-audio /tmp/reply.wav` (part of `termux-api`), or keep our existing
     `aplay`-style shim pointed at `pw-play` / Piper's `--output-raw` piped to
     `play-audio`.
4. Copy `TermuxSTT` trivially — we already have whisper-cli on the device;
   easier than fighting faster-whisper CUDA detection on Android.
5. Verify with the canonical Pipecat `02-llm-say-one-thing.py` example swapped
   to `TermuxAudioTransport`. If that echoes, we're done.

Nothing to copy wholesale; the `freecodecamp` "private voice assistant" and
`ayusc/termux-bot` projects both demonstrate the `termux-microphone-record` +
Whisper + local LLM loop on Termux, so the glue shape is de-risked — but neither
uses Pipecat's frame pipeline, so the transport subclass is ours to write.

### Risks

- **Latency**: `termux-microphone-record` is chunk-based, not streaming.
  Realistic first-word-in latency ~400-600 ms vs. ~150 ms with portaudio.
  Barge-in still works (VAD runs on the chunks), but it will feel laggier than
  the laptop path.
- **Piper on Termux**: already on the migration plan in `ARCHITECTURE.md` §3
  step 5. If Piper aarch64 binary misbehaves, fall back to Android
  `TextToSpeech` via a ~50-LOC Kotlin helper shelled from Termux.
- **USB I/O still needs the Kotlin bridge** (ARCHITECTURE.md §3 step 3). Pipecat
  port is independent of this — we can port voice first and keep pyusb over adb
  until the Kotlin app lands.
- **Pipecat churn**: 0.0.108+ is moving fast; pin the version we port against.
- **pyaudio temptation**: do not burn a day trying the `its-pointless` route.
  The `TermuxAudioTransport` is ~150 LOC and owns the problem cleanly.

---

## 2. Depth sensor

**Verdict (2026-04-19): skip depth hardware for all of 2026. Stay monocular
(Depth-Anything-V2). Do NOT add a manipulator. If W5+ pivots toward "pick up
that cup", buy an Intel RealSense D405 (~$235) — not an OAK-D, not a VL53L5CX.**

### Why skip

- Our robot is a 4× STS3032 quadruped with no arm. Depth is only
  decision-relevant for (a) obstacle avoidance at walking height and (b)
  manipulation. (a) is already handled well enough by monocular "near/far"
  from Depth-Anything-V2 @ 7.9 FPS on the Pixel 6 (`RESEARCH_AGENT_STACK.md`
  §6). (b) requires an arm we don't have.
- Adding a depth camera bulks the robot, competes with the ESP32 for USB
  bandwidth on the phone's single USB-C port (ARCHITECTURE.md §3 step 6 already
  flags this), and adds ~60 g + ~2 W on a platform that is power-constrained.

### If manipulation enters scope later: buy D405

- **D405 @ ~$235** (Intel store 2026-04). 7-50 cm ideal range; sub-mm accuracy
  at 7 cm. This is what Stretch 3 uses as its *wrist* camera and what LeRobot
  documents for SO-100/SO-101 tabletop manipulation. Matches our table-scale
  use case exactly.
- Connects over USB-C (USB 3.2 Gen 1). Requires librealsense; has Python
  bindings. On the laptop: plug-and-play. On the Pixel 6: librealsense does not
  run in Termux — depth would only work in the laptop-host configuration.
- **Integration estimate**: 2-3 days laptop-side; indefinite on Pixel 6 until
  someone ports librealsense to Android NDK (nobody has, as of search
  2026-04-19).

### Why not the alternatives

- **OAK-D Lite (~$150)**: on-board VPU is attractive, but the Myriad X depth is
  optimized for 1-10 m (drones, mobile bases). At tabletop range the D405 is
  empirically better (arXiv 2501.07421 empirical comparison). Our robot is
  table-scale.
- **VL53L5CX ($15, 8×8 ToF grid, I²C)**: cheap and cute. Useful as a
  cliff/obstacle sensor on the ESP32. **Not** a replacement for a depth camera
  — 64 pixels is not enough for any grasp pipeline (OK-Robot's VoxelMap
  assumes >200k points/frame). If we want cliff avoidance, solder one of these
  to the ESP32 front-facing — different decision, different budget line
  (~$20, 0.5 day firmware).
- **Pixel 6 onboard depth**: Pixel 6 has no dedicated ToF (Pixel 4 did, Pixel
  6/7/8 do not; Pro models only got LiDAR in... they didn't — iPhone Pro has
  LiDAR, Pixel Pro has none). ARCore's Depth API works on Pixel 6 via
  depth-from-motion, but (i) it needs the phone to physically translate for
  parallax, and our robot's gait is the phone's motion source — noisy, slow,
  and the API outputs are fused with monocular priors anyway. Not worth the
  complexity over just running Depth-Anything-V2.

### Hard boundary (what we cannot do without depth)

- No 6-DoF grasp pose estimation. No AnyGrasp-style pipelines. No
  OK-Robot-style VoxelMap with metric 3D coordinates.
- No "reach to exact cup position" — we can navigate *toward* it monocularly,
  but not grasp.
- No precise obstacle clearance < ~20 cm (monocular depth is ambiguous in
  absolute units; fine for "something is close", bad for "stop 8 cm before
  impact").

### Budget decision

- **2026 plan**: $0. Stay monocular. Revisit only if/when an arm is added.
- **Cliff sensor upgrade (optional, unrelated to "manipulation")**: $15 +
  0.5 day for a VL53L5CX on the ESP32, feeding a reflex stop. Could ship in
  a W3/W4 slot.
- **Manipulation pivot (hypothetical, not this month)**: $235 D405 + 3 days
  integration + arm hardware = separate project.
