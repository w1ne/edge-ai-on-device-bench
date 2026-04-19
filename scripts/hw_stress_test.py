#!/usr/bin/env python3
"""
hw_stress_test.py — REAL hardware stress test for the on-table robot.

Unlike scripts/stress_test.py (which measures daemon RSS/FD and proves
*nothing* about the motors, IMU, or USB link), this harness actually
exercises the servos + IMU and monitors live ESP32 telemetry for the
whole run.

It talks DIRECTLY to demo.robot_daemon.send_wire — no daemon, no voice,
no vision subprocesses — so the only thing under test is the wire + the
hardware.

Usage:
    python3 scripts/hw_stress_test.py --duration 1800
    python3 scripts/hw_stress_test.py --duration 10 --dry-run

Loop mix over the run (each second rolls for one bucket):
    40%  pose cycle    — random pose (neutral/lean_left/lean_right/bow_front),
                          duration 400-1200 ms, wait 500 ms
    30%  pose sweep    — neutral → lean_left → lean_right → bow_front at 1 Hz
    20%  IMU sanity    — hold neutral; continuously drain telemetry
    10%  jump          — every ~2 min, {"c":"jump"}, settle 3 s

DO NOT WALK.  The robot is on a table.  "walk" does not appear anywhere in
this file.  `--dry-run` simulates telemetry.

At exit (SIGINT / SIGTERM / timeout) we send {"c":"stop"} then
{"c":"pose","n":"neutral","d":800} to leave the robot safe.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import random
import re
import signal
import statistics
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "demo"))

# Import send_wire + the USB lock so our reader thread serializes with
# commands on the same handle.  Lazy pyusb import keeps --dry-run working
# without the library installed.
import robot_daemon as rd  # noqa: E402

LOGS_DIR = REPO / "logs"
LOGS_DIR.mkdir(exist_ok=True)

POSES = ("neutral", "lean_left", "lean_right", "bow_front")
SWEEP_SEQUENCE = ("neutral", "lean_left", "lean_right", "bow_front")

# Health thresholds
VOLT_LOW_V         = 6.5      # same as daemon battery alert
VOLT_LOW_STREAK    = 3        # consecutive packets
TEMP_HIGH_C        = 50.0
TEMP_HIGH_STREAK_S = 30.0
NO_PACKET_TIMEOUT  = 2.0      # s without any state packet -> FAIL
IMU_ZERO_TIMEOUT   = 5.0      # s with all-zero IMU / |az| < 0.5 -> brownout
SERVO_STUCK_TIMEOUT = 2.0     # s after a pose cmd without any servo delta

# Jump cadence — probability chosen so one jump fires every ~2 min.
JUMP_BUDGET_S = 120.0

# Telemetry line regexes (mirror robot_daemon's)
_P_RE    = re.compile(r'"p"\s*:\s*\[([\-\d,\s]+)\]')
_V_RE    = re.compile(r'"v"\s*:\s*(\d+)')
_TMP_RE  = re.compile(r'"tmp"\s*:\s*([\-\d.]+)')
_MS_RE   = re.compile(r'"ms"\s*:\s*(\d+)')
_IMU_RE  = re.compile(r'"imu"\s*:\s*\[([^\]]+)\]')


# ------------------------------------------------------------------ packet type

class Packet:
    __slots__ = ("ts", "p", "voltage", "temp", "uptime_ms", "imu")

    def __init__(self, ts, p, voltage, temp, uptime_ms, imu):
        self.ts = ts
        self.p = p
        self.voltage = voltage
        self.temp = temp
        self.uptime_ms = uptime_ms
        self.imu = imu


def parse_packet(line: str, now: float) -> Packet | None:
    """Extract fields from one ESP32 state line.  Returns None if it's
    an ack / not a state packet."""
    # Must have at least p[] + v + imu to count as state.
    pm = _P_RE.search(line)
    vm = _V_RE.search(line)
    if not (pm and vm):
        return None
    try:
        p = [int(x) for x in pm.group(1).split(",")]
    except ValueError:
        return None
    voltage = int(vm.group(1)) / 10.0
    tm = _TMP_RE.search(line)
    temp = float(tm.group(1)) if tm else None
    ms = _MS_RE.search(line)
    uptime = int(ms.group(1)) if ms else None
    im = _IMU_RE.search(line)
    imu = None
    if im:
        try:
            imu = [float(x) for x in im.group(1).split(",")]
        except ValueError:
            imu = None
    return Packet(now, p, voltage, temp, uptime, imu)


# ------------------------------------------------------------------ telemetry store

class Telemetry:
    """Thread-safe aggregator + live state.

    The reader thread pushes Packets in; the main loop + health-watcher
    read the summary fields (all guarded by a single RLock).
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.start_ts = time.time()
        # Rolling counters
        self.packets_total = 0
        self.errors_total = 0
        self.last_packet_ts: float | None = None
        # Last-seen snapshot (None until first packet)
        self.last: Packet | None = None
        # Running stats per IMU axis
        self.imu_mins = [float("inf")] * 6
        self.imu_maxs = [float("-inf")] * 6
        self.imu_sums = [0.0] * 6
        self.imu_counts = [0] * 6
        # Voltage stats
        self.volt_min = float("inf")
        self.volt_max = float("-inf")
        self.volt_sum = 0.0
        self.volt_count = 0
        # Per-sec rate ring
        self._rate_bucket_ts = int(self.start_ts)
        self._rate_bucket_n = 0
        self._rate_last = 0.0

    def record(self, pkt: Packet) -> None:
        with self.lock:
            self.packets_total += 1
            self.last_packet_ts = pkt.ts
            self.last = pkt
            # voltage
            self.volt_min = min(self.volt_min, pkt.voltage)
            self.volt_max = max(self.volt_max, pkt.voltage)
            self.volt_sum += pkt.voltage
            self.volt_count += 1
            # imu
            if pkt.imu and len(pkt.imu) == 6:
                for i, v in enumerate(pkt.imu):
                    if v < self.imu_mins[i]:
                        self.imu_mins[i] = v
                    if v > self.imu_maxs[i]:
                        self.imu_maxs[i] = v
                    self.imu_sums[i] += v
                    self.imu_counts[i] += 1
            # rate
            sec = int(pkt.ts)
            if sec == self._rate_bucket_ts:
                self._rate_bucket_n += 1
            else:
                self._rate_last = float(self._rate_bucket_n)
                self._rate_bucket_ts = sec
                self._rate_bucket_n = 1

    def note_error(self) -> None:
        with self.lock:
            self.errors_total += 1

    def packets_per_sec(self) -> float:
        with self.lock:
            return self._rate_last

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "packets": self.packets_total,
                "errors": self.errors_total,
                "last_ts": self.last_packet_ts,
                "last": self.last,
                "rate": self._rate_last,
            }


# ------------------------------------------------------------------ reader thread

def reader_loop(stop_evt: threading.Event, tel: Telemetry,
                dry_run: bool, fake_state: "FakeState | None") -> None:
    """Drain USB CDC state packets.  Runs in a thread.

    Real mode: acquire _USB_LOCK briefly, read up to 4 kB with 30 ms
    timeout, release.  The ESP32 streams at 10 Hz so this wakes ~every
    100 ms.

    Dry-run mode: emit a fake packet every 100 ms using fake_state.
    """
    if dry_run:
        assert fake_state is not None
        next_pkt = time.time()
        while not stop_evt.is_set():
            now = time.time()
            if now >= next_pkt:
                line = fake_state.emit_line(now)
                pkt = parse_packet(line, now)
                if pkt is not None:
                    tel.record(pkt)
                next_pkt += 0.1
            stop_evt.wait(0.02)
        return

    # Real mode
    import usb.core  # noqa: F401  (force pyusb available)

    leftover = bytearray()
    while not stop_evt.is_set():
        got_any = False
        with rd._USB_LOCK:
            if rd._USB_DEV is None:
                rd._USB_DEV = rd._open_usb()
            dev = rd._USB_DEV
            if dev is not None:
                try:
                    data = dev.read(0x81, 4096, timeout=30)
                    if data:
                        leftover.extend(bytes(data))
                        got_any = True
                except Exception as e:
                    errno = getattr(e, "errno", None)
                    if errno in (19, 5):
                        rd._drop_usb()
                        tel.note_error()
        if got_any:
            text = leftover.decode("utf-8", "replace")
            # keep everything after the last newline for the next pass
            idx = text.rfind("\n")
            if idx >= 0:
                complete = text[: idx + 1]
                leftover = bytearray(text[idx + 1:].encode("utf-8"))
                now = time.time()
                for line in complete.splitlines():
                    if not line.strip():
                        continue
                    pkt = parse_packet(line, now)
                    if pkt is not None:
                        tel.record(pkt)
        else:
            # back off a hair so we don't starve command writes
            stop_evt.wait(0.02)


# ------------------------------------------------------------------ dry-run faker

class FakeState:
    """Deterministic fake telemetry for --dry-run."""

    def __init__(self):
        self.current_pose = "neutral"
        self.uptime_ms = 0
        self.lock = threading.Lock()

    def set_pose(self, name: str) -> None:
        with self.lock:
            self.current_pose = name

    def emit_line(self, now: float) -> str:
        with self.lock:
            pose = self.current_pose
        # p0 oscillates per pose to simulate servo tracking
        base = {"neutral": 1500, "lean_left": 1350,
                "lean_right": 1650, "bow_front": 1200}.get(pose, 1500)
        p0 = base + int(20 * ((now * 10) % 10 - 5))
        p = [p0, 1500, 1500, 1500]
        self.uptime_ms += 100
        # ax=0, ay=0, az=1.0 (gravity), gyro near zero
        imu = [0.0, 0.0, 1.0, 0.1, -0.1, 0.0]
        line = ('{"p":[%d,%d,%d,%d],"v":78,"tmp":25.0,'
                '"ms":%d,"imu":[%.2f,%.2f,%.2f,%.2f,%.2f,%.2f]}\n') % (
            p[0], p[1], p[2], p[3], self.uptime_ms,
            imu[0], imu[1], imu[2], imu[3], imu[4], imu[5])
        return line


# ------------------------------------------------------------------ failure log

class FailureLog:
    """Dedup'd failure tracker — same kind only logged when it transitions
    from OK -> BAD, plus counted each time it re-enters BAD.
    """

    def __init__(self, logger):
        self.logger = logger
        self.counts: dict[str, int] = {}
        self.active: dict[str, bool] = {}
        self.lock = threading.Lock()

    def raise_(self, kind: str, detail: str) -> None:
        with self.lock:
            if self.active.get(kind):
                return
            self.active[kind] = True
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.logger(f"[FAIL] {kind}: {detail}")

    def clear(self, kind: str) -> None:
        with self.lock:
            if self.active.get(kind):
                self.active[kind] = False
                self.logger(f"[ok]   {kind} cleared")

    def total(self) -> int:
        with self.lock:
            return sum(self.counts.values())


# ------------------------------------------------------------------ health watch

def health_watch_loop(stop_evt: threading.Event, tel: Telemetry,
                      fails: FailureLog,
                      cmd_state: dict) -> None:
    """Runs every 200 ms.  Checks the rolling invariants."""
    volt_low_streak = 0
    temp_high_since: float | None = None
    imu_zero_since: float | None = None
    servo_stuck_since: float | None = None
    last_p_for_cmd: dict = {}  # cmd-send id -> last positions at issue

    while not stop_evt.is_set():
        now = time.time()
        snap = tel.snapshot()
        last = snap["last"]
        last_ts = snap["last_ts"]

        # 1. No-packet watchdog
        if last_ts is None:
            # Give the system 3 s after start before flagging
            if now - tel.start_ts > 3.0:
                fails.raise_("no_packets", f"no state packets in {now - tel.start_ts:.1f}s")
        else:
            gap = now - last_ts
            if gap > NO_PACKET_TIMEOUT:
                fails.raise_("no_packets", f"gap {gap:.1f}s since last packet")
            else:
                fails.clear("no_packets")

        if last is not None:
            # 2. Voltage
            if last.voltage < VOLT_LOW_V:
                volt_low_streak += 1
                if volt_low_streak >= VOLT_LOW_STREAK:
                    fails.raise_("low_voltage",
                                 f"{last.voltage:.1f}V (<{VOLT_LOW_V}V x{volt_low_streak})")
            else:
                if volt_low_streak >= VOLT_LOW_STREAK:
                    fails.clear("low_voltage")
                volt_low_streak = 0

            # 3. Temperature
            if last.temp is not None and last.temp > TEMP_HIGH_C:
                if temp_high_since is None:
                    temp_high_since = now
                elif now - temp_high_since > TEMP_HIGH_STREAK_S:
                    fails.raise_("high_temp",
                                 f"{last.temp:.1f}C for {now - temp_high_since:.1f}s")
            else:
                if temp_high_since is not None and now - temp_high_since > TEMP_HIGH_STREAK_S:
                    fails.clear("high_temp")
                temp_high_since = None

            # 4. IMU brownout / all-zero
            if last.imu and len(last.imu) == 6:
                az = last.imu[2]
                all_zero = all(abs(x) < 0.01 for x in last.imu)
                low_g = abs(az) < 0.5
                if all_zero or low_g:
                    if imu_zero_since is None:
                        imu_zero_since = now
                    elif now - imu_zero_since > IMU_ZERO_TIMEOUT:
                        fails.raise_("imu_zero",
                                     f"|az|={abs(az):.2f} for {now - imu_zero_since:.1f}s")
                else:
                    if imu_zero_since is not None and now - imu_zero_since > IMU_ZERO_TIMEOUT:
                        fails.clear("imu_zero")
                    imu_zero_since = None

            # 5. Servo stuck after pose command
            pending = cmd_state.get("pending_pose")
            if pending is not None:
                issued_at, start_p = pending
                if start_p is not None and last.p is not None:
                    if last.p != start_p:
                        cmd_state["pending_pose"] = None
                        fails.clear("servo_stuck")
                        servo_stuck_since = None
                    else:
                        if now - issued_at > SERVO_STUCK_TIMEOUT:
                            fails.raise_("servo_stuck",
                                         f"p unchanged {now - issued_at:.1f}s after pose")

        stop_evt.wait(0.2)


# ------------------------------------------------------------------ CSV writer

class CsvSink:
    FIELDS = [
        "ts", "cmd_sent",
        "p0", "p1", "p2", "p3",
        "voltage_v", "temp_c", "uptime_ms",
        "ax", "ay", "az", "gx", "gy", "gz",
        "packets_per_sec", "errors_since_start",
    ]

    def __init__(self, path: Path):
        self.path = path
        self.fh = path.open("w", newline="")
        self.w = csv.writer(self.fh)
        self.w.writerow(self.FIELDS)
        self.fh.flush()
        self.lock = threading.Lock()

    def write(self, cmd_sent: str, tel: Telemetry) -> None:
        snap = tel.snapshot()
        last = snap["last"]
        if last is None:
            row = [f"{time.time():.3f}", cmd_sent,
                   "", "", "", "", "", "", "",
                   "", "", "", "", "", "",
                   0, snap["errors"]]
        else:
            p = (list(last.p) + [""] * 4)[:4]
            imu = (list(last.imu) if last.imu else [""] * 6)
            imu = (imu + [""] * 6)[:6]
            row = [f"{last.ts:.3f}", cmd_sent,
                   p[0], p[1], p[2], p[3],
                   f"{last.voltage:.2f}",
                   (f"{last.temp:.1f}" if last.temp is not None else ""),
                   (last.uptime_ms if last.uptime_ms is not None else ""),
                   f"{imu[0]:.3f}" if imu[0] != "" else "",
                   f"{imu[1]:.3f}" if imu[1] != "" else "",
                   f"{imu[2]:.3f}" if imu[2] != "" else "",
                   f"{imu[3]:.3f}" if imu[3] != "" else "",
                   f"{imu[4]:.3f}" if imu[4] != "" else "",
                   f"{imu[5]:.3f}" if imu[5] != "" else "",
                   f"{snap['rate']:.1f}", snap["errors"]]
        with self.lock:
            self.w.writerow(row)
            self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


# ------------------------------------------------------------------ send helpers

def send(cmd: dict, dry_run: bool, fake: "FakeState | None",
         logger, tel: Telemetry,
         cmd_counts: dict[str, int]) -> tuple[str, list | None]:
    """Wrap rd.send_wire with logging + fake-state update + counting."""
    cmd_counts[cmd["c"]] = cmd_counts.get(cmd["c"], 0) + 1
    if dry_run:
        # update fake so reader reflects the new pose in p0
        if fake is not None and cmd.get("c") == "pose":
            fake.set_pose(str(cmd.get("n", "neutral")))
        return "dry-run", None
    try:
        ack, pos = rd.send_wire(cmd, False)
    except Exception as e:
        logger(f"[send] exception {type(e).__name__}: {e}")
        tel.note_error()
        return "exception", None
    if ack == "no-device" or (isinstance(ack, str) and "no-device" in ack):
        logger(f"[send] USB no-device for {cmd}")
        tel.note_error()
    return (ack or ""), pos


# ------------------------------------------------------------------ main loop

def run(duration: float, dry_run: bool, csv_path: Path, md_path: Path) -> int:
    started_wall = time.time()
    ts_started = _dt.datetime.fromtimestamp(started_wall).isoformat(timespec="seconds")

    log_lines: list[str] = []

    def logger(line: str) -> None:
        ts = _dt.datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {line}"
        print(msg, flush=True)
        log_lines.append(msg)

    logger(f"hw_stress_test start duration={duration}s dry_run={dry_run}")
    logger(f"csv={csv_path}")

    tel = Telemetry()
    fails = FailureLog(logger)
    cmd_counts: dict[str, int] = {}
    cmd_state: dict = {"pending_pose": None}
    fake = FakeState() if dry_run else None

    stop_evt = threading.Event()
    reader_thr = threading.Thread(
        target=reader_loop, args=(stop_evt, tel, dry_run, fake),
        name="hw-reader", daemon=True)
    watch_thr = threading.Thread(
        target=health_watch_loop, args=(stop_evt, tel, fails, cmd_state),
        name="hw-watch", daemon=True)
    reader_thr.start()
    watch_thr.start()

    csv_sink = CsvSink(csv_path)

    # SIGINT / SIGTERM — flip the stop flag; main loop observes it.
    interrupted = threading.Event()

    def handle_sig(signum, _frame):
        logger(f"signal {signum} — stopping")
        interrupted.set()
        stop_evt.set()
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    # Jump cadence: fire one jump roughly every JUMP_BUDGET_S.
    last_jump = started_wall - JUMP_BUDGET_S + 30.0  # first jump ~30s in
    # Sweep state
    sweep_i = 0
    sweep_next = started_wall
    # IMU sanity window state
    imu_window_end: float | None = None
    # Per-iter CSV row every second
    next_csv = started_wall

    deadline = started_wall + duration
    exit_code = 0

    try:
        while time.time() < deadline and not interrupted.is_set():
            now = time.time()

            # Priority 1 — IMU sanity windows (20%): if we're mid-window,
            # just hold neutral and let the reader thread record.  Window
            # entered probabilistically but only when not already in one.
            if imu_window_end is not None and now < imu_window_end:
                time.sleep(0.1)
                # one CSV row per second
                if now >= next_csv:
                    csv_sink.write("imu_sanity", tel)
                    next_csv = now + 1.0
                continue
            if imu_window_end is not None and now >= imu_window_end:
                logger("[imu] window ended")
                imu_window_end = None

            # Priority 2 — jump cadence
            if now - last_jump >= JUMP_BUDGET_S:
                logger("[jump] firing")
                ack, _ = send({"c": "jump"}, dry_run, fake, logger, tel, cmd_counts)
                csv_sink.write("jump", tel)
                time.sleep(3.0)
                last_jump = now
                continue

            # Bucket selection (among pose / sweep / imu_window)
            # Normalized: pose 40 / sweep 30 / imu 20 = 90
            r = random.random() * 90
            if r < 40:
                bucket = "pose"
            elif r < 70:
                bucket = "sweep"
            else:
                bucket = "imu"

            if bucket == "imu":
                imu_window_end = now + 30.0
                logger("[imu] entering 30s neutral hold")
                snap = tel.snapshot()
                start_p = list(snap["last"].p) if snap["last"] else None
                cmd_state["pending_pose"] = (now, start_p)
                send({"c": "pose", "n": "neutral", "d": 800},
                     dry_run, fake, logger, tel, cmd_counts)
                csv_sink.write("pose:neutral(imu-win)", tel)
                continue

            if bucket == "sweep":
                # 1 Hz step through SWEEP_SEQUENCE
                if now < sweep_next:
                    time.sleep(max(0.0, sweep_next - now))
                    continue
                pose_name = SWEEP_SEQUENCE[sweep_i % len(SWEEP_SEQUENCE)]
                sweep_i += 1
                sweep_next = now + 1.0
                snap = tel.snapshot()
                start_p = list(snap["last"].p) if snap["last"] else None
                cmd_state["pending_pose"] = (now, start_p)
                ack, _ = send({"c": "pose", "n": pose_name, "d": 800},
                              dry_run, fake, logger, tel, cmd_counts)
                csv_sink.write(f"sweep:{pose_name}", tel)
                continue

            # bucket == "pose"
            pose_name = random.choice(POSES)
            d_ms = random.randint(400, 1200)
            snap = tel.snapshot()
            start_p = list(snap["last"].p) if snap["last"] else None
            cmd_state["pending_pose"] = (now, start_p)
            ack, _ = send({"c": "pose", "n": pose_name, "d": d_ms},
                          dry_run, fake, logger, tel, cmd_counts)
            csv_sink.write(f"pose:{pose_name}", tel)
            # wait for completion (send_wire already blocks up to ~1.3s),
            # then a 500 ms pause.
            time.sleep(0.5)

            # periodic CSV flush (if neither sweep nor pose triggered it)
            if time.time() >= next_csv:
                csv_sink.write("tick", tel)
                next_csv = time.time() + 1.0
    except Exception as e:
        logger(f"[main] unhandled {type(e).__name__}: {e}")
        exit_code = 2
    finally:
        # Safe shutdown — stop motors, return to neutral, always.
        logger("shutdown: stop + neutral")
        try:
            send({"c": "stop"}, dry_run, fake, logger, tel, cmd_counts)
            time.sleep(0.2)
            send({"c": "pose", "n": "neutral", "d": 800},
                 dry_run, fake, logger, tel, cmd_counts)
            time.sleep(0.9)
        except Exception as e:
            logger(f"shutdown send_wire error: {e}")

        stop_evt.set()
        reader_thr.join(timeout=2.0)
        watch_thr.join(timeout=2.0)
        csv_sink.close()

    # ---- summary ------------------------------------------------------
    ended_wall = time.time()
    duration_actual = ended_wall - started_wall

    def _axis_stats(i: int) -> tuple[float, float, float] | None:
        with tel.lock:
            n = tel.imu_counts[i]
            if n == 0:
                return None
            return (round(tel.imu_mins[i], 3),
                    round(tel.imu_maxs[i], 3),
                    round(tel.imu_sums[i] / n, 3))

    imu_labels = ["ax", "ay", "az", "gx", "gy", "gz"]
    imu_stats_rows = []
    for i, lbl in enumerate(imu_labels):
        s = _axis_stats(i)
        if s is None:
            imu_stats_rows.append(f"- {lbl}: no samples")
        else:
            imu_stats_rows.append(f"- {lbl}: min={s[0]} max={s[1]} mean={s[2]}")

    with tel.lock:
        v_min = tel.volt_min if tel.volt_count else None
        v_max = tel.volt_max if tel.volt_count else None
        v_mean = (tel.volt_sum / tel.volt_count) if tel.volt_count else None
        total_pkts = tel.packets_total
        total_errs = tel.errors_total

    failure_total = fails.total()
    verdict = "PASS" if failure_total == 0 and exit_code == 0 else "FAIL"

    summary = []
    summary.append(f"# hw_stress_test summary")
    summary.append("")
    summary.append(f"- started: {ts_started}")
    summary.append(f"- duration: {duration_actual:.1f}s (requested {duration}s)")
    summary.append(f"- dry_run: {dry_run}")
    summary.append(f"- csv: {csv_path}")
    summary.append("")
    summary.append(f"## Commands sent")
    if not cmd_counts:
        summary.append("- none")
    for k in sorted(cmd_counts):
        summary.append(f"- {k}: {cmd_counts[k]}")
    summary.append("")
    summary.append(f"## Telemetry")
    summary.append(f"- state packets: {total_pkts}")
    summary.append(f"- send errors:   {total_errs}")
    if v_min is not None:
        summary.append(f"- voltage: min={v_min:.2f}V max={v_max:.2f}V mean={v_mean:.2f}V")
    else:
        summary.append(f"- voltage: no samples")
    summary.append("")
    summary.append(f"## IMU")
    summary.extend(imu_stats_rows)
    summary.append("")
    summary.append(f"## Failures")
    if not fails.counts:
        summary.append("- none")
    for k in sorted(fails.counts):
        summary.append(f"- {k}: {fails.counts[k]}")
    summary.append("")
    summary.append(f"## Verdict: **{verdict}**")

    summary_text = "\n".join(summary) + "\n"
    md_path.write_text(summary_text)
    print()
    print(summary_text)
    logger(f"summary -> {md_path}")
    # Also stash the full log next to the summary (useful for debugging)
    try:
        (md_path.with_suffix(".log")).write_text("\n".join(log_lines) + "\n")
    except Exception:
        pass

    return 0 if verdict == "PASS" else 1


# ------------------------------------------------------------------ CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--duration", type=float, default=1800.0,
                    help="seconds (default 1800 = 30 min)")
    ap.add_argument("--dry-run", action="store_true",
                    help="simulate USB + telemetry; no real commands")
    ap.add_argument("--out", type=str, default=None,
                    help="override CSV path")
    args = ap.parse_args()

    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.out:
        csv_path = Path(args.out)
    else:
        csv_path = LOGS_DIR / f"hw_stress_{ts}.csv"
    md_path = LOGS_DIR / f"hw_stress_{ts}.md"
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    return run(args.duration, args.dry_run, csv_path, md_path)


if __name__ == "__main__":
    sys.exit(main())
