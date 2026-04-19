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
  - Standards-based — any browser's EventSource() Just Works.

The state file path on RobotState is still honoured, so old clients that
read /tmp/robot_state.json keep working until they migrate.

Runs on 127.0.0.1 by default — this exposes no attack surface beyond the
host.  Pass bind="0.0.0.0" to listen on all interfaces if you want to
reach the UI from another machine on the LAN.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from robot_daemon import RobotState


def make_server(state: "RobotState",
                bind: str = "127.0.0.1",
                port: int = 5556,
                stop_fn: Callable[[], tuple] | None = None,
                logger: Callable[[str], None] = print) -> ThreadingHTTPServer:
    """Build an HTTP server bound to (bind, port).  Caller is responsible
    for running server.serve_forever() on a daemon thread and calling
    server.shutdown() + server.server_close() at teardown.

    ``stop_fn`` should wrap send_wire({"c":"stop"}) so /stop works even
    when the main loop is blocked.  If None, /stop returns 501.
    """

    class _Handler(BaseHTTPRequestHandler):
        # Silence default access log — the daemon has its own logger.
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def _send_json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:                # noqa: N802 (stdlib API)
            path = self.path.split("?", 1)[0]
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
            path = self.path.split("?", 1)[0]
            if path == "/stop":
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
            # CORS preflight for browser UIs served from a different origin.
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def _stream_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")  # nginx passthrough
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            q = state.subscribe()
            try:
                # 25 s heartbeat keeps middleboxes from dropping the
                # connection and gives us a clean error path if the client
                # has silently disappeared.
                while True:
                    try:
                        snap = q.get(timeout=25.0)
                        line = f"data: {json.dumps(snap)}\n\n"
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
    logger(f"[state-server] http://{bind}:{port}  (/state /events /stop /health)")
    return httpd
