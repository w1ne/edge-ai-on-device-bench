#!/usr/bin/env bash
# Run the full benchmark suite on an ADB-connected Android device.
# Writes a timestamped log to ./logs/<device>-<timestamp>.log
# Usage: ADB_DEVICE=<serial> ./run_full_suite.sh
#
# Prereq: push_assets.sh has been run.

set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DEV_FLAG=""
[ -n "${ADB_DEVICE:-}" ] && DEV_FLAG="-s $ADB_DEVICE"

MODEL="$(adb $DEV_FLAG shell 'getprop ro.product.model' | tr -d '\r' | tr ' ' '_')"
TS="$(date +%Y-%m-%d-%H%M%S)"
LOG="$REPO/logs/${MODEL}-${TS}.log"
mkdir -p "$REPO/logs"

REMOTE=/data/local/tmp

{
  echo "### edge-ai-on-device-bench run"
  echo "device: $MODEL"
  echo "timestamp: $TS"
  echo "cpu: $(adb $DEV_FLAG shell 'getprop ro.product.cpu.abi' | tr -d '\r')"
  echo "kernel: $(adb $DEV_FLAG shell 'uname -r' | tr -d '\r')"
  echo "memory:"
  adb $DEV_FLAG shell 'free -m | head -2'
  echo

  echo "### 1. Whisper Tiny (test.wav, 5s JFK sample)"
  adb $DEV_FLAG shell "cd $REMOTE && ./whisper-cli -m ggml-tiny.bin -f test.wav --no-timestamps -l en -t 8 2>&1" | tail -15
  echo

  echo "### 2. TinyLlama 1.1B Q4_0 (llama-bench, 8 threads)"
  adb $DEV_FLAG shell "cd $REMOTE && ./llama-bench -m tinyllama.gguf -t 8 -p 32 -n 32 2>&1" | tail -5
  echo

  echo "### 3. Gemma 3 1B Q4_0 (llama-bench, 8 threads)"
  adb $DEV_FLAG shell "cd $REMOTE && ./llama-bench -m gemma3.gguf -t 8 -p 32 -n 32 2>&1" | tail -5
  echo

  echo "### 4. SmolVLM-256M Q8_0 (llama-bench text-only decode throughput)"
  adb $DEV_FLAG shell "cd $REMOTE && ./llama-bench -m smolvlm.gguf -t 8 -p 16 -n 32 2>&1" | tail -5
  echo

  echo "### 5. SmolVLM-256M full multimodal (image + describe, 8 threads)"
  adb $DEV_FLAG shell "cd $REMOTE && ./llama-mtmd-cli -m smolvlm.gguf --mmproj smolvlm-mmproj.gguf --image test.png -p 'Describe this image in one word.' -n 20 -t 8 2>&1" | tail -10
  echo

  echo "### 6. NCNN full suite (CPU, 8 threads, 4 loops)"
  adb $DEV_FLAG shell "cd $REMOTE/ncnn-bench && $REMOTE/benchncnn 4 8 0 -1 0 2>&1"
  echo

  echo "### 7. Depth Anything V2 Small — FIXED weights, native 518×518, CPU 4 threads"
  adb $DEV_FLAG shell "cd $REMOTE/ncnn-bench && $REMOTE/benchncnn 4 4 0 -1 0 param=depth_v2_fixed.param shape=[518,518,3] 2>&1" | tail -5
  echo

  echo "### 8. RL locomotion (fixed softmax), CPU 4 threads"
  adb $DEV_FLAG shell "cd $REMOTE/ncnn-bench && $REMOTE/benchncnn 500 4 0 -1 0 param=locomotion_fixed.param shape=[48,1,1] 2>&1" | tail -3
  echo

  echo "### done"
} | tee "$LOG"

echo
echo "log saved: $LOG"
