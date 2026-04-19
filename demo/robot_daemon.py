#!/usr/bin/env python3
"""
robot_daemon.py  —  voice-driven robot loop.

A single persistent process.  Two modes:

    voice (default)        press Enter, speak ~3 s, release.
    text                   type a command, press Enter.  no mic required.

Pipeline (voice mode):

    laptop mic (arecord, 16 kHz mono)
      -> adb push to P20 Lite
      -> whisper-cli on phone  (ggml-base.en.bin, 8 threads, -t 8)
      -> keyword matcher       (first-hit, rich synonyms)
      -> USB CDC JSON to ESP32 (VID 0x303a / PID 0x1001)
      -> servos move
      -> espeak-ng acks on laptop speakers

Optional:

    --with-llm              try TinyLlama intent parser (demo/parse_intent.py)
                            as a fallback when keyword matcher returns noop.
                            Slow on P20 (~25 s/call) and hallucinates on
                            ambiguous inputs.  Off by default.
    --with-eyes N           every N seconds, grab a frame via adb screencap,
                            run YOLO on laptop (demo/eyes.py), and log the
                            top detection.  Non-blocking background thread.
    --dry-run               skip adb + USB.  Use for testing the matcher.
    --no-tts                skip espeak-ng.  Use on silent machines.
    --log PATH              append transcripts + decisions to a logfile.

Examples:

    python3 demo/robot_daemon.py
    python3 demo/robot_daemon.py --mode text --dry-run
    python3 demo/robot_daemon.py --with-eyes 5 --log logs/robot-today.log
    python3 demo/robot_daemon.py --with-llm

Ctrl-C quits.  Saying 'shut down' / 'power off' / 'good bye' also quits.

Wire protocol (authoritative in w1ne/PhoneWalker:brain/wire.py):
    {"c":"pose","n":<name>,"d":<speed>}
    {"c":"walk","on":true,"stride":150,"step":400}
    {"c":"stop"}   {"c":"jump"}   {"c":"ping"}
"""
from __future__ import annotations

import argparse, datetime, json, os, queue, re, subprocess, sys, threading, time
from pathlib import Path

HERE     = Path(__file__).parent
CAP_WAV  = str(HERE / "_mic.wav")
EYE_PNG  = "/tmp/_robot_eye.png"

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

def speak(text: str, enabled: bool):
    if not enabled or not text:
        return
    try:
        subprocess.run(["espeak-ng", "-v", "en-us", "-s", "160", text],
                       stderr=subprocess.DEVNULL, timeout=15)
    except Exception:
        pass


# ------------------------------------------------------------------ phone STT

def phone_transcribe(wav: str, dry_run: bool) -> tuple[str, float]:
    if dry_run:
        return "", 0.0
    subprocess.run(["adb", "push", wav, "/data/local/tmp/cmd.wav"],
                   capture_output=True, check=True)
    t0 = time.time()
    r = subprocess.run(
        ["adb", "shell",
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


# ------------------------------------------------------------------ ESP32 wire

def send_wire(cmd: dict, dry_run: bool):
    if dry_run:
        return "dry-run", None
    import usb.core, usb.util  # lazy import so --dry-run works without pyusb
    dev = usb.core.find(idVendor=0x303a, idProduct=0x1001)
    if dev is None:
        return "no-device", None
    try: usb.util.claim_interface(dev, 1)
    except Exception: pass
    try:
        while True: dev.read(0x81, 4096, timeout=60)
    except Exception: pass
    payload = (json.dumps({k: v for k, v in cmd.items() if not k.startswith("_")})
               + "\n").encode()
    dev.write(0x01, payload, timeout=500)
    buf, end = bytearray(), time.time() + 1.3
    while time.time() < end:
        try: buf.extend(dev.read(0x81, 4096, timeout=120))
        except Exception: pass
    ack, pos = None, None
    for line in buf.decode("utf-8", "replace").splitlines():
        if '"ack"' in line or '"err"' in line: ack = line.strip()
        m = re.search(r'"p":\[([\-\d,]+)\]', line)
        if m: pos = [int(x) for x in m.group(1).split(',')]
    return ack, pos


# ------------------------------------------------------------------ LLM fallback

def llm_fallback(transcript: str) -> dict | None:
    """Call demo/parse_intent.py.  Best-effort; returns None on failure."""
    try:
        r = subprocess.run(
            [sys.executable, str(HERE / "parse_intent.py"), transcript],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        return None
    try:
        obj = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("c") in (None, "noop"):
        return None
    return obj


# ------------------------------------------------------------------ eyes (async)

def eyes_loop(interval: int, logger, dry_run: bool, stop_evt: threading.Event):
    """Poll vision in background.  Logs top detection; does not act."""
    eye = HERE / "eyes.py"
    if not eye.exists():
        logger("[eyes] skipped (demo/eyes.py missing)")
        return
    # eyes.py handles its own adb calls; fine to run even when the daemon
    # is in --dry-run mode (voice/USB stubbed but vision still useful).
    while not stop_evt.is_set():
        try:
            r = subprocess.run(
                [sys.executable, str(eye), "--out", EYE_PNG, "--top", "1"],
                capture_output=True, text=True, timeout=25,
            )
            # eyes.py prints detection lines + some banner; pick first non-empty
            # line that looks like a detection (has a bbox) or just the first line.
            picked = "(none)"
            for l in r.stdout.splitlines():
                s = l.strip()
                if s and ("conf" in s.lower() or re.search(r"\d+\s+\d+\s+\d+\s+\d+", s)):
                    picked = s
                    break
            logger(f"[eyes] {picked}")
        except subprocess.TimeoutExpired:
            logger("[eyes] timeout")
        except Exception as e:
            logger(f"[eyes] err: {type(e).__name__}: {e}")
        # sleep, but wake on stop
        stop_evt.wait(interval)


# ------------------------------------------------------------------ main loop

def make_logger(path: str | None):
    fh = open(path, "a", buffering=1) if path else None
    def log(line: str):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        text = f"{stamp} {line}"
        print(text)
        if fh:
            fh.write(text + "\n")
    return log

def one_turn(args, logger) -> dict | None:
    if args.mode == "text":
        try:
            transcript = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return {"_exit": True}
        dt = 0.0
    else:
        try:
            input("press Enter to speak > ")
        except (EOFError, KeyboardInterrupt):
            return {"_exit": True}
        record_window(args.seconds, CAP_WAV)
        transcript, dt = phone_transcribe(CAP_WAV, args.dry_run)
    logger(f"heard ({dt*1000:.0f} ms): {transcript!r}")

    cmd = match_command(transcript)
    if cmd is None and args.with_llm and transcript:
        logger("[matcher] no keyword hit, asking LLM")
        cmd = llm_fallback(transcript)
        if cmd is not None:
            logger(f"[matcher] LLM proposed: {cmd}")

    logger(f"decision: {cmd}")
    if cmd and not cmd.get("_exit"):
        wire_ack, pos = send_wire(cmd, args.dry_run)
        logger(f"wire:   {wire_ack}   servos: {pos}")
    speak(ack_phrase(cmd), args.tts)
    return cmd

def main():
    p = argparse.ArgumentParser(
        description="robot_daemon.py — voice-driven robot loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="examples:\n"
               "  python3 demo/robot_daemon.py\n"
               "  python3 demo/robot_daemon.py --mode text --dry-run\n"
               "  python3 demo/robot_daemon.py --with-eyes 5 --log logs/robot.log\n"
               "  python3 demo/robot_daemon.py --with-llm\n")
    p.add_argument("--mode", choices=["voice", "text"], default="voice",
                   help="voice: arecord 3 s windows; text: typed input")
    p.add_argument("--seconds", type=int, default=3,
                   help="voice mode: recording window length per turn")
    p.add_argument("--with-llm", action="store_true",
                   help="use TinyLlama fallback when keyword matcher fails")
    p.add_argument("--with-eyes", metavar="N", type=int, default=0,
                   help="poll YOLO on a background thread every N seconds")
    p.add_argument("--dry-run", action="store_true",
                   help="skip adb + USB; print decisions only")
    p.add_argument("--no-tts", dest="tts", action="store_false", default=True,
                   help="disable espeak-ng output")
    p.add_argument("--log", metavar="PATH",
                   help="append transcripts + decisions to logfile")
    args = p.parse_args()

    logger = make_logger(args.log)
    logger(f"robot_daemon up  mode={args.mode}  dry_run={args.dry_run}  "
           f"with_llm={args.with_llm}  with_eyes={args.with_eyes}  tts={args.tts}")
    speak("robot online", args.tts)

    stop_evt = threading.Event()
    eye_thread: threading.Thread | None = None
    if args.with_eyes > 0:
        eye_thread = threading.Thread(
            target=eyes_loop,
            args=(args.with_eyes, logger, args.dry_run, stop_evt),
            daemon=True,
        )
        eye_thread.start()

    try:
        while True:
            try:
                cmd = one_turn(args, logger)
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
        speak("goodbye", args.tts)
        logger("shutdown")

if __name__ == "__main__":
    main()
