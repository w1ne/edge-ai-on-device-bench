#!/data/data/com.termux/files/usr/bin/env python3
"""phone_daemon.py — phone-side minimum robot brain.

Runs inside Termux on the Pixel 6. Mirrors the critical path of
demo/robot_daemon.py but without vision, state server, web UI, behaviors
engine, or wake-word — those are deferred to Phase 2 once the Android BLE
companion app (127.0.0.1:5557 socket) is live.

Loop (one_turn):
    voice   -> phone_stt.sh      (WAV via termux-microphone-record + whisper-cli)
    intent  -> phone_intent.py   (DeepInfra chat/completions, stdlib)
    planner -> DeepInfra via requests-free urllib call (optional)
    wire    -> phone_wire.py     (TCP 127.0.0.1:5557, line-JSON)
    speak   -> phone_tts.sh      (piper | termux-tts-speak | espeak-ng)

Modes:
    --mode text  -> read transcript lines from stdin (or --text once)
    --mode voice -> record 3 s via termux-microphone-record, loop forever

Mocks:
    --mock-wire  -> auto-spawn mock_wire_server.py locally if nothing is
                    listening on 127.0.0.1:5557. Used for dev before the
                    companion app ships.

Run:
    python3 $HOME/phone_daemon.py --mode text --mock-wire
    echo "lean left" | python3 $HOME/phone_daemon.py --mode text

Dependencies (Termux):
    pkg install -y python termux-api
    + scripts/termux/phone_{intent,stt,tts,wire}.* in $HOME or --scripts-dir
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("HOME", str(Path.home())))

DEFAULT_WIRE_HOST = "127.0.0.1"
DEFAULT_WIRE_PORT = 5557


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
    for cand in (user_override, os.environ.get("PHONE_SCRIPTS_DIR"), str(HERE), str(HOME)):
        if not cand:
            continue
        p = Path(cand).expanduser().resolve()
        if (p / "phone_intent.py").exists():
            return p
    raise SystemExit(f"cannot locate phone_intent.py — tried {HERE}, {HOME}")


# ---------------------------------------------------------------- subprocesses
def run_stt(scripts: Path, record_sec: int) -> str:
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
    """Call phone_intent.py with transcript on stdin. Return wire cmd dict."""
    script = scripts / "phone_intent.py"
    if not script.exists():
        log(f"WARN: {script} missing — noop")
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
        return {"c": "noop"}
    if proc.returncode not in (0, 4):
        log(f"intent parser rc={proc.returncode}: {proc.stderr.strip()[:200]}")
    # Last JSON-looking line wins.
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


def run_tts(scripts: Path, text: str) -> None:
    if not text:
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


# ---------------------------------------------------------------- wire
def _import_wire():
    """Import phone_wire from the scripts dir (added to sys.path on startup)."""
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
    # Wait for bind.
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


# ---------------------------------------------------------------- one turn
def one_turn(transcript: str, scripts: Path, wire_client, *, speak: bool) -> dict:
    """Process a single utterance end-to-end.

    Returns the decision dict for logging. The wire ack is included as
    'wire_ack'."""
    transcript = transcript.strip()
    if not transcript:
        log("empty transcript — skip")
        return {"transcript": "", "cmd": {"c": "noop"}, "wire_ack": None}

    log(f"transcript: {transcript!r}")
    cmd = run_intent(scripts, transcript)
    log(f"intent: {cmd}")

    if cmd.get("c") == "noop":
        ack = None
        log("decision: noop (intent parser returned noop)")
    else:
        ack = wire_client.send(cmd) if wire_client is not None else {"ok": False, "err": "no wire"}
        log(f"wire ack: {ack}")
        phrase = ack_phrase(cmd)
        if speak and phrase:
            run_tts(scripts, phrase)
        log(f"decision: {cmd}")

    return {"transcript": transcript, "cmd": cmd, "wire_ack": ack}


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("text", "voice"), default="text")
    ap.add_argument("--text", help="single utterance, then exit")
    ap.add_argument("--scripts-dir",
                    help="dir holding phone_intent.py, phone_stt.sh, ...")
    ap.add_argument("--wire-host", default=DEFAULT_WIRE_HOST)
    ap.add_argument("--wire-port", type=int, default=DEFAULT_WIRE_PORT)
    ap.add_argument("--mock-wire", action="store_true",
                    help="auto-spawn mock_wire_server on wire_host:wire_port "
                         "if nothing is listening")
    ap.add_argument("--no-tts", action="store_true")
    ap.add_argument("--record-sec", type=int, default=3)
    args = ap.parse_args()

    scripts = resolve_scripts_dir(args.scripts_dir)
    log(f"scripts dir: {scripts}")
    # Make phone_wire importable.
    sys.path.insert(0, str(scripts))

    phone_wire = _import_wire()

    mock_proc = None
    if args.mock_wire:
        mock_proc = maybe_spawn_mock(scripts, args.wire_host, args.wire_port)

    wire_client = None
    if phone_wire is not None:
        wire_client = phone_wire.WireClient(host=args.wire_host, port=args.wire_port)

    speak = not args.no_tts

    try:
        if args.mode == "text":
            if args.text is not None:
                one_turn(args.text, scripts, wire_client, speak=speak)
            elif not sys.stdin.isatty():
                # Piped input: one line = one turn.
                for line in sys.stdin:
                    one_turn(line, scripts, wire_client, speak=speak)
            else:
                log("text mode, interactive: type utterances, ^D to exit")
                try:
                    for line in sys.stdin:
                        one_turn(line, scripts, wire_client, speak=speak)
                except (KeyboardInterrupt, EOFError):
                    pass
        else:  # voice
            log(f"voice mode: {args.record_sec}s capture windows, ctrl-c to exit")
            try:
                while True:
                    transcript = run_stt(scripts, args.record_sec)
                    if transcript:
                        one_turn(transcript, scripts, wire_client, speak=speak)
                    else:
                        log("no speech — continuing")
            except KeyboardInterrupt:
                log("ctrl-c")
    finally:
        if wire_client is not None:
            wire_client.close()
        if mock_proc is not None:
            mock_proc.terminate()
            try:
                mock_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                mock_proc.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
