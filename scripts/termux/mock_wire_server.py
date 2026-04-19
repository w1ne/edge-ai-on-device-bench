#!/data/data/com.termux/files/usr/bin/env python3
"""mock_wire_server.py — 30-line line-framed JSON echo server.

Binds 127.0.0.1:5557 by default. For each line of JSON received, emits a
corresponding ack line ({"ok": true, "echo": <cmd>}). Multi-connection,
single-threaded via select(). Stdlib only.

Run on the phone OR the laptop for wire.py debugging before the real
Android BLE companion app is live.

Usage:
    python3 mock_wire_server.py                # default 127.0.0.1:5557
    PORT=5558 python3 mock_wire_server.py      # alt port
"""
from __future__ import annotations

import json
import os
import select
import socket
import sys
import time

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5557"))


def eprint(*a, **kw):
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


def main() -> int:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, PORT))
    except OSError as e:
        eprint(f"[mock_wire] bind {HOST}:{PORT} failed: {e}")
        return 2
    srv.listen(4)
    srv.setblocking(False)
    eprint(f"[mock_wire] listening on {HOST}:{PORT}")

    clients: dict[int, tuple[socket.socket, bytearray]] = {}

    try:
        while True:
            rlist = [srv] + [c for c, _ in clients.values()]
            readable, _, _ = select.select(rlist, [], [], 1.0)
            for s in readable:
                if s is srv:
                    c, addr = srv.accept()
                    c.setblocking(False)
                    clients[c.fileno()] = (c, bytearray())
                    eprint(f"[mock_wire] connect {addr}")
                    continue
                fd = s.fileno()
                sock, buf = clients[fd]
                try:
                    chunk = sock.recv(4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    eprint(f"[mock_wire] disconnect fd={fd}")
                    try:
                        sock.close()
                    except Exception:
                        pass
                    clients.pop(fd, None)
                    continue
                buf.extend(chunk)
                while b"\n" in buf:
                    line, _, rest = bytes(buf).partition(b"\n")
                    buf[:] = rest
                    try:
                        cmd = json.loads(line.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        ack = {"ok": False, "err": "mock_wire: bad json"}
                    else:
                        ack = {"ok": True, "echo": cmd, "ts": time.time()}
                    try:
                        sock.sendall((json.dumps(ack, separators=(",", ":")) + "\n").encode())
                    except OSError:
                        break
    except KeyboardInterrupt:
        eprint("[mock_wire] ctrl-c")
        return 0
    finally:
        for fd, (c, _) in list(clients.items()):
            try:
                c.close()
            except Exception:
                pass
        try:
            srv.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
