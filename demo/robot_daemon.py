#!/usr/bin/env python3
"""
robot_daemon.py  —  voice-driven robot loop.

A single persistent process.  Two modes:

    voice (default)        press Enter, speak ~3 s, release.
    text                   type a command, press Enter.  no mic required.

Pipeline (voice mode):

    laptop mic (arecord, 16 kHz mono)
      -> adb push to --stt-phone (default: pixel6)
      -> whisper-cli on phone  (ggml-base.en.bin, 8 threads, -t 8)
      -> regex keyword matcher (first-hit, rich synonyms — primary path)
      -> optional LLM fallback (DeepInfra API if --with-llm and
         $DEEPINFRA_API_KEY is set, else on-phone Gemma)
      -> BehaviorEngine (if --with-vision) — state machine handles
         greet-new-class + walk-until-obstacle + emergency-stop
      -> USB CDC JSON to ESP32 (VID 0x303a / PID 0x1001, persistent handle)
      -> servos move
      -> espeak-ng acks on laptop speakers

Optional:

    --with-llm              fallback to an LLM when keyword matcher misses.
                            Backend auto-picked: api (DeepInfra Llama-3.1-8B,
                            ~1 s/call, 8/8 accuracy) if DEEPINFRA_API_KEY is
                            set, else local on-phone (slower, lower accuracy).
    --llm-api {local,api}   override the auto-pick.
    --llm-model MODEL       which backend model (gemma/gemma4/tinyllama for
                            local; llama31-8b/llama33-70b/gemma3-27b/qwen25
                            for api).
    --llm-fast              use persistent llama-server via
                            scripts/start_llm_server.sh — shaves seconds off
                            local fallback calls.
    --with-vision CLASS     launch vision_watcher as a background thread,
                            drive BehaviorEngine reactions (greet, auto-stop
                            on close obstacle, follow-me on person drift).
    --vision-source SRC     webcam (default, ~20 FPS via cv2) or phone
                            (~2 FPS via adb screencap).
    --vision-phone PHONE    if source=phone, which phone to pull frames from.
    --stt-phone PHONE       which phone runs Whisper (default: pixel6).
    --dry-run               skip USB wire writes and on-phone Whisper.
                            Vision still runs.  Great for matcher testing.
    --no-tts                skip espeak-ng.
    --log PATH              append transcripts + decisions to a logfile.

Examples:

    # typical usage — laptop webcam, API LLM, real ESP32, Pixel 6 STT
    scripts/run_robot.sh

    # same, no hardware — typed commands, no USB, no on-phone Whisper
    python3 demo/robot_daemon.py --mode text --dry-run

    # offline-only — on-phone Gemma 3 as LLM fallback
    python3 demo/robot_daemon.py --with-llm --llm-api local --llm-model gemma

Ctrl-C quits.  Saying 'shut down' / 'power off' / 'good bye' also quits.

Wire protocol (authoritative in w1ne/PhoneWalker:brain/wire.py):
    {"c":"pose","n":<name>,"d":<speed>}
    {"c":"walk","on":true,"stride":150,"step":400}
    {"c":"stop"}   {"c":"jump"}   {"c":"ping"}
"""
from __future__ import annotations

import argparse, datetime, json, os, queue, re, subprocess, sys, threading, time
from pathlib import Path

from robot_behaviors import BehaviorEngine

HERE     = Path(__file__).parent
CAP_WAV  = str(HERE / "_mic.wav")
EYE_PNG  = "/tmp/_robot_eye.png"

# ------------------------------------------------------------------ tunables

# Battery (ESP32 telemetry "v" field is volts * 10; 72 == 7.2 V).
BATTERY_LOW_V          = 6.5   # alert threshold
BATTERY_RECOVER_V      = 6.8   # re-arm once we climb back above this
BATTERY_LOW_STREAK     = 3     # consecutive readings before we announce

# Vision watcher supervisor — exponential backoff on crash.
VISION_BACKOFF_START_S = 1.0
VISION_BACKOFF_MAX_S   = 30.0
VISION_MAX_FAILURES    = 5     # give up after N consecutive quick crashes

# Log rotation: rotate existing log at open time if it's this large or bigger.
LOG_ROTATE_BYTES       = 10 * 1024 * 1024   # 10 MB

# ------------------------------------------------------------------ matcher

# richer vocabulary: regex patterns ordered by specificity.
# first match wins.  patterns are matched against the *lowercased* transcript.
MATCHER: list[tuple[re.Pattern, dict]] = [
    # meta — shutdown phrases
    (re.compile(r"\b(shut\s?down|power\s?off|good\s?bye|goodbye|turn\s?off)\b"),
        {"_exit": True}),
    # diagnostic
    (re.compile(r"\b(ping|status|how\s+are\s+you|are\s+you\s+ok|report)\b"),
        {"c": "ping"}),
    # poses — left
    (re.compile(r"\b(lean|tilt|lilt|lunge)\s+(to\s+(the\s+)?)?left\b"),
        {"c": "pose", "n": "lean_left", "d": 1500}),
    # poses — right
    (re.compile(r"\b(lean|tilt|lilt|lunge)\s+(to\s+(the\s+)?)?right\b"),
        {"c": "pose", "n": "lean_right", "d": 1500}),
    # poses — bow
    (re.compile(r"\bbow(\s+(down|forward|front))?\b|\btake\s+a\s+bow\b"),
        {"c": "pose", "n": "bow_front", "d": 1800}),
    # poses — neutral / stand
    (re.compile(r"\b(neutral|stand(\s+(up|straight))?|reset|home|relax)\b"),
        {"c": "pose", "n": "neutral", "d": 1500}),
    # stop — put before walk so "stop walking" catches here
    (re.compile(r"\b(stop|halt|freeze|hold|cease)(\s+(it|walking|walk))?\b"),
        {"c": "stop"}),
    # walk
    (re.compile(r"\b(walk|march|go(\s+forward)?|move\s+forward|start\s+walking)\b"),
        {"c": "walk", "on": True, "stride": 150, "step": 400}),
    # jump
    (re.compile(r"\b(jump|hop|leap|bounce)\b"),
        {"c": "jump"}),
]

ACK = {
    "lean_left":  "okay, leaning left",
    "lean_right": "okay, leaning right",
    "bow_front":  "bowing",
    "neutral":    "back to neutral",
    "walk":       "starting to walk",
    "stop":       "stopping",
    "jump":       "jumping",
    "ping":       "I am here",
}

def match_command(transcript: str) -> dict | None:
    t = transcript.lower().strip()
    if not t:
        return None
    for pat, cmd in MATCHER:
        if pat.search(t):
            return dict(cmd)
    return None

def ack_phrase(cmd: dict | None) -> str:
    if cmd is None:
        return "sorry, I did not catch that"
    if cmd.get("_exit"):
        return "shutting down"
    c = cmd.get("c")
    if c == "pose":
        return ACK.get(cmd.get("n", ""), f"doing {cmd.get('n')}")
    return ACK.get(c, "okay")


# ------------------------------------------------------------------ laptop IO

def record_window(seconds: int, out_path: str):
    subprocess.run(
        ["arecord", "-q", "-f", "S16_LE", "-c", "1", "-r", "16000",
         "-d", str(seconds), out_path],
        check=True,
    )
    return out_path

# ---- TTS backend selection (Piper with espeak-ng fallback).
#
# Piper is a fast neural TTS that sounds much more natural than espeak-ng.
# We load the voice lazily on first use and cache the PiperVoice instance
# module-level so subsequent calls skip the ~900 ms ONNX load.  If Piper is
# unavailable (missing package, missing voice, runtime error), we fall back
# to espeak-ng for that call without disabling Piper permanently.
#
# Controlled by args.tts_mode / args.tts_voice (threaded in from argparse);
# the global _TTS_MODE / _TTS_VOICE below are set by main() before the first
# speak() call.

_TTS_MODE: str = "piper"                      # "piper" | "espeak" | "off"
_TTS_VOICE: str = "en_US-lessac-low"
_TTS_VOICE_DIR: str = os.path.expanduser("~/.cache/piper")
_TTS_DUMP_DIR: str | None = None              # if set, WAVs are saved here
_PIPER_VOICE = None                           # cached PiperVoice
_PIPER_PROBED: bool = False                   # True once we've tried to load
_PIPER_AVAILABLE: bool = False                # set by _piper_probe()
_PIPER_LOCK = threading.Lock()


def _piper_paths(voice: str) -> tuple[str, str]:
    base = os.path.join(_TTS_VOICE_DIR, voice)
    return base + ".onnx", base + ".onnx.json"


def _piper_probe() -> bool:
    """Load the Piper voice if not yet loaded.  Caches the result so repeat
    calls are free.  Returns True if Piper is ready to synthesize."""
    global _PIPER_VOICE, _PIPER_PROBED, _PIPER_AVAILABLE
    with _PIPER_LOCK:
        if _PIPER_PROBED:
            return _PIPER_AVAILABLE
        _PIPER_PROBED = True
        try:
            from piper import PiperVoice  # type: ignore
        except Exception:
            _PIPER_AVAILABLE = False
            return False
        onnx, cfg = _piper_paths(_TTS_VOICE)
        if not (os.path.exists(onnx) and os.path.exists(cfg)):
            _PIPER_AVAILABLE = False
            return False
        try:
            _PIPER_VOICE = PiperVoice.load(onnx, config_path=cfg)
        except Exception:
            _PIPER_VOICE = None
            _PIPER_AVAILABLE = False
            return False
        _PIPER_AVAILABLE = True
        return True


def _piper_synth_and_play(text: str) -> bool:
    """Synthesize `text` to an in-memory WAV, pipe it to `aplay`.  Blocks
    until playback finishes or aplay fails.  Returns True on success.
    Any exception/error returns False so the caller can fall back to
    espeak-ng for this call."""
    global _PIPER_VOICE
    if _PIPER_VOICE is None:
        return False
    import io, wave
    buf = io.BytesIO()
    try:
        with wave.open(buf, "wb") as wf:
            _PIPER_VOICE.synthesize_wav(text, wf)
    except Exception:
        return False
    wav_bytes = buf.getvalue()
    if not wav_bytes:
        return False
    # Optional: save every synthesis to a dump dir for offline listening.
    if _TTS_DUMP_DIR:
        try:
            os.makedirs(_TTS_DUMP_DIR, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%H%M%S_%f")[:-3]
            safe = re.sub(r"[^a-z0-9]+", "_", text.lower())[:40].strip("_")
            fn = os.path.join(_TTS_DUMP_DIR, f"{stamp}_{safe}.wav")
            with open(fn, "wb") as fh:
                fh.write(wav_bytes)
        except Exception:
            pass
    try:
        r = subprocess.run(
            ["aplay", "-q", "-"],
            input=wav_bytes, stderr=subprocess.DEVNULL, timeout=15,
        )
    except Exception:
        return False
    return r.returncode == 0


def _espeak_speak(text: str) -> None:
    try:
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", "160", text],
                       stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass


# C3 fix: serialize ALL speak() calls.  Piper + aplay + espeak all share
# host audio resources; two threads into speak() produces interleaved PCM
# frames and flaky ALSA contention.  Humans don't want overlapping
# utterances anyway, so a single mutex is both cheapest and correct.
_SPEAK_LOCK = threading.Lock()


def speak(text: str, enabled: bool,
          mute_evt: "threading.Event | None" = None):
    """Speak via Piper (natural-sounding neural TTS), falling back to
    espeak-ng if Piper is unavailable or fails on this call.  If `mute_evt`
    is supplied, set it for max(len(text)*0.05, 1.5) s around the call so
    the wake-word detector drops its own TTS audio.  We arm the flag before
    synthesis starts and schedule clearing on a timer so the mute window
    reliably covers laptop speaker output even if TTS returns early.

    Thread-safe: all synthesis + playback is serialized under _SPEAK_LOCK.

    `enabled` accepts legacy bool (True/False) or the string mode
    ("piper" | "espeak" | "off").  False or "off" is a no-op."""
    if not text:
        return
    # Normalize the `enabled` gate — keep backward compat with callers that
    # still pass a bool (e.g. battery_watcher's partial-applied lambdas).
    if enabled is False or enabled == "off":
        return
    mode = _TTS_MODE if enabled is True else (enabled if isinstance(enabled, str) else _TTS_MODE)
    if mode == "off":
        return

    mute_dur = max(len(text) * 0.05, 1.5)
    if mute_evt is not None:
        mute_evt.set()

    with _SPEAK_LOCK:
        spoke = False
        if mode == "piper":
            if _piper_probe():
                spoke = _piper_synth_and_play(text)
            # else: fall through to espeak silently

        if not spoke:
            _espeak_speak(text)

    if mute_evt is not None:
        # keep muted for the full envelope (TTS return + tail reverb)
        t = threading.Timer(mute_dur, mute_evt.clear)
        t.daemon = True
        t.start()


# ------------------------------------------------------------------ phone STT

PHONE_SERIALS = {"pixel6": "1B291FDF600260", "p20": "9WV4C18C11005454"}

# Path to the laptop-side TFLite runner (see scripts/whisper_tflite_runner.py
# for why it runs on the laptop rather than the phone).
TFLITE_RUNNER_PATH = str(HERE.parent / "scripts" / "whisper_tflite_runner.py")


def _phone_transcribe_whisper_cli(wav: str, phone: str) -> tuple[str, float]:
    """Original adb -> whisper-cli path. 'phone' is an alias or raw serial."""
    serial = PHONE_SERIALS.get(phone, phone)  # allow raw serial pass-through
    # C5 fix: timeout so a hung adb link (phone screen-off / USB flake)
    # doesn't block the daemon indefinitely.
    subprocess.run(["adb", "-s", serial, "push", wav, "/data/local/tmp/cmd.wav"],
                   capture_output=True, check=True, timeout=15)
    t0 = time.time()
    r = subprocess.run(
        ["adb", "-s", serial, "shell",
         "cd /data/local/tmp && ./whisper-cli -m ggml-base.en.bin "
         "-f cmd.wav --no-timestamps -l en -t 8 2>&1"],
        capture_output=True, text=True, timeout=120,
    )
    dt = time.time() - t0
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith(("whisper_", "main:", "system_info", "[", "#")):
            continue
        return s, dt
    return "", dt


# Cache the in-process whisper runner module so we pay the ~2.9 s
# Python/torch/tf import + model-load cost exactly once per daemon run
# instead of per utterance.  Subprocess-shelling would spend that every
# single call and wipe out the compute win.
_WHISPER_TFLITE_MOD = None
_WHISPER_TFLITE_PROBED = False


def _phone_transcribe_tflite(wav: str) -> tuple[str, float] | None:
    """TFLite path — runs scripts/whisper_tflite_runner.py IN-PROCESS.

    Encoder-only TFLite graph + openai-whisper pytorch decoder, both
    cached at the module level so only the first call pays import cost.
    Measured warm mean 1.25 s vs whisper.cpp ggml-tiny 1.80 s on
    utter_16k.wav (1.44x speedup captured end-to-end).  See
    docs/STATUS.md 'Whisper TFLite' section.

    Returns (transcript, wall_s) or None on failure — caller falls back
    to the whisper-cli path so we never lose STT.
    """
    global _WHISPER_TFLITE_MOD, _WHISPER_TFLITE_PROBED
    if not _WHISPER_TFLITE_PROBED:
        _WHISPER_TFLITE_PROBED = True
        scripts_dir = str(HERE.parent / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        try:
            import whisper_tflite_runner  # type: ignore
            _WHISPER_TFLITE_MOD = whisper_tflite_runner
        except Exception:
            _WHISPER_TFLITE_MOD = None
    if _WHISPER_TFLITE_MOD is None:
        return None
    t0 = time.time()
    try:
        out = _WHISPER_TFLITE_MOD.run_encoder_only(wav)
    except Exception:
        return None
    dt = time.time() - t0
    if out is None:
        return None
    text = (out.get("transcript", "") if isinstance(out, dict) else str(out or "")).strip()
    return text, float((out.get("wall_s", dt) if isinstance(out, dict) else dt))


def phone_transcribe(wav: str, dry_run: bool,
                     phone: str = "pixel6",
                     backend: str = "whisper-cli") -> tuple[str, float]:
    """STT entry point. Returns (transcript, wall_time_s).

    backend:
      whisper-cli  (default, unchanged) -- adb push to phone, whisper.cpp
                   with ggml-base.en.bin on 8 threads. Produces accurate
                   transcripts; roughly 2-4 s wall on Pixel 6 for a 3 s clip.
      tflite       -- laptop-side TFLite runner (see
                   scripts/whisper_tflite_runner.py). The nyadla-sys
                   whisper-tiny.en graph runs with XNNPack fp32 at 4 threads.
                   On tflite-runner failure or empty transcript, falls back
                   to whisper-cli for that call so we never lose STT.
    """
    if dry_run:
        return "", 0.0
    if backend == "tflite":
        res = _phone_transcribe_tflite(wav)
        if res is not None:
            text, dt = res
            if text:
                return text, dt
            # Empty text from the TFLite decoder (known issue with this
            # packaged model — the greedy decoder emits only prologue
            # tokens). Fall through to whisper-cli so the daemon still
            # hears the user.
            print("[stt] tflite produced empty transcript, "
                  "falling back to whisper-cli", file=sys.stderr)
        else:
            print("[stt] tflite runner unavailable/failed, "
                  "falling back to whisper-cli", file=sys.stderr)
    return _phone_transcribe_whisper_cli(wav, phone)


# ------------------------------------------------------------------ ESP32 wire

# Persistent USB handle — re-finding + re-claiming the device on every call
# triggers "Resource busy" on this ESP32-S3's USB CDC endpoint after the
# first write.  Cache it; only reopen on error.
_USB_DEV = None
_USB_LOCK = threading.Lock()


def _open_usb():
    import usb.core, usb.util  # lazy so --dry-run works without pyusb
    dev = usb.core.find(idVendor=0x303a, idProduct=0x1001)
    if dev is None:
        return None
    try: usb.util.claim_interface(dev, 1)
    except Exception: pass
    return dev


def _drop_usb():
    """Dispose current handle and null it out so the next send_wire() reopens.

    Safe to call with the lock held OR not (callers tend to hold it).
    """
    global _USB_DEV
    dev = _USB_DEV
    _USB_DEV = None
    if dev is None:
        return
    try:
        import usb.util
        usb.util.dispose_resources(dev)
    except Exception:
        pass


# Callback set by main() once the battery-watcher state exists.  Takes the
# raw telemetry line string; responsible for extracting "v" fields, tracking
# streaks, and announcing low-battery.  Kept as a module-level hook so
# send_wire() doesn't need to know about the daemon state dict.
_TELEMETRY_HOOK = None  # type: ignore[assignment]


def send_wire(cmd: dict, dry_run: bool):
    global _USB_DEV
    if dry_run:
        return "dry-run", None
    with _USB_LOCK:
        if _USB_DEV is None:
            _USB_DEV = _open_usb()
            if _USB_DEV is None:
                return "no-device", None
        dev = _USB_DEV
        # C2 fix: time-capped drain of stale telemetry (was `while True:` which
        # could livelock on a chatty ESP32 because every read returned data and
        # never raised).  Also C4: if a drain error looks like ENODEV / EIO,
        # reopen the handle before writing.
        drain_end = time.time() + 0.2
        while time.time() < drain_end:
            try:
                dev.read(0x81, 4096, timeout=30)
            except Exception as e:
                # usb.core.USBError exposes errno — 19 = ENODEV, 5 = EIO.
                # Avoid importing usb.core at module scope (lazy-imported).
                errno = getattr(e, "errno", None)
                if errno in (19, 5):
                    _drop_usb()
                    _USB_DEV = _open_usb()
                    if _USB_DEV is None:
                        return "no-device", None
                    dev = _USB_DEV
                break
        payload = (json.dumps({k: v for k, v in cmd.items()
                               if not k.startswith("_")}) + "\n").encode()
        try:
            dev.write(0x01, payload, timeout=500)
        except Exception:
            # kernel detached us; drop handle and retry once.
            _drop_usb()
            _USB_DEV = _open_usb()
            if _USB_DEV is None:
                return "no-device", None
            dev = _USB_DEV
            try:
                dev.write(0x01, payload, timeout=500)
            except Exception:
                # Retry also failed — leave _USB_DEV = None so the NEXT call
                # rediscovers a freshly plugged cable without sticking on a
                # dead handle.
                _drop_usb()
                return "no-device", None

        buf, end = bytearray(), time.time() + 1.3
        while time.time() < end:
            try: buf.extend(dev.read(0x81, 4096, timeout=120))
            except Exception: pass
        ack, pos = None, None
        text = buf.decode("utf-8", "replace")
        for line in text.splitlines():
            if '"ack"' in line or '"err"' in line: ack = line.strip()
            m = re.search(r'"p":\[([\-\d,]+)\]', line)
            if m: pos = [int(x) for x in m.group(1).split(',')]
        # C1 fix: capture telemetry text and hook under the lock, but DEFER
        # running the hook until after we release the lock — the hook speaks
        # "battery low" which blocks on aplay for up to 15 s, and we do not
        # want the USB lock held while that plays.  Voice "stop" and the
        # behavior engine's emergency-stop wire writes all need _USB_LOCK.
        hook = _TELEMETRY_HOOK
        telemetry_text = text
    # Lock released here.  Run the hook outside so slow TTS side-effects
    # can never starve emergency stop.
    if hook is not None:
        try:
            hook(telemetry_text)
        except Exception:
            pass
    return ack, pos


# ------------------------------------------------------------------ LLM fallback

def _scan_llm_stderr(stderr: str) -> str | None:
    """Inspect parse_intent_*.py stderr for a transport-level failure signature.

    Returns a short human string when the *API* clearly failed (5xx, timeout,
    network, retries exhausted, non-retryable) — so the daemon can log the
    difference between 'API down' and 'utterance was gibberish'.  Returns None
    on clean 200 responses (even if the model replied noop).

    We intentionally read stderr, not stdout: the wire contract with
    parse_intent_api.py stays noop-on-failure (item 2 constraint).
    """
    if not stderr:
        return None
    # Hard-coded knowledge of parse_intent_api.py's stderr banners.
    for line in stderr.splitlines():
        s = line.strip()
        if "AUTH FAILURE" in s:
            return s.split("]", 1)[-1].strip() or "auth failure"
        if "retries exhausted" in s:
            return s
        if "non-retryable" in s:
            return s
        # Status line: "status=503 wall=..."  — any 5xx or status=0 (timeout/URLError)
        m = re.search(r"status=(\d+)", s)
        if m:
            code = int(m.group(1))
            if code == 0:
                return "timeout or network error"
            if 500 <= code < 600:
                return f"status={code}"
    return None


def llm_fallback(transcript: str, model: str, fast: bool,
                 api: bool = False) -> dict | None:
    """Route to an intent parser.

    Three backends, chosen by (api, fast):
      api=True              -> parse_intent_api.py (DeepInfra-hosted,
                               ~1 s/call, 8/8 on the self-test; requires
                               laptop internet).  `model` forwards as --model
                               (alias like 'llama31-8b' or full id).
      api=False, fast=True  -> parse_intent_fast.py (on-phone llama-server,
                               2-8 s warm; start via scripts/start_llm_server.sh).
      api=False, fast=False -> parse_intent.py (on-phone llama-cli, 25-60 s
                               cold per call).

    TinyLlama Q4_0: 4/8.  Gemma 3 1B Q4_0: 7/8.  Llama-3.1-8B via API: 8/8.
    See logs/parse_intent_tests.log.

    Out-of-band failure reporting (item 2): when the API subprocess surfaces a
    transport-level error (5xx / timeout / network), we stash a short reason
    string on ``llm_fallback.last_error``.  Callers can read that immediately
    after the call to distinguish 'API down' from 'matcher+LLM both said
    noop'.  Cleared to None on every call.
    """
    llm_fallback.last_error = None  # type: ignore[attr-defined]
    if api:
        script = "parse_intent_api.py"
        # The API client enforces its own 10 s hard cap; give subprocess
        # enough headroom for process spawn + 1 retry.
        timeout = 25
    else:
        script = "parse_intent_fast.py" if fast else "parse_intent.py"
        timeout = 150
    try:
        r = subprocess.run(
            [sys.executable, str(HERE / script), "--model", model, transcript],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        llm_fallback.last_error = f"subprocess timeout after {timeout}s"  # type: ignore[attr-defined]
        return None
    except Exception as e:
        llm_fallback.last_error = f"subprocess spawn: {type(e).__name__}: {e}"  # type: ignore[attr-defined]
        return None
    # Even before looking at stdout, check stderr for API-level failure
    # signatures (only parse_intent_api.py emits these; the others are quiet).
    if api:
        why = _scan_llm_stderr(r.stderr or "")
        if why is not None:
            llm_fallback.last_error = why  # type: ignore[attr-defined]
    try:
        obj = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        if llm_fallback.last_error is None:  # type: ignore[attr-defined]
            llm_fallback.last_error = "unparseable output"  # type: ignore[attr-defined]
        return None
    if not isinstance(obj, dict) or obj.get("c") in (None, "noop"):
        return None
    return obj


# Initialize the attribute so first read is safe even if no call yet.
llm_fallback.last_error = None  # type: ignore[attr-defined]


# ------------------------------------------------------------------ eyes (async)

def _vision_run_once(cmd: list[str], logger, stop_evt: threading.Event,
                     event_queue: "queue.Queue[dict]") -> tuple[bool, float]:
    """Run one vision_watcher subprocess to completion.

    Returns (clean_exit, lifetime_seconds).  clean_exit is True when stop_evt
    fired (daemon shutting down) — the supervisor uses that to stop restarting.
    Any other exit (crash, process died, spawn failure) returns False.
    """
    logger(f"[vision] launching: {' '.join(cmd)}")
    t_launch = time.time()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as e:
        logger(f"[vision] spawn failed: {e}")
        return False, time.time() - t_launch

    tick_count = 0
    try:
        while not stop_evt.is_set():
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = msg.get("t")
            if t == "tick":
                tick_count += 1
                if tick_count % 20 == 0:
                    logger(f"[vision] alive (ticks={tick_count}, "
                           f"last_latency={msg.get('latency_ms')} ms)")
            elif t == "event":
                # I1 fix: conf may be None or missing if the watcher schema
                # drifts or a partial JSON line slips through — don't let a
                # format-spec TypeError kill this thread and trigger a silent
                # vision-restart loop.
                conf = msg.get("conf")
                conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
                logger(f"[vision] EVENT {msg.get('class')} "
                       f"conf={conf_s} streak={msg.get('streak')}")
                try: event_queue.put_nowait(msg)
                except queue.Full: pass
    finally:
        try: proc.terminate(); proc.wait(timeout=3)
        except Exception:
            try: proc.kill()
            except Exception: pass
    lifetime = time.time() - t_launch
    if stop_evt.is_set():
        return True, lifetime
    # Peek at a little stderr for the log — bounded so a runaway watcher
    # can't flood us.
    tail = ""
    try:
        if proc.stderr is not None:
            tail = (proc.stderr.read() or "")[-400:]
    except Exception:
        pass
    rc = proc.returncode
    logger(f"[vision] subprocess ended rc={rc} lifetime={lifetime:.1f}s"
           + (f" stderr_tail={tail!r}" if tail.strip() else ""))
    return False, lifetime


def vision_loop(source: str, phone: str, watch_for: str, interval: float,
                logger, dry_run: bool, stop_evt: threading.Event,
                event_queue: "queue.Queue[dict]"):
    """Supervisor: keeps vision_watcher.py alive across crashes.

    Item 1 — exponential backoff (1s, 2s, 4s, ..., 30s cap), give up after
    VISION_MAX_FAILURES consecutive failures.  A 'failure' is any exit that
    happened quickly (<30 s of useful uptime); a long-lived subprocess that
    dies resets the failure counter since it was clearly working.
    """
    watcher = HERE / "vision_watcher.py"
    if not watcher.exists():
        logger("[vision] skipped (demo/vision_watcher.py missing)")
        return
    # Note: we intentionally RUN vision in --dry-run mode.  Dry-run only gates
    # USB wire writes and on-phone Whisper; the vision path is laptop-side and
    # safe to exercise without an ESP32 attached.
    cmd = [sys.executable, str(watcher),
           "--source", source,
           "--phone", phone,
           "--interval", str(interval),
           "--watch-for", watch_for,
           "--output", "jsonl"]

    delay = VISION_BACKOFF_START_S
    consecutive_failures = 0
    restart_no = 0
    while not stop_evt.is_set():
        clean, lifetime = _vision_run_once(cmd, logger, stop_evt, event_queue)
        if clean or stop_evt.is_set():
            break
        # The subprocess died unexpectedly.
        if lifetime >= 30.0:
            # Was up long enough that this probably isn't a flap — reset.
            consecutive_failures = 0
            delay = VISION_BACKOFF_START_S
        consecutive_failures += 1
        restart_no += 1
        if consecutive_failures > VISION_MAX_FAILURES:
            logger(f"[vision] subprocess died, giving up after "
                   f"{VISION_MAX_FAILURES} consecutive failures")
            break
        logger(f"[vision] subprocess died, restart #{restart_no} after "
               f"{delay:.0f} s")
        if stop_evt.wait(delay):
            break
        delay = min(delay * 2, VISION_BACKOFF_MAX_S)
    logger("[vision] watcher stopped")


# ------------------------------------------------------------------ main loop

def _maybe_rotate_log(path: str) -> None:
    """Item 7: if the log at `path` is already >= LOG_ROTATE_BYTES at open
    time, rename it with a `.<timestamp>` suffix so the daemon starts fresh.

    Safe if the file is missing.  No new deps; stdlib only.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return
    except OSError:
        return
    if st.st_size < LOG_ROTATE_BYTES:
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    rotated = f"{path}.{stamp}"
    try:
        os.rename(path, rotated)
    except OSError:
        # If we can't rotate, don't crash — just keep appending.
        return
    # Best-effort breadcrumb on stderr so the user knows it happened even
    # before the logger itself is live.
    try:
        sys.stderr.write(f"[log] rotated {path} -> {rotated} "
                         f"(was {st.st_size} bytes)\n")
        sys.stderr.flush()
    except Exception:
        pass


def make_logger(path: str | None):
    if path:
        _maybe_rotate_log(path)
    fh = open(path, "a", buffering=1) if path else None
    def log(line: str):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"{stamp} {line}"
        print(text)
        if fh:
            fh.write(text + "\n")
    return log

_MEM_REPEAT = re.compile(
    r"\b(do\s+(it|that)\s+again|repeat(\s+that)?|again|once\s+more|one\s+more\s+time)\b"
)
_MEM_UNDO = re.compile(r"\b(undo|undo\s+that|cancel\s+that|revert|go\s+back)\b")

# inverse map for undo — dropping to neutral after a pose is the sane default
_UNDO = {
    "lean_left":  {"c": "pose", "n": "neutral",  "d": 1500},
    "lean_right": {"c": "pose", "n": "neutral",  "d": 1500},
    "bow_front":  {"c": "pose", "n": "neutral",  "d": 1500},
    # for walk/jump there's no semantic inverse — map both to stop
}


def resolve_memory(transcript: str, state: dict) -> dict | None:
    """Resolve short anaphoric commands (‘do it again’, ‘undo’) against the
    daemon's ring buffer.  Returns the wire cmd to execute, or None to fall
    through to the regular matcher."""
    t = transcript.lower().strip()
    if not t:
        return None
    history = state.setdefault("history", [])  # list[dict], newest last
    if _MEM_REPEAT.search(t):
        if not history:
            return None
        return dict(history[-1])
    if _MEM_UNDO.search(t):
        if not history:
            return None
        last = history[-1]
        if last.get("c") == "pose" and last.get("n") in _UNDO:
            return dict(_UNDO[last["n"]])
        if last.get("c") in ("walk", "jump"):
            return {"c": "stop"}
        return None
    return None


def _push_history(state: dict, cmd: dict, limit: int = 8) -> None:
    """Append a wire command to the recent-command ring buffer (max `limit`)."""
    if not cmd or cmd.get("_exit") or cmd.get("c") in (None, "ping"):
        return
    hist = state.setdefault("history", [])
    hist.append({k: v for k, v in cmd.items() if not k.startswith("_")})
    if len(hist) > limit:
        del hist[:-limit]


def one_turn(args, logger, state: dict,
             engine: BehaviorEngine | None = None,
             mute_evt: "threading.Event | None" = None) -> dict | None:
    if args.mode == "text":
        try:
            transcript = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return {"_exit": True}
        dt = 0.0
    elif args.mode == "wake":
        # Hands-free: block inside wake_listener until the wake word fires,
        # then record a command window.  No Enter press required.
        from wake_listener import listen_for_wake_then_record
        try:
            wav = listen_for_wake_then_record(
                wake_word=args.wake_word,
                record_seconds=float(args.seconds),
                out_path=CAP_WAV,
                mute_evt=mute_evt,
                threshold=args.wake_threshold,
                logger=logger,
            )
        except KeyboardInterrupt:
            return {"_exit": True}
        if wav is None:
            return {"_exit": True}
        logger(f"[wake] heard, recording {args.seconds} s...")
        transcript, dt = phone_transcribe(CAP_WAV, args.dry_run, args.stt_phone,
                                          backend=args.stt_backend)
    else:
        try:
            input("press Enter to speak > ")
        except (EOFError, KeyboardInterrupt):
            return {"_exit": True}
        record_window(args.seconds, CAP_WAV)
        transcript, dt = phone_transcribe(CAP_WAV, args.dry_run, args.stt_phone,
                                          backend=args.stt_backend)
    logger(f"heard ({dt*1000:.0f} ms): {transcript!r}")

    # Conversational memory — resolve anaphora ("do it again", "repeat that",
    # "undo") against the recent-command ring buffer BEFORE normal matching.
    mem_cmd = resolve_memory(transcript, state)
    if mem_cmd is not None:
        logger(f"[mem] resolved '{transcript}' -> {mem_cmd}")
        cmd = mem_cmd
    else:
        cmd = match_command(transcript)
    if cmd is None and args.with_llm and transcript:
        use_api = getattr(args, "llm_api", "local") == "api"
        # When --llm-api api, the default on-device model alias ("gemma"
        # et al) is meaningless to parse_intent_api.py — use its default
        # unless the user passed one of the API-alias names through --llm-model.
        api_model_aliases = {"llama31-8b", "llama33-70b", "gemma3-27b", "qwen25-72b"}
        if use_api:
            model_for_api = args.llm_model if args.llm_model in api_model_aliases else "llama31-8b"
            backend = f"api:{model_for_api}"
        else:
            model_for_api = args.llm_model
            backend = ("fast:" if args.llm_fast else "cli:") + args.llm_model
        logger(f"[matcher] no keyword hit, asking LLM ({backend})")
        cmd = llm_fallback(transcript, model_for_api, args.llm_fast, api=use_api)
        # Item 2: make API failures distinguishable from genuine noop output.
        err = getattr(llm_fallback, "last_error", None)
        if err:
            logger(f"[llm] api call failed ({err}): falling through to noop")
        if cmd is not None:
            logger(f"[matcher] LLM proposed: {cmd}")

    logger(f"decision: {cmd}")

    # Route through the behavior engine when vision is active. The engine may
    # substitute a different wire command (e.g. emergency-stop during walk).
    wire_cmd: dict | None = cmd
    if engine is not None and cmd is not None and not cmd.get("_exit"):
        wire_cmd = engine.on_voice_command(cmd)
        if wire_cmd != cmd:
            logger(f"[behav] engine rewrote wire: {cmd} -> {wire_cmd}")

    if wire_cmd and not wire_cmd.get("_exit"):
        wire_ack, pos = send_wire(wire_cmd, args.dry_run)
        logger(f"wire:   {wire_ack}   servos: {pos}")
        # record the wire command to the ring buffer for 'do it again' / 'undo'
        _push_history(state, wire_cmd)
        # legacy walking flag — only meaningful when we don't have an engine
        if engine is None:
            if wire_cmd.get("c") == "walk":
                state["walking"] = True
            elif wire_cmd.get("c") in ("stop", "pose"):
                state["walking"] = False
    speak(ack_phrase(cmd), args.tts, mute_evt=mute_evt)
    return cmd


def drain_vision_events(event_queue: "queue.Queue[dict]", state: dict,
                        args, logger,
                        engine: BehaviorEngine | None = None):
    """Called between voice turns. Pulls any vision events off the queue.

    When an engine is supplied (--with-vision on), delegates to
    engine.on_vision_event — the engine owns greet / auto-stop / follow-me
    logic.  When no engine, falls back to the original simple react (kept
    for backwards compat so --with-vision off still works unchanged).
    """
    GREETING_COOLDOWN = 10.0
    now = time.time()
    try:
        while True:
            ev = event_queue.get_nowait()
            if engine is not None:
                engine.on_vision_event(ev)
                continue
            # ---- legacy path (no engine) ----
            cls = ev.get("class", "?")
            last = state["last_greet"].get(cls, 0.0)
            if now - last < GREETING_COOLDOWN:
                continue
            state["last_greet"][cls] = now
            if state["walking"]:
                logger(f"[react] {cls} in frame while walking — auto-stop")
                wire_ack, pos = send_wire({"c": "stop"}, args.dry_run)
                logger(f"[react] wire: {wire_ack}")
                state["walking"] = False
                speak(f"there is a {cls} in front of me, stopping", args.tts)
            else:
                logger(f"[react] {cls} detected — greeting")
                speak(f"hello, I see a {cls}", args.tts)
    except queue.Empty:
        return


def behavior_tick_loop(engine: BehaviorEngine, args, logger,
                       stop_evt: threading.Event, period: float = 10.0):
    """Low-rate background thread that pokes engine.tick().  Any wire
    command returned is sent on the wire."""
    while not stop_evt.wait(period):
        try:
            out = engine.tick()
        except Exception as e:
            logger(f"[behav] tick error: {type(e).__name__}: {e}")
            continue
        if out is None:
            continue
        try:
            wire_ack, pos = send_wire(out, args.dry_run)
            logger(f"[behav] tick-wire {out} -> {wire_ack}")
        except Exception as e:
            logger(f"[behav] tick send_wire error: {type(e).__name__}: {e}")

# ------------------------------------------------------------------ battery

_VOLT_RE = re.compile(r'"v"\s*:\s*(\d+)')


def make_battery_watcher(logger, speak_fn):
    """Item 3: scan ESP32 telemetry text for ``"v":<int>`` (volts*10).

    When voltage stays below BATTERY_LOW_V for BATTERY_LOW_STREAK consecutive
    packets, announce "battery low" once and log it.  Rearm only after the
    reading climbs back above BATTERY_RECOVER_V — prevents spam when the
    voltage bounces around the threshold.

    Hooks into the telemetry stream already parsed inside send_wire() — no
    new USB reader is opened.
    """
    state = {"low_streak": 0, "armed": True, "last_v": None}

    def watch(raw_text: str) -> None:
        readings = [int(m.group(1)) / 10.0
                    for m in _VOLT_RE.finditer(raw_text)]
        if not readings:
            return
        for v in readings:
            state["last_v"] = v
            if v < BATTERY_LOW_V:
                state["low_streak"] = state["low_streak"] + 1
                if (state["low_streak"] >= BATTERY_LOW_STREAK
                        and state["armed"]):
                    logger(f"[battery] LOW {v:.1f} V "
                           f"(threshold {BATTERY_LOW_V:.1f} V, "
                           f"streak={state['low_streak']})")
                    try:
                        speak_fn("battery low")
                    except Exception:
                        pass
                    state["armed"] = False
            elif v >= BATTERY_RECOVER_V:
                if not state["armed"]:
                    logger(f"[battery] recovered to {v:.1f} V — rearmed")
                state["low_streak"] = 0
                state["armed"] = True
            # Else: hysteresis band — neither increment nor clear.
    return watch, state


# ------------------------------------------------------------------ self-test

def run_self_test(args, logger) -> int:
    """Item 4: boot-time probe of ESP32 / ADB / webcam / API key.

    Never aborts — just prints PASS or an ``N issues`` summary.  Skips the
    USB probe in --dry-run and the webcam probe when vision is phone-sourced.
    Returns the issue count so callers could gate telemetry on it.
    """
    issues = 0

    # (a) ESP32 presence via pyusb — skipped in dry-run.
    if args.dry_run:
        logger("[self-test] esp32:   SKIPPED (--dry-run)")
    else:
        present: bool | None
        try:
            import usb.core  # type: ignore
            present = usb.core.find(idVendor=0x303a, idProduct=0x1001) is not None
        except Exception as e:
            logger(f"[self-test] esp32:   UNKNOWN ({type(e).__name__}: {e})")
            present = None
        if present is True:
            logger("[self-test] esp32:   PRESENT (VID 303a / PID 1001)")
        elif present is False:
            logger("[self-test] esp32:   MISSING (VID 303a / PID 1001 not found)")
            issues += 1
        # pyusb missing -> logged as UNKNOWN above; not counted as an issue.

    # (b) ADB device at ANDROID_SERIAL.
    serial = os.environ.get("ANDROID_SERIAL", "")
    if not serial:
        logger("[self-test] adb:     SKIPPED (ANDROID_SERIAL unset)")
    else:
        try:
            r = subprocess.run(
                ["adb", "-s", serial, "shell", "echo", "ok"],
                capture_output=True, text=True, timeout=5,
                stdin=subprocess.DEVNULL,  # adb drains parent stdin otherwise,
                                           # breaking --mode text piped input
            )
            if r.returncode == 0 and "ok" in (r.stdout or ""):
                logger(f"[self-test] adb:     ok (serial={serial})")
            else:
                logger(f"[self-test] adb:     unreachable "
                       f"(rc={r.returncode}, "
                       f"stderr={(r.stderr or '')[:120]!r})")
                issues += 1
        except FileNotFoundError:
            logger("[self-test] adb:     unreachable (adb binary not on PATH)")
            issues += 1
        except subprocess.TimeoutExpired:
            logger("[self-test] adb:     unreachable (timeout)")
            issues += 1
        except Exception as e:
            logger(f"[self-test] adb:     unreachable "
                   f"({type(e).__name__}: {e})")
            issues += 1

    # (c) Webcam — only relevant when vision source is webcam (or no vision).
    if args.with_vision and args.vision_source == "phone":
        logger("[self-test] webcam:  SKIPPED (--vision-source phone)")
    else:
        dev_path = "/dev/video0"
        if os.path.exists(dev_path):
            logger(f"[self-test] webcam:  present ({dev_path})")
        else:
            logger(f"[self-test] webcam:  missing ({dev_path})")
            # Only count as an issue if vision was actually requested.
            if args.with_vision and args.vision_source == "webcam":
                issues += 1

    # (d) DeepInfra API key.
    if os.environ.get("DEEPINFRA_API_KEY"):
        logger("[self-test] llm-key: set (DEEPINFRA_API_KEY)")
    else:
        logger("[self-test] llm-key: unset (DEEPINFRA_API_KEY)")
        # Only counted as an issue if the user asked to use the API backend.
        if args.with_llm and args.llm_api == "api":
            issues += 1

    if issues == 0:
        logger("=== self-test: PASS ===")
    else:
        word = "issue" if issues == 1 else "issues"
        logger(f"=== self-test: {issues} {word} ===")
    return issues


def main():
    p = argparse.ArgumentParser(
        description="robot_daemon.py — voice-driven robot loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 demo/robot_daemon.py\n"
               "  python3 demo/robot_daemon.py --mode text --dry-run\n"
               "  python3 demo/robot_daemon.py --with-eyes 5 --log logs/robot.log\n"
               "  python3 demo/robot_daemon.py --with-llm\n")
    p.add_argument("--mode", choices=["voice", "text", "wake"], default="voice",
                   help="voice: press-Enter then arecord 3 s windows; "
                        "text: typed input; "
                        "wake: hands-free — OpenWakeWord listens for "
                        "--wake-word, then records a command window.")
    p.add_argument("--seconds", type=int, default=3,
                   help="voice/wake mode: recording window length per turn")
    p.add_argument("--wake-word", default="hey_jarvis",
                   help="wake phrase for --mode wake.  Any OpenWakeWord "
                        "pre-trained model prefix (hey_jarvis, hey_mycroft, "
                        "alexa, hey_rhasspy, timer, weather).  default: hey_jarvis.")
    p.add_argument("--wake-threshold", type=float, default=0.5,
                   help="OpenWakeWord score threshold in [0,1].  Lower = more "
                        "sensitive (more false positives).  default: 0.5.")
    p.add_argument("--with-llm", action="store_true",
                   help="use on-phone LLM fallback when keyword matcher fails")
    p.add_argument("--llm-model", choices=["gemma", "gemma4", "tinyllama"],
                   default="gemma",
                   help="on-phone model for --with-llm.  gemma: Gemma 3 1B "
                        "Q4_0 (720 MB, 7/8 test accuracy, P20+Pixel 6); "
                        "gemma4: Gemma 4 E2B Q4_K_S (3 GB, Pixel 6 only); "
                        "tinyllama: 4/8 accuracy.  default: gemma.")
    p.add_argument("--llm-fast", action="store_true",
                   help="use parse_intent_fast.py (talks to llama-server on "
                        "phone — much faster if you've started the server via "
                        "scripts/start_llm_server.sh).  default: false.")
    p.add_argument("--llm-api", choices=["local", "api"], default=None,
                   help="with --with-llm, choose where the LLM runs.  "
                        "local: on-phone (parse_intent.py / parse_intent_fast.py). "
                        "api: DeepInfra Llama-3.1-8B (~1 s/call, 8/8 accuracy, "
                        "needs $DEEPINFRA_API_KEY).  default: auto — api if "
                        "$DEEPINFRA_API_KEY is set, else local.")
    p.add_argument("--with-vision", metavar="CLASS", default=None,
                   help="launch demo/vision_watcher.py in the background; "
                        "emit greeting / auto-stop reactions when CLASS is "
                        "seen (e.g. 'person').")
    p.add_argument("--stt-phone", choices=["pixel6", "p20"], default="pixel6",
                   help="phone that runs Whisper on-device (default: pixel6). "
                        "only used in voice mode.")
    p.add_argument("--stt-backend", choices=["whisper-cli", "tflite"],
                   default="whisper-cli",
                   help="STT runtime. whisper-cli (default): on-phone "
                        "whisper.cpp with ggml-base.en.bin, 8 threads, most "
                        "accurate. tflite: laptop-side tflite_runtime with "
                        "XNNPack 4T on nyadla-sys/whisper-tiny.en.tflite; "
                        "fast compute but the packaged greedy decoder is "
                        "known to emit empty transcripts — on empty/failure "
                        "the daemon falls back to whisper-cli automatically.")
    p.add_argument("--vision-source", choices=["phone", "webcam"], default="webcam",
                   help="frame source for vision_watcher.  webcam: laptop "
                        "/dev/video0 via OpenCV (~20 FPS, default); phone: "
                        "adb screencap (~2 FPS, fallback).")
    p.add_argument("--vision-phone", choices=["pixel6", "p20"], default="pixel6",
                   help="if --vision-source=phone, which phone to pull frames "
                        "from (default: pixel6)")
    p.add_argument("--vision-interval", type=float, default=0.5,
                   help="seconds between vision frames (default 0.5)")
    p.add_argument("--dry-run", action="store_true",
                   help="skip adb + USB; print decisions only")
    p.add_argument("--tts", choices=["piper", "espeak", "off"], default="piper",
                   help="TTS backend.  piper: neural voice (fast, natural); "
                        "espeak: espeak-ng (robotic, no extra deps); "
                        "off: silent.  default: piper (falls back to espeak "
                        "automatically if the Piper voice is missing).")
    p.add_argument("--tts-voice", default="en_US-lessac-low",
                   help="Piper voice name (looked up in ~/.cache/piper/ as "
                        "<name>.onnx + <name>.onnx.json).  default: "
                        "en_US-lessac-low.")
    p.add_argument("--no-tts", dest="no_tts", action="store_true",
                   help="shortcut for --tts off")
    p.add_argument("--log", metavar="PATH",
                   help="append transcripts + decisions to logfile")
    args = p.parse_args()

    logger = make_logger(args.log)
    # --no-tts is a shortcut for --tts off.
    if getattr(args, "no_tts", False):
        args.tts = "off"
    # Plumb TTS config into the module-level speak() backend selector.
    global _TTS_MODE, _TTS_VOICE
    _TTS_MODE = args.tts
    _TTS_VOICE = args.tts_voice
    # Auto-pick LLM backend if user didn't force it: API when the key is set,
    # local otherwise.  Reduces the default invocation to `scripts/run_robot.sh`
    # without forcing users to remember --llm-api api.
    if args.llm_api is None:
        args.llm_api = "api" if os.environ.get("DEEPINFRA_API_KEY") else "local"
    # Default ANDROID_SERIAL so any subprocess using plain `adb` (parse_intent*,
    # vision_watcher --source phone, etc.) targets the right device when both
    # Pixel 6 and P20 Lite are plugged in.
    os.environ.setdefault("ANDROID_SERIAL",
                          PHONE_SERIALS.get(args.stt_phone, args.stt_phone))
    logger(f"robot_daemon up  mode={args.mode}  dry_run={args.dry_run}  "
           f"with_llm={args.with_llm}/fast={args.llm_fast}/api={args.llm_api}  "
           f"with_vision={args.with_vision}  tts={args.tts}  "
           f"stt_phone={args.stt_phone}(ANDROID_SERIAL={os.environ.get('ANDROID_SERIAL','')})  "
           f"stt_backend={args.stt_backend}")

    # Item 4: surface broken hardware / env BEFORE the user types anything.
    # Does not abort on failure — every probe logs present/missing/skip.
    run_self_test(args, logger)

    # mute_evt: shared "TTS is speaking" flag. wake_listener drops audio
    # frames while set; speak() sets it for max(len(text)*0.05, 1.5) s
    # around every espeak-ng call so the detector cannot self-trigger on
    # its own laptop-speaker output.  Only really matters in --mode wake,
    # but harmless in other modes.
    mute_evt = threading.Event()
    speak("robot online", args.tts, mute_evt=mute_evt)

    # Item 3: install the battery watcher — plumbed through _TELEMETRY_HOOK
    # so send_wire() invokes it with whatever ESP32 telemetry it sees.
    global _TELEMETRY_HOOK
    _bat_watch, _bat_state = make_battery_watcher(
        logger,
        lambda s: speak(s, args.tts, mute_evt=mute_evt),
    )
    _TELEMETRY_HOOK = _bat_watch

    stop_evt = threading.Event()
    event_queue: "queue.Queue[dict]" = queue.Queue(maxsize=32)
    state = {"walking": False, "last_greet": {}}
    eye_thread: threading.Thread | None = None
    engine: BehaviorEngine | None = None
    tick_thread: threading.Thread | None = None
    if args.with_vision:
        eye_thread = threading.Thread(
            target=vision_loop,
            args=(args.vision_source, args.vision_phone, args.with_vision,
                  args.vision_interval, logger, args.dry_run, stop_evt,
                  event_queue),
            daemon=True,
        )
        eye_thread.start()

        engine = BehaviorEngine(
            speak_fn=lambda s: speak(s, args.tts, mute_evt=mute_evt),
            wire_fn=lambda c: send_wire(c, args.dry_run),
            logger=logger,
        )
        tick_thread = threading.Thread(
            target=behavior_tick_loop,
            args=(engine, args, logger, stop_evt, 10.0),
            daemon=True,
        )
        tick_thread.start()

    try:
        while True:
            try:
                drain_vision_events(event_queue, state, args, logger, engine)
                cmd = one_turn(args, logger, state, engine, mute_evt=mute_evt)
            except KeyboardInterrupt:
                logger("interrupted during turn; idle.")
                continue
            except Exception as e:
                logger(f"ERROR: {type(e).__name__}: {e}")
                continue
            if cmd and cmd.get("_exit"):
                break
    finally:
        stop_evt.set()
        if eye_thread is not None:
            eye_thread.join(timeout=3)
        if tick_thread is not None:
            tick_thread.join(timeout=3)
        speak("goodbye", args.tts, mute_evt=mute_evt)
        logger("shutdown")

if __name__ == "__main__":
    main()
