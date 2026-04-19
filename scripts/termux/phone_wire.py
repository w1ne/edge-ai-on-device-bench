#!/data/data/com.termux/files/usr/bin/env python3
"""phone_wire.py — Termux-side wire client.

Opens a TCP connection to 127.0.0.1:5557 where the Android BLE companion
app (built by a sibling agent) exposes the ESP32 wire as a line-framed JSON
socket. Each command is one JSON object + newline; the companion replies
with one JSON ack + newline.

Public API:
    client = WireClient()
    ack = client.send({"c": "pose", "n": "lean_left", "d": 1500})

Used by phone_daemon.py. Also runnable standalone for debugging:
    echo '{"c":"noop"}' | python3 phone_wire.py

The companion app is NOT assumed to be running during dev. Use
mock_wire_server.py on the phone to get a local loopback.

Design notes:
- One connection, serialized sends (no pipelining). The wire protocol is
  request/response; the companion's BLE back-end can't handle concurrent
  writes anyway.
- Reconnect on any send error. Back off briefly, then retry once. If the
  second attempt also fails, return a synthetic error ack so the daemon
  stays alive (mirrors send_wire()'s noop-on-failure behavior).
- No Python deps beyond stdlib.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

HOST = os.environ.get("PHONE_WIRE_HOST", "127.0.0.1")
PORT = int(os.environ.get("PHONE_WIRE_PORT", "5557"))
CONNECT_TIMEOUT_S = float(os.environ.get("PHONE_WIRE_CONNECT_TIMEOUT", "2.0"))
IO_TIMEOUT_S = float(os.environ.get("PHONE_WIRE_IO_TIMEOUT", "3.0"))


def eprint(*a, **kw):
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


class WireClient:
    """Thread-safe single-connection TCP client.

    send() is serialized with a mutex. On any IO error we drop the socket
    and reconnect on the next call.
    """

    def __init__(self, host: str = HOST, port: int = PORT):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._buf = b""
        self._lock = threading.Lock()

    # -- connection mgmt -----------------------------------------------------
    def _connect(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(CONNECT_TIMEOUT_S)
        s.connect((self.host, self.port))
        s.settimeout(IO_TIMEOUT_S)
        return s

    def _drop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
        self._sock = None
        self._buf = b""

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _ensure(self) -> socket.socket:
        if self._sock is None:
            self._sock = self._connect()
        return self._sock

    # -- io helpers ----------------------------------------------------------
    def _readline(self, s: socket.socket) -> bytes:
        # Accumulate until newline or timeout.
        while b"\n" not in self._buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("wire: remote closed")
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        return line

    def _once(self, cmd: dict) -> dict:
        s = self._ensure()
        payload = (json.dumps(cmd, separators=(",", ":")) + "\n").encode("utf-8")
        s.sendall(payload)
        line = self._readline(s)
        if not line.strip():
            return {"ok": False, "err": "wire: empty ack"}
        try:
            return json.loads(line.decode("utf-8", "replace"))
        except json.JSONDecodeError as e:
            return {"ok": False, "err": f"wire: bad ack json: {e}"}

    # -- public --------------------------------------------------------------
    def send(self, cmd: dict) -> dict:
        """Send one command, return ack dict.

        Retries once on any transport error. Never raises — the daemon
        wants a best-effort, noop-on-failure contract.
        """
        with self._lock:
            for attempt in (1, 2):
                try:
                    return self._once(cmd)
                except (OSError, ConnectionError) as e:
                    eprint(f"[phone_wire] attempt {attempt} failed: "
                           f"{type(e).__name__}: {e}")
                    self._drop()
                    if attempt == 2:
                        return {"ok": False, "err": f"wire: {type(e).__name__}"}
                    time.sleep(0.2)
            return {"ok": False, "err": "wire: unreachable"}


# Module-level convenience API (so callers don't have to hold a client).
_DEFAULT: WireClient | None = None


def send_wire(cmd: dict) -> dict:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = WireClient()
    return _DEFAULT.send(cmd)


# -- CLI (for `bash -x phone_daemon.py`-style debugging) ---------------------
def _cli() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--cmd", help="JSON command on the CLI (default: stdin)")
    args = ap.parse_args()

    if args.cmd:
        raw = args.cmd
    else:
        raw = sys.stdin.read().strip()
    if not raw:
        eprint("[phone_wire] no command on --cmd or stdin")
        return 2
    try:
        cmd = json.loads(raw)
    except json.JSONDecodeError as e:
        eprint(f"[phone_wire] bad json on input: {e}")
        return 2

    client = WireClient(host=args.host, port=args.port)
    ack = client.send(cmd)
    print(json.dumps(ack, separators=(",", ":")))
    client.close()
    return 0 if ack.get("ok", False) else 1


if __name__ == "__main__":
    sys.exit(_cli())
