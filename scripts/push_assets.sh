#!/usr/bin/env bash
# Push the binaries + model weights to /data/local/tmp on an ADB-connected Android device.
# Assumes binaries live under ./bin and weights under ./weights (download separately — large files
# are not checked into this repo).
#
# Usage: ADB_DEVICE=<serial> ./push_assets.sh

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV_FLAG=""
[ -n "${ADB_DEVICE:-}" ] && DEV_FLAG="-s $ADB_DEVICE"

REMOTE=/data/local/tmp

echo "=== target device ==="
adb $DEV_FLAG devices -l | head -5
adb $DEV_FLAG shell "getprop ro.product.model; getprop ro.product.cpu.abi"

echo "=== mkdir remote ==="
adb $DEV_FLAG shell "mkdir -p $REMOTE/ncnn-bench"

echo "=== push binaries ==="
for bin in llama-cli llama-bench llama-mtmd-cli whisper-cli benchncnn; do
  if [ -f "$REPO/bin/$bin" ]; then
    adb $DEV_FLAG push "$REPO/bin/$bin" "$REMOTE/$bin"
    adb $DEV_FLAG shell "chmod +x $REMOTE/$bin"
  else
    echo "WARNING: $REPO/bin/$bin missing — download prebuilt ARM64 from upstream"
  fi
done

echo "=== push weights ==="
for f in tinyllama.gguf gemma3.gguf smolvlm.gguf smolvlm-mmproj.gguf ggml-tiny.bin ggml-base.en.bin test.wav test.png; do
  if [ -f "$REPO/weights/$f" ]; then
    adb $DEV_FLAG push "$REPO/weights/$f" "$REMOTE/$f"
  else
    echo "WARNING: $REPO/weights/$f missing"
  fi
done

echo "=== push ncnn-bench assets ==="
if [ -d "$REPO/weights/ncnn-bench" ]; then
  adb $DEV_FLAG push "$REPO/weights/ncnn-bench/." "$REMOTE/ncnn-bench/"
fi

echo "=== push fixed models (this repo) ==="
adb $DEV_FLAG push "$REPO/models-fixed/depth_v2_small.ncnn.param" "$REMOTE/ncnn-bench/depth_v2_fixed.param"
adb $DEV_FLAG push "$REPO/models-fixed/depth_v2_small.ncnn.bin"   "$REMOTE/ncnn-bench/depth_v2_fixed.bin"
adb $DEV_FLAG push "$REPO/models-fixed/locomotion_policy.param"   "$REMOTE/ncnn-bench/locomotion_fixed.param"
adb $DEV_FLAG push "$REPO/models-fixed/locomotion_policy.bin"     "$REMOTE/ncnn-bench/locomotion_fixed.bin"

echo "=== done ==="
