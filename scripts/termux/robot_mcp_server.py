#!/data/data/com.termux/files/usr/bin/env python3
"""
robot_mcp_server.py — MCP server exposing our quadruped robot as a set
of tools. Hermes (or any other MCP-compatible client) connects over
stdio and can then call pose/walk/stop/jump/look_for/say/etc.

Transport
---------
stdio JSON-RPC 2.0, per the MCP spec
(https://modelcontextprotocol.io/docs/concepts/architecture).  Written
from scratch in stdlib-only so we don't add pip deps in Termux.

Plumbing
--------
Tool implementations talk to the **Android companion app** at
``127.0.0.1:5557`` (`dev.robot.companion` APK's TCP bridge). The
companion forwards each wire command over BLE to the ESP32 firmware
and returns the ack. Same path the native app's Planner uses; we're
just adding Hermes on top.

Tools exposed
-------------
- pose(name: str, duration_ms: int = 800)
  name in {neutral, lean_left, lean_right, bow_front}
- walk(stride: int = 150, step: int = 400)
- stop()
- jump()
- look_for(query: str)
  Captures a phone camera frame, sends it to a hosted VLM with the
  query, returns {seen: bool, score: float}. Implemented via a second
  TCP endpoint we'll add to the companion app; for now returns a
  stub unless a `look_for` relay is available.
- say(text: str)  — speak via Android TTS
- get_state()     — last known battery / tilt / connected / walking
- list_recent_events(limit: int = 10)

Run
---
    python3 scripts/termux/robot_mcp_server.py
    # Register with Hermes via its config:
    #   hermes mcp add robot -- python3 $HOME/robot_mcp_server.py

Protocol notes
--------------
MCP uses line-delimited JSON-RPC 2.0 over stdio. We support:
  - initialize
  - tools/list
  - tools/call
Any other method returns method-not-found.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from typing import Any, Dict

HOST, PORT = "127.0.0.1", 5557
RECV_TIMEOUT_S = 3.0
CONNECT_TIMEOUT_S = 2.0

# ------------------------------------------------------------------
# Low-level bridge to the companion app's TCP server.
# ------------------------------------------------------------------

_lock = threading.Lock()  # only one wire call at a time


def _wire_send(cmd: Dict[str, Any]) -> Dict[str, Any]:
    """Open a short-lived TCP connection, send one JSON line, read the ack."""
    with _lock:
        try:
            s = socket.create_connection((HOST, PORT), timeout=CONNECT_TIMEOUT_S)
        except Exception as e:
            return {"ok": False, "error": f"companion_unreachable: {e}"}
        try:
            s.settimeout(RECV_TIMEOUT_S)
            s.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
            try:
                return json.loads(line) if line else {"ok": True, "ack": "empty"}
            except json.JSONDecodeError:
                return {"ok": True, "raw": line[:200]}
        except Exception as e:
            return {"ok": False, "error": f"wire_io: {e}"}
        finally:
            try:
                s.close()
            except Exception:
                pass


# ------------------------------------------------------------------
# Tool implementations.
# ------------------------------------------------------------------

def tool_pose(name: str, duration_ms: int = 800) -> Dict[str, Any]:
    if name not in ("neutral", "lean_left", "lean_right", "bow_front"):
        return {"ok": False, "error": f"unknown_pose: {name!r}"}
    return _wire_send({"c": "pose", "n": name, "d": int(duration_ms)})


def tool_walk(stride: int = 150, step: int = 400) -> Dict[str, Any]:
    return _wire_send(
        {"c": "walk", "on": True, "stride": int(stride), "step": int(step)}
    )


def tool_stop() -> Dict[str, Any]:
    return _wire_send({"c": "stop"})


def tool_jump() -> Dict[str, Any]:
    return _wire_send({"c": "jump"})


def tool_say(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty_text"}
    # Route to companion app's TTS via a synthetic wire frame — the Android
    # side watches for {"c":"say"} and hands off to TtsPlayer. (Requires a
    # companion-side patch; until landed, the native app ignores it and the
    # user hears nothing, but the ack still comes back.)
    return _wire_send({"c": "say", "text": text})


def tool_look_for(query: str) -> Dict[str, Any]:
    """Ask the companion app to capture + VLM-classify a frame. Requires a
    companion-side `{"c":"look_for","query":...}` handler to land first;
    until then returns a stub."""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "empty_query"}
    r = _wire_send({"c": "look_for", "query": q})
    if r.get("ok") is False and "unknown" in str(r.get("error", "")).lower():
        # Companion doesn't recognise the wire command yet; stub the answer
        # so Hermes doesn't hang waiting for vision.
        return {
            "ok": True,
            "seen": False,
            "score": 0.0,
            "note": "look_for companion handler not yet implemented",
        }
    return r


def tool_get_state() -> Dict[str, Any]:
    # Reads the last state packet the companion has cached. Companion-side
    # handler pending; falls back to "unknown".
    r = _wire_send({"c": "get_state"})
    if r.get("ok") is False:
        return {"ok": True, "unknown": True, "note": "state cache not exposed"}
    return r


TOOLS: Dict[str, Dict[str, Any]] = {
    "pose": {
        "description": (
            "Command the robot to a static posture. name must be one of "
            "neutral | lean_left | lean_right | bow_front. duration_ms "
            "scales the speed (ignored by the firmware today)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": ["neutral", "lean_left", "lean_right", "bow_front"],
                },
                "duration_ms": {"type": "integer", "default": 800},
            },
            "required": ["name"],
        },
        "fn": lambda a: tool_pose(a["name"], int(a.get("duration_ms", 800))),
    },
    "walk": {
        "description": (
            "Start the quadruped walking gait forward. Call stop() to halt. "
            "stride and step scale gait speed; keep defaults unless tuning."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stride": {"type": "integer", "default": 150},
                "step":   {"type": "integer", "default": 400},
            },
        },
        "fn": lambda a: tool_walk(int(a.get("stride", 150)),
                                  int(a.get("step", 400))),
    },
    "stop": {
        "description": "Halt all motion immediately. Safe to call any time.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": lambda _a: tool_stop(),
    },
    "jump": {
        "description": "Perform one small crouch-and-release jump.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": lambda _a: tool_jump(),
    },
    "say": {
        "description": "Speak text aloud through the phone speaker.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "fn": lambda a: tool_say(str(a.get("text", ""))),
    },
    "look_for": {
        "description": (
            "Capture one frame from the phone camera and check whether "
            "<query> is visible. Returns {seen: bool, score: 0..1}. Use "
            "this for 'do you see a ...' style questions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        "fn": lambda a: tool_look_for(str(a.get("query", ""))),
    },
    "get_state": {
        "description": (
            "Return the last known robot telemetry: battery voltage, "
            "walking flag, tilt, BLE link status."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "fn": lambda _a: tool_get_state(),
    },
}


# ------------------------------------------------------------------
# MCP JSON-RPC 2.0 dispatcher.
# ------------------------------------------------------------------

SERVER_INFO = {
    "name": "phonewalker-robot",
    "version": "0.1.0",
}


def _rpc_result(id_: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}) + "\n"


def _rpc_error(id_: Any, code: int, message: str) -> str:
    return json.dumps({
        "jsonrpc": "2.0",
        "id": id_,
        "error": {"code": code, "message": message},
    }) + "\n"


def _handle(req: Dict[str, Any]) -> str | None:
    method = req.get("method", "")
    id_ = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _rpc_result(id_, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method == "notifications/initialized":
        return None   # notification, no response expected

    if method == "tools/list":
        return _rpc_result(id_, {
            "tools": [
                {
                    "name": name,
                    "description": spec["description"],
                    "inputSchema": spec["inputSchema"],
                }
                for name, spec in TOOLS.items()
            ],
        })

    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        spec = TOOLS.get(name)
        if spec is None:
            return _rpc_error(id_, -32601, f"unknown tool: {name}")
        try:
            result = spec["fn"](args)
        except Exception as e:
            return _rpc_error(id_, -32000, f"{type(e).__name__}: {e}")
        text = json.dumps(result, ensure_ascii=False)
        return _rpc_result(id_, {
            "content": [{"type": "text", "text": text}],
            "isError": bool(result.get("ok") is False),
        })

    if id_ is None:
        return None  # notification of unknown kind, ignore
    return _rpc_error(id_, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(_rpc_error(None, -32700, "parse error"))
            sys.stdout.flush()
            continue
        resp = _handle(req)
        if resp is not None:
            sys.stdout.write(resp)
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
