#!/data/data/com.termux/files/usr/bin/env python3
"""phone_daemon.py — reactive phone-side robot brain.

Runs inside Termux on the Pixel 6.  This is a thread-based reactive loop,
NOT the old one-shot command executor.  Architecture mirrors the laptop
demo/robot_daemon.py but keeps the thread count minimal to fit on phone:

    voice_thread     — reads transcripts from STT (or stdin in --mode text).
                        Short imperatives route through phone_intent +
                        wire (fast path).  Multi-step / future-tense goals
                        route through GoalKeeper.set_goal (planner path).

    vision_thread    — polls phone_vision.CameraQuery.query() every
                        VISION_POLL_S.  Any "seen" phrase becomes an
                        event on GoalKeeper + updates engine._seen_classes
                        for the `look` tool.  Gracefully absent if
                        scripts/termux/phone_vision.py is missing.

    ble_reader_thread— reads notifications from the wire socket (battery,
                        state packets).  Pushes battery-low events to
                        GoalKeeper.  Exposes last state via engine.

    state_thread     — writes phone_state.json to
                        /sdcard/Download/edge-ai-phone/ every 500 ms for
                        adb-based introspection (atomic os.replace).

    main_thread      — waits on SIGINT, then tears everything down.

The planner runs ON the GoalKeeper's follow-up thread — main loop never
blocks on a DeepInfra turn.

CLI:
    python3 phone_daemon.py                      # full reactive loop
    python3 phone_daemon.py --mode text          # stdin instead of STT
    python3 phone_daemon.py --no-vision          # disable vision thread
    python3 phone_daemon.py --no-tts             # silent
    python3 phone_daemon.py --no-goal-keeper     # one-shot mode (legacy)
    python3 phone_daemon.py --mock-wire          # spawn mock_wire_server

Exit cleanly on SIGINT / EOF.

Dependencies (Termux):
    pkg install -y python termux-api
    + scripts/termux/phone_{intent,stt,tts,wire,planner,goal_keeper}.*
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HOME", str(Path.home())))

DEFAULT_WIRE_HOST = "127.0.0.1"
DEFAULT_WIRE_PORT = 5557

# ---- state publishing ------------------------------------------------------
# /sdcard path is readable by adb without run-as; Termux's $HOME is not.
# Writes via os.replace on a tmp sibling so a partial read never happens.
STATE_DIR = Path(os.environ.get("PHONE_STATE_DIR",
                                "/sdcard/Download/edge-ai-phone"))
STATE_FILE = STATE_DIR / "phone_state.json"
STATE_WRITE_HZ = 2.0  # -> every 500 ms per brief

# ---- vision polling --------------------------------------------------------
# 2 Hz fixed.  Goal-adaptive rate is a known weakness (see return note).
VISION_POLL_S = 0.5
# Default watchlist when no goal is active — matches the "interesting
# phrases" examples in the brief.  Goal-active watchlist is goal text.
DEFAULT_WATCHLIST: tuple[str, ...] = (
    "a person",
    "an obstacle",
    "the user",
    "a laptop",
)


def eprint(*a, **kw):
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


def log(msg: str) -> None:
    eprint(f"[phone_daemon {time.strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------- scripts dir
def resolve_scripts_dir(user_override: str | None) -> Path:
    """Find the directory holding phone_*.sh/.py siblings.

    Order:
      1. --scripts-dir arg
      2. env PHONE_SCRIPTS_DIR
      3. directory of phone_daemon.py itself (this file)
      4. $HOME (bootstrap-copied case)
    """
    for cand in (user_override, os.environ.get("PHONE_SCRIPTS_DIR"),
                 str(HERE), str(HOME)):
        if not cand:
            continue
        p = Path(cand).expanduser().resolve()
        if (p / "phone_intent.py").exists():
            return p
    raise SystemExit(f"cannot locate phone_intent.py — tried {HERE}, {HOME}")


# ---------------------------------------------------------------- subprocesses
def run_stt(scripts: Path, record_sec: int) -> str:
    """Blocking: record + whisper-cli transcribe via phone_stt.sh."""
    script = scripts / "phone_stt.sh"
    if not script.exists():
        log(f"WARN: {script} missing — returning empty transcript")
        return ""
    env = os.environ.copy()
    env["RECORD_SEC"] = str(record_sec)
    try:
        proc = subprocess.run(
            ["bash", str(script)],
            env=env,
            capture_output=True,
            text=True,
            timeout=record_sec + 30,
        )
    except subprocess.TimeoutExpired:
        log("STT timed out")
        return ""
    if proc.returncode != 0:
        log(f"STT rc={proc.returncode} stderr={proc.stderr.strip()[:200]}")
        return ""
    return proc.stdout.strip()


def run_intent(scripts: Path, transcript: str) -> dict:
    script = scripts / "phone_intent.py"
    if not script.exists():
        return {"c": "noop"}
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=transcript,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        log("intent parser timed out — noop")
        return {"c": "noop"}
    if proc.returncode == 2:
        log(f"intent parser AUTH FAILURE: {proc.stderr.strip()[:200]}")
        return {"c": "noop", "_auth_fail": True}
    if proc.returncode not in (0, 4):
        log(f"intent parser rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    last = ""
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            last = line
    if not last:
        return {"c": "noop"}
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        log(f"intent parser emitted non-json: {last!r}")
        return {"c": "noop"}


def run_tts(scripts: Path, text: str, enabled: bool) -> None:
    if not enabled or not text:
        return
    script = scripts / "phone_tts.sh"
    if not script.exists():
        log(f"WARN: {script} missing — would say: {text!r}")
        return
    try:
        subprocess.run(
            ["bash", str(script)],
            input=text,
            text=True,
            timeout=15,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log("TTS timed out")


# ---------------------------------------------------------------- wire client
def _import_wire():
    try:
        import phone_wire  # type: ignore
        return phone_wire
    except ImportError as e:
        log(f"phone_wire import failed: {e}")
        return None


def port_listening(host: str, port: int, timeout: float = 0.3) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        s.close()
        return True
    except OSError:
        return False


def maybe_spawn_mock(scripts: Path, host: str, port: int) -> subprocess.Popen | None:
    if port_listening(host, port):
        log(f"wire: something already listening on {host}:{port}")
        return None
    mock = scripts / "mock_wire_server.py"
    if not mock.exists():
        log(f"wire: no {mock} — connect will fail")
        return None
    env = os.environ.copy()
    env["HOST"] = host
    env["PORT"] = str(port)
    log(f"wire: spawning mock_wire_server on {host}:{port}")
    proc = subprocess.Popen(
        [sys.executable, str(mock)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if port_listening(host, port):
            return proc
        time.sleep(0.05)
    log("wire: mock did not come up in 3 s")
    proc.terminate()
    return None


# ---------------------------------------------------------------- speech text
def ack_phrase(cmd: dict) -> str:
    c = cmd.get("c", "")
    n = cmd.get("n", "")
    if c == "pose":
        return {
            "lean_left": "leaning left",
            "lean_right": "leaning right",
            "bow_front": "bowing",
            "neutral": "neutral",
        }.get(n, "posing")
    if c == "walk":
        return "walking"
    if c == "stop":
        return "stopping"
    if c == "jump":
        return "jumping"
    return ""


# ---------------------------------------------------------------- shutdown patterns
# Kept tiny on purpose — shut down shouldn't route through DeepInfra.
_SHUTDOWN_RE = re.compile(
    r"\b(shut\s*down|shutdown|power\s*off|good\s*bye|goodbye|turn\s*off"
    r"|quit|exit)\b", re.I,
)
_CANCEL_RE = re.compile(
    r"\b(never\s*mind|cancel|forget\s*it|stop\s*the\s*goal|abort)\b", re.I,
)


def looks_multistep(text: str) -> bool:
    """Heuristic: does this utterance look like a multi-step / standing goal?

    Route to GoalKeeper+Planner iff we think it does.  False = let the
    regex+intent fast path handle it.  Err on the side of false positives;
    a short imperative routed to the planner still works, just slower.
    """
    t = (text or "").strip().lower()
    if not t:
        return False
    # Any of these tokens strongly imply a standing goal / future event.
    if re.search(r"\b(until|when|wait\s+for|watch\s+for|find|look\s+for|"
                 r"search\s+for|if\s+you\s+see|let\s+me\s+know|tell\s+me\s+"
                 r"when|tell\s+me\s+if|greet|every\s+time|whenever)\b", t):
        return True
    # Multiple imperatives chained with commas / 'then' / 'and then'.
    if re.search(r",\s*\w|\bthen\b|\band\s+then\b", t):
        return True
    # 4+ content words and ends in a question or includes 'see' —
    # likely a visual question.
    if len(t.split()) >= 5 and ("?" in t or " see " in t):
        return True
    return False


# ---------------------------------------------------------------- engine/state
class Engine:
    """Tiny shared state surface used by the planner's `look` tool, the
    vision thread, and the state publisher.  Thread-safe via a single lock.
    """

    def __init__(self):
        self._lock = threading.Lock()
        # class_name -> last_seen_ts
        self._seen_classes: dict[str, float] = {}
        self._battery_v: float | None = None
        self._last_wire_ack: dict | None = None
        self._last_ble_state: dict | None = None
        self._last_transcript: str = ""
        self._last_cmd: dict | None = None
        self._walking: bool = False

    def seen_classes(self) -> list[str]:
        with self._lock:
            return sorted(self._seen_classes.keys())

    def mark_seen(self, classes: list[str], ts: float) -> None:
        if not classes:
            return
        with self._lock:
            for c in classes:
                self._seen_classes[c] = ts
            # prune beyond 30 s
            cutoff = ts - 30.0
            self._seen_classes = {k: v for k, v in self._seen_classes.items()
                                  if v >= cutoff}

    def set_wire_ack(self, ack: dict | None) -> None:
        with self._lock:
            self._last_wire_ack = ack

    def set_ble_state(self, st: dict) -> None:
        with self._lock:
            self._last_ble_state = dict(st)
            # best-effort battery extraction
            v = st.get("v")
            if isinstance(v, (int, float)):
                self._battery_v = float(v) / 1000.0 if v > 100 else float(v)

    def set_transcript(self, text: str, cmd: dict | None) -> None:
        with self._lock:
            self._last_transcript = text
            self._last_cmd = cmd

    def set_walking(self, on: bool) -> None:
        with self._lock:
            self._walking = bool(on)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ts": time.time(),
                "seen_classes": sorted(self._seen_classes.keys()),
                "battery_v": self._battery_v,
                "last_wire_ack": self._last_wire_ack,
                "last_ble_state": self._last_ble_state,
                "last_transcript": self._last_transcript,
                "last_cmd": self._last_cmd,
                "walking": self._walking,
            }


# ---------------------------------------------------------------- planner tools
def build_planner_tools(wire_client, scripts: Path, engine: Engine, *,
                        tts_enabled: bool, camera_query=None,
                        dry_run: bool = False):
    """Bind the 8 tools the planner expects to the actual wire / TTS /
    vision subsystems.  `camera_query` may be None if phone_vision is
    unavailable — we fall back to engine._seen_classes for look_for."""

    def _wire(cmd: dict) -> dict:
        if dry_run or wire_client is None:
            engine.set_wire_ack({"ok": True, "dry_run": True, "echo": cmd})
            return {"ok": True, "dry_run": True}
        ack = wire_client.send(cmd)
        engine.set_wire_ack(ack)
        return ack if isinstance(ack, dict) else {"ok": False, "ack": str(ack)}

    def pose(name: str, duration_ms: int = 1500) -> dict:
        ack = _wire({"c": "pose", "n": name, "d": int(duration_ms)})
        return {"ok": bool(ack.get("ok", True)), "ack": ack}

    def walk(stride: int = 150, step: int = 400, **_) -> dict:
        ack = _wire({"c": "walk", "on": True,
                     "stride": int(stride), "step": int(step)})
        engine.set_walking(True)
        return {"ok": bool(ack.get("ok", True)), "ack": ack}

    def stop() -> dict:
        ack = _wire({"c": "stop"})
        engine.set_walking(False)
        return {"ok": bool(ack.get("ok", True)), "ack": ack}

    def jump() -> dict:
        ack = _wire({"c": "jump"})
        return {"ok": bool(ack.get("ok", True)), "ack": ack}

    def look(direction: str) -> dict:
        """Return whatever the vision thread has observed recently plus the
        requested direction (the latter is advisory — we don't pan
        servos; the phone camera has a fixed FOV)."""
        classes = engine.seen_classes()
        return {"ok": True, "direction": direction, "seen": classes}

    def look_for(query: str) -> dict:
        """Real-time one-shot vision query.  If phone_vision isn't loaded,
        fall back to the recent-events cache."""
        if camera_query is not None:
            try:
                r = camera_query.query([query])
                seen_list = r.get("seen", []) if isinstance(r, dict) else []
                scores = r.get("scores", {}) if isinstance(r, dict) else {}
                frame_ms = (r.get("frame_ms", 0)
                            if isinstance(r, dict) else 0)
                seen = bool(seen_list)
                score = 0.0
                if scores:
                    try:
                        score = float(max(scores.values()))
                    except Exception:
                        score = 0.0
                return {"ok": True, "seen": seen, "score": score,
                        "frame_ms": int(frame_ms),
                        "matches": seen_list}
            except Exception as e:
                log(f"[tools] look_for({query!r}) err: "
                    f"{type(e).__name__}: {e}")
        # Fallback: check the recent-events cache.
        classes = engine.seen_classes()
        q_lc = (query or "").lower()
        matched = [c for c in classes if c.lower() in q_lc or q_lc in c.lower()]
        return {"ok": True, "seen": bool(matched), "score": 0.0,
                "frame_ms": 0, "matches": matched,
                "_source": "cache_fallback"}

    def say(text: str) -> dict:
        run_tts(scripts, text, tts_enabled)
        return {"ok": True}

    def wait(seconds: float) -> dict:
        s = max(0.0, min(5.0, float(seconds)))
        time.sleep(s)
        return {"ok": True}

    return {
        "pose": pose,
        "walk": walk,
        "stop": stop,
        "jump": jump,
        "look": look,
        "look_for": look_for,
        "say": say,
        "wait": wait,
    }


# ---------------------------------------------------------------- handle a short command
def handle_short_cmd(cmd: dict, wire_client, scripts: Path, engine: Engine,
                     *, tts_enabled: bool) -> dict:
    """Fast path: regex/intent said this is a single primitive.  Send it."""
    if cmd.get("c") == "noop":
        engine.set_wire_ack(None)
        return {"ok": False, "noop": True}
    if wire_client is None:
        ack = {"ok": False, "err": "no wire"}
    else:
        ack = wire_client.send(cmd)
    engine.set_wire_ack(ack)
    if cmd.get("c") == "walk":
        engine.set_walking(True)
    elif cmd.get("c") == "stop":
        engine.set_walking(False)
    phrase = ack_phrase(cmd)
    if phrase and tts_enabled:
        run_tts(scripts, phrase, tts_enabled)
    return ack if isinstance(ack, dict) else {"ok": False, "ack": str(ack)}


# ---------------------------------------------------------------- voice thread
def voice_thread_fn(stop_evt: threading.Event, args, scripts: Path,
                    engine: Engine, wire_client, goal_keeper, planner,
                    tts_enabled: bool, record_sec: int,
                    input_source) -> None:
    """input_source: iterator of lines (stdin) OR None for STT loop."""
    def _route_utterance(text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        log(f"voice: {text!r}")
        # Shutdown phrases short-circuit — no network call needed.
        if _SHUTDOWN_RE.search(text):
            log("shutdown phrase detected — stopping daemon")
            if tts_enabled:
                run_tts(scripts, "shutting down", tts_enabled)
            stop_evt.set()
            return
        # Cancel standing goal?
        if _CANCEL_RE.search(text) and goal_keeper is not None:
            gs = goal_keeper.status()
            if gs.get("state") == "active":
                goal_keeper.cancel()
                if tts_enabled:
                    run_tts(scripts, "okay, cancelling", tts_enabled)
                return

        # Decide routing.  If GoalKeeper+Planner are both disabled, always
        # use the short path (legacy one-shot mode).
        if goal_keeper is None or planner is None:
            cmd = run_intent(scripts, text)
            engine.set_transcript(text, cmd)
            if cmd.get("_auth_fail") and tts_enabled:
                run_tts(scripts, "I'm not connected", tts_enabled)
                return
            handle_short_cmd(cmd, wire_client, scripts, engine,
                             tts_enabled=tts_enabled)
            return

        multistep = looks_multistep(text)
        if not multistep:
            # Fast path: regex/intent → single wire cmd.
            cmd = run_intent(scripts, text)
            engine.set_transcript(text, cmd)
            if cmd.get("_auth_fail"):
                if tts_enabled:
                    run_tts(scripts, "I'm not connected", tts_enabled)
                return
            if cmd.get("c") and cmd.get("c") != "noop":
                handle_short_cmd(cmd, wire_client, scripts, engine,
                                 tts_enabled=tts_enabled)
                return
            # intent said noop — promote to planner since the user said
            # something.
            log("intent returned noop on non-empty utterance — "
                "promoting to planner")

        # Planner path: install as a standing goal.  Initial turn is
        # synchronous (GoalKeeper.set_goal blocks).  That's fine — we're
        # on the voice thread, not the event hot path.
        engine.set_transcript(text, None)
        log(f"goal-keeper: installing goal {text!r}")
        try:
            result = goal_keeper.set_goal(text)
        except Exception as e:
            log(f"goal-keeper error: {type(e).__name__}: {e}")
            if tts_enabled:
                run_tts(scripts, "I couldn't plan that", tts_enabled)
            return
        reason = str(result.get("reason") or "")
        if reason == "auth" and tts_enabled:
            run_tts(scripts, "I'm not connected", tts_enabled)
            return
        # If planner already said something, phone_tts was called by the
        # `say` tool binding inside the planner loop.  Nothing more here.

    # Main voice loop.
    if input_source is not None:
        # stdin line source (text mode).
        for line in input_source:
            if stop_evt.is_set():
                break
            _route_utterance(line)
            if stop_evt.is_set():
                break
        # Stdin exhausted — signal shutdown so the daemon doesn't hang.
        log("voice: stdin EOF, requesting shutdown")
        stop_evt.set()
        return

    # STT mode: record windows in a loop until stop_evt.
    log(f"voice: STT loop, record_sec={record_sec}")
    while not stop_evt.is_set():
        transcript = run_stt(scripts, record_sec)
        if stop_evt.is_set():
            break
        if transcript:
            _route_utterance(transcript)
        # short breather to avoid hot-spinning when stt_ returns empty
        time.sleep(0.1)


# ---------------------------------------------------------------- vision thread
def vision_thread_fn(stop_evt: threading.Event, engine: Engine,
                     camera_query, goal_keeper) -> None:
    """Poll phone_vision at VISION_POLL_S.  Emit events to GoalKeeper.

    The phrase list is goal-aware: when a goal is active we pull tokens
    out of the goal text; otherwise we use DEFAULT_WATCHLIST.  Rate is
    fixed 2 Hz — see "known weaknesses" in the final report.
    """
    if camera_query is None:
        log("vision: disabled (CameraQuery is None)")
        return
    log(f"vision: polling every {VISION_POLL_S:.2f}s")
    # class label -> last-seen ts, to debounce (10 s cool-off per class).
    last_fire: dict[str, float] = {}
    COOLOFF_S = 10.0
    while not stop_evt.wait(VISION_POLL_S):
        # Choose phrases to look for.
        phrases = list(DEFAULT_WATCHLIST)
        if goal_keeper is not None:
            gs = goal_keeper.status()
            goal_text = gs.get("goal") if gs.get("state") == "active" else None
            if goal_text:
                # Include the goal's own substantive phrases — crude:
                # pull noun-ish chunks by looking for "a NOUN" / "the NOUN"
                # or a bare content word.  Kept simple to avoid pulling
                # NLTK onto the phone.
                extra: list[str] = []
                for m in re.finditer(
                        r"\b(?:an?|the)\s+[a-z]+(?:\s+[a-z]+)?", goal_text, re.I):
                    extra.append(m.group(0).strip())
                # Fall back: single content words > 3 chars that aren't
                # common stopwords.
                tokens = re.findall(r"[A-Za-z]{4,}", goal_text.lower())
                tokens = [t for t in tokens if t not in {
                    "walk", "until", "find", "when", "you", "see", "wait",
                    "look", "watch", "then", "with", "that", "this"}]
                for t in tokens[:3]:
                    extra.append(f"a {t}")
                if extra:
                    phrases = list(dict.fromkeys(extra + phrases))[:6]
        try:
            r = camera_query.query(phrases)
        except Exception as e:
            log(f"vision query err: {type(e).__name__}: {e}")
            continue
        if not isinstance(r, dict):
            continue
        err = r.get("error")
        if err:
            # Camera busy or bad init — just log, don't spam.
            continue
        seen = r.get("seen") or []
        scores = r.get("scores") or {}
        if not isinstance(seen, list) or not seen:
            continue
        # Update engine.
        engine.mark_seen([str(c) for c in seen], time.time())
        # Fan to GoalKeeper (debounced per class).
        if goal_keeper is None:
            continue
        now = time.time()
        for cls in seen:
            cls_s = str(cls)
            if now - last_fire.get(cls_s, 0.0) < COOLOFF_S:
                continue
            last_fire[cls_s] = now
            try:
                conf = float(scores.get(cls_s, 0.0)) if isinstance(
                    scores, dict) else 0.0
            except Exception:
                conf = 0.0
            try:
                goal_keeper.on_event({
                    "type": "vision",
                    "class": cls_s,
                    "phrase": cls_s,
                    "conf": conf,
                    "ts": now,
                })
            except Exception as e:
                log(f"vision -> gk err: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- ble reader thread
class BleReader:
    """Consume unsolicited state lines from the wire socket.

    The wire is request/response for commands, but the companion app
    also pushes periodic state packets ("v", "t_c", etc.) between acks.
    This thread opens a separate long-lived connection and drains lines.
    On any state line we update engine.set_ble_state.  Battery thresholds
    synthesize events to GoalKeeper.
    """

    BATTERY_LOW_MV = 3450  # per-cell-ish threshold for our pack
    BATTERY_COOLOFF_S = 60.0

    def __init__(self, host: str, port: int, engine: Engine,
                 goal_keeper, stop_evt: threading.Event,
                 tts_enabled: bool, scripts: Path):
        self.host = host
        self.port = port
        self.engine = engine
        self.gk = goal_keeper
        self.stop_evt = stop_evt
        self.tts = tts_enabled
        self.scripts = scripts
        self._last_battery_warn = 0.0

    def run(self) -> None:
        log(f"ble_reader: connecting to {self.host}:{self.port}")
        while not self.stop_evt.is_set():
            try:
                self._loop_one_connection()
            except Exception as e:
                log(f"ble_reader: {type(e).__name__}: {e}")
            # brief backoff
            for _ in range(20):
                if self.stop_evt.is_set():
                    return
                time.sleep(0.1)

    def _loop_one_connection(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        try:
            s.connect((self.host, self.port))
        except OSError as e:
            log(f"ble_reader: connect failed: {e}")
            s.close()
            return
        s.settimeout(1.0)
        buf = b""
        try:
            while not self.stop_evt.is_set():
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                except OSError as e:
                    log(f"ble_reader: recv err: {e}")
                    break
                if not chunk:
                    log("ble_reader: socket closed by peer")
                    break
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    self._handle_line(line)
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _handle_line(self, line: bytes) -> None:
        if not line.strip():
            return
        try:
            msg = json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        # An ack ({"ok": true, "echo": ...}) doesn't carry state — skip it.
        if "echo" in msg and set(msg.keys()) <= {"ok", "echo", "ts", "err"}:
            return
        # Plausible state packet: voltage, temperature, etc.
        self.engine.set_ble_state(msg)
        # Battery event?
        v_raw = msg.get("v")
        if isinstance(v_raw, (int, float)):
            now = time.time()
            is_low = (v_raw < self.BATTERY_LOW_MV if v_raw > 100
                      else v_raw < (self.BATTERY_LOW_MV / 1000.0))
            if is_low and (now - self._last_battery_warn
                           > self.BATTERY_COOLOFF_S):
                self._last_battery_warn = now
                log(f"ble_reader: LOW battery v={v_raw}")
                if self.tts:
                    run_tts(self.scripts, "battery low", self.tts)
                if self.gk is not None:
                    try:
                        self.gk.on_event({"type": "battery",
                                          "v": float(v_raw), "ts": now})
                    except Exception as e:
                        log(f"ble_reader -> gk err: {e}")


# ---------------------------------------------------------------- state writer thread
def state_writer_thread_fn(stop_evt: threading.Event, engine: Engine,
                           goal_keeper, state_path: Path) -> None:
    """Writes a JSON snapshot to phone_state.json every 500 ms via atomic
    os.replace.  The adb host polls this for introspection."""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        log(f"state_writer: mkdir {state_path.parent}: {e}")
        return
    period = 1.0 / STATE_WRITE_HZ
    tmp_path = str(state_path) + ".tmp"
    while not stop_evt.wait(period):
        try:
            snap = engine.snapshot()
            if goal_keeper is not None:
                snap["goal"] = goal_keeper.status()
            else:
                snap["goal"] = None
            with open(tmp_path, "w") as fh:
                json.dump(snap, fh, default=str)
            os.replace(tmp_path, str(state_path))
        except Exception as e:
            # Never crash the daemon on a failed publish.
            log(f"state_writer err: {type(e).__name__}: {e}")


# ---------------------------------------------------------------- per-turn logger
class TurnLog:
    """Appends human-readable per-turn records to a /sdcard log so the host
    can read them via adb.  Also used for validation flag output."""

    def __init__(self, path: Path):
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            log(f"turnlog mkdir: {e}")
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._lock:
            try:
                with open(self.path, "a") as fh:
                    fh.write(f"[{stamp}] {line}\n")
            except Exception as e:
                log(f"turnlog append: {e}")


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("text", "voice"), default="voice")
    ap.add_argument("--text", help="single utterance, then exit")
    ap.add_argument("--scripts-dir",
                    help="dir holding phone_intent.py, phone_stt.sh, ...")
    ap.add_argument("--wire-host", default=DEFAULT_WIRE_HOST)
    ap.add_argument("--wire-port", type=int, default=DEFAULT_WIRE_PORT)
    ap.add_argument("--mock-wire", action="store_true",
                    help="auto-spawn mock_wire_server on wire_host:wire_port "
                         "if nothing is listening")
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--no-vision", action="store_true",
                    help="disable the vision poll thread")
    ap.add_argument("--no-goal-keeper", action="store_true",
                    help="disable GoalKeeper + Planner — legacy one-shot mode")
    ap.add_argument("--no-ble-reader", action="store_true",
                    help="don't open the secondary socket for state packets")
    ap.add_argument("--no-state-file", action="store_true",
                    help="disable writing phone_state.json")
    ap.add_argument("--record-sec", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true",
                    help="log wire commands but don't actually send")
    ap.add_argument("--log-file",
                    default=str(STATE_DIR / "phone_daemon_test.log"),
                    help="per-turn append log (for adb introspection)")
    args = ap.parse_args()

    scripts = resolve_scripts_dir(args.scripts_dir)
    log(f"scripts dir: {scripts}")
    sys.path.insert(0, str(scripts))

    turnlog = TurnLog(Path(args.log_file))
    turnlog.append(
        f"START mode={args.mode} no_vision={args.no_vision} "
        f"no_tts={args.no_tts} no_goal_keeper={args.no_goal_keeper} "
        f"dry_run={args.dry_run}")

    # wire
    phone_wire = _import_wire()
    mock_proc = None
    if args.mock_wire:
        mock_proc = maybe_spawn_mock(scripts, args.wire_host, args.wire_port)
    wire_client = None
    if phone_wire is not None:
        wire_client = phone_wire.WireClient(host=args.wire_host,
                                            port=args.wire_port)

    tts_enabled = not args.no_tts

    # engine
    engine = Engine()

    # vision — optional, graceful
    camera_query = None
    if not args.no_vision:
        try:
            import phone_vision  # type: ignore
            camera_query = phone_vision.CameraQuery()
            log("vision: phone_vision.CameraQuery() ready")
            turnlog.append("vision: ready")
        except Exception as e:
            log(f"vision: phone_vision unavailable ({type(e).__name__}: {e}) "
                "— falling back to look() only")
            turnlog.append(f"vision: unavailable ({type(e).__name__}: {e})")
            camera_query = None

    # planner + goal_keeper — optional
    planner = None
    goal_keeper = None
    if not args.no_goal_keeper:
        try:
            import phone_planner  # type: ignore
            import phone_goal_keeper  # type: ignore
            tools = build_planner_tools(
                wire_client, scripts, engine,
                tts_enabled=tts_enabled,
                camera_query=camera_query,
                dry_run=args.dry_run,
            )
            planner = phone_planner.Planner(tools, logger=log)
            goal_keeper = phone_goal_keeper.GoalKeeper(planner, logger=log,
                                                       max_followups=5)
            log("planner + goal_keeper ready")
            turnlog.append("planner: ready")
        except SystemExit:
            # phone_planner exits on missing API key — degrade gracefully.
            log("planner: no API key — one-shot mode only")
            turnlog.append("planner: disabled (no api key)")
            planner = None
            goal_keeper = None
        except Exception as e:
            log(f"planner: init failed ({type(e).__name__}: {e})")
            turnlog.append(f"planner: disabled ({type(e).__name__}: {e})")
            planner = None
            goal_keeper = None

    # threads
    stop_evt = threading.Event()

    def _sigint(_signum, _frame):
        log("SIGINT received")
        stop_evt.set()

    signal.signal(signal.SIGINT, _sigint)
    try:
        signal.signal(signal.SIGTERM, _sigint)
    except Exception:
        pass

    threads: list[threading.Thread] = []

    # state writer
    if not args.no_state_file:
        state_path = Path(args.log_file).parent / "phone_state.json"
        t = threading.Thread(
            target=state_writer_thread_fn,
            args=(stop_evt, engine, goal_keeper, state_path),
            name="state_writer", daemon=True)
        t.start()
        threads.append(t)

    # ble reader
    if not args.no_ble_reader and wire_client is not None:
        reader = BleReader(args.wire_host, args.wire_port, engine,
                           goal_keeper, stop_evt, tts_enabled, scripts)
        t = threading.Thread(target=reader.run, name="ble_reader",
                             daemon=True)
        t.start()
        threads.append(t)

    # vision
    if camera_query is not None:
        t = threading.Thread(
            target=vision_thread_fn,
            args=(stop_evt, engine, camera_query, goal_keeper),
            name="vision", daemon=True)
        t.start()
        threads.append(t)

    # voice
    # Text mode: feed stdin/--text (the debugging path).
    # Voice mode: use phone_voice.VoiceListener — a continuous STT thread
    # so the user can actually TALK to the robot, instead of the old
    # blocking stdin loop.  We still go through voice_thread_fn: it
    # owns the routing logic (shutdown phrases, goal-keeper vs fast
    # path).  VoiceListener drops transcripts into a queue that
    # voice_thread_fn drains as if it were stdin.
    input_source = None
    voice_listener = None
    if args.mode == "text":
        if args.text is not None:
            input_source = iter([args.text])
        else:
            input_source = iter(sys.stdin)
    else:
        try:
            import phone_voice  # type: ignore
            utter_q: "queue.Queue[str]" = queue.Queue(maxsize=32)

            def _on_utter(text: str) -> None:
                try:
                    utter_q.put_nowait(text)
                except queue.Full:
                    log("voice: utterance queue full, dropping")

            # Wake word default matches VoiceListener's own default
            # ("hey robot").  Set PHONE_VOICE_WAKE_WORD='' to disable.
            env_wake = os.environ.get("PHONE_VOICE_WAKE_WORD", "hey robot")
            wake = env_wake if env_wake else None
            voice_listener = phone_voice.VoiceListener(
                on_utterance=_on_utter,
                wake_word=wake,
                record_seconds=float(args.record_sec),
                logger=log,
            )
            voice_listener.start()
            turnlog.append(f"voice: listener started wake={wake!r}")

            # Feed the queue to voice_thread_fn as a stdin-like iterator.
            def _q_iter():
                while not stop_evt.is_set():
                    try:
                        yield utter_q.get(timeout=0.5)
                    except queue.Empty:
                        continue

            input_source = _q_iter()
        except Exception as e:
            log(f"voice_listener init failed ({type(e).__name__}: {e}) "
                "— falling back to phone_stt.sh one-shot loop")
            turnlog.append(
                f"voice: listener unavailable ({type(e).__name__}: {e})")
            voice_listener = None
            input_source = None
    vt = threading.Thread(
        target=voice_thread_fn,
        args=(stop_evt, args, scripts, engine, wire_client,
              goal_keeper, planner, tts_enabled, args.record_sec,
              input_source),
        name="voice", daemon=True)
    vt.start()
    threads.append(vt)

    # Main loop: wait for stop signal.
    try:
        while not stop_evt.is_set():
            vt.join(timeout=0.25)
            if not vt.is_alive():
                # voice thread finished naturally (stdin EOF in text mode)
                # — give async workers a moment to drain.
                break
    except KeyboardInterrupt:
        stop_evt.set()
    finally:
        stop_evt.set()
        # Let goal-keeper follow-ups finish, but don't block forever.
        if goal_keeper is not None:
            try:
                goal_keeper.wait_idle(timeout=5.0)
            except Exception:
                pass
        if wire_client is not None:
            try:
                wire_client.close()
            except Exception:
                pass
        if mock_proc is not None:
            try:
                mock_proc.terminate()
                mock_proc.wait(timeout=2.0)
            except Exception:
                try:
                    mock_proc.kill()
                except Exception:
                    pass
        # One final snapshot of useful summary.
        snap = engine.snapshot()
        turnlog.append(
            f"STOP snap.last_transcript={snap['last_transcript']!r} "
            f"last_cmd={snap['last_cmd']} "
            f"last_wire_ack={snap['last_wire_ack']} "
            f"walking={snap['walking']} "
            f"seen_classes={snap['seen_classes']}")
        if goal_keeper is not None:
            try:
                gs = goal_keeper.status()
                turnlog.append(
                    f"STOP goal={gs.get('goal')!r} state={gs.get('state')} "
                    f"followups={gs.get('followups')}")
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
