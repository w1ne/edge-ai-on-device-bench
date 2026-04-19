#!/usr/bin/env python3
"""
stress_test.py — 30-minute stress harness for demo/robot_daemon.py.

Launches the daemon in --mode text with webcam vision, pushes a safe
round-robin of inputs on 10 s cadence (pose / ping / memory phrases only —
never walk or jump, the robot is on a table), samples daemon RSS / CPU% /
open-FD count / vision child RSS / ESP32 telemetry / webcam liveness /
Pixel adb uptime once per minute, and writes the samples to
logs/stress_<date>.csv.  After --minutes wall clock it sends 'shut down',
waits for clean exit, and emits logs/stress_<date>_summary.md with a
pass/fail verdict.

Safety:
    * Inputs are drawn from a fixed list that excludes 'walk' and 'jump'.
    * Hard cap of 40 minutes regardless of --minutes.
    * If the daemon exits unexpectedly, logs last 50 log lines and stops.

This script is additive — it does not modify robot_daemon.py or
vision_watcher.py.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DAEMON = REPO / "demo" / "robot_daemon.py"
LOGS_DIR = REPO / "logs"
DAEMON_LOG = Path("/tmp/stress.log")
PIXEL_SERIAL = "1B291FDF600260"

# Round-robin inputs — ALL SAFE for a table-top robot.
SAFE_INPUTS = [
    "ping",
    "neutral",
    "lean_left",
    "neutral",
    "lean_right",
    "neutral",
    "bow",
    "neutral",
    "do it again",
    "repeat that",
]

# Hard cap of 40 min wall clock, per task spec.
HARD_CAP_SECONDS = 40 * 60

# ESP32 telemetry regex — matches the inline JSON lines the daemon logs
# from vision/wire paths.  "v":72 == 7.2 V, "tmp":31 == 31 C.
_TMP_RE = re.compile(r'"tmp"\s*:\s*(\d+)')
_VOLT_RE = re.compile(r'"v"\s*:\s*(\d+)')
# Vision restart counter lines look like:  [vision] subprocess died, restart #N
_VISION_RESTART_RE = re.compile(r"\[vision\] subprocess died, restart #(\d+)")
_VISION_DIED_RE = re.compile(r"\[vision\] .*(died|spawn failed|giving up)")
_ERROR_RE = re.compile(r"\bERROR:\s*(\S+)")


# ---------------------------------------------------------------- /proc helpers

def _read_proc_status(pid: int) -> dict[str, str]:
    try:
        with open(f"/proc/{pid}/status") as fh:
            out = {}
            for line in fh:
                k, _, v = line.partition(":")
                out[k.strip()] = v.strip()
            return out
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return {}


def _read_vmrss_mb(pid: int) -> float | None:
    st = _read_proc_status(pid)
    v = st.get("VmRSS", "")
    if not v:
        return None
    # "VmRSS:  12345 kB"
    m = re.match(r"(\d+)", v)
    if not m:
        return None
    return int(m.group(1)) / 1024.0


def _read_jiffies(pid: int) -> int | None:
    """Sum utime + stime from /proc/<pid>/stat.  The comm (field 2) may
    contain spaces, so we split on the final ')' and index into the rest
    of the line (post-(pid, comm, state) the layout is stable)."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            raw = fh.read()
        close = raw.rfind(")")
        rest = raw[close + 2:].split()
        utime = int(rest[11])  # field 14 (stat(5)) minus pid+comm+state
        stime = int(rest[12])  # field 15
        return utime + stime
    except (FileNotFoundError, ProcessLookupError, PermissionError,
            IndexError, ValueError):
        return None


def _read_fd_count(pid: int) -> int | None:
    try:
        return len(os.listdir(f"/proc/{pid}/fd"))
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return None


def _find_vision_child(daemon_pid: int) -> int | None:
    """Walk /proc looking for a child whose ppid == daemon_pid AND whose
    cmdline contains 'vision_watcher.py'."""
    try:
        pids = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status") as fh:
                ppid = None
                for line in fh:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            if ppid != daemon_pid:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "replace")
            if "vision_watcher" in cmd:
                return pid
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    return None


# ---------------------------------------------------------------- probes

def probe_webcam() -> bool:
    """Report whether the webcam device node is present and readable.

    We intentionally DO NOT call cv2.VideoCapture(0).isOpened() here: the
    vision_watcher subprocess holds /dev/video0 exclusively during the test,
    so an opportunistic cv2 open always fails and would mask the real state.
    Checking for the node + O_RDONLY access is enough to detect the two
    failure modes we care about (USB camera yanked out, permission change).
    """
    dev = "/dev/video0"
    try:
        return os.path.exists(dev) and os.access(dev, os.R_OK)
    except Exception:
        return False


def probe_pixel(serial: str = PIXEL_SERIAL) -> bool:
    try:
        r = subprocess.run(
            ["adb", "-s", serial, "shell", "uptime"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL,
        )
        return r.returncode == 0 and bool(r.stdout.strip())
    except Exception:
        return False


def tail_log_for_telemetry(log_path: Path, n_bytes: int = 32 * 1024
                           ) -> tuple[int | None, int | None]:
    """Scan the last n_bytes of the daemon log for the most recent
    '"tmp":N' and '"v":M' values.  Returns (temp_c, volts_x10) or (None, None)
    if the log had nothing parseable."""
    if not log_path.exists():
        return None, None
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as fh:
            if size > n_bytes:
                fh.seek(-n_bytes, 2)
            chunk = fh.read().decode("utf-8", "replace")
    except Exception:
        return None, None
    tmp = None
    v = None
    for m in _TMP_RE.finditer(chunk):
        tmp = int(m.group(1))
    for m in _VOLT_RE.finditer(chunk):
        v = int(m.group(1))
    return tmp, v


def scan_log_for_events(log_path: Path) -> dict[str, int]:
    """One-shot full scan of daemon log.  Counts ERROR: lines by type,
    '[vision] subprocess died' events, and '[vision] spawn failed'."""
    counts = {
        "error_total": 0,
        "error_types": {},
        "vision_died": 0,
        "vision_restart_max": 0,
        "vision_giving_up": 0,
    }
    if not log_path.exists():
        return counts
    try:
        with open(log_path, "r", errors="replace") as fh:
            for line in fh:
                m = _ERROR_RE.search(line)
                if m:
                    counts["error_total"] += 1
                    kind = m.group(1).rstrip(":")
                    counts["error_types"][kind] = counts["error_types"].get(kind, 0) + 1
                rm = _VISION_RESTART_RE.search(line)
                if rm:
                    n = int(rm.group(1))
                    if n > counts["vision_restart_max"]:
                        counts["vision_restart_max"] = n
                if _VISION_DIED_RE.search(line):
                    counts["vision_died"] += 1
                if "giving up after" in line and "[vision]" in line:
                    counts["vision_giving_up"] += 1
    except Exception:
        pass
    return counts


# ---------------------------------------------------------------- input driver

class InputDriver(threading.Thread):
    """Feeds SAFE_INPUTS round-robin to the daemon stdin every `period` s."""

    def __init__(self, stdin, period: float, stop_evt: threading.Event,
                 logger):
        super().__init__(daemon=True)
        self.stdin = stdin
        self.period = period
        self.stop_evt = stop_evt
        self.logger = logger
        self.sent = 0

    def run(self):
        idx = 0
        # Wait a few seconds before first input so the daemon has time to
        # finish boot, self-test, and spawn the vision thread.
        if self.stop_evt.wait(5.0):
            return
        while not self.stop_evt.is_set():
            text = SAFE_INPUTS[idx % len(SAFE_INPUTS)]
            # Table-top safety guard: belt and suspenders — never let walk/jump
            # slip in even if SAFE_INPUTS were edited.
            if any(bad in text.lower() for bad in ("walk", "jump", "march",
                                                   "hop", "leap")):
                self.logger(f"[driver] REFUSING unsafe input {text!r}")
            else:
                try:
                    self.stdin.write(text + "\n")
                    self.stdin.flush()
                    self.sent += 1
                    self.logger(f"[driver] sent #{self.sent}: {text!r}")
                except (BrokenPipeError, ValueError, OSError) as e:
                    self.logger(f"[driver] stdin write failed: {e}")
                    return
            idx += 1
            if self.stop_evt.wait(self.period):
                return


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--minutes", type=float, default=30.0,
                   help="test duration in minutes (default 30)")
    p.add_argument("--input-period", type=float, default=10.0,
                   help="seconds between inputs (default 10)")
    p.add_argument("--sample-period", type=float, default=60.0,
                   help="seconds between resource samples (default 60)")
    args = p.parse_args()

    test_secs = min(args.minutes * 60, HARD_CAP_SECONDS - 10 * 60)
    date = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    LOGS_DIR.mkdir(exist_ok=True)
    csv_path = LOGS_DIR / f"stress_{date}.csv"
    kept_log_path = LOGS_DIR / f"stress_{date}.log"
    summary_path = LOGS_DIR / f"stress_{date}_summary.md"

    # Fresh daemon log — the daemon appends, so wipe any leftover.
    try:
        DAEMON_LOG.unlink()
    except FileNotFoundError:
        pass

    # Harness console logger (stderr + tee to summary on fail).
    harness_lines: list[str] = []

    def hlog(s: str) -> None:
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} [harness] {s}"
        print(line, file=sys.stderr, flush=True)
        harness_lines.append(line)

    hlog(f"csv={csv_path}")
    hlog(f"daemon_log={DAEMON_LOG} (kept copy: {kept_log_path})")
    hlog(f"duration={test_secs:.0f}s  input_period={args.input_period}s  "
         f"sample_period={args.sample_period}s")

    # Launch the daemon.
    daemon_cmd = [
        sys.executable, str(DAEMON),
        "--mode", "text",
        "--no-tts",
        "--with-vision", "person",
        "--vision-source", "webcam",
        "--vision-interval", "0.1",
        "--log", str(DAEMON_LOG),
    ]
    hlog(f"launching daemon: {' '.join(daemon_cmd)}")

    env = dict(os.environ)
    # Make sure adb subprocesses target Pixel 6 if daemon spawns any.
    env.setdefault("ANDROID_SERIAL", PIXEL_SERIAL)
    # Unbuffered Python so we see output promptly.
    env["PYTHONUNBUFFERED"] = "1"

    proc = subprocess.Popen(
        daemon_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,   # daemon writes its transcript to --log
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
        cwd=str(REPO),
        env=env,
    )
    hlog(f"daemon pid={proc.pid}")

    stop_driver = threading.Event()
    driver = InputDriver(proc.stdin, args.input_period, stop_driver, hlog)
    driver.start()

    # CSV writer
    csv_fh = open(csv_path, "w", buffering=1)
    csv_fh.write("t_s,daemon_rss_mb,daemon_cpu_pct,daemon_fds,"
                 "vision_rss_mb,esp_tmp_c,esp_v_x10,webcam_ok,pixel_ok\n")

    t_start = time.time()
    missed_samples = 0

    # Baselines: first sample happens at t=0 but the daemon is still
    # importing pyusb/torch/etc., so its RSS hasn't settled.  We defer the
    # start_rss / start_fds baseline until the first sample taken >=
    # WARMUP_SECS into the test (after boot + vision subprocess up).
    WARMUP_SECS = 90.0
    start_rss = None
    start_fds = None
    peak_rss = 0.0
    peak_fds = 0
    peak_tmp = None
    last_rss = None
    last_fds = None
    samples_taken = 0

    # Prime CPU accounting
    prev_jiffies = _read_jiffies(proc.pid)
    prev_time = time.time()
    # Determine clock ticks per second (normally 100).
    try:
        clk_tck = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        clk_tck = 100

    try:
        while True:
            elapsed = time.time() - t_start
            if elapsed >= test_secs:
                hlog(f"duration reached ({elapsed:.0f}s); beginning shutdown")
                break
            # Cap
            if elapsed >= HARD_CAP_SECONDS - 10 * 60:
                hlog("hard cap approaching; beginning shutdown")
                break
            # Did daemon die?
            if proc.poll() is not None:
                hlog(f"DAEMON EXITED UNEXPECTEDLY rc={proc.returncode} "
                     f"at t={elapsed:.0f}s — aborting")
                break

            # Sample.
            sample_t0 = time.time()
            rss = _read_vmrss_mb(proc.pid)
            fds = _read_fd_count(proc.pid)
            now_j = _read_jiffies(proc.pid)
            now_t = time.time()
            if rss is None or fds is None or now_j is None:
                missed_samples += 1
                hlog(f"sample t={elapsed:.0f}s: daemon proc metrics unreadable")
                cpu_pct = None
            else:
                dt = now_t - prev_time
                if prev_jiffies is not None and dt > 0:
                    cpu_pct = 100.0 * (now_j - prev_jiffies) / (clk_tck * dt)
                else:
                    cpu_pct = None
                prev_jiffies = now_j
                prev_time = now_t

            vision_pid = _find_vision_child(proc.pid)
            vision_rss = _read_vmrss_mb(vision_pid) if vision_pid else None

            tmp, volts = tail_log_for_telemetry(DAEMON_LOG)
            webcam_ok = probe_webcam()
            pixel_ok = probe_pixel()

            # Update aggregates.
            if rss is not None:
                if start_rss is None and elapsed >= WARMUP_SECS:
                    start_rss = rss
                if rss > peak_rss:
                    peak_rss = rss
                last_rss = rss
            if fds is not None:
                if start_fds is None and elapsed >= WARMUP_SECS:
                    start_fds = fds
                if fds > peak_fds:
                    peak_fds = fds
                last_fds = fds
            if tmp is not None:
                if peak_tmp is None or tmp > peak_tmp:
                    peak_tmp = tmp

            row = ",".join([
                f"{elapsed:.0f}",
                f"{rss:.2f}" if rss is not None else "",
                f"{cpu_pct:.2f}" if cpu_pct is not None else "",
                f"{fds}" if fds is not None else "",
                f"{vision_rss:.2f}" if vision_rss is not None else "",
                f"{tmp}" if tmp is not None else "",
                f"{volts}" if volts is not None else "",
                "1" if webcam_ok else "0",
                "1" if pixel_ok else "0",
            ])
            csv_fh.write(row + "\n")
            samples_taken += 1
            hlog(f"sample t={elapsed:.0f}s  rss={rss}  cpu={cpu_pct}  "
                 f"fds={fds}  vision_rss={vision_rss}  tmp={tmp}  v={volts}  "
                 f"webcam={webcam_ok}  pixel={pixel_ok}")

            # Sleep to the next sample boundary.
            sample_dur = time.time() - sample_t0
            remain = max(args.sample_period - sample_dur, 0.0)
            # Check elapsed + remain vs test_secs; if the next nap would push
            # past, nap just enough to land on the end.
            remaining_in_test = test_secs - (time.time() - t_start)
            nap = min(remain, max(remaining_in_test, 0.0))
            if nap > 0:
                time.sleep(nap)
    except KeyboardInterrupt:
        hlog("interrupted by user — shutting down")

    # ---- shutdown ----
    stop_driver.set()
    daemon_survived = proc.poll() is None

    if daemon_survived:
        try:
            proc.stdin.write("shut down\n")
            proc.stdin.flush()
        except Exception as e:
            hlog(f"failed to send 'shut down' on stdin: {e}")
        try:
            proc.stdin.close()
        except Exception:
            pass
        hlog("sent 'shut down'; waiting up to 30 s for clean exit")
        try:
            proc.wait(timeout=30)
            hlog(f"daemon exited cleanly rc={proc.returncode}")
        except subprocess.TimeoutExpired:
            hlog("daemon did not exit; sending SIGTERM")
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                hlog("still alive; SIGKILL")
                proc.kill()
                proc.wait()
    else:
        hlog(f"daemon already exited rc={proc.returncode} before shutdown")

    csv_fh.close()

    # Copy the daemon log next to the CSV (preserves tmp path for the user).
    try:
        if DAEMON_LOG.exists():
            shutil.copy2(DAEMON_LOG, kept_log_path)
    except Exception as e:
        hlog(f"could not copy daemon log: {e}")

    # ---- analysis ----
    events = scan_log_for_events(kept_log_path if kept_log_path.exists()
                                  else DAEMON_LOG)
    # If the test ended before WARMUP_SECS elapsed, start_rss will be None.
    # Fall back to last_rss so the delta is 0 (no creep possible to judge)
    # rather than n/a.  Noted in the summary.
    baseline_note = ""
    if start_rss is None and last_rss is not None:
        start_rss = last_rss
        baseline_note = f" (test shorter than {WARMUP_SECS:.0f}s warmup; baseline==final)"
    if start_fds is None and last_fds is not None:
        start_fds = last_fds
    rss_delta_mb = (last_rss - start_rss) if (last_rss is not None and start_rss is not None) else None
    rss_growth_pct = ((rss_delta_mb / start_rss) * 100.0
                      if rss_delta_mb is not None and start_rss else None)
    fd_delta = (last_fds - start_fds) if (last_fds is not None and start_fds is not None) else None

    rss_flag = (rss_growth_pct is not None and rss_growth_pct > 20.0)
    fd_flag = (fd_delta is not None and fd_delta > 20)
    error_flag = events["error_total"] > 0
    vision_flag = events["vision_died"] > 0 or events["vision_giving_up"] > 0
    temp_flag = peak_tmp is not None and peak_tmp > 70  # dangerous for the MCU

    overall_pass = daemon_survived and not (rss_flag or error_flag or
                                            vision_flag or temp_flag)

    # Last 50 lines of daemon log for the report if things went south.
    tail50 = []
    log_src = kept_log_path if kept_log_path.exists() else DAEMON_LOG
    try:
        with open(log_src, "r", errors="replace") as fh:
            lines = fh.readlines()
        tail50 = lines[-50:]
    except Exception:
        pass

    def fmt(x, fmt_str="{:.1f}"):
        if x is None:
            return "n/a"
        try:
            return fmt_str.format(x)
        except Exception:
            return str(x)

    summary = []
    summary.append(f"# stress_{date} summary\n")
    summary.append(f"**Verdict:** {'PASS' if overall_pass else 'FAIL'}\n")
    summary.append(f"- Duration: {test_secs:.0f} s ({test_secs/60:.1f} min)")
    summary.append(f"- Samples taken: {samples_taken}  "
                   f"(missed: {missed_samples})")
    summary.append(f"- Daemon survived to shutdown: {daemon_survived}")
    summary.append(f"- Driver inputs sent: {driver.sent}")
    summary.append(f"- Start RSS (MB): {fmt(start_rss)}{baseline_note}")
    summary.append(f"- End RSS (MB):   {fmt(last_rss)}")
    summary.append(f"- Peak RSS (MB):  {fmt(peak_rss)}")
    summary.append(f"- RSS delta (MB): {fmt(rss_delta_mb)} "
                   f"({fmt(rss_growth_pct)}%)  flag={rss_flag}")
    summary.append(f"- Start FDs: {fmt(start_fds, '{}')}  End FDs: "
                   f"{fmt(last_fds, '{}')}  Peak FDs: {fmt(peak_fds, '{}')}  "
                   f"delta={fmt(fd_delta, '{}')}  flag={fd_flag}")
    summary.append(f"- Peak ESP32 temp (C): {fmt(peak_tmp, '{}')}  "
                   f"flag={temp_flag}")
    summary.append(f"- ERROR: lines in log: {events['error_total']}  "
                   f"types={events['error_types']}")
    summary.append(f"- Vision deaths: {events['vision_died']}  "
                   f"max restart #: {events['vision_restart_max']}  "
                   f"gave up: {events['vision_giving_up']}")
    summary.append(f"- CSV: {csv_path}")
    summary.append(f"- Kept daemon log: {kept_log_path}")
    summary.append("")
    if not overall_pass:
        summary.append("## last 50 lines of daemon log\n")
        summary.append("```")
        summary.extend(l.rstrip() for l in tail50)
        summary.append("```\n")
    summary.append("## harness timeline\n")
    summary.append("```")
    summary.extend(harness_lines[-200:])
    summary.append("```")

    summary_path.write_text("\n".join(summary) + "\n")
    hlog(f"wrote summary -> {summary_path}")

    # Print a compact final line so the caller can grep it.
    print(f"RESULT={'PASS' if overall_pass else 'FAIL'} "
          f"start_rss={fmt(start_rss)}MB end_rss={fmt(last_rss)}MB "
          f"rss_delta={fmt(rss_delta_mb)}MB fd_delta={fmt(fd_delta,'{}')} "
          f"errors={events['error_total']} vision_deaths={events['vision_died']} "
          f"peak_tmp={fmt(peak_tmp,'{}')}C csv={csv_path} "
          f"summary={summary_path}")

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
