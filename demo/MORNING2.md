# Morning 2 — Pixel 6 acceleration + P20 reliability

Second autonomous pass. Scope was: "reliable P20 stack" + "Pixel 6 with real
acceleration so we see the difference". Both done, honest numbers.

## Pixel 6 Vulkan (llama.cpp + ncnn)

Built both from source with Android NDK r26d + Vulkan-Headers + SPIRV-Headers,
target `arm64-v8a android-28`, `-DGGML_VULKAN=ON` / `-DNCNN_VULKAN=ON`.

**llama.cpp** at b8840 aborts on Mali-G78 with
`GGML_ASSERT(ctx->descriptor_set_idx < ctx->descriptor_sets.size()) failed` —
stock graph exhausts the pre-sized descriptor pool. Patched `ggml-vulkan.cpp`
to grow the pool on demand (6 lines added). Patch + build instructions saved
in `scripts/vulkan-patches/`. Post-patch, Vulkan runs end-to-end.

**ncnn** needed no patch. Its Vulkan backend handles Mali cleanly.

**Verdict** — on Pixel 6's Mali-G78, CPU beats GPU for every model in the
stack. TinyLlama tg64: 24.9 → 10.4 t/s. YOLO-Fastest v2: 10 → 22 ms. Depth
Anything V2 @ 256: 120 ms → 4.8 s (40× slower). Mali-G78 has no matrix cores,
32 KiB shared memory, subgroup size 16 — not hardware for quantized matvec
against a 2×X1 + 2×A76 + 4×A55 CPU running NEON.

The real acceleration on Pixel 6 is the Edge TPU via NNAPI, which needs
`.tflite` models. We don't have those today. Porting is the next budget
decision, not a 10-minute job.

Full logs:
- `logs/pixel6_vulkan_accelerated_2026-04-19.log` (llama.cpp)
- `logs/pixel6_ncnn_vulkan_2026-04-19.log` (ncnn)

## P20 Lite reliability

Everything that needs to run on the old phone runs. Peak RAM per pipeline stage
is ~720 MB (Gemma), 3.7 GB total RAM — plenty of headroom.

- **Intent parsing:** upgraded `demo/parse_intent.py` to accept
  `--model {tinyllama,gemma}`. Gemma 3 1B hits 7/8 on the test set (TinyLlama
  was 4/8). `robot_daemon.py --with-llm` defaults to Gemma now. Winning
  invocation is `-st --simple-io < /dev/null` (documented in
  `logs/parse_intent_tests.log`).
- **On-phone vision:** `demo/eyes.py --on-phone-timing` now works. Real YOLO
  weights (`yolo-fastestv2-opt.{param,bin}`) are pushed to
  `/data/local/tmp/ncnn-bench/` on both phones. Pixel 6 ~10 ms, P20 Lite
  ~92 ms (cold, 4 threads). Laptop-side bounding-box detection still required
  because benchncnn is a timer, not a detector — porting the detector shim
  to on-device needs either NDK cross-build of a tiny detector or the
  `ncnn` python wheel through Termux.
- **Reliability:** Whisper Base, TinyLlama, Gemma 3, SmolVLM, YOLO-Fastest v2,
  NanoDet-M, and the fixed RL locomotion policy all load and run cleanly.
  Depth Anything V2 still crashes on P20 (RAM / graph size) — documented in
  the README.

## What's committed

- `scripts/vulkan-patches/` — the Mali hotfix patch + build instructions.
- `logs/pixel6_vulkan_accelerated_*.log`, `logs/pixel6_ncnn_vulkan_*.log` —
  raw benchmark outputs, with verdict blocks.
- `logs/pixel6_llama_accel_*.log` — earlier recon showing the pre-existing
  phone binaries were CPU-only.
- `logs/parse_intent_tests.log` — TinyLlama + Gemma side-by-side on the 8
  test phrases.
- `logs/eyes_onphone_*.log` — on-phone YOLO timing with real weights.
- `demo/parse_intent.py`, `demo/eyes.py`, `demo/robot_daemon.py` — updates
  for `--model gemma`, `--on-phone-timing`, and the `--llm-model` flag.
- `README.md` — new "Pixel 6 GPU acceleration" section with the honest table.

## Nothing is standing on shakier ground than yesterday

Every number cited in the carousel still traces back to a raw log in this repo.
Pixel 6 is still the hero, P20 Lite is still the receipt, and the acceleration
line is now honest instead of speculative.
