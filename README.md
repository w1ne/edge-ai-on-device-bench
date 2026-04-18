# edge-ai-on-device-bench

Real on-device AI benchmarks for 8 models run on commodity smartphones — a 2018 Huawei P20 Lite (~$30 secondhand) and a 2021 Pixel 6 (~$200 secondhand). Everything measured live over ADB, no cloud, no GPU cheating (CPU-only unless noted).

This repo exists so every number publicly cited elsewhere can be traced back to a raw `benchncnn` / `llama-bench` / `whisper-cli` log with a timestamp and the exact command that produced it.

## Devices

| Device | SoC | Cores | RAM | Notes |
|---|---|---|---|---|
| Huawei P20 Lite (ANE-LX1) | Kirin 659 | 8× Cortex-A53 | 3.7 GB | Android 9, released 2018 |
| Google Pixel 6 (oriole) | Google Tensor G1 | 2×X1 + 2×A76 + 4×A55 | 7.6 GB | Android 14, released 2021 |

## Models

1. **Whisper Tiny** (77 MB `ggml-tiny.bin`) — speech → text
2. **TinyLlama 1.1B** Q4_0 (607 MB `.gguf`) — LLM
3. **Gemma 3 1B** Q4_0 (681 MB `.gguf`) — LLM
4. **SmolVLM-256M** Q8_0 (175 MB `.gguf` + 190 MB mmproj) — vision + language
5. **YOLO-Fastest v2** (ncnn) — object detection
6. **NanoDet-M** (ncnn) — small-object detection
7. **Depth Anything V2 Small** (ncnn, regenerated — see `models-fixed/`) — monocular depth
8. **RL locomotion policy** (ncnn MLP, fixed softmax — see `models-fixed/`) — motor control

Raw runtimes: `llama.cpp`, `whisper.cpp`, `NCNN benchncnn`.

## Headline numbers

| Model | Metric | P20 Lite | Pixel 6 |
|---|---|---|---|
| Whisper Tiny | real-time factor (5 s audio) | 0.92× RT | **0.52× RT** |
| TinyLlama 1.1B | token generation | 5.39 t/s | **20.61 t/s** |
| Gemma 3 1B | token generation | 4.10 t/s | **16.93 t/s** |
| SmolVLM-256M | token generation (decode) | 12.97 t/s | **26.85 t/s** |
| SmolVLM full image+caption | end-to-end (total) | 18.4 s | **4.7 s** |
| YOLO-Fastest v2 | detection throughput | 24 FPS (41 ms) | **149 FPS (6.7 ms)** |
| NanoDet-M | detection throughput | 12 FPS (85 ms) | **80 FPS (12.5 ms)** |
| Depth Anything V2 Small (fixed, 518×518, 4thr) | depth map throughput | ❌ crash | **1.46 FPS (687 ms)** |
| RL locomotion (fixed) | policy inference | n/a | **~20 kHz avg, 100 kHz peak** |

## What had to be fixed before numbers were measurable

Two of the eight models shipped with broken artifacts that prevented them from running on either device:

- **`depth_anything_v2_small.bin`** — on disk it was 99 KB. The real model is ~50 MB. It was a stub file. A new weight file was regenerated from the official `depth-anything/Depth-Anything-V2-Small-hf` checkpoint on HuggingFace, traced through TorchScript, converted to NCNN with `pnnx`. See `scripts/rebuild_depth.py` + `models-fixed/depth_v2_small.ncnn.param` + `.bin`.
- **`locomotion_policy.param`** — `Softmax 0=1` parameter format is pre-2021 NCNN ABI. Modern NCNN rejects it with *"layer load_param failed"* → *"network graph not ready"*. Fixed by updating to `Softmax 0=0 1=1` (axis=0, fixbug=1). `.bin` file was already correct. See `models-fixed/locomotion_policy.param`.

Both fixes land on both devices identically.

## Per-log reproducibility

Every number above corresponds to a file under `logs/` — with timestamp, device id, and the exact command. If a future reader wants to reproduce:

1. Read `scripts/push_assets.sh` — pushes binaries + models to `/data/local/tmp/` on an ADB-connected Android device.
2. Run `scripts/run_full_suite.sh` — produces `logs/<device>-<timestamp>.log`.
3. Parse with `scripts/extract_table.py` — emits the markdown table in this README.

## Scope

This repo benchmarks *models* on *phones*. It does not:
- Build an integrated voice-to-motion pipeline (that's application work, not benchmark work).
- Contain the walking-robot platform. That lives in a separate repo.
- Promote any specific product.

## License

MIT.
