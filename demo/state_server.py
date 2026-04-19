"""
state_server.py -- small HTTP + SSE server for the robot daemon.

Replaces the /tmp/robot_state.json polling loop with a push-based API:

    GET  /state   -> current snapshot as JSON, one response
    GET  /events  -> Server-Sent Events stream, one event per state update
    POST /stop    -> emergency halt via pyusb (works even if main loop hung)
    GET  /health  -> {"ok": True, "ts": <unix-ts>, "subscribers": N}

Why HTTP/SSE instead of a flat JSON file:
  - Push, not poll.  UIs react instantly to state changes.
  - Fans out: multiple UIs, metrics scrapers, MCP clients all subscribe.
  - No filesystem involvement; no FS syscalls per update.
  - Standards-based -- any browser's EventSource() Just Works.

The state file path on RobotState is still honoured, so old clients that
read /tmp/robot_state.json keep working until they migrate.

Runs on 127.0.0.1 by default -- this exposes no attack surface beyond the
host.  Pass bind="0.0.0.0" to listen on all interfaces if you want to
reach the UI from another machine on the LAN.

Security model (iteration 2, 2026-04):
  - Bearer-token auth via Authorization: Bearer <tok>.  Token is a random
    32-byte urlsafe string written to ``auth_token_file`` (mode 0600) by
    the caller.  All endpoints require auth UNLESS the request originates
    from localhost (RFC 6761 127.0.0.0/8 + ::1).  ``/health`` is the only
    endpoint that stays public regardless -- uptime monitors can't read
    the token file.
  - CORS origin allowlist (default: http://127.0.0.1:5555, the bundled UI).
    Previously "*".  Non-matching origins get no CORS headers (browser
    blocks the request, which is the desired behaviour).
  - Optional TLS via tls_cert + tls_key.  For public (non-loopback) binds,
    generate a cert with:
      openssl req -newkey rsa:2048 -nodes -keyout k.pem -x509 -days 365 \
        -out c.pem
  - ``/events`` accepts ``?token=<tok>`` as a fallback to the Authorization
    header, because the browser EventSource API does not support setting
    custom headers.  No other endpoint accepts the query-string fallback.
  - ROBOT_FORCE_AUTH=1 disables the loopback carve-out -- purely for tests
    that need to exercise the auth path without binding to a real NIC.
"""
from __future__ import annotations

import ipaddress
import json
import os
import queue
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Callable, Iterable
from urllib.parse import parse_qs, urlsplit

if TYPE_CHECKING:
    from robot_daemon import RobotState


_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")


def _is_loopback(host: str) -> bool:
    """Return True iff ``host`` is an RFC 6761 loopback address.

    The ``ROBOT_FORCE_AUTH=1`` env var forces this to False so tests can
    exercise the auth path without binding a real non-loopback NIC.
    """
    if os.environ.get("ROBOT_FORCE_AUTH") == "1":
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _LOOPBACK_V4
    return ip.is_loopback  # ::1


def make_server(state: "RobotState",
                bind: str = "127.0.0.1",
                port: int = 5556,
                stop_fn: Callable[[], tuple] | None = None,
                logger: Callable[[str], None] = print,
                auth_token: str | None = None,
                auth_token_path: str | None = None,
                cors_origins: Iterable[str] = ("http://127.0.0.1:5555",),
                tls_cert: str | None = None,
                tls_key: str | None = None) -> ThreadingHTTPServer:
    """Build an HTTP server bound to (bind, port).  Caller is responsible
    for running server.serve_forever() on a daemon thread and calling
    server.shutdown() + server.server_close() at teardown.

    ``stop_fn`` should wrap send_wire({"c":"stop"}) so /stop works even
    when the main loop is blocked.  If None, /stop returns 501.

    ``auth_token`` when provided is required on every endpoint except
    /health and any request whose ``client_address`` is loopback.  Pass
    None to disable auth entirely (and log a [warn] line about it).
    ``auth_token_path`` is logged (not the token) so users can find the
    file to ``cat`` into their own clients.
    """

    cors_set = frozenset(cors_origins)

    class _Handler(BaseHTTPRequestHandler):
        # Silence default access log -- the daemon has its own logger.
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        # ---- auth helpers ----------------------------------------------
        def _client_is_loopback(self) -> bool:
            return _is_loopback(self.client_address[0])

        def _extract_bearer(self) -> str | None:
            raw = self.headers.get("Authorization", "")
            if not raw:
                return None
            parts = raw.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1].strip()
            return None

        def _query_token(self) -> str | None:
            qs = urlsplit(self.path).query
            if not qs:
                return None
            vals = parse_qs(qs).get("token")
            return vals[0] if vals else None

        def _auth_ok(self, path: str) -> tuple[bool, int]:
            """Return (ok, http-status-if-not-ok).  status=0 when ok."""
            # /health is always public -- uptime monitors need it.
            if path == "/health":
                return True, 0
            if auth_token is None:
                return True, 0
            # Loopback carve-out: default zero-friction on localhost.
            if self._client_is_loopback():
                return True, 0
            presented = self._extract_bearer()
            # EventSource can't set custom headers; /events takes ?token=.
            if presented is None and path == "/events":
                presented = self._query_token()
            if presented is None:
                return False, 401
            # Timing-safe compare: constant-time over the shorter of the
            # two strings.  Defeats the byte-by-byte timing oracle that a
            # plain `!=` exposes.
            if not secrets.compare_digest(presented, auth_token):
                return False, 403
            return True, 0

        # ---- CORS ------------------------------------------------------
        def _cors_headers(self) -> list[tuple[str, str]]:
            origin = self.headers.get("Origin", "")
            if origin and origin in cors_set:
                return [
                    ("Access-Control-Allow-Origin", origin),
                    ("Vary", "Origin"),
                    ("Access-Control-Allow-Credentials", "true"),
                ]
            return []

        # ---- CSRF ------------------------------------------------------
        def _csrf_ok(self) -> bool:
            """Origin-check for state-mutating POSTs.

            CORS does NOT protect state-mutating requests: the browser sends
            them (and attaches cookies / Authorization if the attacker
            injects it) before it checks the response's CORS headers.  For
            /stop we additionally require the Origin header to be absent
            (curl / native clients) or in our allowlist.  A browser page on
            http://evil.example cannot forge a matching Origin -- the UA
            sets it based on the real page origin.
            """
            origin = self.headers.get("Origin")
            if origin is None or origin == "":
                return True  # curl / native clients: no browser-forged CSRF
            return origin in cors_set

        # ---- response helpers ------------------------------------------
        def _send_json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in self._cors_headers():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _send_auth_error(self, status: int) -> None:
            body = {401: {"error": "missing bearer token"},
                    403: {"error": "bad bearer token"}}[status]
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            # RFC 7235 says 401 carries WWW-Authenticate.  Purely advisory
            # for our CLI + browser clients, but costs nothing.
            if status == 401:
                self.send_header("WWW-Authenticate", 'Bearer realm="robot"')
            self.send_header("Cache-Control", "no-store")
            for k, v in self._cors_headers():
                self.send_header(k, v)
            payload = json.dumps(body).encode()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        # ---- routes ----------------------------------------------------
        def do_GET(self) -> None:                # noqa: N802 (stdlib API)
            path = urlsplit(self.path).path
            ok, err = self._auth_ok(path)
            if not ok:
                self._send_auth_error(err)
                return
            if path == "/state":
                self._send_json(200, state.snapshot())
                return
            if path == "/health":
                with state._lock:                 # noqa: SLF001 - internal
                    n_subs = len(state._subs)     # noqa: SLF001
                self._send_json(200, {"ok": True, "ts": time.time(),
                                      "subscribers": n_subs})
                return
            if path == "/events":
                self._stream_sse()
                return
            self._send_json(404, {"error": "not found", "path": path})

        def do_POST(self) -> None:                # noqa: N802
            path = urlsplit(self.path).path
            ok, err = self._auth_ok(path)
            if not ok:
                self._send_auth_error(err)
                return
            if path == "/stop":
                # CSRF: even with a valid bearer, refuse browser-origin
                # POSTs whose Origin isn't in the allowlist.  See _csrf_ok.
                if not self._csrf_ok():
                    self._send_json(403, {"error": "csrf: origin not allowed"})
                    return
                if stop_fn is None:
                    self._send_json(501, {"error": "stop_fn not configured"})
                    return
                try:
                    ack, pos = stop_fn()
                    self._send_json(200, {"ok": True, "ack": ack, "servos": pos})
                except Exception as e:
                    self._send_json(500, {"error": f"{type(e).__name__}: {e}"})
                return
            self._send_json(404, {"error": "not found", "path": path})

        def do_OPTIONS(self) -> None:             # noqa: N802
            # CORS preflight.  Only echo allow-* if the Origin is in our
            # allowlist; otherwise respond 204 with no CORS headers so the
            # browser blocks the subsequent request.
            self.send_response(204)
            cors = self._cors_headers()
            for k, v in cors:
                self.send_header(k, v)
            if cors:
                self.send_header("Access-Control-Allow-Methods",
                                 "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers",
                                 "Authorization, Content-Type")
                self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        # ---- SSE -------------------------------------------------------
        def _stream_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # nginx passthrough
            for k, v in self._cors_headers():
                self.send_header(k, v)
            self.end_headers()

            # Tell the browser to retry after 1 s on disconnect (HTML5
            # default is 3 s; faster is friendlier for a live robot UI).
            # Also flush a Last-Event-ID catch-up frame before entering the
            # queue wait loop -- when the client reconnects it includes the
            # last id it saw in the ``Last-Event-ID`` header, and we push the
            # current snapshot immediately so the dashboard doesn't stall
            # waiting for the next state change.
            try:
                self.wfile.write(b": retry 1000\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

            last_event_id = self.headers.get("Last-Event-ID") or ""
            # Snapshot + seq captured atomically so we know exactly which id
            # to stamp onto the catch-up frame.  If the StubState used in
            # tests doesn't expose snapshot_with_seq(), degrade gracefully.
            snap_with_seq = getattr(state, "snapshot_with_seq", None)
            if callable(snap_with_seq):
                cur_seq, cur_snap = snap_with_seq()
            else:
                cur_seq, cur_snap = 0, state.snapshot()

            emitted_catchup = False
            if last_event_id.strip():
                try:
                    client_seq = int(last_event_id.strip())
                except ValueError:
                    client_seq = -1
                if client_seq >= 0 and cur_seq > client_seq:
                    gap = cur_seq - client_seq
                    # ``: gap of N events`` comment is informational only --
                    # browsers ignore it but it shows up in curl and aids
                    # offline debugging.  Then push the current state with
                    # the current id so the client's Last-Event-ID tracker
                    # advances in lock-step.
                    comment = b""
                    if gap > 1:
                        comment = f": gap of {gap} events\n".encode()
                    frame = (comment
                             + f"id: {cur_seq}\n".encode()
                             + f"data: {json.dumps(cur_snap)}\n\n".encode())
                    try:
                        self.wfile.write(frame)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    emitted_catchup = True

            q = state.subscribe()
            # subscribe() primes the queue with the current (seq, snapshot).
            # If we already flushed the same data as a catch-up frame, drop
            # the duplicate so the client doesn't see the same id twice.
            if emitted_catchup:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
            try:
                # 25 s heartbeat keeps middleboxes from dropping the
                # connection and gives us a clean error path if the client
                # has silently disappeared.
                while True:
                    try:
                        item = q.get(timeout=25.0)
                        # Subscribers now carry (seq, snapshot) so we can
                        # emit ``id: N`` on every event.  Tolerate the
                        # older plain-snapshot shape for stub-state tests
                        # that pre-date this change.
                        if isinstance(item, tuple) and len(item) == 2:
                            seq, snap = item
                            line = (f"id: {seq}\n"
                                    f"data: {json.dumps(snap)}\n\n")
                        else:
                            line = f"data: {json.dumps(item)}\n\n"
                    except queue.Empty:
                        line = ": heartbeat\n\n"
                    try:
                        self.wfile.write(line.encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            finally:
                state.unsubscribe(q)

    httpd = ThreadingHTTPServer((bind, port), _Handler)
    httpd.daemon_threads = True

    # Optional TLS wrap.  For loopback the default plaintext is fine; TLS
    # is strongly recommended once the bind address leaves 127.0.0.0/8.
    scheme = "http"
    if tls_cert and tls_key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"

    endpoints = "/state /events /stop /health"
    logger(f"[state-server] {scheme}://{bind}:{port}  ({endpoints})")
    if auth_token is None:
        logger("[state-server] [warn] auth disabled (--no-auth) -- any "
               "non-loopback client can call /stop")
    else:
        path_disp = auth_token_path or "(in-memory)"
        if auth_token_path:
            path_disp = auth_token_path.replace(os.path.expanduser("~"), "~", 1)
        logger(f"[state-server] token: {path_disp} "
               f"(loopback carve-out active; /health is public)")
    if cors_set:
        logger(f"[state-server] cors allowlist: "
               f"{', '.join(sorted(cors_set)) or '(none)'}")
    return httpd
