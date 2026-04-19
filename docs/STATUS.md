# STATUS — where the robot is today

Single source of truth for what's working, what's still open, and how to run it. Replaces the scattered `demo/MORNING*.md` files (kept in the repo for git-archaeology but no longer authoritative).

## Run it

```bash
# full stack — webcam vision + neural TTS + API reasoning + real ESP32
scripts/run_robot.sh

# same, no hardware (typed input, no USB, no on-phone Whisper)
scripts/run_robot.sh --mode text --dry-run --no-tts

# hands-free — say "hey jarvis, <command>"
scripts/run_robot.sh --mode wake

# dashboard at http://localhost:5555 (run alongside the daemon)
python3 demo/web_ui.py
```

The launcher sources `~/Projects/AIHW/.env.local` for `DEEPINFRA_API_KEY`, defaults to webcam vision + Piper TTS + API LLM when the key is set, and writes a per-run log under `logs/robot-<timestamp>.log`.

## What's in the stack

| Layer | Default | Alternatives |
|---|---|---|
| Voice input | push-to-talk (Enter) | `--mode wake` (hey jarvis via OpenWakeWord, 31 ms trigger→record) |
| STT | Whisper Base on Pixel 6 | TFLite+XNNPack port available as 2.5× speedup — not yet shipped |
| Intent parsing | regex keyword matcher (instant, ~14 commands) | `--with-llm` falls through to DeepInfra Llama-3.1-8B (~1 s, 8/8 on tests) or on-phone Gemma 3 (~2 s server / ~25 s reload, 7/8) |
| Vision | laptop webcam at 10-20 FPS, YOLO-Fastest v2 | `--vision-source phone` (adb screencap, 2 FPS) |
| Behavior | `BehaviorEngine` state machine: idle/walking/paused/following | greet-once-per-class, walk-until-close-obstacle, emergency-stop, follow-me |
| Wire | USB CDC to ESP32 (persistent handle under lock) | n/a |
| TTS | Piper `en_US-lessac-low` (48-111 ms synth, natural voice) | `--tts espeak` (robotic, lightweight) / `--tts off` |
| Monitoring | startup self-test (ESP32 + adb + webcam + $DEEPINFRA_API_KEY) | web UI at `:5555` |

## Reliability features landed

- **USB recovery**: `send_wire()` caches the pyusb handle; on any error it drops the handle so the next call rediscovers. Survives ESP32 power-cycle / cable replug.
- **Vision supervisor**: if `vision_watcher.py` crashes, the daemon restarts it with 1/2/4/…/30 s backoff, gives up after 5 consecutive quick crashes.
- **Battery alert**: when ESP32 reports voltage < 6.5 V for 3 packets, daemon speaks "battery low"; re-arms once voltage recovers above 6.8 V. No spam.
- **LLM failure visibility**: when `parse_intent_api` fails, daemon logs `[llm] api call failed (<reason>)` instead of silent noop.
- **Log rotation**: `--log PATH` rotates at 10 MB.
- **Multi-phone safety**: `ANDROID_SERIAL` is seeded at startup; all subprocesses inherit it.
- **Webcam reconnect**: `vision_watcher` re-opens `cv2.VideoCapture` after 3 failed reads.
- **Wake-word self-trigger guard**: TTS speak() sets a `threading.Event` that mutes wake detection for the duration of the audio envelope.
- **Web dashboard STOP button**: pyusb-direct, works even when the daemon is hung.

## Accelerator story on Pixel 6 (Tensor G1 / Mali-G78)

Cross-built and measured both paths against CPU:

| Model | CPU (4T) | Mali-G78 Vulkan | Edge TPU (NNAPI) | verdict |
|---|---:|---:|---:|---|
| MobileNet v1 int8 | 11 ms | ~19 ms (slower) | **1.08 ms** | **TPU 10× faster** |
| EfficientNet-lite0 fp32 | 10 ms | — | 2.02 ms | TPU 5× faster |
| TinyLlama 1.1B tg | 24.9 t/s | 10.4 t/s (slower) | — | GPU loses |
| YOLO-Fastest v2 | 10 ms | 22 ms (slower) | 11 ms (fragments) | CPU wins |
| Whisper Tiny encoder | 505 ms (TFLite) / 1262 ms (ggml) | — | fragments, falls back | **TFLite CPU 2.5× over ggml** |

Headline: **Edge TPU is a real win for classifier-class backbones (10× measured).** Detector / transformer graphs fragment into partitions and NNAPI's Edge TPU firmware refuses them. Mali-G78 Vulkan loses across the board. The best untapped lever today is swapping whisper.cpp for TFLite+XNNPack on Pixel 6 — same CPU cores, 2.5× faster encoder.

## Open items

**Blocked:** Firmware IMU stays pinned at zero. Audit (`docs/FIRMWARE_IMU_AUDIT.md`) shows `w1ne/PhoneWalker` is **not** the running firmware — its `main.cpp` is a char pass-through CLI with zero IMU code, yet the live ESP32 emits JSON packets with an `imu` field. The actual firmware source is elsewhere; once located we can drop in a standard MPU-6050 driver with retry. Until then, the robot walks dead-reckoned.

**Next good investment (half a day):** Whisper TFLite on-phone swap for the measured 2.5× encoder speedup.

**Nice-to-haves:**
- Conversational memory (short history so "repeat that" works)
- First-time-setup guide for a cold-clone reader
- Long-duration walking stress test (hours of vision + motor, haven't been run)
- Wake-word sensitivity tuning in noisy environments (currently threshold 0.5)

## Security note

DeepInfra API key literal leaked to the public repo once, was purged from history via `git-filter-repo` and force-pushed, key was rotated. Current code reads only from `$DEEPINFRA_API_KEY`.
