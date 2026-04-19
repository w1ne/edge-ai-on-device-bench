#!/usr/bin/env python3
"""
test_sse_reliability.py -- exercise the three failure modes the roast
called out: SSE reconnect, queue-full drop detection, and daemon-restart
recovery.  Adds a heartbeat assertion for completeness.

Runs against ``demo/robot_daemon.RobotState`` wired to the real
``demo/state_server.make_server``.  We spawn a tiny state-only daemon in
a subprocess per test so we can kill and restart it without touching the
rest of the daemon's hardware stack (USB, TTS, vision).

Usage:
    python3 scripts/test_sse_reliability.py

Exit code 0 iff all four tests PASS.  Non-zero otherwise, with a reason
printed on stderr so CI logs show why.

No pip deps -- stdlib only.  We re-implement a minimal SSE parser on top
of urllib so the test never needs ``requests`` (present on this host, but
we don't want to bind the test to that).
"""
from __future__ import annotations

import io
import json
import os
import queue
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEMO = REPO / "demo"
# Some subprocess code imports robot_daemon which imports robot_behaviors;
# keep the demo dir on sys.path inside the child so that works.
CHILD_ENV = os.environ.copy()
CHILD_ENV["PYTHONPATH"] = f"{DEMO}:{CHILD_ENV.get('PYTHONPATH', '')}"


# ---------------------------------------------------------------- helpers
def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_listening(port: int, timeout: float = 5.0) -> bool:
    """Poll until the port accepts a TCP connection, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.05)
    return False


# Tiny daemon that hosts exactly one RobotState + state_server on a given
# port.  Shares stdin as a command pipe so the test driver can trigger
# state.update() from outside.  Commands are one line of JSON on stdin;
# ``{"op":"update","kw":{...}}`` calls state.update(**kw), ``{"op":"quit"}``
# exits cleanly.  A second stdin pipe isn't needed: ``sys.stdin`` is fine.
_DAEMON_SRC = r"""
import json, sys, threading, time
# Parent supplies --demo-dir so we don't depend on __file__ (which is
# undefined when Python is invoked via -c).
port = int(sys.argv[1])
demo_dir = sys.argv[2]
sys.path.insert(0, demo_dir)
from robot_daemon import RobotState
from state_server import make_server
state = RobotState(path=None)  # no legacy file write
# Route drop logs to stderr so the test can grep for them.
state._drop_log = lambda msg: print(msg, file=sys.stderr, flush=True)

server = make_server(
    state, bind="127.0.0.1", port=port, stop_fn=None,
    logger=lambda _m: None,
    auth_token=None,  # loopback + no auth = frictionless test
    auth_token_path=None,
    cors_origins=("http://127.0.0.1:5555",),
)
th = threading.Thread(target=server.serve_forever, daemon=True)
th.start()
# Announce readiness on stdout so the parent can unblock.
print("READY", flush=True)
try:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        op = msg.get("op")
        if op == "update":
            state.update(**msg.get("kw", {}))
            print(f"ACK {state.current_seq()}", flush=True)
        elif op == "quit":
            break
finally:
    server.shutdown()
    server.server_close()
"""


def _spawn_daemon(port: int):
    """Launch the mini-daemon subprocess and wait for its READY line."""
    # We pass the source via -c so we don't litter tmp files.  stderr is
    # piped so test 2 can assert the drop-log line appeared.
    proc = subprocess.Popen(
        [sys.executable, "-c", _DAEMON_SRC, str(port), str(DEMO)],
        cwd=str(REPO),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=CHILD_ENV,
    )
    # Wait for READY (or early-exit).
    line = proc.stdout.readline() if proc.stdout else ""
    if line.strip() != "READY":
        try:
            proc.terminate()
        except Exception:
            pass
        out = proc.stdout.read() if proc.stdout else ""
        err = proc.stderr.read() if proc.stderr else ""
        raise RuntimeError(
            f"daemon failed to start: out={line!r}{out!r} err={err!r}")
    if not _wait_listening(port):
        proc.terminate()
        raise RuntimeError(f"daemon port {port} never opened")
    return proc


def _kill_daemon(proc) -> str:
    """Kill + drain stderr.  Return the captured stderr string."""
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2.0)
    err = ""
    if proc.stderr is not None:
        try:
            err = proc.stderr.read() or ""
        except Exception:
            err = ""
    return err


def _send_cmd(proc, op: str, **kw) -> str | None:
    """Pipe a JSON command to the daemon's stdin; wait for its ACK line.
    Returns the ACK line (or None for 'quit')."""
    if proc.stdin is None:
        return None
    proc.stdin.write(json.dumps({"op": op, "kw": kw}) + "\n")
    proc.stdin.flush()
    if op == "quit":
        return None
    if proc.stdout is None:
        return None
    return proc.stdout.readline().strip()


# ---------------------------------------------------------------- SSE client
class SSEClient:
    """Stdlib-only SSE reader.  Parses ``id: N`` and ``data: ...`` lines,
    yields one event per blank-line-terminated block.

    Why not ``requests.iter_lines``?  The roast bans pip deps and we want
    the test to work on a minimal machine.  urllib.request gives us a
    streaming handle when we don't call read() upfront.
    """

    def __init__(self, url: str, *, last_event_id: str | None = None,
                 timeout: float = 5.0):
        # Use raw sockets + hand-rolled HTTP/1.1 request so we control the
        # recv buffer and can vary the per-read timeout (fast-path events
        # need ~3 s, heartbeat test needs ~30 s).  urllib.request wraps the
        # response in a BufferedReader which eats the initial bytes we
        # want to see.
        from urllib.parse import urlsplit
        u = urlsplit(url)
        host = u.hostname or "127.0.0.1"
        port = u.port or (443 if u.scheme == "https" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        self._sock = socket.create_connection((host, port), timeout=timeout)
        req = [
            f"GET {path} HTTP/1.1",
            f"Host: {host}:{port}",
            "Accept: text/event-stream",
            "Connection: keep-alive",
        ]
        if last_event_id is not None:
            req.append(f"Last-Event-ID: {last_event_id}")
        req.append("\r\n")
        self._sock.sendall(("\r\n".join(req)).encode())
        self._buf = b""
        self._closed = False
        # Consume response headers up through the empty line.  ``status`` is
        # parsed from the first line for the /events 200 assertion.
        self.status = None
        while b"\r\n\r\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                break
            self._buf += chunk
        head_end = self._buf.index(b"\r\n\r\n")
        head = self._buf[:head_end].decode("iso-8859-1", "replace")
        self._buf = self._buf[head_end + 4:]  # body starts here
        try:
            self.status = int(head.split(" ", 2)[1])
        except Exception:
            self.status = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sock.close()
        except Exception:
            pass

    def _read_line(self, timeout: float) -> bytes | None:
        """Read one \\n-terminated line from the body.  Uses per-call
        select() so callers can vary the deadline (3 s for fast-path
        events, 30 s for the heartbeat test).

        Returns None on EOF or timeout.
        """
        import select
        deadline = time.time() + timeout
        while b"\n" not in self._buf:
            remaining = deadline - time.time()
            if remaining <= 0:
                return None
            try:
                r, _, _ = select.select([self._sock], [], [], remaining)
            except OSError:
                return None
            if not r:
                return None
            try:
                chunk = self._sock.recv(4096)
            except (OSError, socket.error):
                return None
            if not chunk:
                # EOF: hand back whatever's in the buffer if non-empty.
                if self._buf:
                    out, self._buf = self._buf, b""
                    return out
                return None
            self._buf += chunk
        idx = self._buf.index(b"\n")
        line = self._buf[: idx + 1]
        self._buf = self._buf[idx + 1 :]
        return line

    def next_event(self, timeout: float = 5.0):
        """Read raw SSE lines until we hit the blank-line terminator.
        Returns a dict: {"id": str|None, "data": str|None, "comments": [str]}
        or None on timeout / EOF."""
        deadline = time.time() + timeout
        event = {"id": None, "data": None, "comments": []}
        saw_any = False
        while True:
            remaining = max(0.05, deadline - time.time())
            line = self._read_line(remaining)
            if line is None:
                return event if saw_any else None
            saw_any = True
            stripped = line.rstrip(b"\r\n")
            if stripped == b"":
                # End of event.  A frame with only comments + no data is
                # still returned (e.g., the ``: retry 1000`` prelude).
                return event
            if stripped.startswith(b":"):
                event["comments"].append(stripped[1:].lstrip().decode(
                    "utf-8", "replace"))
                continue
            if stripped.startswith(b"id:"):
                event["id"] = stripped[3:].strip().decode()
                continue
            if stripped.startswith(b"data:"):
                event["data"] = stripped[5:].lstrip().decode(
                    "utf-8", "replace")
                continue
            # unknown field -> ignore per spec


# ---------------------------------------------------------------- tests
PASSES: list[str] = []
FAILS: list[str] = []


def _ok(msg: str) -> None:
    print(f"  PASS {msg}")
    PASSES.append(msg)


def _bad(msg: str) -> None:
    print(f"  FAIL {msg}")
    FAILS.append(msg)


def test_reconnect() -> None:
    """Test 1: daemon dies + restarts on the SAME port; client reconnects
    and receives a fresh initial snapshot.  We drive reconnection manually
    (urllib doesn't auto-retry) -- a browser's EventSource would do the
    same, just automatically after the ``retry 1000`` comment."""
    port = _free_port()
    proc = _spawn_daemon(port)
    try:
        url = f"http://127.0.0.1:{port}/events"
        client = SSEClient(url)
        # Skip the ``: retry`` prelude, then grab the initial snapshot.
        first = client.next_event(timeout=3.0)
        assert first is not None and "retry 1000" in (first["comments"] or [""])[0], \
            f"missing retry prelude: {first}"
        snap = client.next_event(timeout=3.0)
        assert snap is not None and snap["data"] is not None, \
            f"no initial snapshot: {snap}"
        client.close()
    finally:
        _kill_daemon(proc)

    # Port is now free; brief pause mimicking the roast's 2 s daemon-down
    # window and giving the kernel time to release the socket.
    time.sleep(2.0)
    proc2 = _spawn_daemon(port)
    try:
        url = f"http://127.0.0.1:{port}/events"
        client = SSEClient(url)
        prelude = client.next_event(timeout=3.0)
        assert prelude is not None, "post-restart: no prelude"
        snap = client.next_event(timeout=3.0)
        assert snap is not None and snap["data"] is not None, \
            f"post-restart: no initial snapshot: {snap}"
        body = json.loads(snap["data"])
        assert isinstance(body, dict) and "ts" in body, \
            f"post-restart snapshot looks wrong: {body}"
        client.close()
        _ok("reconnect after daemon restart on same port")
    except AssertionError as e:
        _bad(f"reconnect: {e}")
    finally:
        _kill_daemon(proc2)


def test_queue_full_drop() -> None:
    """Test 2: a deliberately slow client is dropped once the per-subscriber
    queue (maxsize=16) backs up.  A parallel fast client keeps up and is
    NOT dropped.  We look for the ``dropped subscriber`` log line on the
    daemon's stderr."""
    port = _free_port()
    proc = _spawn_daemon(port)
    # Drain stderr on a thread so drop-log lines can't block on the pipe.
    stderr_chunks: list[str] = []

    def _stderr_pump():
        assert proc.stderr is not None
        for line in iter(proc.stderr.readline, ""):
            stderr_chunks.append(line)

    pump = threading.Thread(target=_stderr_pump, daemon=True)
    pump.start()
    try:
        url = f"http://127.0.0.1:{port}/events"
        slow = SSEClient(url)   # never read after initial prelude
        fast = SSEClient(url)
        # Drain the initial prelude + snapshot for fast, to get it into the
        # steady read state.
        fast.next_event(timeout=3.0)
        fast.next_event(timeout=3.0)
        # Fire 30 updates rapidly.  maxsize=16 means the slow subscriber
        # will saturate around update #16-#18 and be dropped.
        fast_reads: list[int] = []

        stop = threading.Event()

        def _fast_reader():
            while not stop.is_set():
                ev = fast.next_event(timeout=2.0)
                if ev is None:
                    return
                if ev.get("data"):
                    fast_reads.append(1)

        t = threading.Thread(target=_fast_reader, daemon=True)
        t.start()

        # Saturation strategy: Linux can buffer several MB between the
        # server's TCP send buffer and the client's recv buffer, so we
        # push large payloads in bulk.  Each update carries a ~256 KB
        # transcript -- that's ~4 MB per 16 queued events, more than any
        # default tcp_wmem/tcp_rmem window -- guaranteeing wfile.write
        # blocks and the per-subscriber queue fills to maxsize=16.  The
        # fast client keeps consuming so its queue drains faster than we
        # fan out.
        big = "x" * (256 * 1024)
        for i in range(60):
            _send_cmd(proc, "update", transcript=f"msg-{i}-{big}")

        # Give the fast reader a moment to catch up, then stop.
        time.sleep(0.5)
        stop.set()
        slow.close()
        fast.close()
        # Also give the stderr pump a moment to catch drop lines emitted
        # just before we terminate.
        time.sleep(0.2)
    finally:
        _kill_daemon(proc)
        pump.join(timeout=2.0)
        err = "".join(stderr_chunks)

    try:
        assert "[state] dropped subscriber" in err, \
            f"expected drop log in stderr, got: {err!r}"
        _ok("slow subscriber dropped with visible log line")
        # The fast reader must have received at least SOME of the 30
        # updates.  We don't assert >= 30 because SSE fan-out happens
        # asynchronously and some races are fine; we just assert non-zero.
        assert len(fast_reads) >= 1, \
            f"fast reader got 0 updates; dropped too? (reads={len(fast_reads)})"
        # Moreover: the drop log should NOT appear twice per update * 30
        # (if we dropped BOTH clients we'd see ~60 lines; we expect <30
        # because fast should not get dropped).  Loose bound: strictly
        # less than 30 drop lines means fast survived.
        drop_count = err.count("[state] dropped subscriber")
        assert drop_count < 30, \
            f"too many drop lines ({drop_count}); fast client was dropped too"
        _ok(f"fast subscriber NOT dropped ({drop_count} drops, {len(fast_reads)} reads)")
    except AssertionError as e:
        _bad(f"queue-full: {e}")


def test_last_event_id_catchup() -> None:
    """Test 3: reconnect with Last-Event-ID < current seq.  Server pushes
    the current snapshot immediately with id=<cur_seq> and -- because we
    intentionally missed >1 snapshot -- a ``: gap of N events`` comment."""
    port = _free_port()
    proc = _spawn_daemon(port)
    try:
        url = f"http://127.0.0.1:{port}/events"
        client = SSEClient(url)
        # Drain prelude + initial snapshot (seq=0).
        client.next_event(timeout=3.0)
        init = client.next_event(timeout=3.0)
        assert init is not None, "no initial snapshot"
        # Bump seq to 5 via 5 updates.
        for i in range(5):
            _send_cmd(proc, "update", transcript=f"pre-{i}")
        # Read five events to confirm ids 1..5 arrive.
        last_id = None
        for _ in range(5):
            ev = client.next_event(timeout=3.0)
            if ev is None:
                break
            if ev.get("id"):
                last_id = ev["id"]
        assert last_id == "5", f"expected id=5, got {last_id!r}"
        client.close()

        # Now push two more updates (seq -> 7), then reconnect with
        # Last-Event-ID: 5.  Server should emit a gap comment + the
        # current snapshot at id=7.
        _send_cmd(proc, "update", transcript="miss-1")
        _send_cmd(proc, "update", transcript="miss-2")

        client2 = SSEClient(url, last_event_id="5")
        prelude = client2.next_event(timeout=3.0)
        assert prelude is not None, "catch-up: no prelude"
        catchup = client2.next_event(timeout=3.0)
        assert catchup is not None and catchup["data"] is not None, \
            f"catch-up: no catchup frame: {catchup}"
        assert catchup["id"] == "7", \
            f"catch-up: id=7 expected, got {catchup['id']!r}"
        # Gap comment should appear in the same frame (since it comes
        # before the id: line in the SSE block we write).
        has_gap = any("gap of 2 events" in c for c in catchup["comments"])
        assert has_gap, \
            f"catch-up: expected 'gap of 2 events' comment, got {catchup['comments']}"
        client2.close()
        _ok("Last-Event-ID catch-up pushes current snapshot with gap comment")
    except AssertionError as e:
        _bad(f"last-event-id: {e}")
    finally:
        _kill_daemon(proc)


def test_heartbeat() -> None:
    """Test 4: subscribe, don't trigger any updates, expect a ``: heartbeat``
    comment within ~30 s.  The server's heartbeat interval is 25 s so 30 s
    is the safe upper bound."""
    port = _free_port()
    proc = _spawn_daemon(port)
    try:
        url = f"http://127.0.0.1:{port}/events"
        client = SSEClient(url)
        # Prelude (retry 1000), then initial snapshot.
        client.next_event(timeout=3.0)
        client.next_event(timeout=3.0)
        # Now wait up to 30 s for a heartbeat.
        ev = client.next_event(timeout=30.0)
        assert ev is not None, "heartbeat: EOF or socket timeout"
        assert any("heartbeat" in c for c in ev["comments"]), \
            f"heartbeat: no heartbeat comment, got {ev}"
        client.close()
        _ok("heartbeat comment arrived within 30s")
    except AssertionError as e:
        _bad(f"heartbeat: {e}")
    finally:
        _kill_daemon(proc)


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    t0 = time.time()
    print("[1/4] reconnect after daemon restart...")
    test_reconnect()
    print("[2/4] queue-full drop detection...")
    test_queue_full_drop()
    print("[3/4] Last-Event-ID catch-up...")
    test_last_event_id_catchup()
    print("[4/4] heartbeat within 30s...")
    test_heartbeat()
    dt = time.time() - t0
    if FAILS:
        print(f"FAIL: {len(FAILS)} failing test(s) in {dt:.2f}s", file=sys.stderr)
        for f in FAILS:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)
    print(f"ALL PASS ({len(PASSES)} assertions, {dt:.2f}s)")
