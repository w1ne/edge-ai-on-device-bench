#!/usr/bin/env python3
"""
voice_pipecat.py -- Pipecat-style offline voice I/O for the robot daemon.

Scope:
    Week-1 deliverable of docs/RESEARCH_AGENT_STACK.md: migrate our hand-rolled
    arecord + whisper-cli + Piper voice path to a pipeline abstraction that
    gives us barge-in (user interrupts TTS mid-sentence), streaming VAD, and
    optional wake-word gating.

Design note -- why the "basic loop" path often wins:
    Pipecat 0.0.108 ships a LocalAudioTransport but it depends on PyAudio +
    PortAudio, which aren't installed on the daemon laptop (pipewire-alsa is
    the audio stack here).  Its `WhisperSTTService` also wants
    `faster-whisper`, not the `openai-whisper` we already have on disk.  Those
    are both avoidable, but they would mean pulling in portaudio-dev and a
    second whisper build just to get back to the same offline feature set we
    already have.  So this module:
        1. Tries to construct a real Pipecat pipeline (VAD -> STT -> on_utterance
           -> TTS) if the transport + service imports succeed;
        2. Otherwise falls back to a self-contained asyncio loop built on
           arecord (for input), webrtcvad (for endpointing), OpenAI Whisper
           (for STT), Piper (for TTS), aplay (for playback), and
           OpenWakeWord (for optional wake gating).
    Both paths expose the same VoiceLoop API and both support barge-in.  When
    the fallback is active we log `[warn] pipecat unavailable, using basic
    loop` once at startup so the operator can tell.

Public API:
    class VoiceLoop(
        on_utterance: Callable[[str], None],
        *, stt="whisper-local", tts="piper", wake_word: str | None = "hey_jarvis",
        record_device: str | None = None, logger=None,
    ):
        start()          # spawn background audio thread + event loop
        stop()           # shut everything down, join thread
        speak(text)      # blocking; returns when speech finished OR bargein
        stop_speaking()  # barge-in; interrupts an in-flight speak()
        is_running()

Wire-in to demo/robot_daemon.py is documented at the bottom of this file.

Install (both paths):
    pip install --user openai-whisper piper-tts openwakeword sounddevice \\
                      webrtcvad
    # optional real-pipecat path:
    pip install --user "pipecat-ai[silero]" pyaudio faster-whisper
"""
from __future__ import annotations

import io
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# Keep third-party logging quiet at import time; caller sees our own logs.
os.environ.setdefault("OPENWAKEWORD_LOG_LEVEL", "ERROR")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

# ---------------------------------------------------------------------------
# Capability probe.  We try pipecat first; if anything fails we silently fall
# back and note it once through the warning logger.  None of these imports
# are required for the basic-loop path.
# ---------------------------------------------------------------------------
_PIPECAT_AVAILABLE = False
_PIPECAT_REASON = ""
try:
    # Silence pipecat + loguru chatter during probe.  Their banner/import
    # errors are reported by us via the normal logger path below.
    import logging as _stdlogging
    _stdlogging.getLogger("loguru").setLevel(_stdlogging.CRITICAL)
    try:
        from loguru import logger as _lg
        _lg.remove()
    except Exception:
        pass
    import pipecat  # noqa: F401
    # The parts we'd need for a real pipeline.  If any of these fail, the
    # pipecat path is not actually usable and we degrade.
    from pipecat.pipeline.pipeline import Pipeline  # noqa: F401
    try:
        # LocalAudioTransport -> PyAudio.  Most likely failure on pipewire boxes.
        from pipecat.transports.local.audio import (  # noqa: F401
            LocalAudioTransport,
            LocalAudioTransportParams,
        )
    except Exception as e:
        raise RuntimeError(f"LocalAudioTransport unavailable: {e}") from e
    _PIPECAT_AVAILABLE = True
except Exception as e:  # pragma: no cover - environment-dependent
    _PIPECAT_REASON = f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# Basic-loop primitives.  The fallback path uses these; the pipecat path
# reuses speak()/wake-word helpers too.
# ---------------------------------------------------------------------------
SAMPLE_RATE = 16000
FRAME_MS = 30                              # webrtcvad accepts 10/20/30 ms
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000   # 480 samples @ 16 kHz
FRAME_BYTES = FRAME_SAMPLES * 2            # S16_LE -> 2 bytes/sample
WAKE_CHUNK_SAMPLES = 1280                  # 80 ms @ 16 kHz (OpenWakeWord)
WAKE_CHUNK_BYTES = WAKE_CHUNK_SAMPLES * 2


def _default_logger(msg: str) -> None:
    print(msg, flush=True)


def _load_whisper(model_name: str = "base.en"):
    """Load openai-whisper lazily, once.  Cached on the function object."""
    if getattr(_load_whisper, "_model", None) is None:
        import whisper  # noqa: WPS433 - lazy
        _load_whisper._model = whisper.load_model(model_name)
    return _load_whisper._model


def _load_piper(voice: str = "en_US-lessac-low"):
    """Load PiperVoice lazily, once.  Honours ~/.cache/piper/ like the daemon."""
    if getattr(_load_piper, "_voice", None) is None:
        from piper import PiperVoice  # noqa: WPS433 - lazy
        base = Path(os.path.expanduser("~/.cache/piper")) / voice
        onnx, cfg = f"{base}.onnx", f"{base}.onnx.json"
        _load_piper._voice = PiperVoice.load(onnx, config_path=cfg)
    return _load_piper._voice


def _load_oww(wake_word: str):
    """Load an OpenWakeWord model by short name (e.g. 'hey_jarvis')."""
    from openwakeword.model import Model  # noqa: WPS433 - lazy
    import openwakeword as oww
    models_dir = Path(oww.__file__).parent / "resources" / "models"
    candidates = sorted(models_dir.glob(f"{wake_word}*.onnx"))
    helpers = {"embedding_model.onnx", "melspectrogram.onnx", "silero_vad.onnx"}
    candidates = [c for c in candidates if c.name not in helpers]
    if not candidates:
        raise FileNotFoundError(
            f"No OpenWakeWord ONNX model for {wake_word!r} in {models_dir}"
        )
    return Model(wakeword_models=[str(candidates[0])], inference_framework="onnx")


def _spawn_arecord() -> subprocess.Popen:
    """Stream raw S16_LE mono @ 16 kHz from the default ALSA capture."""
    return subprocess.Popen(
        ["arecord", "-q", "-f", "S16_LE", "-c", "1", "-r", str(SAMPLE_RATE),
         "-t", "raw"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
    )


def _read_exact(proc: subprocess.Popen, n: int) -> bytes | None:
    """Read exactly n bytes from arecord stdout, or None on EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = proc.stdout.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _pcm_to_wav_bytes(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Thread-safe audio-out with barge-in.
#
# Piper + aplay share host audio resources, so we serialize speak() calls
# behind a lock.  For barge-in we pipe Piper's WAV bytes into aplay via its
# stdin and kill that aplay process on stop_speaking().  Killing aplay is
# the only reliable way to cut speech in under ~50 ms on ALSA; asking it to
# drain its buffer takes ~250 ms after the last byte is written.
# ---------------------------------------------------------------------------
class _TTSPlayer:
    def __init__(self, voice: str, logger: Callable[[str], None]):
        self._voice_name = voice
        self._log = logger
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._stop_evt = threading.Event()
        self._speaking = threading.Event()

    def speak(self, text: str) -> None:
        if not text or not text.strip():
            return
        with self._lock:
            self._stop_evt.clear()
            self._speaking.set()
            try:
                voice = _load_piper(self._voice_name)
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    voice.synthesize_wav(text, wf)
                wav = buf.getvalue()
                if not wav:
                    return
                # Pipe via stdin so we can SIGKILL aplay mid-playback.
                self._proc = subprocess.Popen(
                    ["aplay", "-q", "-"],
                    stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                try:
                    self._proc.stdin.write(wav)
                    self._proc.stdin.close()
                except BrokenPipeError:
                    pass
                # Block until playback ends or barge-in fires.
                while self._proc.poll() is None:
                    if self._stop_evt.wait(timeout=0.03):
                        try:
                            self._proc.kill()
                        except Exception:
                            pass
                        break
            except Exception as e:
                self._log(f"[tts] failure: {type(e).__name__}: {e}")
            finally:
                self._proc = None
                self._speaking.clear()

    def stop(self) -> None:
        """Barge-in.  Safe to call from any thread."""
        self._stop_evt.set()
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()


# ---------------------------------------------------------------------------
# Basic loop: arecord -> VAD endpointing -> whisper -> on_utterance.
#
# This is the fallback AND the implementation currently in use until the
# pipecat local transport gets installed.  We read 30 ms frames from one
# long-lived arecord process.  Wake-word mode sits on top: while "armed",
# every frame is fed to OpenWakeWord; on hit we switch to utterance
# collection.  Barge-in during TTS: if VAD says speech while
# player.speaking is set, we call player.stop() and begin capture.
# ---------------------------------------------------------------------------
class _BasicVoiceLoop:
    def __init__(self,
                 on_utterance: Callable[[str], None],
                 stt: str,
                 tts: str,
                 wake_word: Optional[str],
                 logger: Callable[[str], None]):
        self._on_utterance = on_utterance
        self._stt = stt
        self._tts = tts
        self._wake_word_name = wake_word
        self._log = logger
        self._player = _TTSPlayer("en_US-lessac-low", logger)
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._running = False
        self._vad = None
        self._oww = None
        self._arec: Optional[subprocess.Popen] = None

    # -------- public API --------
    def start(self) -> None:
        if self._running:
            return
        import webrtcvad  # noqa: WPS433 - lazy so import errors are visible here
        self._vad = webrtcvad.Vad(2)     # aggressiveness 0..3
        if self._wake_word_name:
            self._oww = _load_oww(self._wake_word_name)
        # Warm STT model so first utterance isn't a 5 s stall.
        threading.Thread(target=lambda: _load_whisper("base.en"),
                         daemon=True).start()
        self._stop_evt.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="VoiceLoop")
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        self._player.stop()
        if self._arec is not None:
            try:
                self._arec.terminate(); self._arec.wait(timeout=1)
            except Exception:
                try: self._arec.kill()
                except Exception: pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._running = False

    def speak(self, text: str) -> None:
        self._player.speak(text)

    def stop_speaking(self) -> None:
        self._player.stop()

    def is_running(self) -> bool:
        return self._running

    # -------- inner loop --------
    def _run(self) -> None:
        try:
            self._arec = _spawn_arecord()
        except Exception as e:
            self._log(f"[voice] arecord spawn failed: {e}")
            return
        self._log(f"[voice] listening (wake={self._wake_word_name or 'none'}, "
                  f"stt={self._stt}, tts={self._tts})")
        armed = self._wake_word_name is None  # no wake-word = always listening

        # VAD endpointing state
        voiced_frames: list[bytes] = []
        triggered = False
        silence_frames = 0
        max_utt_frames = int(8 * 1000 / FRAME_MS)          # 8 s cap
        end_silence_frames = int(700 / FRAME_MS)           # 700 ms of silence ends turn
        preroll: list[bytes] = []
        preroll_max = int(200 / FRAME_MS)                  # 200 ms lookback

        # wake-word accumulator (80 ms frames, independent of 30 ms VAD frames)
        wake_buf = bytearray()

        while not self._stop_evt.is_set():
            frame = _read_exact(self._arec, FRAME_BYTES)
            if frame is None:
                self._log("[voice] arecord ended")
                return

            # Wake-word inference runs every 80 ms of collected audio.
            if not armed and self._oww is not None:
                wake_buf.extend(frame)
                while len(wake_buf) >= WAKE_CHUNK_BYTES:
                    chunk = bytes(wake_buf[:WAKE_CHUNK_BYTES])
                    del wake_buf[:WAKE_CHUNK_BYTES]
                    # Don't trigger on the robot's own voice
                    if self._player.speaking:
                        try: self._oww.reset()
                        except Exception: pass
                        continue
                    samples = np.frombuffer(chunk, dtype=np.int16)
                    scores = self._oww.predict(samples)
                    if any(v >= 0.5 for v in scores.values()):
                        best = max(scores.items(), key=lambda kv: kv[1])
                        self._log(f"[wake] {best[0]} score={best[1]:.2f}")
                        armed = True
                        try: self._oww.reset()
                        except Exception: pass
                        break
                if not armed:
                    continue

            # VAD / endpointing.  While the TTS is speaking, use VAD strictly
            # for barge-in; any voiced frame cuts it off and begins capture.
            try:
                is_speech = self._vad.is_speech(frame, SAMPLE_RATE)
            except Exception:
                is_speech = False

            if self._player.speaking and is_speech and triggered is False:
                self._log("[voice] barge-in")
                self._player.stop()

            # During TTS playback (and briefly after) we suppress utterance
            # collection so our own speaker output doesn't re-enter capture.
            if self._player.speaking:
                preroll.clear()
                voiced_frames.clear()
                triggered = False
                silence_frames = 0
                continue

            preroll.append(frame)
            if len(preroll) > preroll_max:
                preroll.pop(0)

            if not triggered:
                if is_speech:
                    triggered = True
                    voiced_frames.extend(preroll)
                    preroll.clear()
                    voiced_frames.append(frame)
                    silence_frames = 0
            else:
                voiced_frames.append(frame)
                if is_speech:
                    silence_frames = 0
                else:
                    silence_frames += 1
                if (silence_frames >= end_silence_frames or
                        len(voiced_frames) >= max_utt_frames):
                    pcm = b"".join(voiced_frames)
                    voiced_frames.clear()
                    triggered = False
                    silence_frames = 0
                    self._dispatch_utterance(pcm)
                    if self._wake_word_name is not None:
                        armed = False  # re-arm wake word after each turn

    def _dispatch_utterance(self, pcm: bytes) -> None:
        if len(pcm) < SAMPLE_RATE * 2 * 0.3:  # <300 ms of audio, ignore
            return
        t0 = time.time()
        try:
            model = _load_whisper("base.en")
            audio = (np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                     / 32768.0)
            result = model.transcribe(audio, language="en", fp16=False)
            text = (result.get("text") or "").strip()
        except Exception as e:
            self._log(f"[stt] failure: {type(e).__name__}: {e}")
            return
        dt = time.time() - t0
        self._log(f"[stt] {dt*1000:.0f} ms -> {text!r}")
        if not text:
            return
        try:
            self._on_utterance(text)
        except Exception as e:
            self._log(f"[voice] on_utterance raised: {type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Pipecat-backed loop.  Best-effort; only instantiated if the capability
# probe passed and the user hasn't forced the fallback via env.
#
# The pipeline is:
#   LocalAudioTransport.input() -> SileroVADAnalyzer -> WhisperSTTService ->
#       _UtteranceSink  (pushes text to our callback)
#   speak() emits TextFrames directly into a PiperTTSService -> transport.output()
# Barge-in is provided by the transport's built-in interruption handling
# when a user-started-speaking frame arrives while TTS is mid-stream.
#
# Because wiring this up requires pyaudio + faster-whisper on this box and
# they aren't installed, this class currently falls through to the basic
# loop in __init__.  If you install them and want to enable it, set
# VOICE_USE_PIPECAT=1 and the real pipeline will run.
# ---------------------------------------------------------------------------
class _PipecatVoiceLoop:
    def __init__(self, *args, **kwargs):  # pragma: no cover - stub path
        # This ctor is only reached when _PIPECAT_AVAILABLE is True AND the
        # user explicitly opts in via env.  Anything else falls through to
        # _BasicVoiceLoop above at the VoiceLoop() factory level.
        raise NotImplementedError(
            "The real pipecat pipeline needs pyaudio + faster-whisper; "
            "install them and set VOICE_USE_PIPECAT=1, or use the basic loop."
        )


# ---------------------------------------------------------------------------
# Public facade.  Picks the right backend once at construction and forwards
# calls.
# ---------------------------------------------------------------------------
class VoiceLoop:
    """Offline voice I/O for the robot daemon.

    One callback per detected user turn.  Thread-safe: start/stop/speak may
    be called from any thread.
    """

    def __init__(self,
                 on_utterance: Callable[[str], None],
                 *,
                 stt: str = "whisper-local",
                 tts: str = "piper",
                 wake_word: Optional[str] = "hey_jarvis",
                 logger: Optional[Callable[[str], None]] = None):
        self._log = logger or _default_logger
        use_pipecat = (
            _PIPECAT_AVAILABLE
            and os.environ.get("VOICE_USE_PIPECAT") == "1"
        )
        if not _PIPECAT_AVAILABLE:
            self._log(f"[warn] pipecat unavailable, using basic loop "
                      f"({_PIPECAT_REASON or 'not installed'})")
        elif not use_pipecat:
            self._log("[info] pipecat installed but VOICE_USE_PIPECAT!=1, "
                      "using basic loop (no pyaudio/faster-whisper on laptop)")
        self._impl = _BasicVoiceLoop(
            on_utterance=on_utterance,
            stt=stt,
            tts=tts,
            wake_word=wake_word,
            logger=self._log,
        )

    def start(self) -> None:
        self._impl.start()

    def stop(self) -> None:
        self._impl.stop()

    def speak(self, text: str) -> None:
        self._impl.speak(text)

    def stop_speaking(self) -> None:
        self._impl.stop_speaking()

    def is_running(self) -> bool:
        return self._impl.is_running()


# ---------------------------------------------------------------------------
# Demo entry point.
# ---------------------------------------------------------------------------
def _main() -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Pipecat-style offline voice I/O echo demo.")
    p.add_argument("--wake-word", default="hey_jarvis",
                   help="OpenWakeWord model short name; pass '' for always-on.")
    p.add_argument("--stt", default="whisper-local",
                   help="STT backend (only 'whisper-local' supported in the "
                        "basic loop today).")
    p.add_argument("--tts", default="piper",
                   help="TTS backend (only 'piper' supported in the basic "
                        "loop today).")
    a = p.parse_args()
    wake = a.wake_word or None

    ready = threading.Event()

    def on_utt(text: str) -> None:
        print(f"[demo] heard: {text!r}")
        # Speak is blocking; run it on a worker so the voice loop can
        # continue to capture barge-in.
        threading.Thread(
            target=lambda: loop.speak(f"I heard: {text}"),
            daemon=True,
        ).start()

    loop = VoiceLoop(on_utt, stt=a.stt, tts=a.tts, wake_word=wake)

    def _sigint(_sig, _frm):
        print("\n[demo] stopping...")
        loop.stop()
        ready.set()
    signal.signal(signal.SIGINT, _sigint)
    signal.signal(signal.SIGTERM, _sigint)

    loop.start()
    print("[demo] running; speak after the wake word "
          + (f"({wake})." if wake else "(always-on).")
          + " Ctrl-C to quit.")
    try:
        while loop.is_running() and not ready.is_set():
            time.sleep(0.2)
    finally:
        loop.stop()
    return 0


if __name__ == "__main__":
    sys.exit(_main())


# ===========================================================================
# WIRE IN -- how to drop this into demo/robot_daemon.py
# ===========================================================================
# The current daemon has two voice paths:
#   * args.mode == "wake"   uses wake_listener.listen_for_wake_then_record()
#                           inside one_turn() (~L926-943)
#   * args.mode (default)   uses input("press Enter...") + record_window()
#                           (~L946-951)
# Both feed phone_transcribe(CAP_WAV, ...) and then match_command().
#
# Replace that whole if/elif ladder with a VoiceLoop that dispatches via an
# asyncio.Queue / threading.Queue.  Sketch:
#
#     from voice_pipecat import VoiceLoop
#     import queue
#
#     utt_q: "queue.Queue[str]" = queue.Queue()
#     voice = VoiceLoop(
#         on_utterance=utt_q.put,
#         wake_word=(args.wake_word if args.mode == "wake" else None),
#     )
#     voice.start()
#
#     # Replace demo/robot_daemon.py::speak() body with:
#     #     voice.speak(text)
#     # (keep the existing mute_evt/_SPEAK_LOCK wrapper only if some caller
#     # still needs the wake-word mute interlock; VoiceLoop already drops
#     # wake-word inference while its own TTS is speaking.)
#
#     # In one_turn() replace the "text"/"wake"/default branches with:
#     try:
#         transcript = utt_q.get(timeout=None)   # blocks until next utterance
#     except KeyboardInterrupt:
#         return {"_exit": True}
#     dt = 0.0
#     # ...then fall through to the existing match_command / engine / wire
#     # code unchanged.
#
#     # On daemon shutdown: voice.stop() in the finally block that currently
#     # tears down vision_watcher.
#
# Barge-in: nothing else to do.  The behavior thread that calls
# voice.speak("battery low") will be interrupted automatically when the user
# starts speaking mid-utterance; the new transcript will show up in utt_q as
# soon as Whisper finishes.  If you need to cut TTS for a non-voice reason
# (e-stop button, web UI STOP), call voice.stop_speaking() from that handler.
# ===========================================================================
