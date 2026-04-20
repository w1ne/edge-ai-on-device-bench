#!/data/data/com.termux/files/usr/bin/env python3
"""phone_vision.py — Termux-side camera + VLM "does the robot see X?".

Runs inside Termux on the Pixel 6. Consumed by scripts/termux/phone_daemon.py
as:

    from phone_vision import CameraQuery
    cq = CameraQuery(logger=print)
    res = cq.query(["a person", "a laptop", "a red mug"], threshold=0.2)
    # res => {"seen": ["a person"],
    #        "scores": {"a person": 0.95, "a laptop": 0.0, "a red mug": 0.0},
    #        "frame_ms": 520, "error": None}

Capture path: termux-camera-photo -c <id> <path> (requires com.termux.api +
CAMERA permission granted; `pm grant com.termux.api android.permission.CAMERA`
works on F-Droid builds, else a one-time Android dialog on first call).

Scoring path (chosen): hosted VLM via DeepInfra (Llama-3.2-11B-Vision). Why:
 - ONNX Runtime has no Termux Python 3.13 aarch64 wheel
 - Termux $HOME is inaccessible from adb shell (SELinux), so pushing large
   .onnx weights and warming them is brittle
 - The phone already has DeepInfra creds + urllib works out of the box
 - ~4-6 s wall per 3-phrase query vs. unknown ONNX CLIP latency on Cortex

Override to a different model with $PHONE_VISION_MODEL.

API-key lookup matches phone_intent.py: $DEEPINFRA_API_KEY -> $DIA_KEY_FILE
-> ~/.dia_key (mode 0600).

CLI self-test:
    python3 phone_vision.py --query "a person" "a laptop" "a red mug"

Drops a JSON report at $PHONE_VISION_FLAG (default
/sdcard/Download/edge-ai-phone/vision_test.flag) for adb-side verification.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from PIL import Image  # type: ignore
    _PIL_OK = True
except ImportError:
    _PIL_OK = False

HOME = Path(os.environ.get("HOME", str(Path.home())))

DEFAULT_MODEL = os.environ.get(
    "PHONE_VISION_MODEL", "meta-llama/Llama-3.2-11B-Vision-Instruct"
)
DEFAULT_URL = os.environ.get(
    "PHONE_VISION_URL", "https://api.deepinfra.com/v1/openai/chat/completions"
)
# Camera shot timeout: termux-camera-photo can stall if another app holds
# the camera HAL. 8 s is generous.
CAM_TIMEOUT_S = float(os.environ.get("PHONE_VISION_CAM_TIMEOUT", "8"))
API_TIMEOUT_S = float(os.environ.get("PHONE_VISION_API_TIMEOUT", "25"))
# Frame scratch file (on-device).
DEFAULT_FRAME = os.environ.get(
    "PHONE_VISION_FRAME",
    "/data/data/com.termux/files/usr/tmp/phone_vision_last.jpg",
)
# Default external flag path so adb can read results (same pattern as
# phone_smoke.flag / bootstrap_ok.flag).
DEFAULT_FLAG = os.environ.get(
    "PHONE_VISION_FLAG", "/sdcard/Download/edge-ai-phone/vision_test.flag"
)


def _default_logger(msg: str) -> None:
    print(f"[phone_vision {time.strftime('%H:%M:%S')}] {msg}",
          file=sys.stderr, flush=True)


def _load_key() -> str:
    """Same order as phone_intent.py."""
    k = (os.environ.get("DEEPINFRA_API_KEY") or "").strip()
    if k:
        return k
    path = os.environ.get("DIA_KEY_FILE") or str(HOME / ".dia_key")
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# --------------------------------------------------------------- JSON parsing
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _extract_scores(raw: str, phrases: list[str]) -> dict[str, float]:
    """VLM often wraps JSON in prose or markdown fences. Pull the first
    balanced object. If that fails, regex-scan per-phrase."""
    raw = (raw or "").strip()
    # Strip markdown fences.
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```\s*$", "", raw)
    # Try direct parse first.
    for candidate in (raw, *_iter_balanced_objects(raw)):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return _coerce_scores(obj, phrases)
    # Fallback: regex phrase -> number.
    out: dict[str, float] = {}
    for p in phrases:
        m = re.search(
            r'"' + re.escape(p) + r'"\s*:\s*([0-9]*\.?[0-9]+)', raw
        )
        if m:
            try:
                out[p] = max(0.0, min(1.0, float(m.group(1))))
            except ValueError:
                pass
    return out


def _iter_balanced_objects(text: str):
    """Yield top-level balanced JSON objects in text order."""
    i = text.find("{")
    while i != -1:
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i:j + 1]
                        break
        i = text.find("{", i + 1)


def _coerce_scores(obj: dict, phrases: list[str]) -> dict[str, float]:
    """Normalise a dict from the VLM to {phrase: float in [0,1]}."""
    out: dict[str, float] = {}
    # Accept both {phrase: 0.9} and {"scores": {phrase: 0.9}}.
    if "scores" in obj and isinstance(obj["scores"], dict):
        obj = obj["scores"]
    for p in phrases:
        v = obj.get(p)
        if v is None:
            # Case-insensitive fallback.
            for k, vv in obj.items():
                if isinstance(k, str) and k.strip().lower() == p.strip().lower():
                    v = vv
                    break
        if v is None:
            continue
        if isinstance(v, bool):
            out[p] = 1.0 if v else 0.0
        elif isinstance(v, (int, float)):
            out[p] = max(0.0, min(1.0, float(v)))
        elif isinstance(v, str):
            s = v.strip().lower()
            if s in ("yes", "true", "y"):
                out[p] = 1.0
            elif s in ("no", "false", "n"):
                out[p] = 0.0
            else:
                try:
                    out[p] = max(0.0, min(1.0, float(s)))
                except ValueError:
                    pass
    return out


# --------------------------------------------------------------- CameraQuery
class CameraQuery:
    """Phone-camera + hosted-VLM phrase scorer.

    Not thread-safe. One instance per daemon loop is fine; query() is
    synchronous (~4-6 s on Pixel 6 for 3 phrases)."""

    def __init__(self, logger=None, camera_id: int = 0,
                 model: str | None = None, url: str | None = None,
                 frame_path: str | None = None,
                 compress: bool = True,
                 max_dim: int = 512,
                 jpeg_quality: int = 70):
        self._log = logger or _default_logger
        self.camera_id = int(camera_id)
        self.model = model or DEFAULT_MODEL
        self.url = url or DEFAULT_URL
        self.frame_path = frame_path or DEFAULT_FRAME
        self.compress = bool(compress)
        self.max_dim = int(max_dim)
        self.jpeg_quality = int(jpeg_quality)
        self._cam_bin = shutil.which("termux-camera-photo")
        self._closed = False
        self._pil_warned = False
        if self.compress and not _PIL_OK:
            self._log("Pillow not installed; sending full-res JPEG "
                      "(pkg install python-pillow libjpeg-turbo to enable compression)")
            self._pil_warned = True
        try:
            Path(self.frame_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # -- low-level --------------------------------------------------------
    def capture_jpeg(self, out_path: str) -> bool:
        """Grab one frame. Returns True if a non-empty JPEG lands at out_path."""
        if self._closed:
            return False
        if not self._cam_bin:
            self._log("termux-camera-photo not on PATH (pkg install termux-api)")
            return False
        try:
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        # Remove stale file so size check is meaningful.
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        cmd = [self._cam_bin, "-c", str(self.camera_id), out_path]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=CAM_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            self._log(f"termux-camera-photo timed out after {CAM_TIMEOUT_S}s")
            return False
        except OSError as e:
            self._log(f"termux-camera-photo exec failed: {e}")
            return False
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0 and stderr:
            self._log(f"termux-camera-photo rc={proc.returncode} {stderr[:200]}")
        # termux-camera-photo sometimes prints an error to stdout ("No such
        # camera", "CAMERA permission not granted") and still rc=0.
        stdout = (proc.stdout or "").strip()
        if stdout:
            low = stdout.lower()
            if "permission" in low or "denied" in low or "not granted" in low:
                self._log(f"camera denied: {stdout[:200]}")
                return False
            if "no such" in low or "error" in low:
                self._log(f"camera error: {stdout[:200]}")
                return False
        try:
            sz = os.path.getsize(out_path)
        except OSError:
            return False
        if sz < 1024:
            self._log(f"camera produced {sz} bytes at {out_path} (too small)")
            return False
        return True

    # -- api --------------------------------------------------------------
    def _diagnose_cam_error(self, proc_stdout: str = "") -> str:
        """Pick the most likely reason capture_jpeg returned False."""
        if not self._cam_bin:
            return "cam_missing"
        low = (proc_stdout or "").lower()
        if "permission" in low or "denied" in low or "not granted" in low:
            return "cam_denied"
        return "cam_busy"

    def query(self, phrases: list[str], threshold: float = 0.20) -> dict:
        """Capture a fresh frame and score each phrase.

        Returns:
            {"seen": [<phrase>...], "scores": {phrase: prob},
             "frame_ms": int, "error": str | None}
        """
        phrases = [p.strip() for p in (phrases or []) if p and p.strip()]
        if not phrases:
            return {"seen": [], "scores": {}, "frame_ms": 0,
                    "error": "no_phrases"}

        t0 = time.time()
        ok = self.capture_jpeg(self.frame_path)
        frame_ms = int((time.time() - t0) * 1000)
        if not ok:
            return {"seen": [], "scores": {}, "frame_ms": frame_ms,
                    "error": self._diagnose_cam_error()}

        key = _load_key()
        if not key:
            self._log("DEEPINFRA_API_KEY missing (~/.dia_key or env)")
            return {"seen": [], "scores": {}, "frame_ms": frame_ms,
                    "error": "no_api_key"}

        try:
            with open(self.frame_path, "rb") as fh:
                raw = fh.read()
        except OSError as e:
            self._log(f"read frame failed: {e}")
            return {"seen": [], "scores": {}, "frame_ms": frame_ms,
                    "error": "frame_read_failed"}
        # Compress the frame before sending: the VLM downsamples server-side
        # anyway, and a 512x... q70 JPEG is ~15-25 KB vs the ~1.4 MB raw.
        # Cuts upload + server-decode time by ~5-7 s on a typical run.
        encoded = raw
        if self.compress and _PIL_OK:
            try:
                img = Image.open(io.BytesIO(raw)).convert("RGB")
                before_kb = len(raw) // 1024
                img.thumbnail((self.max_dim, self.max_dim))
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=self.jpeg_quality, optimize=True)
                encoded = buf.getvalue()
                after_kb = len(encoded) // 1024
                self._log(
                    f"compressed {before_kb}KB -> {after_kb}KB "
                    f"({img.size[0]}x{img.size[1]})"
                )
            except Exception as e:
                self._log(f"compress failed ({type(e).__name__}: {e}); "
                          f"sending raw JPEG")
                encoded = raw
        b64 = base64.b64encode(encoded).decode("ascii")

        # Compose a terse prompt that forces a JSON-only answer.
        phrases_json = json.dumps(phrases)
        prompt = (
            "You are a visual classifier. Look at the attached image. "
            "For EACH phrase in the list, output the probability in [0,1] "
            "that the phrase is clearly visible in the image. "
            "Output ONLY a single JSON object mapping each phrase verbatim "
            "to its probability. No prose, no markdown, no extra keys.\n"
            f"Phrases: {phrases_json}"
        )
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            "max_tokens": 256,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        t_api = time.time()
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
                body = resp.read().decode("utf-8", "replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            self._log(f"VLM HTTP {e.code}: {body[:200]}")
            err = "api_auth" if e.code in (401, 403) else f"api_http_{e.code}"
            return {"seen": [], "scores": {}, "frame_ms": frame_ms, "error": err}
        except (urllib.error.URLError, TimeoutError) as e:
            self._log(f"VLM network: {e}")
            return {"seen": [], "scores": {}, "frame_ms": frame_ms,
                    "error": "api_network"}
        api_ms = int((time.time() - t_api) * 1000)
        if status != 200:
            self._log(f"VLM non-200: {status} {body[:200]}")
            return {"seen": [], "scores": {}, "frame_ms": frame_ms,
                    "error": f"api_http_{status}"}
        try:
            content = json.loads(body)["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            self._log(f"VLM bad body: {body[:200]}")
            return {"seen": [], "scores": {}, "frame_ms": frame_ms,
                    "error": "api_bad_body"}
        scores = _extract_scores(content, phrases)
        # Ensure every phrase has an entry (0.0 if the VLM dropped it).
        for p in phrases:
            scores.setdefault(p, 0.0)
        seen = sorted(
            [p for p, v in scores.items() if v >= threshold],
            key=lambda p: -scores[p],
        )
        self._log(
            f"cam={frame_ms}ms api={api_ms}ms model={self.model} "
            f"seen={seen} scores={scores}"
        )
        return {"seen": seen, "scores": scores, "frame_ms": frame_ms,
                "error": None}

    def close(self) -> None:
        self._closed = True


# ---------------------------------------------------------------- CLI driver
def _cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", nargs="+", required=True,
                    help="phrases to score")
    ap.add_argument("--camera-id", type=int, default=0,
                    help="0=back (default), 1=front/selfie")
    ap.add_argument("--threshold", type=float, default=0.20)
    ap.add_argument("--runs", type=int, default=3,
                    help="repeated captures (each a fresh frame)")
    ap.add_argument("--flag-path", default=DEFAULT_FLAG,
                    help="write JSON summary here for adb to read")
    ap.add_argument("--no-flag", action="store_true")
    ap.add_argument("--no-compress", action="store_true",
                    help="send raw JPEG (no Pillow resize + recompress)")
    ap.add_argument("--max-dim", type=int, default=512,
                    help="max dimension for compressed frames (default 512)")
    ap.add_argument("--jpeg-quality", type=int, default=70)
    args = ap.parse_args()

    cq = CameraQuery(camera_id=args.camera_id,
                     compress=not args.no_compress,
                     max_dim=args.max_dim,
                     jpeg_quality=args.jpeg_quality)
    runs = []
    t_all = time.time()
    for i in range(args.runs):
        t0 = time.time()
        res = cq.query(args.query, threshold=args.threshold)
        wall_ms = int((time.time() - t0) * 1000)
        res["wall_ms"] = wall_ms
        runs.append(res)
        print(f"[run {i + 1}/{args.runs}] wall={wall_ms}ms "
              f"frame={res.get('frame_ms')}ms err={res.get('error')} "
              f"seen={res.get('seen')} scores={res.get('scores')}",
              flush=True)
    cq.close()

    summary = {
        "model": DEFAULT_MODEL,
        "camera_id": args.camera_id,
        "threshold": args.threshold,
        "phrases": args.query,
        "runs": runs,
        "total_ms": int((time.time() - t_all) * 1000),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not args.no_flag:
        try:
            Path(args.flag_path).parent.mkdir(parents=True, exist_ok=True)
            with open(args.flag_path, "w") as fh:
                json.dump(summary, fh, indent=2)
            print(f"[phone_vision] wrote flag -> {args.flag_path}",
                  file=sys.stderr, flush=True)
        except OSError as e:
            print(f"[phone_vision] flag write failed: {e}",
                  file=sys.stderr, flush=True)
    # exit 0 if at least one run saw something or had no error
    for r in runs:
        if r.get("error") is None:
            return 0
    return 3


if __name__ == "__main__":
    sys.exit(_cli())
