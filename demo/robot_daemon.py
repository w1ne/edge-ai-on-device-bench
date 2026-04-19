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

PHONE_SERIALS = {"pixel6": "1B291FDF600260", "p20": "9WV4C18C11005454"}


def phone_transcribe(wav: str, dry_run: bool,
                     phone: str = "pixel6") -> tuple[str, float]:
    if dry_run:
        return "", 0.0
    serial = PHONE_SERIALS.get(phone, phone)  # allow raw serial pass-through
    subprocess.run(["adb", "-s", serial, "push", wav, "/data/local/tmp/cmd.wav"],
                   capture_output=True, check=True)
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
        # drain any stale telemetry first
        try:
            while True: dev.read(0x81, 4096, timeout=60)
        except Exception: pass
        payload = (json.dumps({k: v for k, v in cmd.items()
                               if not k.startswith("_")}) + "\n").encode()
        try:
            dev.write(0x01, payload, timeout=500)
        except Exception:
            # kernel detached us; drop handle and retry once
            try: import usb.util; usb.util.dispose_resources(dev)
            except Exception: pass
            _USB_DEV = _open_usb()
            if _USB_DEV is None:
                return "no-device", None
            dev = _USB_DEV
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
    """
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
    except Exception:
        return None
    try:
        obj = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("c") in (None, "noop"):
        return None
    return obj


# ------------------------------------------------------------------ eyes (async)

def vision_loop(source: str, phone: str, watch_for: str, interval: float,
                logger, dry_run: bool, stop_evt: threading.Event,
                event_queue: "queue.Queue[dict]"):
    """Background consumer of demo/vision_watcher.py stdout.

    Each JSONL line from the watcher is either {"t":"tick", ...} (liveness) or
    {"t":"event", "class": ..., "conf": ..., "bbox": [...], "streak": ...}.
    Events get pushed into event_queue; ticks are just logged at low volume.
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
    logger(f"[vision] launching: {' '.join(cmd)}")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except Exception as e:
        logger(f"[vision] spawn failed: {e}")
        return

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
                logger(f"[vision] EVENT {msg.get('class')} "
                       f"conf={msg.get('conf'):.2f} streak={msg.get('streak')}")
                try: event_queue.put_nowait(msg)
                except queue.Full: pass
    finally:
        try: proc.terminate(); proc.wait(timeout=3)
        except Exception: pass
        logger("[vision] watcher stopped")


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

def one_turn(args, logger, state: dict,
             engine: BehaviorEngine | None = None) -> dict | None:
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
        transcript, dt = phone_transcribe(CAP_WAV, args.dry_run, args.stt_phone)
    logger(f"heard ({dt*1000:.0f} ms): {transcript!r}")

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
        # legacy walking flag — only meaningful when we don't have an engine
        if engine is None:
            if wire_cmd.get("c") == "walk":
                state["walking"] = True
            elif wire_cmd.get("c") in ("stop", "pose"):
                state["walking"] = False
    speak(ack_phrase(cmd), args.tts)
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
    p.add_argument("--no-tts", dest="tts", action="store_false", default=True,
                   help="disable espeak-ng output")
    p.add_argument("--log", metavar="PATH",
                   help="append transcripts + decisions to logfile")
    args = p.parse_args()

    logger = make_logger(args.log)
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
           f"stt_phone={args.stt_phone}(ANDROID_SERIAL={os.environ.get('ANDROID_SERIAL','')})")
    speak("robot online", args.tts)

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
            speak_fn=lambda s: speak(s, args.tts),
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
                cmd = one_turn(args, logger, state, engine)
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
        speak("goodbye", args.tts)
        logger("shutdown")

if __name__ == "__main__":
    main()
