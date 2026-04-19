# Morning 4 — hardware loop closed + real acceleration numbers

Second autonomous run (grocery-store / sports session). You connected the robot + Pixel 6; I spent the window on real-hardware tests, stack roast, and pushing TFLite / NNAPI to get honest Edge TPU numbers. Everything is on `main` at commit `16433a3`.

## Real hardware works, end-to-end

Tested with the ESP32 (voltage 72, temp 23-28 °C, IMU still pinned at zero):

```bash
scripts/run_robot.sh --mode text --no-tts
```

7-pose sweep (`neutral / lean_left / neutral / lean_right / neutral / bow_front / neutral`) all acked, servos landed at expected positions, no resource-busy errors. Vision in parallel ran at ~10 FPS (100 ticks in 10 s, 7-10 ms per frame). Walk + stop voice commands moved the robot and halted it cleanly.

## Fixed bugs exposed by the real hardware

1. **`USBError: Resource busy` after first wire write.** `send_wire()` re-claimed the ESP32's CDC interface on every call, and the ESP32-S3's USB stack refuses that on round 2. Now there's a persistent module-level `usb.core` handle under a `threading.Lock`, reopened only on error. Validated across 7 back-to-back commands.

2. **`adb` calls without `-s <serial>`** would collide whenever both phones were plugged in. `phone_transcribe()` now takes `--stt-phone {pixel6,p20}`; the daemon sets `ANDROID_SERIAL` at startup so subprocesses (`parse_intent*`, `vision_watcher --source phone`) inherit the right target.

3. **`--llm-api` always defaulted to `local`**, forcing users to remember `--llm-api api` to get the fast DeepInfra path. Now auto-detects: `api` if `$DEEPINFRA_API_KEY` is set, else `local`.

4. **Vision was gated by `--dry-run`.** It shouldn't be — vision is laptop-side, safe without ESP32. Dry-run now only stubs USB + on-phone Whisper.

## Real acceleration numbers on Pixel 6 (Edge TPU via NNAPI)

Cross-built the TFLite benchmark binary, downloaded / converted models, ran with `--use_nnapi=true --nnapi_accelerator_name=google-edgetpu`.

| Model | CPU (XNNPack 4T) | NNAPI / Edge TPU | speedup | TPU actually hit |
|---|---:|---:|---:|---|
| **MobileNet v1 224 int8** | 11.29 ms | **1.08 ms** | **10.5×** | yes (31/31 ops, 1 partition) |
| EfficientNet-lite0 int8 | 3.54 ms | 1.39 ms | 2.55× | yes (62/64 ops) |
| EfficientNet-lite0 fp32 | 10.19 ms | 2.02 ms | 5.03× | yes |
| EfficientDet-lite0 int8 | 9.21 ms | fallback | — | `TFLite_Detection_PostProcess` rejected |
| YOLO-Fastest v2 fp32 | 5.74 ms | 11.45 ms | 0.50× (slower) | partial, 28 partitions |
| Whisper Tiny (full) | 324 ms | rejected | — | dynamic-shape KV cache |
| Whisper Tiny (encoder only) | 505 ms | fallback | — | 18 partitions, driver `MISSED_DEADLINE_TRANSIENT` |

**So Edge TPU is a real 10× win for classifier backbones and a no-go for everything else on this SoC.** Detector / transformer graphs fragment into dozens of partitions and the Edge TPU firmware gives up; whisper-family + YOLO-family both hit this wall.

Free CPU win along the way: **TFLite+XNNPack runs the Whisper encoder in 505 ms vs whisper.cpp/ggml's 1262 ms — 2.5× faster on the same ARM cores.** No GPU or TPU involved. That's the one runtime swap that would meaningfully cut voice latency today.

All numbers + setup + verdicts: `logs/pixel6_nnapi_accelerated_2026-04-19.log` (~1000 lines), repro script at `scripts/tflite_bench.sh`.

## Security note (resolved)

During the DeepInfra wiring I briefly committed the API key literal. Caught it within minutes: removed from working tree, purged from every historical commit via `git-filter-repo`, force-pushed. You rotated the key (`AgEf…` now lives only in `~/Projects/AIHW/.env.local`). Repo code reads only from env.

## Not done, recommended next

- **Swap whisper.cpp for TFLite+XNNPack** in `robot_daemon.py` to capture the 2.5× encoder speedup. ~1 day of work: build TFLite runtime on phone, update `phone_transcribe()`.
- **Swap YOLO for a MobileNet-class feature extractor** if the carousel needs a "TPU-accelerated" vision line — MobileNet int8 hits 1 ms on the Edge TPU.
- **Firmware IMU fix** (pinned at zero). Plan is in `docs/FIRMWARE_TODO.md`; needs a pass in `w1ne/PhoneWalker`.
- **Real long-duration walk test** with obstacle avoidance. The infrastructure works in dry-run and short real runs; I didn't stress-test minutes of walking with vision-triggered stops.

## How to run the whole thing

```bash
# the one-liner (webcam vision, API LLM if the key is sourced, real ESP32)
scripts/run_robot.sh

# typed input, no USB (just for testing the matcher / behavior engine)
scripts/run_robot.sh --mode text --dry-run --no-tts
```

Both were exercised in this pass.
