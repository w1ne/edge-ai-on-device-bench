#!/usr/bin/env python3
"""
test_state_auth.py -- fast test for state_server.py auth + CORS hardening.

Runs the state server in-process on a random port against a stub
RobotState.  Avoids starting the full robot_daemon (slow: TTS init, USB
probe, vision warmup).  We're testing the HTTP auth layer, not the
daemon; end-to-end smoke of the real daemon happens in the manual curl
steps documented in the PR.

Usage:
    python3 scripts/test_state_auth.py

Exit code 0 = all PASS.
"""
from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "demo"))

# Import after sys.path manipulation so state_server resolves.
from state_server import make_server  # noqa: E402


# ---------------------------------------------------------------- stubs
class StubState:
    """Minimal RobotState shim: exposes the three attributes/methods
    state_server reads: .snapshot(), .subscribe(), .unsubscribe(),
    ._lock, ._subs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []
        self._snap = {"ok": True, "stub": True, "ts": time.time()}

    def snapshot(self) -> dict:
        return dict(self._snap)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=8)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


def _free_port() -> int:
    """Ask the kernel for an unused port, release it, hand it back.
    Racy in theory, reliable in practice for a one-shot test."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url: str, *, token: str | None = None,
         expect: int | None = None,
         timeout: float = 2.0) -> tuple[int, bytes]:
    """Do a GET, return (status, body).  ``expect`` is informational and
    printed on mismatch; the caller still asserts."""
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""


# ---------------------------------------------------------------- tests
def test_loopback_no_auth() -> None:
    """Phase 1: loopback bind, auth configured, but ROBOT_FORCE_AUTH is
    NOT set.  /state must return 200 without any auth header because the
    client IP (127.0.0.1) falls inside the RFC 6761 carve-out."""
    os.environ.pop("ROBOT_FORCE_AUTH", None)
    port = _free_port()
    state = StubState()
    server = make_server(
        state,
        bind="127.0.0.1",
        port=port,
        stop_fn=None,
        logger=lambda _m: None,
        auth_token="SECRET_TOKEN_XYZ",
        auth_token_path="/tmp/fake-token",
        cors_origins=("http://127.0.0.1:5555",),
    )
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        code, body = _get(f"http://127.0.0.1:{port}/state")
        assert code == 200, f"loopback /state: expected 200, got {code} ({body!r})"
        js = json.loads(body)
        assert js.get("stub") is True, f"unexpected body: {js}"

        # /health is always public, even with ROBOT_FORCE_AUTH later.
        code, body = _get(f"http://127.0.0.1:{port}/health")
        assert code == 200, f"/health: expected 200, got {code}"
        print("  PASS loopback /state 200 without auth")
        print("  PASS /health 200 without auth")
    finally:
        server.shutdown()
        server.server_close()


def test_force_auth() -> None:
    """Phase 2: ROBOT_FORCE_AUTH=1 disables the loopback carve-out.  Now
    we exercise the three branches: no token -> 401, wrong token -> 403,
    right token -> 200.  /health stays 200 with no auth."""
    os.environ["ROBOT_FORCE_AUTH"] = "1"
    try:
        port = _free_port()
        state = StubState()
        tok = "GOOD_TOKEN_ABC123"
        server = make_server(
            state,
            bind="127.0.0.1",
            port=port,
            stop_fn=None,
            logger=lambda _m: None,
            auth_token=tok,
            auth_token_path="/tmp/fake-token",
            cors_origins=("http://127.0.0.1:5555",),
        )
        th = threading.Thread(target=server.serve_forever, daemon=True)
        th.start()
        try:
            # No header -> 401.
            code, _ = _get(f"http://127.0.0.1:{port}/state")
            assert code == 401, f"no-token /state: expected 401, got {code}"
            print("  PASS FORCE_AUTH no-token /state -> 401")

            # Wrong token -> 403.
            code, _ = _get(f"http://127.0.0.1:{port}/state", token="WRONG")
            assert code == 403, f"wrong-token /state: expected 403, got {code}"
            print("  PASS FORCE_AUTH wrong-token /state -> 403")

            # Right token -> 200.
            code, body = _get(f"http://127.0.0.1:{port}/state", token=tok)
            assert code == 200, f"good-token /state: expected 200, got {code}"
            js = json.loads(body)
            assert js.get("stub") is True
            print("  PASS FORCE_AUTH good-token /state -> 200")

            # /health still public.
            code, _ = _get(f"http://127.0.0.1:{port}/health")
            assert code == 200, f"/health with FORCE_AUTH: expected 200, got {code}"
            print("  PASS FORCE_AUTH /health -> 200 (always public)")

            # /events accepts ?token= (EventSource compat), but ONLY there.
            # Fire a quick request and bail out on the first byte.
            # It's an open-ended stream, so check the response code via
            # urlopen + close immediately.
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/events?token={tok}")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                assert resp.status == 200, \
                    f"/events?token=: expected 200, got {resp.status}"
            print("  PASS FORCE_AUTH /events?token=<good> -> 200")

            # ...but query-string token must NOT work on /state.
            code, _ = _get(f"http://127.0.0.1:{port}/state?token={tok}")
            assert code == 401, \
                f"query-token on /state should not auth: got {code}"
            print("  PASS FORCE_AUTH /state?token=<good> rejected (401)")
        finally:
            server.shutdown()
            server.server_close()
    finally:
        os.environ.pop("ROBOT_FORCE_AUTH", None)


def test_cors_allowlist() -> None:
    """Phase 3: verify the CORS allowlist echoes only approved origins.
    A mismatched Origin must NOT receive Access-Control-Allow-Origin (the
    browser will then block the request, which is the whole point)."""
    os.environ.pop("ROBOT_FORCE_AUTH", None)
    port = _free_port()
    state = StubState()
    server = make_server(
        state,
        bind="127.0.0.1",
        port=port,
        stop_fn=None,
        logger=lambda _m: None,
        auth_token=None,  # focus on CORS here
        auth_token_path=None,
        cors_origins=("http://127.0.0.1:5555",),
    )
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        # Allowed origin: header echoed back.
        req = urllib.request.Request(f"http://127.0.0.1:{port}/state")
        req.add_header("Origin", "http://127.0.0.1:5555")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            got = resp.headers.get("Access-Control-Allow-Origin")
        assert got == "http://127.0.0.1:5555", \
            f"allowed origin not echoed: got {got!r}"
        print("  PASS CORS allowed origin echoed")

        # Disallowed origin: no CORS header at all.
        req = urllib.request.Request(f"http://127.0.0.1:{port}/state")
        req.add_header("Origin", "http://evil.example.com")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            got = resp.headers.get("Access-Control-Allow-Origin")
        assert got is None, f"disallowed origin leaked CORS: {got!r}"
        print("  PASS CORS disallowed origin suppressed")
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    t0 = time.time()
    print("[1/3] loopback carve-out...")
    test_loopback_no_auth()
    print("[2/3] FORCE_AUTH 401/403/200...")
    test_force_auth()
    print("[3/3] CORS allowlist...")
    test_cors_allowlist()
    dt = time.time() - t0
    print(f"ALL PASS ({dt:.2f}s)")
