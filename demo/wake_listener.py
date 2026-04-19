#!/usr/bin/env python3
"""
wake_listener.py — hands-free wake-word front-end for robot_daemon.

Replaces the Enter-key gate in voice mode with an always-on wake-word
detector.  On wake, records a short command window and returns a 16 kHz
mono WAV path ready for phone_transcribe().

Library: OpenWakeWord (ONNX CPU backend) with the pre-trained
"hey_jarvis" model.  ~5 % CPU idle, <100 ms end-of-wake-word to start
of recording on this laptop.

Audio IO: arecord subprocess streamed in 80 ms chunks (1280 samples
S16_LE @ 16 kHz).  sounddevice/portaudio isn't installed on this box,
so we stay subprocess-only.

Self-trigger mitigation: caller passes a threading.Event (`mute_evt`)
that is set for ~1.5 s after every speak() call.  While set, inference
frames are dropped and detector state is reset so leaking TTS audio
cannot tip us over threshold.

Usage (inside robot_daemon.py::one_turn):

    from wake_listener import listen_for_wake_then_record
    wav = listen_for_wake_then_record(
        wake_word="hey_jarvis",
        record_seconds=3.0,
        out_path=CAP_WAV,
        mute_evt=mute_evt,
    )
    if wav is None:  # Ctrl-C
        return {"_exit": True}

Quick standalone test:

    python3 demo/wake_listener.py          # listen, print on hit, exit
"""
from __future__ import annotations

import os
import signal
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np

# OpenWakeWord logs INFO on import; keep it quiet.
os.environ.setdefault("OPENWAKEWORD_LOG_LEVEL", "ERROR")

CHUNK_SAMPLES = 1280          # 80 ms @ 16 kHz (OpenWakeWord's native frame)
CHUNK_BYTES   = CHUNK_SAMPLES * 2   # S16_LE -> 2 bytes/sample
SAMPLE_RATE   = 16000
DEFAULT_THRESHOLD = 0.5       # OpenWakeWord score in [0,1]; 0.5 is the docs default

_MODEL_CACHE: dict = {}       # {wake_word: openwakeword.Model}


def _load_model(wake_word: str):
    """Lazy, cached loader.  ONNX backend (no tflite-runtime dep quirks)."""
    if wake_word in _MODEL_CACHE:
        return _MODEL_CACHE[wake_word]
    from openwakeword.model import Model  # lazy import
    import openwakeword as oww

    models_dir = Path(oww.__file__).parent / "resources" / "models"
    # Match "hey_jarvis" -> hey_jarvis_v0.1.onnx
    candidates = sorted(models_dir.glob(f"{wake_word}*.onnx"))
    # Drop helper onnx (embedding_model, melspectrogram, silero_vad)
    helpers = {"embedding_model.onnx", "melspectrogram.onnx", "silero_vad.onnx"}
    candidates = [c for c in candidates if c.name not in helpers]
    if not candidates:
        raise FileNotFoundError(
            f"No OpenWakeWord ONNX model for {wake_word!r} in {models_dir}. "
            f"Run `python3 -c \"import openwakeword.utils; "
            f"openwakeword.utils.download_models()\"` first."
        )
    model_path = str(candidates[0])
    model = Model(wakeword_models=[model_path], inference_framework="onnx")
    _MODEL_CACHE[wake_word] = model
    return model


def _spawn_arecord(rate: int = SAMPLE_RATE) -> subprocess.Popen:
    """Start arecord streaming raw S16_LE mono PCM to stdout."""
    return subprocess.Popen(
        ["arecord", "-q",
         "-f", "S16_LE",
         "-c", "1",
         "-r", str(rate),
         "-t", "raw"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )


def _write_wav(path: str, pcm_bytes: bytes, rate: int = SAMPLE_RATE) -> None:
    """Write mono int16 PCM to a WAV file."""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm_bytes)


def listen_for_wake_then_record(
    wake_word: str = "hey_jarvis",
    record_seconds: float = 3.0,
    out_path: str = "/tmp/_cmd.wav",
    mute_evt: threading.Event | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    logger=None,
) -> str | None:
    """Block until the wake-word fires, then capture `record_seconds`
    of follow-up audio to `out_path` (16 kHz mono WAV).

    Implementation note: we keep a SINGLE arecord process alive across
    both phases — detection and capture.  Spawning a fresh arecord
    post-detect costs ~130-200 ms (process + ALSA init), which blows
    our <100 ms end-of-wake-word-to-record-start budget.  Instead we
    just keep reading from the same stream after the model fires.

    While `mute_evt` is set, inference is suppressed and detector state
    is reset so self-TTS audio cannot tip us over threshold.  We still
    drain frames from arecord so the pipe doesn't back up.

    Returns `out_path` on success, or None if interrupted / stream dies.
    """
    log = logger or (lambda s: print(s, flush=True))

    try:
        model = _load_model(wake_word)
    except Exception as e:
        log(f"[wake] model load failed: {type(e).__name__}: {e}")
        return None

    proc = _spawn_arecord()
    log(f"[wake] listening for {wake_word!r} (threshold={threshold})")

    t_detect = None
    try:
        muted_prev = False
        while True:
            raw = proc.stdout.read(CHUNK_BYTES)
            if not raw or len(raw) < CHUNK_BYTES:
                log("[wake] arecord stream ended")
                return None

            # Muted window: flush state so TTS echo never accumulates.
            muted_now = bool(mute_evt and mute_evt.is_set())
            if muted_now:
                if not muted_prev:
                    # reset rolling prediction buffer so stale high scores
                    # from just before the mute don't persist
                    try:
                        model.reset()
                    except Exception:
                        pass
                muted_prev = True
                continue
            if muted_prev and not muted_now:
                try:
                    model.reset()
                except Exception:
                    pass
            muted_prev = False

            samples = np.frombuffer(raw, dtype=np.int16)
            scores = model.predict(samples)
            # scores: {model_name: score}
            if any(v >= threshold for v in scores.values()):
                t_detect = time.time()
                best = max(scores.items(), key=lambda kv: kv[1])
                log(f"[wake] detected {best[0]} score={best[1]:.2f}")
                break

        # --- capture phase: keep reading from the SAME arecord --------
        t_record_start = time.time()
        latency_ms = (t_record_start - t_detect) * 1000 if t_detect else -1
        log(f"[wake] recording {record_seconds:.1f} s "
            f"(wake->rec latency {latency_ms:.1f} ms)")
        want_bytes = int(record_seconds * SAMPLE_RATE) * 2  # S16 mono
        buf = bytearray()
        while len(buf) < want_bytes:
            # 8192-byte reads keep the pipe drained without blocking long
            chunk = proc.stdout.read(min(8192, want_bytes - len(buf)))
            if not chunk:
                log("[wake] arecord ended mid-capture")
                return None
            buf.extend(chunk)
    except KeyboardInterrupt:
        log("[wake] interrupted")
        return None
    finally:
        try:
            proc.terminate(); proc.wait(timeout=1)
        except Exception:
            try: proc.kill()
            except Exception: pass

    try:
        _write_wav(out_path, bytes(buf))
    except Exception as e:
        log(f"[wake] wav write failed: {e}")
        return None
    return out_path


# ---------------------------------------------------------------------- CLI
def _cli():
    import argparse
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--wake-word", default="hey_jarvis")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--record-seconds", type=float, default=3.0)
    p.add_argument("--out", default="/tmp/_cmd.wav")
    p.add_argument("--loop", action="store_true",
                   help="keep listening after each hit (Ctrl-C to stop)")
    a = p.parse_args()

    signal.signal(signal.SIGINT, signal.default_int_handler)
    while True:
        wav = listen_for_wake_then_record(
            wake_word=a.wake_word,
            record_seconds=a.record_seconds,
            out_path=a.out,
            threshold=a.threshold,
        )
        if wav is None:
            sys.exit(1)
        print(f"[wake] wrote {wav}")
        if not a.loop:
            return


if __name__ == "__main__":
    _cli()
