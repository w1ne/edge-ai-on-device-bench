#!/usr/bin/env bash
# TFLite CPU-vs-NNAPI (Edge TPU) benchmark on Pixel 6 (Tensor G1).
#
# Run from laptop with the phone adb-connected.  Reproduces
# logs/pixel6_nnapi_accelerated_<date>.log.
#
# Prereqs (install once):
#   pip install --user onnx onnxsim onnxscript onnx2tf tensorflow ai_edge_litert torchsummary
# Android NDK lives at $HOME/android-ndk/android-ndk-r26d (not strictly
# needed here — we use the upstream prebuilt aarch64 benchmark_model binary).
#
# Hardware tested: Pixel 6 (serial 1B291FDF600260), Android 15 / SDK 35,
# Tensor G1, NNAPI feature level 7, Edge TPU accelerator exposed as
# "google-edgetpu" (confirmed via benchmark_model log:
#   INFO: NNAPI accelerators available: [google-edgetpu,nnapi-reference]).
#
# No external dependencies at runtime except adb and curl.

set -euo pipefail

REPO=${REPO:-$(cd "$(dirname "$0")/.." && pwd)}
MODELS_DIR=${MODELS_DIR:-/tmp/tflite-models}
PHONE_DIR=/data/local/tmp/tflite
LOG=${LOG:-$REPO/logs/pixel6_nnapi_accelerated_$(date +%F).log}

# ---------- Step 1: grab the prebuilt aarch64 benchmark_model --------------
# Upstream nightly build from TensorFlow Lite tools.
# URL is a stable redirect to the latest nightly native binary (~7 MB).
BM_URL="https://storage.googleapis.com/tensorflow-nightly-public/prod/tensorflow/release/lite/tools/nightly/latest/android_aarch64_benchmark_model"
mkdir -p "$MODELS_DIR"
if [ ! -f "$MODELS_DIR/benchmark_model" ]; then
  curl -sSL -o "$MODELS_DIR/benchmark_model" "$BM_URL"
fi

# ---------- Step 2: fetch prebuilt classifier + detector tflite -------------
fetch () { local name=$1 url=$2; [ -f "$MODELS_DIR/$name" ] || curl -sSL -o "$MODELS_DIR/$name" "$url"; }

# MobileNet V1 1.0 224 quant (int8) from Google Coral test_data
fetch mobilenet_v1_quant.tflite \
  "https://raw.githubusercontent.com/google-coral/test_data/master/mobilenet_v1_1.0_224_quant.tflite"
# EfficientNet-Lite0 classifier (int8 + fp32) from MediaPipe models bucket
fetch efficientnet_lite0_int8.tflite \
  "https://storage.googleapis.com/mediapipe-models/image_classifier/efficientnet_lite0/int8/1/efficientnet_lite0.tflite"
fetch efficientnet_lite0_fp32.tflite \
  "https://storage.googleapis.com/mediapipe-models/image_classifier/efficientnet_lite0/float32/1/efficientnet_lite0.tflite"
# EfficientDet-Lite0 detector (int8 + fp32) — has TFLite_Detection_PostProcess
# which NNAPI cannot compile; kept to document the failure mode.
fetch efficientdet_lite0_int8.tflite \
  "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/1/efficientdet_lite0.tflite"
fetch efficientdet_lite0_fp32.tflite \
  "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/float32/1/efficientdet_lite0.tflite"
# YOLOv8n detector from SpotLab (fp32)
fetch yolov8_det.tflite \
  "https://huggingface.co/SpotLab/YOLOv8Detection/resolve/main/tflite_model.tflite"

# ---------- Step 3: convert YOLO-FastestV2 ncnn/pytorch -> tflite -----------
# Upstream repo: https://github.com/dog-qiuqiu/Yolo-FastestV2
# Pytorch weights: modelzoo/coco2017-0.241078ap-model.pth  (shipped in the
# repo, ~975 KB).  Chain: pytorch -> onnx (pytorch2onnx.py) ->
# onnx2tf -> fp32/fp16/dynamic-range tflite.  Int8 PTQ fails on CONV_2D
# grouped-conv (input_channel % filter_input_channel != 0) – known onnx2tf
# issue for shufflenet-derived graphs.
if [ ! -f "$MODELS_DIR/yolo-fastestv2_float32.tflite" ]; then
  WORK=$(mktemp -d)
  git clone --depth=1 https://github.com/dog-qiuqiu/Yolo-FastestV2.git "$WORK/yfv2"
  cd "$WORK/yfv2"
  python3 pytorch2onnx.py \
      --data data/coco.data \
      --weights modelzoo/coco2017-0.241078ap-model.pth \
      --output yolo-fastestv2.onnx
  # onnx2tf tries to download a test image on startup; the URL returns a
  # non-pickle-safe payload. Monkey-patch the loader before importing.
  python3 - <<'PY'
import numpy as np
import onnx2tf.utils.common_functions as cf
import onnx2tf.onnx2tf as o2t
def _fake():
    return np.random.rand(20, 3, 112, 112).astype(np.float32)
cf.download_test_image_data = _fake
o2t.download_test_image_data = _fake
from onnx2tf import convert
convert(
    input_onnx_file_path="yolo-fastestv2.onnx",
    output_folder_path="yfv2_tflite",
    output_signaturedefs=True,
)
PY
  cp yfv2_tflite/yolo-fastestv2_float32.tflite "$MODELS_DIR/"
  cp yfv2_tflite/yolo-fastestv2_float16.tflite "$MODELS_DIR/"
  cp yolo-fastestv2.onnx                       "$MODELS_DIR/"
fi

# ---------- Step 4: push everything to the phone ---------------------------
adb shell "mkdir -p $PHONE_DIR"
adb push "$MODELS_DIR/benchmark_model"                         "$PHONE_DIR/"
adb shell "chmod 755 $PHONE_DIR/benchmark_model"
for f in mobilenet_v1_quant.tflite \
         efficientnet_lite0_int8.tflite efficientnet_lite0_fp32.tflite \
         efficientdet_lite0_int8.tflite efficientdet_lite0_fp32.tflite \
         yolov8_det.tflite \
         yolo-fastestv2_float32.tflite yolo-fastestv2_float16.tflite; do
  adb push "$MODELS_DIR/$f" "$PHONE_DIR/"
done

# ---------- Step 5: run the benchmarks -------------------------------------
mkdir -p "$(dirname "$LOG")"
{
  echo "=========================================="
  echo "Pixel 6 TFLite CPU-vs-NNAPI Benchmark"
  echo "Date: $(date -Iseconds)"
  echo "Device: $(adb shell getprop ro.product.model) / $(adb shell getprop ro.hardware) / SDK $(adb shell getprop ro.build.version.sdk)"
  echo "benchmark_model: $BM_URL"
  echo "=========================================="
} > "$LOG"

run () {
  local label=$1 model=$2 extra=$3
  echo "" | tee -a "$LOG"
  echo "### $label ###" | tee -a "$LOG"
  echo "CMD: benchmark_model --graph=$model --num_runs=50 --warmup_runs=5 $extra" | tee -a "$LOG"
  adb shell "$PHONE_DIR/benchmark_model --graph=$PHONE_DIR/$model --num_runs=50 --warmup_runs=5 $extra 2>&1 | grep -vE 'SL_ANeuralNetworksDiagnostic'" | tee -a "$LOG"
}

# Classifiers (clean full-graph delegation — best-case NNAPI wins)
run "mobilenet_v1_quant int8 CPU (4T)"            mobilenet_v1_quant.tflite           "--num_threads=4"
run "mobilenet_v1_quant int8 NNAPI edgetpu"       mobilenet_v1_quant.tflite           "--use_nnapi=true --nnapi_accelerator_name=google-edgetpu"
run "efficientnet_lite0 int8 CPU (4T)"            efficientnet_lite0_int8.tflite      "--num_threads=4"
run "efficientnet_lite0 int8 NNAPI edgetpu"       efficientnet_lite0_int8.tflite      "--use_nnapi=true --nnapi_accelerator_name=google-edgetpu"
run "efficientnet_lite0 fp32 CPU (4T)"            efficientnet_lite0_fp32.tflite      "--num_threads=4"
run "efficientnet_lite0 fp32 NNAPI edgetpu fp16"  efficientnet_lite0_fp32.tflite      "--use_nnapi=true --nnapi_accelerator_name=google-edgetpu --nnapi_allow_fp16=true"

# Detectors (partial fallback / PostProcess unsupported)
run "efficientdet_lite0 int8 CPU (4T)"            efficientdet_lite0_int8.tflite      "--num_threads=4"
run "efficientdet_lite0 int8 NNAPI edgetpu"       efficientdet_lite0_int8.tflite      "--use_nnapi=true --nnapi_accelerator_name=google-edgetpu"
run "yolov8_det fp32 CPU (4T)"                    yolov8_det.tflite                   "--num_threads=4"
run "yolov8_det fp32 NNAPI edgetpu fp16"          yolov8_det.tflite                   "--use_nnapi=true --nnapi_accelerator_name=google-edgetpu --nnapi_allow_fp16=true"
run "yolo-fastestv2 fp32 CPU (4T)"                yolo-fastestv2_float32.tflite       "--num_threads=4"
run "yolo-fastestv2 fp32 NNAPI edgetpu fp16"      yolo-fastestv2_float32.tflite       "--use_nnapi=true --nnapi_accelerator_name=google-edgetpu --nnapi_allow_fp16=true"
run "yolo-fastestv2 fp16 CPU (4T)"                yolo-fastestv2_float16.tflite       "--num_threads=4"
run "yolo-fastestv2 fp16 NNAPI edgetpu fp16"      yolo-fastestv2_float16.tflite       "--use_nnapi=true --nnapi_accelerator_name=google-edgetpu --nnapi_allow_fp16=true"

echo "Log written: $LOG"
