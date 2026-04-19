# Morning report — overnight robot build

You went to sleep saying "make it work." Here's what landed. All of it is on `main`, all of it is pushed, nothing is behind a paywall or requires manual setup from you.

## What now exists

| File | What it does |
|---|---|
| `demo/robot_daemon.py` | Long-running voice loop. Push-to-talk, Whisper Base on phone, rich keyword matcher, TTS ack, optional eyes + optional LLM fallback. |
| `demo/parse_intent.py` | TinyLlama (on phone) free-form → JSON wire command. Grammar-constrained. Usable for clean phrases. |
| `demo/eyes.py` | Phone screencap → YOLO-Fastest v2 → detections + overlay PNG. Laptop-side inference for v0. |
| `scripts/benchmark_moonshine.py` | Moonshine tiny/base benchmark against the existing test wavs. Laptop-side, CPU ONNX. |
| `logs/parse_intent_tests.log` | 8-phrase self-test of the intent parser. |
| `logs/moonshine-2026-04-19-032743.log` | Moonshine latencies + transcripts. |
| `logs/eyes-2026-04-19.log` | Vision sanity check. |

## How to run the robot right now

```bash
# robot is plugged in via USB, phone via ADB, mic on the laptop:
python3 demo/robot_daemon.py

# same but no hardware (type commands, see decisions, no USB or adb):
python3 demo/robot_daemon.py --mode text --dry-run

# with vision watchdog every 5 s in the background:
python3 demo/robot_daemon.py --with-eyes 5 --log logs/robot.log
```

Ctrl-C quits. "shut down" / "good bye" / "power off" also quits.

## What works and what didn't

### Worked

- **Daemon + matcher.** 14 test phrases, 14 correct decisions including synonyms ("tilt right", "halt", "hop", "are you ok", "stand up straight").
- **Whisper Base on phone.** Unchanged from before; still the default STT.
- **Eyes.** `person 0.94` with a correct bbox on a real bus.jpg frame. 7–38 ms laptop-side inference. Overlay PNG written to `/tmp/eyes-out.png`.
- **Moonshine on laptop (x86_64).** Sub-200 ms inference, sub-0.1 RTF on CPU. Correctly transcribed `cmd.wav` → "Lean left" with the base model.

### Didn't

- **TinyLlama Q4_0 intent parsing is unreliable.** On ambiguous inputs like "jump" / "tell me a joke" / "turn around", the grammar-constrained decoder collapses to the first schema option (`pose lean_left`). With temperature 0 + top-k 1 + a 1.1 B weights-only model, it's picking the highest-probability JSON path regardless of semantic fit.
- **Moonshine quality on short laptop-mic clips is shaky.** Tiny hallucinated "jacket" twice, base returned empty on one clip. x86 baseline is fine; don't swap Whisper for it on-phone until we WER-test a wider set of real robot-command clips.
- **Eyes runs on laptop, not phone.** The phone has `.param` files for YOLO-Fastest v2 but no `.bin` weights. The subagent pulled the matching weights into `/tmp/eyes-models/` and runs inference with the laptop's `ncnn` Python wheel. Porting to phone needs either (a) pushing the `.bin` weights + writing a small C++ detector shim, or (b) using an on-phone `ncnn-python` build. Acceptable for v0 since the phone is physically plugged into the laptop anyway.

## Recommended next moves (when you're up)

1. **Kick the tires on `robot_daemon.py`.** Say "lean left", watch the robot actually lean left. Then "start walking", "stop", "bow", "hop". Check the `heard:` vs `decision:` lines in the output — if Whisper mistranscribes and the matcher misses, that's a mic / SNR problem, not a matcher problem.
2. **Try `--with-eyes 3`** in a separate terminal while you're demoing. It just logs the top detection every 3 s; doesn't act yet. Next iteration: "if person within N px → auto-stop walking".
3. **Decide about TinyLlama.** Keep it as experimental fallback, or drop it and keep the matcher? My vote: drop it from the happy path, keep the script for when we move to Gemma 3 1B or a larger model where grammar-constrained output actually holds.
4. **Port Moonshine to phone (ARM64).** The `onnxruntime` ARM64 wheel works on Android via Termux. A 1-hour port. Then do a real WER bake-off against Whisper Base on `cmd*.wav`.
5. **Fix the IMU.** Still pinned to zero. Firmware work on the PhoneWalker side; the bench repo doesn't touch firmware.

## Checklists

### Things committed + pushed tonight

- `demo/robot_daemon.py` — new, the persistent loop.
- `demo/parse_intent.py` — new (subagent B).
- `demo/eyes.py` — new (subagent C).
- `scripts/benchmark_moonshine.py` — new (subagent A).
- `README.md` — "Run it without complications" section now describes the full robot + eyes + intent scripts, with limits called out.
- `logs/parse_intent_tests.log` — populated.
- `logs/moonshine-*.log`, `logs/eyes-*.log` — from subagents.

### Things not touched

- `brain/wire.py` (PhoneWalker repo, not this one)
- `pipeline_demo.py`, `simple_pose.py`, `voice_to_pose.py` — left alone, still work.
- ESP32 firmware. Still has the IMU-pinned-to-zero quirk.
- P20 Lite on-phone model files in `/data/local/tmp/` — unchanged.

Good morning.
