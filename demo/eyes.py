#!/usr/bin/env python3
"""
eyes.py - v0 vision scaffold for the robot.

Grabs a frame (default: adb screencap from the connected phone; or --image PATH),
runs YOLO-Fastest v2 on the laptop via the `ncnn` Python wheel, prints detections,
and writes an overlay PNG to /tmp/eyes-out.png.

Rationale: real on-phone inference would need a C++ shim linked against NCNN.
Laptop-side inference proves the pipeline end-to-end for v0; we will port to
the phone later (the `.param/.bin` are the canonical artifacts - laptop copies
live under /tmp/eyes-models/).

Usage:
    python3 eyes.py                         # adb screencap -> inference
    python3 eyes.py --image foo.jpg         # local image
    python3 eyes.py --image foo.jpg --no-push   # skip adb push of frame

Exit code 0 on success. Non-zero if the model can't load or adb is unreachable.
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

# `ncnn` is only needed for the laptop-side detection path. The --on-phone-timing
# mode invokes benchncnn on-device via adb and does not import ncnn here.
try:
    import ncnn  # type: ignore
    _NCNN_IMPORT_ERR: Exception | None = None
except ImportError as _e:  # noqa: F841 - kept for the fail path below
    ncnn = None  # type: ignore
    _NCNN_IMPORT_ERR = _e


# --- paths -----------------------------------------------------------------

MODEL_DIR = Path("/tmp/eyes-models")
PARAM = MODEL_DIR / "yolo-fastestv2-opt.param"
BIN = MODEL_DIR / "yolo-fastestv2-opt.bin"

PHONE_EYE_PATH = "/data/local/tmp/eye.png"
OUT_PNG = Path("/tmp/eyes-out.png")

# --- on-phone inference config --------------------------------------------
# P20 Lite (Kirin 659, 8x A53). Serial is pinned to avoid any Pixel 6 crosstalk.
PHONE_SERIAL = "9WV4C18C11005454"
PHONE_BENCHNCNN = "/data/local/tmp/benchncnn"
PHONE_NCNN_DIR = "/data/local/tmp/ncnn-bench"
PHONE_PARAM = f"{PHONE_NCNN_DIR}/yolo-fastestv2-opt.param"
PHONE_BIN = f"{PHONE_NCNN_DIR}/yolo-fastestv2-opt.bin"

# --- model constants (YOLO-Fastest v2, 352 input, COCO-80) -----------------

INPUT_SIZE = 352
# Anchors (w,h pairs) for stride 16 and stride 32 heads, per dog-qiuqiu reference.
# Three anchors per head. Values are in input-image pixels.
ANCHORS = {
    16: [(12.64, 19.39), (37.88, 51.48), (55.71, 138.31)],
    32: [(126.91, 78.23), (131.57, 214.55), (279.92, 258.87)],
}
STRIDES = [16, 32]  # output blobs 794 and 796 respectively
OUT_BLOB = {16: "794", 32: "796"}

# COCO-80 class names.
COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
    "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
    "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
    "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
    "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
    "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
    "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
    "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
    "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
    "toothbrush",
]

CONF_THRESH = 0.30
IOU_THRESH = 0.45


# --- helpers ---------------------------------------------------------------


@dataclass
class Detection:
    class_id: int
    class_name: str
    confidence: float
    x: float
    y: float
    w: float
    h: float


def sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-x))


def adb_screencap(out_path: Path) -> None:
    """adb exec-out screencap -p > out_path."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        r = subprocess.run(
            ["adb", "exec-out", "screencap", "-p"],
            stdout=f,
            stderr=subprocess.PIPE,
            check=False,
        )
    if r.returncode != 0 or out_path.stat().st_size < 1024:
        raise RuntimeError(
            f"adb screencap failed (rc={r.returncode}, size={out_path.stat().st_size}): "
            f"{r.stderr.decode(errors='replace')[:200]}"
        )


def adb_push(local: Path, remote: str) -> None:
    r = subprocess.run(
        ["adb", "push", str(local), remote],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"adb push failed: {r.stderr[:200]}")


def on_phone_timing(loops: int = 4, threads: int = 4, powersave: int = 2) -> dict:
    """
    Run YOLO-Fastest v2 inference on the P20 Lite via benchncnn with the real
    .param file on-phone. Returns min/max/avg ms + raw stdout.

    Caveat: benchncnn loads weights via DataReaderFromEmpty (zeros), so the
    `.bin` isn't consulted for timing. We still push/verify the .bin because
    any future real detector shim needs it, and we verify the .param parses
    end-to-end through ncnn on the phone (proves the graph is loadable there).

    Graph shape 352x352x3 matches the dog-qiuqiu reference. Threads=8 uses all
    P20 Lite A53 cores; powersave=0 keeps the governor off the little cluster
    choice so we actually get 8-wide.
    """
    # Verify prerequisites on-phone.
    probe = subprocess.run(
        ["adb", "-s", PHONE_SERIAL, "shell",
         f"ls -la {PHONE_BENCHNCNN} {PHONE_PARAM} {PHONE_BIN} 2>&1"],
        capture_output=True, text=True, check=False,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"on-phone prereq probe failed: {probe.stderr[:200]}")
    if "No such file" in probe.stdout:
        raise RuntimeError(f"missing phone artifact:\n{probe.stdout}")

    cmd = [
        "adb", "-s", PHONE_SERIAL, "shell",
        f"cd {PHONE_NCNN_DIR} && {PHONE_BENCHNCNN} "
        f"{loops} {threads} {powersave} -1 0 "
        f"param=yolo-fastestv2-opt.param shape=[352,352,3] 2>&1",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"on-phone benchncnn failed: {r.stdout}\n{r.stderr}")

    # Parse the single "model  min = X  max = Y  avg = Z" line.
    tmin = tmax = tavg = None
    for line in r.stdout.splitlines():
        if "min =" in line and "avg =" in line:
            try:
                tmin = float(line.split("min =")[1].split()[0])
                tmax = float(line.split("max =")[1].split()[0])
                tavg = float(line.split("avg =")[1].split()[0])
            except (IndexError, ValueError):
                pass
            break

    return {
        "raw": r.stdout,
        "cmd": " ".join(cmd[3:]),
        "min_ms": tmin,
        "max_ms": tmax,
        "avg_ms": tavg,
        "probe": probe.stdout,
        "loops": loops,
        "threads": threads,
        "powersave": powersave,
    }


def load_net() -> "ncnn.Net":
    if ncnn is None:
        raise RuntimeError(
            f"laptop-side detection requires the `ncnn` Python wheel "
            f"(pip install --user ncnn); import error: {_NCNN_IMPORT_ERR}"
        )
    if not PARAM.exists() or not BIN.exists():
        raise FileNotFoundError(
            f"Missing model files. Expected {PARAM} and {BIN}. "
            f"Fetch them via:\n"
            f"  mkdir -p {MODEL_DIR} && cd {MODEL_DIR} && \\\n"
            f"  curl -fsSLO https://raw.githubusercontent.com/dog-qiuqiu/Yolo-FastestV2/"
            f"main/sample/ncnn/model/yolo-fastestv2-opt.param && \\\n"
            f"  curl -fsSLO https://raw.githubusercontent.com/dog-qiuqiu/Yolo-FastestV2/"
            f"main/sample/ncnn/model/yolo-fastestv2-opt.bin"
        )
    net = ncnn.Net()
    net.opt.use_vulkan_compute = False
    net.opt.num_threads = 4
    if net.load_param(str(PARAM)) != 0:
        raise RuntimeError("load_param failed")
    if net.load_model(str(BIN)) != 0:
        raise RuntimeError("load_model failed")
    return net


def _mat_to_np(mat: "ncnn.Mat") -> np.ndarray:
    """Convert ncnn.Mat (c, h, w) -> float32 numpy (c, h, w)."""
    return np.array(mat)


def _decode_head(feat: np.ndarray, stride: int) -> list[tuple[int, float, float, float, float, float]]:
    """
    Decode one YOLO-Fastest v2 head.

    feat: (h, w, 95) float32 with channels-last (final Permute 0=3 in the
    optimized .param reorders to (n, h, w, c)). Layout per grid cell:
        [0:12]  -> 4 box regs * 3 anchors, ALREADY sigmoid()-ed by upstream graph
                   (empirically values all in (0, 1))
        [12:15] -> objectness * 3 anchors, ALREADY sigmoid()-ed
        [15:95] -> 80-way class probabilities, ALREADY softmax()-ed
                   (the .param has Softmax_265/_270 applied pre-Concat)

    The anchors are SHARED across the class prob slot (class slot is
    anchor-agnostic in YOLO-Fastest v2).

    Returns (cls_id, conf, cx, cy, w, h) in INPUT_SIZE pixel coords.
    """
    h, w, c = feat.shape
    assert c == 95, f"expected 95 channels, got {c}"
    anchors = ANCHORS[stride]
    out: list[tuple[int, float, float, float, float, float]] = []

    # grid
    gy, gx = np.mgrid[0:h, 0:w].astype(np.float32)  # (h, w)

    cls_block = feat[:, :, 15:95]            # (h, w, 80)
    cls_id = cls_block.argmax(axis=-1)       # (h, w)
    cls_score = cls_block.max(axis=-1)       # (h, w)

    for a in range(3):
        aw, ah = anchors[a]
        # Values are already sigmoided upstream.
        bx = feat[:, :, a * 4 + 0]
        by = feat[:, :, a * 4 + 1]
        bw = feat[:, :, a * 4 + 2]
        bh = feat[:, :, a * 4 + 3]
        obj = feat[:, :, 12 + a]

        conf = obj * cls_score
        mask = conf > CONF_THRESH
        if not mask.any():
            continue

        # YOLO-Fastest v2 box decode (dog-qiuqiu reference):
        #   cx = (sigmoid(tx) * 2 - 0.5 + gx) * stride
        #   cy = (sigmoid(ty) * 2 - 0.5 + gy) * stride
        #   bw = (sigmoid(tw) * 2) ** 2 * anchor_w
        #   bh = (sigmoid(th) * 2) ** 2 * anchor_h
        cx = (bx * 2.0 - 0.5 + gx) * stride
        cy = (by * 2.0 - 0.5 + gy) * stride
        wv = (bw * 2.0) ** 2 * aw
        hv = (bh * 2.0) ** 2 * ah

        ii, jj = np.where(mask)
        for y_i, x_i in zip(ii, jj):
            out.append(
                (
                    int(cls_id[y_i, x_i]),
                    float(conf[y_i, x_i]),
                    float(cx[y_i, x_i]),
                    float(cy[y_i, x_i]),
                    float(wv[y_i, x_i]),
                    float(hv[y_i, x_i]),
                )
            )
    return out


def nms(dets: list[tuple[int, float, float, float, float, float]], iou_thr: float) -> list[int]:
    if not dets:
        return []
    # class-agnostic NMS is fine for v0
    boxes = np.array([[d[2] - d[4] / 2, d[3] - d[5] / 2, d[2] + d[4] / 2, d[3] + d[5] / 2] for d in dets])
    scores = np.array([d[1] for d in dets])
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        if order.size == 1:
            break
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        a_i = (boxes[i, 2] - boxes[i, 0]) * (boxes[i, 3] - boxes[i, 1])
        a_o = (boxes[order[1:], 2] - boxes[order[1:], 0]) * (boxes[order[1:], 3] - boxes[order[1:], 1])
        iou = inter / (a_i + a_o - inter + 1e-9)
        order = order[1:][iou <= iou_thr]
    return keep


def infer(net: ncnn.Net, img_bgr: np.ndarray) -> tuple[list[Detection], float]:
    H, W = img_bgr.shape[:2]
    mat = ncnn.Mat.from_pixels_resize(
        img_bgr, ncnn.Mat.PixelType.PIXEL_BGR, W, H, INPUT_SIZE, INPUT_SIZE
    )
    mat.substract_mean_normalize([0.0, 0.0, 0.0], [1 / 255.0, 1 / 255.0, 1 / 255.0])

    ex = net.create_extractor()
    ex.input("input.1", mat)

    t0 = time.time()
    raw: list[tuple[int, float, float, float, float, float]] = []
    for stride in STRIDES:
        ret, out = ex.extract(OUT_BLOB[stride])
        if ret != 0:
            raise RuntimeError(f"extract {OUT_BLOB[stride]} ret={ret}")
        feat = _mat_to_np(out)  # (95, h, w)
        raw.extend(_decode_head(feat, stride))
    latency_ms = (time.time() - t0) * 1000.0

    keep = nms(raw, IOU_THRESH)
    # map boxes from INPUT_SIZE coords back to original image coords
    sx = W / INPUT_SIZE
    sy = H / INPUT_SIZE
    out_dets: list[Detection] = []
    for i in keep:
        cid, conf, cx, cy, bw, bh = raw[i]
        x = (cx - bw / 2) * sx
        y = (cy - bh / 2) * sy
        w_ = bw * sx
        h_ = bh * sy
        out_dets.append(
            Detection(
                class_id=cid,
                class_name=COCO_NAMES[cid] if 0 <= cid < len(COCO_NAMES) else str(cid),
                confidence=conf,
                x=x, y=y, w=w_, h=h_,
            )
        )
    # strongest first
    out_dets.sort(key=lambda d: d.confidence, reverse=True)
    return out_dets, latency_ms


def draw_overlay(img_bgr: np.ndarray, dets: list[Detection]) -> np.ndarray:
    img = img_bgr.copy()
    for d in dets:
        p1 = (int(d.x), int(d.y))
        p2 = (int(d.x + d.w), int(d.y + d.h))
        cv2.rectangle(img, p1, p2, (0, 255, 0), 2)
        label = f"{d.class_name} {d.confidence:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (p1[0], p1[1] - th - 4), (p1[0] + tw + 4, p1[1]), (0, 255, 0), -1)
        cv2.putText(img, label, (p1[0] + 2, p1[1] - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return img


# --- main ------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Robot eyes v0 - YOLO-Fastest v2 on a single frame.")
    ap.add_argument("--image", help="local JPG/PNG instead of adb screencap", default=None)
    ap.add_argument("--no-push", action="store_true",
                    help="skip adb push of the captured frame to the phone")
    ap.add_argument("--out", default=str(OUT_PNG), help="overlay PNG path")
    ap.add_argument("--top", type=int, default=3, help="print only top-N detections")
    ap.add_argument("--on-phone-timing", action="store_true",
                    help="time YOLO-Fastest v2 on P20 Lite via benchncnn (no bbox "
                         "output; proves .param loads on-device and returns ms).")
    ap.add_argument("--on-phone-loops", type=int, default=4,
                    help="benchncnn loop_count when --on-phone-timing (default 4, "
                         "matches README config)")
    ap.add_argument("--on-phone-threads", type=int, default=4,
                    help="benchncnn num_threads (default 4; P20 has 8xA53 but the "
                         "README baseline and the stable min is at 4/powersave=2)")
    ap.add_argument("--on-phone-powersave", type=int, default=2,
                    help="0=all, 1=little, 2=big cluster (default 2)")
    args = ap.parse_args()

    if args.on_phone_timing:
        try:
            stats = on_phone_timing(
                loops=args.on_phone_loops,
                threads=args.on_phone_threads,
                powersave=args.on_phone_powersave,
            )
        except Exception as e:
            print(f"FATAL: on-phone timing failed: {e}", file=sys.stderr)
            return 6
        print(f"# device=P20Lite serial={PHONE_SERIAL}")
        print(f"# cmd={stats['cmd']}")
        print(f"# artifacts:\n{stats['probe'].rstrip()}")
        print(f"# raw_bench_output:\n{stats['raw'].rstrip()}")
        if stats['min_ms'] is not None:
            print(f"on_phone_min_ms={stats['min_ms']:.2f} "
                  f"on_phone_max_ms={stats['max_ms']:.2f} "
                  f"on_phone_avg_ms={stats['avg_ms']:.2f} "
                  f"loops={stats['loops']} threads={stats['threads']}")
        else:
            print("WARN: could not parse min/max/avg from benchncnn output",
                  file=sys.stderr)
            return 7
        return 0

    if args.image:
        img_path = Path(args.image)
        if not img_path.exists():
            print(f"FATAL: image not found: {img_path}", file=sys.stderr)
            return 2
        img_bgr = cv2.imread(str(img_path))
        source = f"file:{img_path}"
    else:
        tmp_png = Path("/tmp/eyes-screencap.png")
        try:
            adb_screencap(tmp_png)
        except Exception as e:
            print(f"FATAL: {e}", file=sys.stderr)
            return 3
        img_bgr = cv2.imread(str(tmp_png))
        source = f"screencap:{tmp_png}"
        if not args.no_push:
            try:
                adb_push(tmp_png, PHONE_EYE_PATH)
                print(f"# pushed frame -> {PHONE_EYE_PATH}")
            except Exception as e:
                print(f"WARN: adb push failed (non-fatal): {e}", file=sys.stderr)

    if img_bgr is None:
        print("FATAL: failed to read image", file=sys.stderr)
        return 4

    print(f"# source={source}  size={img_bgr.shape[1]}x{img_bgr.shape[0]}")

    try:
        net = load_net()
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 5

    dets, latency_ms = infer(net, img_bgr)
    print(f"# latency_ms={latency_ms:.2f}  detections={len(dets)}")
    print("# class_id class_name confidence x y w h")
    for d in dets[: args.top] if args.top > 0 else dets:
        print(f"{d.class_id} {d.class_name} {d.confidence:.3f} "
              f"{d.x:.1f} {d.y:.1f} {d.w:.1f} {d.h:.1f}")

    overlay = draw_overlay(img_bgr, dets)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), overlay)
    print(f"# overlay={out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
