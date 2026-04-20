#!/data/data/com.termux/files/usr/bin/env python3
"""phone_voice.py — continuous speech-to-text listener for phone_daemon.

Replaces the old stdin-based voice path so the user can actually TALK to the
robot from the Pixel 6 instead of piping text in.  Lives as its own module
so phone_daemon.py only needs a tiny hunk to adopt it.

Design (runs on Termux / Pixel 6, Python 3.13):
  - Start a daemon thread that loops:
      record 3 s via `termux-microphone-record -f <wav> -l <sec>`
      optional cheap amplitude VAD on the WAV — skip whisper if silent
      whisper-cli transcribe -> transcript string
      optional wake-word filter — drop anything that doesn't start with
        one of the configured wake phrases
      fire on_utterance(text)
  - The caller passes in a `mute_event: threading.Event`; while set, we
    drop the audio chunk (prevents TTS from self-triggering the loop).
  - Every step is guarded — a missing whisper-cli, OOM, bad WAV, timeout
    etc. logs and keeps listening.

Why not OpenWakeWord / webrtcvad:
  - OpenWakeWord depends on onnxruntime.  There is no pre-built aarch64
    onnxruntime wheel for CPython 3.13 on Termux (pypi has manylinux-only
    wheels; Termux is not manylinux).  Building from source on-device is
    multi-hour and a known flake.
  - webrtcvad has a native build requirement and does not ship as a
    Termux pkg on recent Termux releases.
  - A RMS-amplitude gate on the raw PCM is trivially implementable with
    just the `wave` stdlib and catches "silent chunk" cheaply.  Plus the
    wake-word filter on the transcript text handles the rest.
  - Net: pay one extra whisper-cli invocation for non-silent noise
    (e.g. HVAC), gate "speech vs no speech" by whether whisper outputs
    non-empty text, and trust the wake-word filter for false-positive
    suppression.

CLI (self-test):
    python3 phone_voice.py --self-test [--wav <path>]

The self-test injects a canned WAV into VoiceListener._process_chunk and
asserts on_utterance fires with a non-empty string.  Useful from the
host via adb-push to verify the pipeline without speaking.
"""
from __future__ import annotations

import argparse
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent

# ---- defaults tuned for Termux / Pixel 6 -----------------------------------
DEFAULT_RECORD_SEC = 3.0
DEFAULT_WHISPER_BIN = "/data/local/tmp/whisper-cli"
DEFAULT_WHISPER_MODEL = "/data/local/tmp/ggml-tiny.bin"
DEFAULT_WAV_PATH = "/data/data/com.termux/files/usr/tmp/phone_voice_cap.wav"
# Amplitude VAD: RMS below this (on 16-bit signed PCM) = "silent".
# Empirically, room noise on the Pixel 6's far-field mic sits around
# 80-200 RMS; a spoken word peaks in the thousands.  Conservative: 350.
DEFAULT_RMS_GATE = 350
# Wake words (lowercased).  The transcript may start with any of these.
# Trailing punctuation is stripped before matching.
DEFAULT_WAKE_WORDS: tuple[str, ...] = ("hey robot", "robot", "jarvis")


def _log(logger, msg: str) -> None:
    if logger is not None:
        try:
            logger(msg)
            return
        except Exception:
            pass
    print(f"[phone_voice {time.strftime('%H:%M:%S')}] {msg}",
          file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
def _wav_rms(path: str) -> float:
    """RMS of a mono/stereo 16-bit PCM WAV.  Returns 0.0 on any error —
    we fail-open so a weird WAV still gets forwarded to whisper."""
    try:
        with wave.open(path, "rb") as wf:
            if wf.getsampwidth() != 2:
                return 1.0e9  # unknown width, send to whisper
            n = wf.getnframes()
            if n == 0:
                return 0.0
            # cap read at ~1 M frames (~62 s at 16 kHz) to bound cost
            n_read = min(n, 1_000_000)
            raw = wf.readframes(n_read)
    except (wave.Error, FileNotFoundError, EOFError, OSError):
        return 1.0e9
    if not raw:
        return 0.0
    # struct unpack is faster than numpy for this size on Termux.
    count = len(raw) // 2
    # Guard against odd-length buffers.
    if count == 0:
        return 0.0
    try:
        samples = struct.unpack("<" + "h" * count, raw[: count * 2])
    except struct.error:
        return 1.0e9
    # RMS via int math — avoid float overflow on large buffers.
    sq_sum = 0
    for s in samples:
        sq_sum += s * s
    mean_sq = sq_sum / count
    return mean_sq ** 0.5


def _strip_wake_word(text: str, wake_words: Iterable[str]) -> Optional[str]:
    """If `text` begins with any wake word (case-insensitive, ignoring
    leading punct/whitespace), return the remainder (stripped).  Else
    return None.  Empty remainder returns "" (wake-word-only utterance,
    still a valid trigger)."""
    if not text:
        return None
    t = text.strip().lower()
    # Peel leading punctuation commonly inserted by whisper ("[ hey ...]",
    # ">> hey ...", "...hey..." etc.)
    while t and t[0] in "\"'.,!?:;-[]()<>":
        t = t[1:].lstrip()
    for wake in wake_words:
        w = wake.strip().lower()
        if not w:
            continue
        if t == w:
            return ""
        if t.startswith(w + " ") or t.startswith(w + ",") or \
                t.startswith(w + "."):
            rest = t[len(w):].lstrip(" ,.!?")
            return rest
    return None


# ---------------------------------------------------------------------------
class VoiceListener:
    """Callback-based continuous STT listener.

    Parameters
    ----------
    on_utterance : Callable[[str], None]
        Fired once per completed user turn, AFTER wake-word stripping.
        The argument is the user-intent text ("lean left"), not the raw
        "hey robot, lean left".
    wake_word : str | None (default "hey robot")
        If set, only transcripts that begin with this wake word (or any
        of the default aliases) are forwarded.  None = every non-empty
        transcript goes through.  Useful to disable for short hacking
        sessions.
    record_seconds : float (default 3.0)
        Length of each capture window.  3 s is the Termux-tested sweet
        spot — longer windows bloat whisper latency, shorter ones cut
        mid-word.
    mute_event : threading.Event | None
        If set by the caller (typically while TTS is speaking), the
        listener drops the current chunk and does not transcribe it.
    logger : callable(str) | None
        Where to write diagnostic lines.  Defaults to stderr.
    wake_words : iterable[str] | None
        Override list.  If None, derived from `wake_word` + defaults.
    record_cmd : list[str] | None
        Override for the underlying record command (for tests).  Format
        is a template with {wav} and {sec} placeholders.
    whisper_bin, whisper_model, wav_path : str
        Termux-side paths.  Defaults match docs/SETUP.md.
    rms_gate : float
        Silent-chunk threshold.  Set <=0 to disable the cheap VAD.
    """

    def __init__(
        self,
        on_utterance: Callable[[str], None],
        *,
        wake_word: Optional[str] = "hey robot",
        record_seconds: float = DEFAULT_RECORD_SEC,
        mute_event: Optional[threading.Event] = None,
        logger=None,
        wake_words: Optional[Iterable[str]] = None,
        record_cmd: Optional[list[str]] = None,
        whisper_bin: str = DEFAULT_WHISPER_BIN,
        whisper_model: str = DEFAULT_WHISPER_MODEL,
        wav_path: str = DEFAULT_WAV_PATH,
        rms_gate: float = DEFAULT_RMS_GATE,
    ):
        self._on_utterance = on_utterance
        self._record_seconds = float(record_seconds)
        self._mute_event = mute_event
        self._logger = logger
        self._whisper_bin = whisper_bin
        self._whisper_model = whisper_model
        self._wav_path = wav_path
        self._rms_gate = float(rms_gate)

        if wake_words is not None:
            self._wake_words = tuple(w for w in wake_words if w)
        elif wake_word is None:
            self._wake_words = tuple()  # no filtering
        else:
            # Include the user's primary + the defaults (dedup, preserve order).
            seen = set()
            acc: list[str] = []
            for w in (wake_word, *DEFAULT_WAKE_WORDS):
                lw = (w or "").strip().lower()
                if lw and lw not in seen:
                    seen.add(lw)
                    acc.append(lw)
            self._wake_words = tuple(acc)
        self._wake_required = wake_word is not None

        self._record_cmd_tmpl = record_cmd  # None -> use defaults below

        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        # Expose for tests.
        self.last_transcript: Optional[str] = None
        self.last_fire_text: Optional[str] = None

    # ------ public API -----------------------------------------------------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="phone_voice", daemon=True)
        self._running = True
        self._thread.start()
        _log(self._logger,
             f"listener started (wake_words={self._wake_words!r} "
             f"record_s={self._record_seconds} rms_gate={self._rms_gate})")

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=self._record_seconds + 2.0)
        self._running = False
        _log(self._logger, "listener stopped")

    def is_running(self) -> bool:
        return bool(self._running and self._thread and self._thread.is_alive())

    # ------ internals ------------------------------------------------------
    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._one_cycle()
            except Exception as e:  # never let the thread die
                _log(self._logger,
                     f"cycle err: {type(e).__name__}: {e}")
                # brief backoff to avoid hot-looping on a hard failure
                if self._stop_evt.wait(0.5):
                    return
        self._running = False

    def _one_cycle(self) -> None:
        # Drop while muted (TTS speaking).
        if self._mute_event is not None and self._mute_event.is_set():
            # Sleep a short slot and re-check; don't waste a whole
            # record window here.
            self._stop_evt.wait(0.2)
            return

        wav_bytes = self._record_chunk()
        if self._stop_evt.is_set():
            return
        if not wav_bytes:
            # record failed — back off a bit
            self._stop_evt.wait(0.25)
            return
        # Re-check mute (TTS may have started during record).
        if self._mute_event is not None and self._mute_event.is_set():
            return
        self._process_chunk(wav_bytes)

    def _record_chunk(self) -> Optional[bytes]:
        """Record a WAV chunk via termux-microphone-record.  Returns
        raw WAV bytes, or None on failure."""
        wav = self._wav_path
        # Clean stale WAV so short-read doesn't replay the previous capture.
        try:
            if os.path.exists(wav):
                os.remove(wav)
        except OSError:
            pass

        # Prefer explicit cmd override (tests); else termux-api.
        if self._record_cmd_tmpl is not None:
            argv = [a.format(wav=wav, sec=int(round(self._record_seconds)))
                    for a in self._record_cmd_tmpl]
        else:
            if shutil.which("termux-microphone-record") is None:
                _log(self._logger,
                     "termux-microphone-record missing — "
                     "install termux-api pkg")
                return None
            argv = ["termux-microphone-record", "-d",
                    "-l", str(int(round(self._record_seconds))),
                    "-f", wav]

        # termux-microphone-record returns immediately and captures in bg,
        # so we launch, wait for the capture window, then send -q to stop.
        try:
            subprocess.run(argv, capture_output=True, text=True,
                           timeout=5.0, check=False)
        except subprocess.TimeoutExpired:
            _log(self._logger, "record launcher timed out")
        # Wait for the capture window to fill — plus a small margin.
        end = time.monotonic() + self._record_seconds + 0.5
        while time.monotonic() < end:
            if self._stop_evt.is_set():
                return None
            time.sleep(0.1)
        # Best-effort quit (no-op if record was a one-shot in test mode).
        if self._record_cmd_tmpl is None:
            try:
                subprocess.run(["termux-microphone-record", "-q"],
                               capture_output=True, text=True,
                               timeout=3.0, check=False)
            except Exception:
                pass

        # Read WAV.
        try:
            with open(wav, "rb") as fh:
                blob = fh.read()
        except OSError as e:
            _log(self._logger, f"wav read failed: {e}")
            return None
        if not blob:
            return None
        return blob

    def _process_chunk(self, wav_bytes: bytes) -> None:
        """Transcribe WAV bytes, apply wake-word filter, fire callback.

        Exposed (public-ish) for self-test and for unit tests that want
        to inject a canned WAV without spawning termux-microphone-record.
        """
        if not wav_bytes:
            return
        # Persist to disk so whisper-cli -f <path> can read it.  Even in
        # the normal path we write to disk already; for the self-test
        # this lets us feed canned bytes.
        try:
            os.makedirs(os.path.dirname(self._wav_path), exist_ok=True)
        except OSError:
            pass
        try:
            with open(self._wav_path, "wb") as fh:
                fh.write(wav_bytes)
        except OSError as e:
            _log(self._logger, f"wav write failed: {e}")
            return

        # Cheap VAD — skip whisper if the chunk is below the RMS gate.
        if self._rms_gate > 0:
            rms = _wav_rms(self._wav_path)
            if rms < self._rms_gate:
                # silent / ambient — skip
                return

        transcript = self._whisper_transcribe(self._wav_path)
        self.last_transcript = transcript
        if not transcript:
            return
        _log(self._logger, f"transcript: {transcript!r}")

        if self._wake_required:
            stripped = _strip_wake_word(transcript, self._wake_words)
            if stripped is None:
                # no wake word — drop silently
                return
            # Empty stripped ("hey robot" alone) still counts as an
            # acknowledgement; we forward it as the raw wake word so
            # the downstream intent sees something to work with.
            fire_text = stripped if stripped else transcript.strip()
        else:
            fire_text = transcript.strip()

        if not fire_text:
            return
        self.last_fire_text = fire_text
        try:
            self._on_utterance(fire_text)
        except Exception as e:
            _log(self._logger,
                 f"on_utterance err: {type(e).__name__}: {e}")

    def _whisper_transcribe(self, wav_path: str) -> str:
        if not os.access(self._whisper_bin, os.X_OK):
            _log(self._logger,
                 f"whisper-cli not executable at {self._whisper_bin}")
            return ""
        if not os.path.isfile(self._whisper_model):
            _log(self._logger,
                 f"whisper model missing at {self._whisper_model}")
            return ""
        argv = [
            self._whisper_bin,
            "-m", self._whisper_model,
            "-f", wav_path,
            "-l", "en",
            "--no-timestamps",
            "-nt",
        ]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=30.0, check=False)
        except subprocess.TimeoutExpired:
            _log(self._logger, "whisper timed out")
            return ""
        except OSError as e:
            _log(self._logger, f"whisper exec err: {e}")
            return ""
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()[:200]
            _log(self._logger,
                 f"whisper rc={proc.returncode} stderr={stderr}")
            return ""
        lines = [ln.strip() for ln in (proc.stdout or "").splitlines()
                 if ln.strip()]
        return " ".join(lines).strip()


# ---------------------------------------------------------------------------
# Self-test entry point.  Runs from the phone (preferred) or host.
# ---------------------------------------------------------------------------
def _make_silence_wav(path: str, seconds: float = 0.5,
                      sample_rate: int = 16000) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        n = int(sample_rate * seconds)
        wf.writeframes(b"\x00\x00" * n)


def _make_tone_wav(path: str, seconds: float = 0.5,
                   sample_rate: int = 16000, freq: float = 440.0,
                   amp: int = 8000) -> None:
    import math
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = bytearray()
        for i in range(int(sample_rate * seconds)):
            v = int(amp * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += struct.pack("<h", v)
        wf.writeframes(bytes(frames))


def _self_test(wav: Optional[str], verbose: bool = True) -> int:
    """Inject a canned WAV into _process_chunk, verify callback fires.

    Strategy:
      1. Verify amplitude VAD correctly drops a silence WAV.
      2. With wake_word=None, feed a non-silent WAV and assert the
         callback receives *whatever* whisper returns (including empty —
         in which case we still exercise the pipeline by stubbing the
         whisper call).

    Returns 0 on pass, 1 on fail.
    """
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="phone_voice_selftest_"))
    results: dict[str, bool] = {}

    # --- 1. silence -> no callback ----------------------------------------
    sil_wav = wav or str(tmpdir / "sil.wav")
    if not wav:
        _make_silence_wav(sil_wav, seconds=0.5)
    fired_sil: list[str] = []
    lst_sil = VoiceListener(
        on_utterance=lambda t: fired_sil.append(t),
        wake_word=None,
        wav_path=str(tmpdir / "cap_sil.wav"),
        rms_gate=DEFAULT_RMS_GATE,
        logger=(print if verbose else (lambda _m: None)),
    )
    with open(sil_wav, "rb") as fh:
        lst_sil._process_chunk(fh.read())
    results["silence_drops"] = (len(fired_sil) == 0)
    if verbose:
        print(f"[self-test] silence path: fired={fired_sil} -> "
              f"{'PASS' if results['silence_drops'] else 'FAIL'}")

    # --- 2. tone (above VAD gate) -> pipeline reaches whisper -------------
    # We can't count on whisper returning a real transcript for a pure
    # tone, and whisper-cli may be absent on the host.  So: inject a
    # fake whisper by monkey-patching the instance method.
    tone_wav = str(tmpdir / "tone.wav")
    _make_tone_wav(tone_wav, seconds=0.5, amp=12000)
    fired_tone: list[str] = []
    lst_tone = VoiceListener(
        on_utterance=lambda t: fired_tone.append(t),
        wake_word=None,
        wav_path=str(tmpdir / "cap_tone.wav"),
        rms_gate=DEFAULT_RMS_GATE,
        logger=(print if verbose else (lambda _m: None)),
    )
    # Stub whisper to return a deterministic transcript.
    lst_tone._whisper_transcribe = lambda _p: "lean left"  # type: ignore
    with open(tone_wav, "rb") as fh:
        lst_tone._process_chunk(fh.read())
    results["nonsilence_fires"] = (fired_tone == ["lean left"])
    if verbose:
        print(f"[self-test] nonsilence path: fired={fired_tone} -> "
              f"{'PASS' if results['nonsilence_fires'] else 'FAIL'}")

    # --- 3. wake-word filter: transcript w/o wake word is dropped --------
    fired_nowake: list[str] = []
    lst_wake = VoiceListener(
        on_utterance=lambda t: fired_nowake.append(t),
        wake_word="hey robot",
        wav_path=str(tmpdir / "cap_nowake.wav"),
        rms_gate=DEFAULT_RMS_GATE,
        logger=(print if verbose else (lambda _m: None)),
    )
    lst_wake._whisper_transcribe = lambda _p: "lean left"  # type: ignore
    with open(tone_wav, "rb") as fh:
        lst_wake._process_chunk(fh.read())
    results["no_wake_drops"] = (len(fired_nowake) == 0)
    if verbose:
        print(f"[self-test] no-wake drop path: fired={fired_nowake} -> "
              f"{'PASS' if results['no_wake_drops'] else 'FAIL'}")

    # --- 4. wake word present -> fires with stripped text ----------------
    fired_wake: list[str] = []
    lst_wake2 = VoiceListener(
        on_utterance=lambda t: fired_wake.append(t),
        wake_word="hey robot",
        wav_path=str(tmpdir / "cap_wake.wav"),
        rms_gate=DEFAULT_RMS_GATE,
        logger=(print if verbose else (lambda _m: None)),
    )
    lst_wake2._whisper_transcribe = lambda _p: "hey robot lean left"  # type: ignore
    with open(tone_wav, "rb") as fh:
        lst_wake2._process_chunk(fh.read())
    results["wake_fires_stripped"] = (fired_wake == ["lean left"])
    if verbose:
        print(f"[self-test] wake fires stripped: fired={fired_wake} -> "
              f"{'PASS' if results['wake_fires_stripped'] else 'FAIL'}")

    # --- 5. mute_event blocks while set ----------------------------------
    mute = threading.Event()
    mute.set()
    fired_mute: list[str] = []
    lst_mute = VoiceListener(
        on_utterance=lambda t: fired_mute.append(t),
        wake_word=None,
        wav_path=str(tmpdir / "cap_mute.wav"),
        rms_gate=DEFAULT_RMS_GATE,
        mute_event=mute,
        logger=(print if verbose else (lambda _m: None)),
    )
    lst_mute._whisper_transcribe = lambda _p: "should not fire"  # type: ignore
    # _process_chunk itself doesn't check mute (that's _one_cycle's job)
    # — but the whole thread respects it, so we verify _one_cycle's
    # pre-record branch drops instantly.
    start = time.monotonic()
    lst_mute._one_cycle()
    elapsed = time.monotonic() - start
    results["mute_fastpath"] = (len(fired_mute) == 0 and elapsed < 1.0)
    if verbose:
        print(f"[self-test] mute fastpath: fired={fired_mute} "
              f"elapsed={elapsed:.2f}s -> "
              f"{'PASS' if results['mute_fastpath'] else 'FAIL'}")

    # --- 6. wake-word parsing unit cases ---------------------------------
    cases = [
        ("hey robot lean left", ("hey robot",), "lean left"),
        ("hey robot, lean left", ("hey robot",), "lean left"),
        ("Hey Robot. Lean left.", ("hey robot",), "lean left."),
        ("jarvis what time is it", ("hey robot", "jarvis"), "what time is it"),
        ("stop walking", ("hey robot",), None),
        ("", ("hey robot",), None),
        ("hey robot", ("hey robot",), ""),
    ]
    parse_ok = True
    for text, words, want in cases:
        got = _strip_wake_word(text, words)
        if got != want:
            parse_ok = False
            if verbose:
                print(f"[self-test] parse FAIL {text!r} words={words} "
                      f"want={want!r} got={got!r}")
    results["parse_wake_word"] = parse_ok
    if verbose:
        print(f"[self-test] wake-word parsing -> "
              f"{'PASS' if parse_ok else 'FAIL'}")

    passed = all(results.values())
    print(f"\n[self-test] summary: "
          f"{sum(results.values())}/{len(results)} passed "
          f"-> {'OK' if passed else 'FAIL'}")
    for k, v in results.items():
        print(f"  {k}: {'PASS' if v else 'FAIL'}")
    return 0 if passed else 1


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--self-test", action="store_true",
                    help="run the canned-WAV -> callback self-test "
                         "and exit")
    ap.add_argument("--wav", default=None,
                    help="(self-test) path to a canned WAV.  If omitted, "
                         "a synthesized silent WAV is used to validate the "
                         "VAD drop path; the non-silent path uses a "
                         "synthesized tone.")
    ap.add_argument("--listen", action="store_true",
                    help="start the listener for real (Termux only) — "
                         "prints each utterance")
    ap.add_argument("--wake-word", default="hey robot",
                    help="wake word required at start of transcript.  "
                         "'' = no wake word.")
    ap.add_argument("--record-sec", type=float, default=DEFAULT_RECORD_SEC)
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test(args.wav, verbose=True)

    if args.listen:
        def _cb(text: str) -> None:
            print(f"UTTERANCE: {text!r}", flush=True)
        wake = args.wake_word or None
        lst = VoiceListener(on_utterance=_cb, wake_word=wake,
                            record_seconds=args.record_sec)
        lst.start()
        try:
            while lst.is_running():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            lst.stop()
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
