#!/usr/bin/env python3
"""
web_ui.py -- single-file web dashboard for the robot daemon.

Launch alongside the daemon:

    # terminal 1
    scripts/run_robot.sh --mode text
    # (writes logs/robot-YYYYMMDD-HHMMSS.log, paints /tmp/_robot_eye.png,
    #  publishes /tmp/robot_state.json atomically on every event)

    # terminal 2
    python3 demo/web_ui.py           # defaults: port 5555, logs-dir = repo/logs
                                     # and state-file = /tmp/robot_state.json

Open http://localhost:5555/ -- the page auto-refreshes every 500 ms when the
state JSON is live, 1 s when we've fallen back to log regex.  The STOP button
bypasses the daemon and drives the ESP32 directly over pyusb (same VID/PID
and endpoints as send_wire() in robot_daemon.py), so it still works if the
daemon is wedged.  SHUTDOWN sends SIGTERM to the daemon PID (looked up via
`ps`).

Source precedence:
  1. /tmp/robot_state.json (published atomically by robot_daemon.RobotState).
     Primary.  A header badge shows "source: state-json".
  2. Log regex over logs/robot-*.log.  Only used if the state file is missing
     or its `ts` is stale (>60 s old).  Header badge shows "source: log-regex".

A separate "frozen" indicator trips if the state snapshot is >10 s old — so a
hung daemon is visible at a glance without waiting for the 60 s fallback.

Reads only: /tmp/robot_state.json, newest logs/robot-*.log, /tmp/_robot_eye.png, ps.
Writes only: one {"c":"stop"} USB packet on demand.  Deps: stdlib + flask
(auto falls back to http.server) + pyusb.
"""
from __future__ import annotations

import argparse, glob, html, json, os, re, signal, subprocess, time
from pathlib import Path

try:
    from flask import Flask, Response, redirect
    USE_FLASK = True
except ImportError:
    USE_FLASK = False

# Always import the stdlib server so the fallback class can be defined --
# we just don't start it unless Flask is absent.
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = REPO_ROOT / "logs"
DEFAULT_STATE   = "/tmp/robot_state.json"
DEFAULT_DAEMON_URL = "http://127.0.0.1:5556"
DEFAULT_TOKEN_FILE = str(Path.home() / ".cache" / "edge-robot-token")
EYE_PNG    = "/tmp/_robot_eye.png"
ROBOT_NAME = os.environ.get("ROBOT_NAME", "PhoneWalker")
VID, PID   = 0x303a, 0x1001
LOG_GLOB   = "robot-*.log"


def read_daemon_token(path: str | None) -> str | None:
    """Read the bearer token the daemon wrote.  Returns None if missing
    so requests still work on the loopback carve-out."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        return tok or None
    except (FileNotFoundError, PermissionError, IsADirectoryError):
        return None

# Freshness windows.
STATE_FROZEN_S   = 10.0   # show "daemon frozen" badge beyond this
STATE_FALLBACK_S = 60.0   # beyond this, fall back to log-regex entirely

# ---------------------------------------------------------------- state JSON

def read_state_file(path: str) -> dict | None:
    """Load the daemon's atomic state snapshot.  Returns None if the file is
    missing, unreadable, or obviously truncated."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            return None
        return json.loads(data.decode("utf-8", "replace"))
    except (FileNotFoundError, IsADirectoryError):
        return None
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------- log scrape

def latest_log(logs_dir: Path) -> Path | None:
    paths = glob.glob(str(logs_dir / LOG_GLOB))
    return Path(max(paths, key=os.path.getmtime)) if paths else None


def read_tail(path: Path, n: int = 400) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2); size = fh.tell()
            read = min(size, 64 * 1024); fh.seek(size - read)
            data = fh.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return []
    return data.splitlines()[-n:]


# Regexes pulled from the daemon's logger format ("HH:MM:SS  <line>").
# Kept intact for the fallback path — when the state JSON is absent/stale,
# we still want a populated dashboard.
RE_STAMP      = re.compile(r"^(\d{2}:\d{2}:\d{2})\s+(.*)$")
RE_HEARD      = re.compile(r"heard \(\d+ ms\):\s*(.+)$")
RE_DECISION   = re.compile(r"decision:\s*(.+)$")
RE_V_TICK     = re.compile(r"\[vision\] alive \(ticks=(\d+), last_latency=([\d.]+) ms\)")
RE_V_EVT      = re.compile(r"\[vision\] EVENT (\S+) conf=([\d.]+) streak=(\d+)")
RE_BEHAV      = re.compile(r"\[behav\].*?(?:->|\u2192)\s*(walking|idle|paused|following)")
RE_WIRE_ACK   = re.compile(r"wire:\s+(\{.*?\})\s*servos:\s*\[([^\]]+)\]")
RE_WIRE_TELM  = re.compile(r'"(v|ms|temp|t_c|imu|yaw|pitch|roll)"\s*:\s*([\-\d.,\[\]]+)')
RE_UP         = re.compile(r"robot_daemon up\s+(.+)$")
RE_LLM_FAIL   = re.compile(r"\[llm\] api call failed \(([^)]+)\)")
RE_LLM_ASK    = re.compile(r"\[matcher\] no keyword hit, asking LLM \(([^)]+)\)")


def scrape(lines: list[str]) -> dict:
    s: dict = {
        "last_heard": None, "last_decision": None, "behav_state": "idle",
        "vision_last_event": None, "vision_recent_ticks": [],
        "wire_last_ack": None, "servos": None, "telemetry": {},
        "daemon_up_line": None, "llm_last_ask": None, "llm_last_fail": None,
        "tail": lines[-10:], "vision_running": False,
    }
    for raw in lines:
        m = RE_STAMP.match(raw)
        if not m: continue
        ts, body = m.group(1), m.group(2)
        if (mm := RE_HEARD.search(body)):    s["last_heard"]    = (ts, mm.group(1).strip())
        if (mm := RE_DECISION.search(body)): s["last_decision"] = (ts, mm.group(1).strip())
        if (mm := RE_V_TICK.search(body)):
            s["vision_recent_ticks"].append((ts, int(mm.group(1)), float(mm.group(2))))
            s["vision_running"] = True
        if (mm := RE_V_EVT.search(body)):
            s["vision_last_event"] = {"class": mm.group(1), "conf": float(mm.group(2)),
                                      "streak": int(mm.group(3)), "when": ts}
        if (mm := RE_BEHAV.search(body)):    s["behav_state"] = mm.group(1)
        if (mm := RE_WIRE_ACK.search(body)):
            s["wire_last_ack"] = (ts, mm.group(1))
            try: s["servos"] = [int(x) for x in mm.group(2).split(",")]
            except ValueError: pass
        for k, v in RE_WIRE_TELM.findall(body):
            s["telemetry"][k] = v.strip()
        if (mm := RE_UP.search(body)):       s["daemon_up_line"] = (ts, mm.group(1).strip())
        if (mm := RE_LLM_ASK.search(body)):  s["llm_last_ask"]   = (ts, mm.group(1))
        if (mm := RE_LLM_FAIL.search(body)): s["llm_last_fail"]  = (ts, mm.group(1))
        if "[vision] watcher stopped" in body: s["vision_running"] = False
    recent = s["vision_recent_ticks"][-10:]
    if recent:
        s["vision_ticks_window"] = recent[-1][1]
        s["vision_latency_avg"]  = sum(x[2] for x in recent) / len(recent)
    else:
        s["vision_ticks_window"] = None
        s["vision_latency_avg"]  = None
    return s


# ---------------------------------------------------------------- state -> UI

def _fmt_clock(unix_ts: float | None) -> str:
    if not unix_ts:
        return "--:--:--"
    try:
        return time.strftime("%H:%M:%S", time.localtime(float(unix_ts)))
    except Exception:
        return "--:--:--"


def state_to_ui(sj: dict, tail_lines: list[str]) -> dict:
    """Project the daemon's atomic snapshot into the same shape the HTML
    renderer expects from scrape().  We reuse the existing UI layout so
    nothing regresses — only the source of each field changes.
    """
    now = time.time()
    ts  = float(sj.get("ts") or 0.0)

    transcript = sj.get("transcript")
    last_heard = (_fmt_clock(ts), transcript) if transcript else None

    cmd = sj.get("cmd")
    last_decision = (_fmt_clock(ts), json.dumps(cmd)) if cmd else None

    vision_recent = sj.get("vision_recent") or []
    vision_last_event = None
    if vision_recent:
        last = vision_recent[-1]
        vision_last_event = {
            "class": last.get("class", "?"),
            "conf": 1.0,            # state snapshot doesn't carry conf today
            "streak": len(vision_recent),
            "when": _fmt_clock(last.get("ts") or ts),
        }

    vision_fps = sj.get("vision_fps")
    vision_running = isinstance(vision_fps, (int, float)) and (now - ts) < 30.0
    vision_latency = (1000.0 / float(vision_fps)) if vision_fps else None

    telemetry: dict = {}
    if sj.get("voltage_v") is not None:
        # log regex shows volts*10 as "v"; keep the same convention so the UI
        # row labeled "v" stays consistent.
        telemetry["v"] = f'{int(round(float(sj["voltage_v"]) * 10))}'
    if sj.get("temp_c")    is not None: telemetry["temp"] = f'{sj["temp_c"]}'
    if sj.get("uptime_ms") is not None: telemetry["ms"]   = f'{sj["uptime_ms"]}'
    if sj.get("imu"):
        try: telemetry["imu"] = "[" + ",".join(f"{x:.2f}" for x in sj["imu"]) + "]"
        except Exception: pass

    wire_ack_raw = sj.get("wire_ack")
    wire_last_ack = (_fmt_clock(ts), str(wire_ack_raw)) if wire_ack_raw else None

    planner = sj.get("planner_last")
    if planner:
        plan_msg = (f"{'ok' if planner.get('success') else 'fail'}: "
                    f"{planner.get('reason','?')} ({planner.get('steps',0)} steps)")
        llm_last_ask = (_fmt_clock(ts), f"planner goal: {str(planner.get('goal',''))[:80]}")
        llm_last_fail = ((_fmt_clock(ts), plan_msg)
                         if not planner.get("success") else None)
    else:
        llm_last_ask = None
        llm_last_fail = None

    daemon_up_line = (_fmt_clock(ts),
                      f"mode={sj.get('mode') or '?'}  walking={sj.get('walking')}")

    if sj.get("error_last"):
        llm_last_fail = (_fmt_clock(ts), str(sj["error_last"])[:200])

    return {
        "last_heard": last_heard,
        "last_decision": last_decision,
        "behav_state": sj.get("behavior_state") or "idle",
        "vision_last_event": vision_last_event,
        "vision_recent_ticks": [],
        "wire_last_ack": wire_last_ack,
        "servos": sj.get("servos"),
        "telemetry": telemetry,
        "daemon_up_line": daemon_up_line,
        "llm_last_ask": llm_last_ask,
        "llm_last_fail": llm_last_fail,
        "tail": tail_lines[-10:],
        "vision_running": vision_running,
        "vision_ticks_window": None,
        "vision_latency_avg":  vision_latency,
    }


# ------------------------------------------------------------ daemon process

def find_daemon() -> tuple[int | None, str | None, float | None]:
    try:
        out = subprocess.check_output(
            ["ps", "-eo", "pid,lstart,cmd", "--no-headers"], text=True, timeout=2)
    except Exception:
        return None, None, None
    for line in out.splitlines():
        if "robot_daemon.py" in line and "web_ui.py" not in line and "grep" not in line:
            parts = line.strip().split(None, 6)
            if len(parts) < 7: continue
            try:
                epoch = time.mktime(time.strptime(" ".join(parts[1:6])))
            except Exception:
                epoch = None
            return int(parts[0]), parts[6], epoch
    return None, None, None


def human_uptime(secs: float) -> str:
    secs = int(secs); d, r = divmod(secs, 86400); h, r = divmod(r, 3600); m, s = divmod(r, 60)
    if d: return f"{d}d {h}h {m}m"
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"


# ------------------------------------------------------------ USB stop packet

def fire_stop() -> str:
    """Write {"c":"stop"}\\n to the ESP32.  Standalone so the daemon being
    wedged cannot block it -- mirrors the minimal path of send_wire()."""
    try:
        import usb.core, usb.util
    except ImportError:
        return "pyusb missing"
    dev = usb.core.find(idVendor=VID, idProduct=PID)
    if dev is None:
        return "no device"
    try:
        try: usb.util.claim_interface(dev, 1)
        except Exception: pass
        dev.write(0x01, (json.dumps({"c": "stop"}) + "\n").encode(), timeout=500)
        return "ok"
    except Exception as e:
        return f"err: {type(e).__name__}: {e}"
    finally:
        try:
            import usb.util; usb.util.dispose_resources(dev)
        except Exception: pass


def fire_stop_via_daemon(daemon_url: str, token: str | None,
                         timeout: float = 2.0) -> str | None:
    """POST /stop to the daemon's state server with bearer auth.  Returns
    a status string on success / failure, or None if the call wasn't even
    attempted (e.g., daemon_url is empty).  Meant as the *preferred* stop
    path for the web UI -- the daemon then drives the ESP32, which avoids
    fighting pyusb for the same interface.  Falls back to local pyusb if
    this returns None or an error string."""
    if not daemon_url:
        return None
    import urllib.request, urllib.error
    req = urllib.request.Request(
        daemon_url.rstrip("/") + "/stop",
        method="POST",
        data=b"",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return "ok (via daemon)"
            return f"daemon http {resp.status}"
    except urllib.error.HTTPError as e:
        return f"daemon http {e.code}"
    except urllib.error.URLError as e:
        return f"daemon unreachable: {e.reason}"
    except Exception as e:
        return f"daemon err: {type(e).__name__}: {e}"


def fire_stop_best(daemon_url: str, token: str | None) -> str:
    """Prefer the daemon's /stop endpoint (clean, avoids pyusb contention
    when the daemon already owns the interface).  If the daemon didn't
    respond or isn't configured, fall back to local pyusb so STOP still
    works when the daemon is hung."""
    remote = fire_stop_via_daemon(daemon_url, token)
    if remote == "ok (via daemon)":
        return remote
    local = fire_stop()
    if remote:
        return f"{remote}; local: {local}"
    return local


def shutdown_daemon() -> str:
    pid, _, _ = find_daemon()
    if pid is None: return "no daemon"
    try:
        os.kill(pid, signal.SIGTERM); return f"SIGTERM -> {pid}"
    except ProcessLookupError: return "gone"
    except PermissionError:    return "perm denied"
    except Exception as e:     return f"err: {e}"


# ------------------------------------------------------------ HTML rendering

CSS = (
  "*{box-sizing:border-box;font-family:ui-monospace,Menlo,Consolas,monospace}"
  "body{background:#0b0f14;color:#cfe;margin:0;padding:18px}"
  "h1{margin:0 0 10px 0;font-size:20px;color:#7df}"
  "h2{margin:14px 0 6px 0;font-size:13px;color:#9ac;letter-spacing:.08em;"
  "text-transform:uppercase;border-bottom:1px solid #244;padding-bottom:3px}"
  ".grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}"
  ".card{background:#111820;border:1px solid #234;border-radius:6px;padding:12px}"
  ".hdr{display:flex;align-items:center;gap:16px;flex-wrap:wrap;"
  "background:#13202b;border:1px solid #245;border-radius:6px;padding:10px}"
  ".tag{background:#0a2a3a;padding:2px 8px;border-radius:4px;font-size:12px}"
  ".src-json{background:#0a3a22;color:#8fd}"
  ".src-log{background:#3a2a0a;color:#fc8}"
  ".frozen{background:#4a0e0e;color:#fcc;animation:blink 1s steps(2,start) infinite}"
  "@keyframes blink{to{visibility:hidden}}"
  ".ok{color:#7f8}.warn{color:#fc6}.bad{color:#f77}.big{font-size:15px}"
  "pre{background:#06090d;border:1px solid #1a2530;padding:8px;border-radius:4px;"
  "white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px;"
  "max-height:240px;overflow:auto}"
  ".kv{display:grid;grid-template-columns:110px 1fr;gap:4px 10px;font-size:12px}"
  ".kv b{color:#9cd}"
  ".stop{background:#b00;color:#fff;border:0;padding:16px 28px;font-size:18px;"
  "font-weight:bold;border-radius:6px;cursor:pointer;margin-right:12px}"
  ".stop:hover{background:#d22}.sdn{background:#333;color:#eee;border:1px solid #555}"
  ".sdn:hover{background:#555}"
  "img.eye{max-width:320px;max-height:240px;border:1px solid #234;border-radius:4px}"
  ".row{display:flex;gap:18px;align-items:flex-start;flex-wrap:wrap}"
  ".small{font-size:11px;color:#789}form{display:inline}"
)


def render_page(state: dict, daemon: tuple, flash: str | None,
                source: str, snapshot_age: float | None,
                refresh_ms: int,
                daemon_url: str = DEFAULT_DAEMON_URL,
                token: str | None = None) -> str:
    esc = html.escape
    pid, _cmd, start_epoch = daemon
    if pid is not None and start_epoch is not None:
        pid_html = f'<span class="ok">PID {pid}</span> &middot; uptime {human_uptime(time.time() - start_epoch)}'
    else:
        pid_html = '<span class="bad">daemon not running</span>'

    def _when(pair):
        return (f'<div class="big">{esc(str(pair[1]))}</div><div class="small">@ {pair[0]}</div>'
                if pair else '<div class="small">(nothing yet)</div>')
    heard_html, dec_html = _when(state["last_heard"]), _when(state["last_decision"])

    vev = state["vision_last_event"]
    vision_evt_html = (f'<b>{esc(vev["class"])}</b> conf={vev["conf"]:.2f} '
                       f'streak={vev["streak"]} <span class="small">@ {vev["when"]}</span>'
                       ) if vev else '<span class="small">(no vision events yet)</span>'
    vstate = "watching" if state["vision_running"] else "silent"
    vcls   = "ok" if state["vision_running"] else "warn"

    try:
        st = os.stat(EYE_PNG)
        eye_img = f'<img class="eye" src="/eye.png?t={int(st.st_mtime)}" alt="eye frame">'
    except FileNotFoundError:
        eye_img = '<div class="small">(no /tmp/_robot_eye.png yet)</div>'

    bstate = state["behav_state"] or "idle"
    b_cls  = {"walking": "ok", "following": "ok", "paused": "warn", "idle": ""}.get(bstate, "")
    tel    = state["telemetry"]
    tel_rows = "".join(f'<b>{k}</b><span>{esc(str(tel[k]))}</span>'
                       for k in ("v", "ms", "temp", "t_c", "imu", "yaw", "pitch", "roll")
                       if k in tel) or '<b>--</b><span class="small">no telemetry yet</span>'
    if state["servos"]:
        tel_rows += f'<b>servos</b><span>{state["servos"]}</span>'
    wire_html = ""
    if state["wire_last_ack"]:
        ts, ack = state["wire_last_ack"]
        wire_html = f'<div class="small">last wire ack @ {ts}: {esc(str(ack))}</div>'

    api_set = bool(os.environ.get("DEEPINFRA_API_KEY"))
    llm_rows = (f'<b>DEEPINFRA_API_KEY</b><span class="{"ok" if api_set else "bad"}">'
                f'{"set" if api_set else "UNSET"}</span>')
    if (a := state["llm_last_ask"]):
        llm_rows += f'<b>last ask</b><span>{esc(str(a[1]))} @ {a[0]}</span>'
    if (f := state["llm_last_fail"]):
        llm_rows += f'<b>last failure</b><span class="bad">{esc(str(f[1]))} @ {f[0]}</span>'

    up = state["daemon_up_line"]
    up_html = f'<div class="small">{esc(str(up[1]))}</div>' if up else ""
    tail_html = esc("\n".join(state["tail"])) or "(log empty)"
    flash_html = f'<div class="tag warn">{esc(flash)}</div>' if flash else ""

    ticks = state["vision_ticks_window"]; lat = state["vision_latency_avg"]
    lat_str = f'{lat:.1f} ms' if lat is not None else '--'

    # Source + freshness badges.
    src_cls = "src-json" if source == "state-json" else "src-log"
    src_badge = f'<span class="tag {src_cls}">source: {source}</span>'
    if source == "state-json" and snapshot_age is not None:
        age_html = f'<span class="tag">state age: {snapshot_age:.1f}s</span>'
    else:
        age_html = ""
    frozen_html = ""
    if source == "state-json" and snapshot_age is not None and snapshot_age > STATE_FROZEN_S:
        frozen_html = (f'<span class="tag frozen">daemon frozen '
                       f'({snapshot_age:.0f}s since last update)</span>')

    # Preferred path: SSE subscription to the daemon's state-server pushes an
    # event on every RobotState.update().  We reload on each event — this is
    # the rendering cost of the server-rendered HTML, but we avoid polling.
    # Fallback: if EventSource never connects or drops for >5 s, resume the
    # classic meta-refresh + JS-timer so the page doesn't go stale when the
    # daemon's HTTP server is unreachable.
    meta_refresh_s = max(1, refresh_ms // 1000) if refresh_ms >= 1000 else 1
    # EventSource can't set custom headers (fetch spec restriction), so we
    # pass the bearer token as ?token=<tok>.  The state server only accepts
    # the query-string token on /events -- other endpoints still require
    # the Authorization header.  JSON-encode both so quoting is safe.
    sse_url = f"{daemon_url.rstrip('/')}/events"
    if token:
        sse_url += f"?token={token}"
    sse_js = (
        "(function(){"
        "try{"
        f"var es=new EventSource({json.dumps(sse_url)});"
        "var last=Date.now();"
        "es.onmessage=function(e){last=Date.now();"
        "if(!window._ssePending){window._ssePending=true;"
        "setTimeout(function(){location.reload();},150);}};"
        "es.onerror=function(){setTimeout(function(){"
        "if(Date.now()-last>5000){location.reload();}},1000);};"
        "window._sse=es;"
        "}catch(e){}"
        "})();"
    )
    fallback_js = (
        f"setTimeout(function(){{"
        f"if(!window._sse||window._sse.readyState!==1){{location.reload();}}"
        f"}}, {refresh_ms});"
    )
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="{meta_refresh_s}">'
        f'<title>{esc(ROBOT_NAME)} dashboard</title><style>{CSS}</style>'
        f'<script>{sse_js}{fallback_js}</script>'
        '</head><body>'
        f'<div class="hdr"><h1>{esc(ROBOT_NAME)}</h1>'
        f'<span class="tag">{pid_html}</span>'
        f'{src_badge}{age_html}{frozen_html}'
        f'<span class="tag">behavior: <span class="{b_cls}">{bstate}</span></span>'
        f'<span class="tag">vision: <span class="{vcls}">{vstate}</span></span>'
        '<form method="POST" action="/stop"><button class="stop">STOP</button></form>'
        '<form method="POST" action="/shutdown"><button class="stop sdn">SHUTDOWN daemon</button></form>'
        f'{flash_html}</div>{up_html}'
        '<div class="grid">'
        f'<div class="card"><h2>last heard</h2>{heard_html}'
        f'<h2>last decision</h2>{dec_html}'
        f'<h2>tail (last 10 lines)</h2><pre>{tail_html}</pre></div>'
        f'<div class="card"><h2>vision</h2><div class="kv">'
        f'<b>state</b><span class="{vcls}">{vstate}</span>'
        f'<b>ticks</b><span>{ticks if ticks is not None else "--"}</span>'
        f'<b>avg latency</b><span>{lat_str}</span>'
        f'<b>last event</b><span>{vision_evt_html}</span></div>'
        f'<div class="row" style="margin-top:10px">{eye_img}</div>'
        f'<h2>behavior</h2><div class="big"><span class="{b_cls}">{bstate}</span></div>'
        f'<h2>ESP32 telemetry</h2><div class="kv">{tel_rows}</div>{wire_html}'
        f'<h2>LLM / planner</h2><div class="kv">{llm_rows}</div></div></div>'
        f'<div class="small" style="margin-top:12px">refresh {refresh_ms} ms &middot; '
        f'source: {esc(source)} &middot; log: {esc(str(state.get("_log","")))}</div>'
        '</body></html>'
    )


# ------------------------------------------------------------ plumbing

def build_state(logs_dir: Path, state_path: str | None) -> tuple[dict, tuple, str, float | None, int]:
    """Assemble the UI state dict, preferring /tmp/robot_state.json.

    Returns (state, daemon_tuple, source, snapshot_age, refresh_ms).
    source is "state-json" when we used the daemon's atomic snapshot,
    "log-regex" when we fell back to tailing the newest robot-*.log.
    """
    log = latest_log(logs_dir)
    tail = read_tail(log) if log else []

    sj = read_state_file(state_path) if state_path else None
    age: float | None = None
    if sj is not None:
        ts = float(sj.get("ts") or 0.0)
        age = max(0.0, time.time() - ts) if ts else None

    if sj is not None and (age is None or age <= STATE_FALLBACK_S):
        state = state_to_ui(sj, tail)
        state["_log"] = str(log) if log else "(none)"
        # state-json mode is cheap — refresh twice as fast.
        return state, find_daemon(), "state-json", age, 500

    # Fallback: legacy regex path.  Keeps the UI alive if --no-state-file was
    # passed to the daemon or the snapshot file was removed.
    state = scrape(tail)
    state["_log"] = str(log) if log else "(none)"
    return state, find_daemon(), "log-regex", None, 1000


def make_flask_app(logs_dir: Path, state_path: str | None,
                   daemon_url: str = DEFAULT_DAEMON_URL,
                   token_file: str | None = DEFAULT_TOKEN_FILE) -> "Flask":
    app = Flask(__name__)
    flash = {"msg": None, "ts": 0.0}

    def live():
        return flash["msg"] if flash["msg"] and time.time() - flash["ts"] < 4 else None

    def _tok() -> str | None:
        # Re-read the token on each request: cheap (one open+read of ~50
        # bytes) and picks up rotation without a UI restart.
        return read_daemon_token(token_file)

    @app.route("/")
    def index():
        st, dm, src, age, refresh_ms = build_state(logs_dir, state_path)
        return Response(render_page(st, dm, live(), src, age, refresh_ms,
                                    daemon_url=daemon_url, token=_tok()),
                        mimetype="text/html")

    @app.route("/state.json")
    def raw_state():
        # Convenience debug endpoint: serves whatever the daemon wrote.
        if state_path and os.path.exists(state_path):
            try:
                with open(state_path, "rb") as fh:
                    return Response(fh.read(), mimetype="application/json")
            except OSError:
                pass
        return Response(b"{}", status=404, mimetype="application/json")

    @app.route("/eye.png")
    def eye():
        try:
            with open(EYE_PNG, "rb") as fh: return Response(fh.read(), mimetype="image/png")
        except FileNotFoundError:
            return Response(b"", status=404)

    @app.route("/stop", methods=["POST"])
    def stop():
        flash.update(msg=f"STOP: {fire_stop_best(daemon_url, _tok())}",
                     ts=time.time())
        return redirect("/")

    @app.route("/shutdown", methods=["POST"])
    def shutdown():
        flash.update(msg=f"SHUTDOWN: {shutdown_daemon()}", ts=time.time()); return redirect("/")

    @app.route("/healthz")
    def healthz(): return "ok"

    return app


class StdlibHandler(BaseHTTPRequestHandler):  # type: ignore[misc]
    logs_dir: Path = DEFAULT_LOG_DIR
    state_path: str | None = DEFAULT_STATE
    daemon_url: str = DEFAULT_DAEMON_URL
    token_file: str | None = DEFAULT_TOKEN_FILE
    flash: dict = {"msg": None, "ts": 0.0}

    def log_message(self, fmt, *a): pass

    def _live(self):
        return self.flash["msg"] if self.flash["msg"] and time.time() - self.flash["ts"] < 4 else None

    def _tok(self):
        return read_daemon_token(self.token_file)

    def do_GET(self):
        if self.path.startswith("/eye.png"):
            try:
                data = open(EYE_PNG, "rb").read()
                self.send_response(200); self.send_header("Content-Type", "image/png")
                self.end_headers(); self.wfile.write(data)
            except FileNotFoundError:
                self.send_response(404); self.end_headers()
            return
        if self.path == "/state.json":
            if self.state_path and os.path.exists(self.state_path):
                try:
                    data = open(self.state_path, "rb").read()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers(); self.wfile.write(data); return
                except OSError:
                    pass
            self.send_response(404); self.end_headers(); return
        if self.path == "/healthz":
            self.send_response(200); self.end_headers(); self.wfile.write(b"ok"); return
        st, dm, src, age, refresh_ms = build_state(self.logs_dir, self.state_path)
        body = render_page(st, dm, self._live(), src, age, refresh_ms,
                           daemon_url=self.daemon_url,
                           token=self._tok()).encode()
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/stop":
            StdlibHandler.flash = {
                "msg": f"STOP: {fire_stop_best(self.daemon_url, self._tok())}",
                "ts": time.time(),
            }
        elif self.path == "/shutdown":
            StdlibHandler.flash = {"msg": f"SHUTDOWN: {shutdown_daemon()}", "ts": time.time()}
        self.send_response(303); self.send_header("Location", "/"); self.end_headers()


def run_stdlib(host: str, port: int, logs_dir: Path, state_path: str | None,
               daemon_url: str = DEFAULT_DAEMON_URL,
               token_file: str | None = DEFAULT_TOKEN_FILE):
    StdlibHandler.logs_dir = logs_dir
    StdlibHandler.state_path = state_path
    StdlibHandler.daemon_url = daemon_url
    StdlibHandler.token_file = token_file
    print(f"[web_ui] stdlib http.server on http://{host}:{port}/")
    HTTPServer((host, port), StdlibHandler).serve_forever()


def main():
    ap = argparse.ArgumentParser(description="robot dashboard")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--logs-dir", default=str(DEFAULT_LOG_DIR))
    ap.add_argument("--state-file", default=DEFAULT_STATE,
                    help="atomic JSON snapshot published by robot_daemon.  "
                         f"default: {DEFAULT_STATE}.  Pass '' to disable.")
    ap.add_argument("--daemon-url", default=DEFAULT_DAEMON_URL,
                    help="base URL of the daemon's state server.  "
                         f"default: {DEFAULT_DAEMON_URL}.  Supersedes the "
                         "old hardcoded location.hostname:5556 in the SSE "
                         "client; set this when the UI and daemon run on "
                         "different boxes.")
    ap.add_argument("--daemon-token-file", default=DEFAULT_TOKEN_FILE,
                    help="path to the daemon's bearer-token file "
                         f"(default: {DEFAULT_TOKEN_FILE}).  Read on each "
                         "request so rotation doesn't need a UI restart.  "
                         "Pass '' to disable and rely on loopback carve-out.")
    a = ap.parse_args()
    logs_dir = Path(a.logs_dir).resolve()
    logs_dir.mkdir(parents=True, exist_ok=True)
    state_path = a.state_file if a.state_file else None
    token_file = a.daemon_token_file if a.daemon_token_file else None
    print(f"[web_ui] robot={ROBOT_NAME}  logs_dir={logs_dir}  "
          f"state_file={state_path or '(disabled)'}  "
          f"daemon_url={a.daemon_url}  "
          f"token_file={token_file or '(disabled)'}  flask={USE_FLASK}")
    if USE_FLASK:
        make_flask_app(logs_dir, state_path, a.daemon_url, token_file).run(
            host=a.host, port=a.port, debug=False, use_reloader=False)
    else:
        run_stdlib(a.host, a.port, logs_dir, state_path,
                   daemon_url=a.daemon_url, token_file=token_file)


if __name__ == "__main__":
    main()
