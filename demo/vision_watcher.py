#!/usr/bin/env python3
"""
vision_watcher.py - continuous vision layer for the robot.

Long-running watcher. Grabs frames via `adb exec-out screencap -p`, runs
YOLO-Fastest v2 laptop-side (reusing eyes.py's `load_net`/`infer`), and
emits one JSON-ish line per frame on stdout:

    tick   -> every frame, regardless of detections
    event  -> when --watch-for <class> has been present for >= --min-streak
              consecutive frames (once per entry; re-fires if streak breaks)

Lines are flushed immediately so a consumer daemon can pipe stdout.

Usage:
    python3 vision_watcher.py [--source phone|webcam] [--phone pixel6|p20]
                              [--interval 0.5] [--watch-for <class_name>]
                              [--threshold 0.5] [--min-streak 2]
                              [--output stdout|jsonl] [--log PATH]

Sources:
    phone  - adb exec-out screencap -p (default, ~2 FPS, backward-compatible)
    webcam - cv2.VideoCapture(N), laptop webcam (20+ FPS, --webcam-index N)

Ctrl-C exits cleanly.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2

# Reuse eyes.py's model loader + inference.  eyes.py lives in the same dir.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import eyes  # noqa: E402


PHONE_SERIALS = {
    "pixel6": "1B291FDF600260",
    "p20": "9WV4C18C11005454",
}

FRAME_PATH = Path("/tmp/_vision.png")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def adb_screencap(serial: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        r = subprocess.run(
            ["adb", "-s", serial, "exec-out", "screencap", "-p"],
            stdout=f,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    if r.returncode != 0 or out_path.stat().st_size < 1024:
        raise RuntimeError(
            f"adb screencap failed (rc={r.returncode}, "
            f"size={out_path.stat().st_size if out_path.exists() else 0}): "
            f"{r.stderr.decode(errors='replace')[:200]}"
        )


def emit(
    line_obj: dict,
    out_mode: str,
    log_fh,
) -> None:
    """Print one line (strict JSON for jsonl, prefixed for stdout), flush, log."""
    payload = json.dumps(line_obj, separators=(",", ":"))
    if out_mode == "jsonl":
        line = payload
    else:
        prefix = line_obj.get("t", "?").upper()
        line = f"[{prefix}] {payload}"
    print(line, flush=True)
    if log_fh is not None:
        log_fh.write(line + "\n")
        log_fh.flush()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Continuous YOLO vision watcher (streams ticks + class events)."
    )
    ap.add_argument("--source", choices=["phone", "webcam"], default="phone",
                    help="frame source (default phone = adb screencap)")
    ap.add_argument("--webcam-index", type=int, default=0,
                    help="/dev/videoN index for --source webcam (default 0)")
    ap.add_argument("--webcam-width", type=int, default=640,
                    help="requested webcam capture width (default 640)")
    ap.add_argument("--webcam-height", type=int, default=480,
                    help="requested webcam capture height (default 480)")
    ap.add_argument("--phone", choices=list(PHONE_SERIALS.keys()), default="pixel6")
    ap.add_argument("--interval", type=float, default=0.5,
                    help="seconds between frames (default 0.5 = ~2 FPS; "
                         "for --source webcam, consider 0.05 = ~20 FPS)")
    ap.add_argument("--watch-for", default=None,
                    help="COCO class name to emit events for (e.g. 'person')")
    ap.add_argument("--threshold", type=float, default=0.5,
                    help="minimum confidence to count a detection (default 0.5)")
    ap.add_argument("--min-streak", type=int, default=2,
                    help="min consecutive frames before emitting an event (default 2)")
    ap.add_argument("--output", choices=["stdout", "jsonl"], default="stdout",
                    help="stdout: human-prefixed; jsonl: strict JSON per line")
    ap.add_argument("--log", default=None, help="append ticks+events to this file")
    ap.add_argument("--max-seconds", type=float, default=None,
                    help="stop after N wall-clock seconds (smoke-test helper)")
    ap.add_argument("--fake-frame", default=None,
                    help="bypass adb; re-use this local image every frame "
                         "(offline smoke-test / replay mode)")
    args = ap.parse_args()

    serial = PHONE_SERIALS[args.phone]

    # Load YOLO once.
    try:
        net = eyes.load_net()
    except Exception as e:
        print(f"FATAL: load_net: {e}", file=sys.stderr)
        return 2

    log_fh = None
    if args.log:
        log_path = Path(args.log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", buffering=1)

    # Announce startup on stderr so pipes stay clean.
    print(
        f"# vision_watcher source={args.source} phone={args.phone} serial={serial} "
        f"interval={args.interval}s threshold={args.threshold} "
        f"watch_for={args.watch_for} min_streak={args.min_streak} "
        f"output={args.output}",
        file=sys.stderr,
        flush=True,
    )

    # Open webcam once if requested.  Kept local so phone path is untouched.
    cap = None
    if args.source == "webcam":
        cap = cv2.VideoCapture(args.webcam_index, cv2.CAP_V4L2)
        if not cap.isOpened():
            # Fall back to default backend.
            cap = cv2.VideoCapture(args.webcam_index)
        if not cap.isOpened():
            print(f"FATAL: cannot open /dev/video{args.webcam_index}",
                  file=sys.stderr, flush=True)
            return 3
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.webcam_width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.webcam_height)
        # Keep latency low: only buffer one frame if the backend supports it.
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        # Warm up: first few reads on some UVC cams return None.
        for _ in range(3):
            cap.read()
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        print(f"# webcam opened index={args.webcam_index} "
              f"size={actual_w}x{actual_h}",
              file=sys.stderr, flush=True)

    # Webcam reconnect helper — item 6. After 3 consecutive failed reads we
    # release the capture, sleep 2 s, and reopen with the same index.  Never
    # crashes the watcher on a transient USB camera disconnect.
    WEBCAM_FAIL_THRESHOLD = 3
    WEBCAM_RECONNECT_SLEEP = 2.0

    def _reopen_webcam(idx: int, w: int, h: int):
        c = cv2.VideoCapture(idx, cv2.CAP_V4L2)
        if not c.isOpened():
            c = cv2.VideoCapture(idx)
        if not c.isOpened():
            return None
        c.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        c.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        try:
            c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        for _ in range(3):
            c.read()  # warm-up frames
        return c

    webcam_fail_count = 0

    # Streak tracking per class name.  Resets to 0 if class isn't in current frame.
    streaks: dict[str, int] = {}
    fired: dict[str, bool] = {}  # avoid re-firing while streak continues

    # Clean Ctrl-C.
    stopping = {"v": False}

    def _stop(_sig, _frm):
        stopping["v"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    t_start = time.time()
    frames = 0
    try:
        while not stopping["v"]:
            loop_t0 = time.time()
            if args.source == "webcam":
                ok, img_bgr = (False, None)
                if cap is not None:
                    ok, img_bgr = cap.read()
                if not ok or img_bgr is None:
                    webcam_fail_count += 1
                    print(
                        f"WARN: webcam read failed "
                        f"(fail_count={webcam_fail_count}/"
                        f"{WEBCAM_FAIL_THRESHOLD})",
                        file=sys.stderr, flush=True,
                    )
                    if webcam_fail_count >= WEBCAM_FAIL_THRESHOLD:
                        print(
                            f"WARN: reconnecting webcam index="
                            f"{args.webcam_index} after "
                            f"{WEBCAM_RECONNECT_SLEEP:.0f} s",
                            file=sys.stderr, flush=True,
                        )
                        try:
                            if cap is not None:
                                cap.release()
                        except Exception:
                            pass
                        cap = None
                        time.sleep(WEBCAM_RECONNECT_SLEEP)
                        cap = _reopen_webcam(
                            args.webcam_index,
                            args.webcam_width,
                            args.webcam_height,
                        )
                        if cap is None:
                            print(
                                f"WARN: reconnect failed for /dev/video"
                                f"{args.webcam_index}; will retry",
                                file=sys.stderr, flush=True,
                            )
                        else:
                            print(
                                f"# webcam reconnected index="
                                f"{args.webcam_index}",
                                file=sys.stderr, flush=True,
                            )
                        # Reset counter whether we got a handle back or not;
                        # a still-failed open retries on the next loop.
                        webcam_fail_count = 0
                    time.sleep(args.interval)
                    continue
                webcam_fail_count = 0
            elif args.fake_frame:
                # Offline mode: simulate the adb-screencap latency so the
                # tick numbers remain realistic, then load the local image.
                try:
                    # Copy to FRAME_PATH to mimic the real path exactly.
                    import shutil
                    shutil.copyfile(args.fake_frame, FRAME_PATH)
                except Exception as e:
                    print(f"WARN: fake-frame copy failed: {e}",
                          file=sys.stderr, flush=True)
                    time.sleep(args.interval)
                    continue
                img_bgr = cv2.imread(str(FRAME_PATH))
                if img_bgr is None:
                    print(f"WARN: cv2.imread returned None for {FRAME_PATH}",
                          file=sys.stderr, flush=True)
                    time.sleep(args.interval)
                    continue
            else:
                try:
                    adb_screencap(serial, FRAME_PATH)
                except Exception as e:
                    print(f"WARN: screencap failed: {e}",
                          file=sys.stderr, flush=True)
                    time.sleep(args.interval)
                    continue
                img_bgr = cv2.imread(str(FRAME_PATH))
                if img_bgr is None:
                    print(f"WARN: cv2.imread returned None for {FRAME_PATH}",
                          file=sys.stderr, flush=True)
                    time.sleep(args.interval)
                    continue

            try:
                dets, infer_ms = eyes.infer(net, img_bgr)
            except Exception as e:
                print(f"WARN: infer failed: {e}", file=sys.stderr, flush=True)
                time.sleep(args.interval)
                continue

            # Filter by confidence threshold.
            kept = [d for d in dets if d.confidence >= args.threshold]

            # Update streaks.  For each class seen this frame, increment.
            seen_classes = {d.class_name for d in kept}
            for cls in list(streaks.keys()):
                if cls not in seen_classes:
                    streaks[cls] = 0
                    fired[cls] = False
            for cls in seen_classes:
                streaks[cls] = streaks.get(cls, 0) + 1

            total_ms = (time.time() - loop_t0) * 1000.0

            # Tick line every frame.
            emit(
                {
                    "t": "tick",
                    "detections": len(kept),
                    "latency_ms": round(total_ms, 1),
                    "infer_ms": round(infer_ms, 1),
                    "ts": iso_now(),
                },
                args.output,
                log_fh,
            )

            # Event: watch-for class present for >= min-streak.
            if args.watch_for is not None and args.watch_for in seen_classes:
                streak = streaks[args.watch_for]
                if streak >= args.min_streak and not fired.get(args.watch_for, False):
                    # Pick the best-conf detection of that class.
                    best = max(
                        (d for d in kept if d.class_name == args.watch_for),
                        key=lambda d: d.confidence,
                    )
                    emit(
                        {
                            "t": "event",
                            "class": best.class_name,
                            "conf": round(best.confidence, 3),
                            "bbox": [round(best.x, 1), round(best.y, 1),
                                     round(best.w, 1), round(best.h, 1)],
                            "streak": streak,
                            "ts": iso_now(),
                        },
                        args.output,
                        log_fh,
                    )
                    fired[args.watch_for] = True

            frames += 1
            if args.max_seconds is not None and (time.time() - t_start) >= args.max_seconds:
                break

            # Sleep the remainder of the interval (never negative).
            elapsed = time.time() - loop_t0
            remaining = args.interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
    finally:
        dt = time.time() - t_start
        print(
            f"# vision_watcher exiting: frames={frames} wall_s={dt:.1f} "
            f"fps={frames / dt if dt > 0 else 0:.2f}",
            file=sys.stderr,
            flush=True,
        )
        if log_fh is not None:
            log_fh.close()
        if cap is not None:
            cap.release()

    return 0


if __name__ == "__main__":
    sys.exit(main())
