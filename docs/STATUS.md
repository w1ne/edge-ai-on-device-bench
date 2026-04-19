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
| STT | Whisper Base on Pixel 6 | TFLite encoder + laptop pytorch decoder — correct transcripts, 1.44× faster end-to-end when run in-process; subprocess wire in daemon still pending |
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
| Whisper Tiny encoder | 505 ms (TFLite) / 1262 ms (ggml) | — | fragments, falls back | **TFLite CPU 2.5× over ggml** (encoder-only compute; end-to-end transcribe now 1.44× faster laptop warm vs phone ggml — see Open items) |

Headline, honestly: **The Edge TPU's 10× win on MobileNet v1 int8 does not translate to any model the robot actually runs today.** YOLO-Fastest v2 fragments into partitions (TPU falls back to CPU and loses), Whisper-Tiny encoder fragments similarly, TinyLlama tg is CPU-only on NNAPI, Mali-G78 Vulkan loses every workload we measured. The TPU speedup is real for classifier backbones but is of no practical use to this robot until we port a relevant graph (SmolVLM? DINOv2 encoder?) that stays fully on-device. Until then, **our shipped acceleration story on Pixel 6 is: nothing accelerates the things we use.** The best realistic lever remains swapping whisper.cpp for TFLite+XNNPack on Pixel 6 — same CPU cores, 2.5× faster encoder.

## Open items

**IMU status update (2026-04-19 afternoon):** the "IMU pinned at zero" observation is obsolete. After a power cycle, the running firmware now emits live IMU data every 10 Hz state packet: `[-0.01, -0.01, 0.98, -4.3, -1.1, -0.1]` — gravity on Z (0.98 is exactly right for a horizontal chassis), small gyro bias. Every axis moves with natural noise floor (sd ~0.005-0.18). This confirms the earlier hypothesis: the firmware's one-shot `MPU.begin()` failed silently on boot once; power-cycling re-ran init successfully. The firmware is healthy — we still don't have its source, but we don't urgently need to replace it. The only remaining fragility is that IMU can die silently if init fails; mitigation is a periodic voltage-brownout power-cycle, which the user does when needed.

Separately, the source audit (`docs/FIRMWARE_IMU_AUDIT.md`) still stands: `w1ne/PhoneWalker` is NOT the running firmware — its `main.cpp` is a char pass-through CLI with zero IMU code. The actual firmware binary lives only on the ESP32's flash. Dumping it requires the BOOT button held during reset (physical access). Noting for future reference; not blocking anything today.

**Firmware command surface, verified on live hardware:**
- `{"c":"ping"}` → `{"t":"ack","c":"ping","ok":true}` + 10 Hz state stream
- `{"c":"pose","n":<neutral|lean_left|lean_right|bow_front>,"d":<ms>}` → ack + servo motion
- `{"c":"walk","on":true,"stride":150,"step":400}` → ack, starts walking
- `{"c":"stop"}` → ack, halts motion
- `{"c":"jump"}` → ack, jump motion
- Unknown commands → `{"t":"err","msg":"unknown cmd"}` (proper error path exists)
- State packet fields: `p` (4 servo positions), `v` (voltage x10), `tmp` (temp °C), `ms` (uptime), `imu` (6 floats: ax/ay/az in g, gx/gy/gz in deg/s)

**Whisper TFLite — correctness unblocked, end-to-end wire still pending (2026-04-19 evening):** the runner at `scripts/whisper_tflite_runner.py` now defaults to the encoder-only TFLite graph (`whisper_enc_tiny_en_fp32.tflite`, 33 MB) fed into openai-whisper's pytorch decoder (`DecodingTask` with `_get_audio_features` patched to skip the pytorch encoder). It produces correct English transcripts that match whisper.cpp modulo punctuation. 5-run comparison on `utter_16k.wav`:

| Path | Mean wall | Transcript |
|---|---:|---|
| whisper.cpp, Pixel 6, ggml-tiny, 8T (adb) | 1.80 s | "Walk forward now, please." |
| TFLite-enc + pytorch-dec, laptop, **warm in-process** | 1.25 s | "Walk forward now, please stop." |
| TFLite-enc + pytorch-dec, laptop, **cold subprocess** | 5.16 s | "Walk forward now, please stop." |

Warm in-process beats the phone baseline by **1.44×**. Cold subprocess loses by 2.87× because Python + torch + tf import takes ~2.2 s per call and the pytorch model loads in another ~0.7 s. The daemon currently invokes the runner via `subprocess.run` per utterance, which is why the win isn't captured end-to-end yet. The minimal wire change is to `import whisper_tflite_runner` in `demo/robot_daemon.py` and call `run_encoder_only()` directly instead of shelling out — one import, ~10 lines. Left as a follow-up to preserve the "don't touch robot_daemon.py" constraint. Full test log: `logs/whisper_tflite_encoder_only_2026-04-19.log`. Legacy `--model-mode full` is kept on the runner for regression reference (the nyadla-sys graph still has its broken baked-in decoder).

**Nice-to-haves:**
- Conversational memory (short history so "repeat that" works)
- First-time-setup guide for a cold-clone reader
- Long-duration walking stress test (hours of vision + motor, haven't been run)
- Wake-word sensitivity tuning in noisy environments (currently threshold 0.5)

## Security note

DeepInfra API key literal leaked to the public repo once, was purged from history via `git-filter-repo` and force-pushed, key was rotated. Current code reads only from `$DEEPINFRA_API_KEY`.
