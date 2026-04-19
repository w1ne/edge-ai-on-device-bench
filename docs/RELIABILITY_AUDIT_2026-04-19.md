# Reliability audit — robot daemon codebase

Date: 2026-04-19
Scope: `demo/{robot_daemon,vision_watcher,robot_behaviors,wake_listener,parse_intent,parse_intent_fast,parse_intent_api,eyes,web_ui}.py`, `scripts/{run_robot.sh,start_llm_server.sh,whisper_tflite_runner.py}`.
Method: static read. Read-only; no patches applied.

Findings are ranked by impact: crash / data-loss > silent degradation > defensive nits. Line numbers refer to the files as read today.

---

## 1. Critical (would cause crash or data loss in normal use)

### C1. Battery TTS runs inside the USB lock — can wedge emergency stop for up to 15 s

`demo/robot_daemon.py:487-506`, hook installed `demo/robot_daemon.py:1220-1225`, hook body `demo/robot_daemon.py:980-1007`.

`send_wire()` calls `_TELEMETRY_HOOK(text)` on line 503 **while still holding `_USB_LOCK`**. The hook is `make_battery_watcher()`'s `watch()`, which on every low-voltage detection calls `speak_fn("battery low")`. `speak_fn` wraps `speak()`, which synchronously:
1. Loads / runs Piper ONNX synthesis (100 ms – 1 s),
2. Blocks on `subprocess.run(["aplay", ...])` with `timeout=15` (demo/robot_daemon.py:254).

While this runs, no other thread can acquire `_USB_LOCK`. The behavior-tick thread, the vision-driven engine STOP, and even a voice "stop" that flows through `one_turn` → `send_wire` will all block for up to 15 seconds on what is *supposed* to be the emergency path. Worst case: user shouts "stop", robot keeps walking because the TTS aplay from "battery low" is still playing.

Proposed fix (sketch; do NOT apply yet — wants human triage):

```diff
@@ demo/robot_daemon.py send_wire, near line 497
-        hook = _TELEMETRY_HOOK
-        if hook is not None:
-            try:
-                hook(text)
-            except Exception:
-                pass
         return ack, pos
+    # Run the telemetry hook OUTSIDE the USB lock so slow side-effects
+    # (TTS "battery low") can never starve emergency stop.
+    hook = _TELEMETRY_HOOK
+    if hook is not None:
+        try:
+            hook(text)
+        except Exception:
+            pass
+    return ack, pos  # (restructured: stash ack/pos/text before exiting `with`)
```

A cleaner fix is to queue the telemetry text to a small background thread that owns the announce logic; that also de-couples speak() from every send_wire call.

---

### C2. `send_wire` drain loop is unbounded — a chatty ESP32 causes a livelock

`demo/robot_daemon.py:464-466`:

```python
try:
    while True: dev.read(0x81, 4096, timeout=60)
except Exception: pass
```

Purpose: drain stale telemetry before writing. But there is no *time* bound — only a 60 ms per-read timeout. If the ESP32 is emitting telemetry continuously (e.g. 1 kHz IMU spam due to firmware misconfig, or a runaway debug loop), the loop never raises a timeout Exception because every `dev.read` returns data. The result: `send_wire` blocks forever, the `_USB_LOCK` is held forever, the daemon appears completely unresponsive, and Ctrl-C (which the main thread can catch) is no help because the *lock* is held by the thread doing the draining.

Proposed fix:

```diff
-        # drain any stale telemetry first
-        try:
-            while True: dev.read(0x81, 4096, timeout=60)
-        except Exception: pass
+        # drain any stale telemetry first — hard-capped so a chatty ESP32
+        # cannot livelock us here.
+        drain_end = time.time() + 0.2
+        while time.time() < drain_end:
+            try: dev.read(0x81, 4096, timeout=30)
+            except Exception: break
```

---

### C3. `speak()` is not thread-safe — two threads into Piper / aplay at once corrupts audio & may crash the ONNX session

`demo/robot_daemon.py:269-307` and its callers.

`speak()` is invoked from at minimum:
- main thread (`one_turn`, ack after each voice turn),
- battery hook (currently inside send_wire, see C1),
- the behavior engine via `_speak_fn` — which is called from `drain_vision_events` (main thread, OK) AND from `tick()` which runs on the `tick_thread` (demo/robot_daemon.py:1248-1253, calling `behavior_tick_loop` → `engine.tick()` → inside tick logic `self._speak("resuming")` at `demo/robot_behaviors.py:180`).

`_PIPER_LOCK` only protects the *load* path; `_piper_synth_and_play` calls `_PIPER_VOICE.synthesize_wav` with no lock. PiperVoice holds an ONNXRuntime session; concurrent `Run()` on the same session is documented as thread-safe in ORT, but the `wave.open(buf, "wb")` buffer is shared state that *is not*. Two overlapping synth calls will interleave PCM frames and aplay may also contend on ALSA.

Also: the `_piper_probe` check-and-set on `_PIPER_PROBED` / `_PIPER_AVAILABLE` is protected, but the guard at line 228 `if _PIPER_VOICE is None: return False` races with a second thread that just set `_PIPER_VOICE = None` in the except on line 215.

Proposed fix: promote `_PIPER_LOCK` to cover the whole synth+play window, or add a dedicated `_SPEAK_LOCK`:

```diff
+_SPEAK_LOCK = threading.Lock()
@@ def speak(...)
-    if mode == "piper":
-        if _piper_probe():
-            spoke = _piper_synth_and_play(text)
+    with _SPEAK_LOCK:
+        if mode == "piper":
+            if _piper_probe():
+                spoke = _piper_synth_and_play(text)
+        if not spoke:
+            _espeak_speak(text)
-
-    if not spoke:
-        _espeak_speak(text)
```

(Serializing TTS is fine — humans don't want overlapping utterances anyway.)

---

### C4. `dev.write` retry mutates `_USB_DEV` without `global` — silent scope bug

`demo/robot_daemon.py:453-485`.

Line 454 declares `global _USB_DEV`. But inside the `except` branch at line 471 the handoff is `_drop_usb()` (which itself correctly uses `global`), then `_USB_DEV = _open_usb()` (line 474) — this assignment **does** use the outer global because of the declaration at line 454. That part is correct.

However, the flow has a subtler bug: after line 474 sets `_USB_DEV` from `_open_usb()`, line 477 re-assigns `dev = _USB_DEV`, then line 479 tries `dev.write` inside another `try`. If *that* fails (line 480), line 484 calls `_drop_usb()` which nulls `_USB_DEV`. Good. But the surrounding `while True: dev.read(...)` drain loop at 464-466 ran against the *original* `dev` reference before the retry — if the first write threw because the device was unplugged mid-drain, the drain loop silently swallowed the USBError (line 466: `except Exception: pass`), and the stale `buf` content at 487 still reflects the old device. The read phase at 487-490 then hammers the NEW device with `dev.read`, but `dev` was reassigned — wait, let me re-verify.

Actually the subtlety: after a successful retry-write, the code falls through to `buf, end = bytearray(), ...` at line 487 and reads from `dev` which IS the new device. That's fine. The real issue: **there is no retry for the initial drain failure**. If the first `dev.read` drain returns a USBError indicating disconnect, the drain silently aborts, then the write at 470 fires into a stale handle. The write's own except does reopen, but then the reopened device's READ phase (487) begins and we may get *more* stale-from-reopen-buffer telemetry attributed to this command. Minor data-integrity risk for the battery watcher.

Proposed fix: handle drain-time disconnects explicitly.

```diff
-        try:
-            while True: dev.read(0x81, 4096, timeout=60)
-        except Exception: pass
+        drain_end = time.time() + 0.2
+        drain_broke = False
+        while time.time() < drain_end:
+            try: dev.read(0x81, 4096, timeout=30)
+            except usb.core.USBError as e:  # type: ignore[attr-defined]
+                # "No such device" → reopen before we write.
+                if getattr(e, "errno", None) in (19, 5):  # ENODEV, EIO
+                    _drop_usb()
+                    _USB_DEV = _open_usb()
+                    if _USB_DEV is None:
+                        return "no-device", None
+                    dev = _USB_DEV
+                drain_broke = True
+                break
+            except Exception:
+                break
```

---

### C5. `_phone_transcribe_whisper_cli` adb push has no timeout — phone death hangs the daemon forever

`demo/robot_daemon.py:322-323`:

```python
subprocess.run(["adb", "-s", serial, "push", wav, "/data/local/tmp/cmd.wav"],
               capture_output=True, check=True)
```

No `timeout=`. If the phone's USB link is hung (ADB daemon alive but device unresponsive — a routine occurrence on Pixel 6 after screen-off) this call blocks indefinitely. The enclosing `phone_transcribe` is synchronous from the main loop; Ctrl-C will interrupt, but any caller waiting on `wav = listen_for_wake_then_record(...)` → `phone_transcribe(...)` appears as a frozen daemon.

Same issue at `demo/parse_intent.py:199-202` (`adb shell 'cat > schema.json'` has a 15 s timeout — OK) and at `demo/parse_intent.py:238-241` (the `killall llama-cli` fallback has 10 s — OK). But `demo/eyes.py:117-122` (`adb exec-out screencap`) also has NO timeout, so any code path that calls `eyes.adb_screencap(...)` can hang indefinitely.

Proposed fix:

```diff
-    subprocess.run(["adb", "-s", serial, "push", wav, "/data/local/tmp/cmd.wav"],
-                   capture_output=True, check=True)
+    subprocess.run(["adb", "-s", serial, "push", wav, "/data/local/tmp/cmd.wav"],
+                   capture_output=True, check=True, timeout=15)
```

And the same `timeout=10` (or larger) on `demo/eyes.py:117`.

---

## 2. Important (degrades reliability, silently loses events or logs)

### I1. `[vision] EVENT` logger format-spec crashes if `conf` missing

`demo/robot_daemon.py:647-649`:

```python
logger(f"[vision] EVENT {msg.get('class')} "
       f"conf={msg.get('conf'):.2f} streak={msg.get('streak')}")
```

If `vision_watcher.py` ever sends an `event` line without a `conf` field (e.g. a schema bump or a partial JSON write due to line truncation), `.2f` applied to `None` raises `TypeError`, which is caught by the outer `try:/except:` block that only handles `json.JSONDecodeError`. Wait — actually looking at line 636-639: the JSON parse is wrapped in `except json.JSONDecodeError`. The logger line itself is NOT wrapped, so a TypeError propagates up and exits `_vision_run_once`, which is caught by the outer supervisor at lines 700-719 — so vision respawns. But the net effect is a silent vision-restart loop with no obvious root cause in the log.

Proposed fix:

```diff
-            elif t == "event":
-                logger(f"[vision] EVENT {msg.get('class')} "
-                       f"conf={msg.get('conf'):.2f} streak={msg.get('streak')}")
+            elif t == "event":
+                conf = msg.get("conf")
+                conf_s = f"{conf:.2f}" if isinstance(conf, (int, float)) else "?"
+                logger(f"[vision] EVENT {msg.get('class')} "
+                       f"conf={conf_s} streak={msg.get('streak')}")
```

---

### I2. Web UI's `RE_WIRE_ACK` silently drops every log line where `wire_ack` is `None`

`demo/web_ui.py:69`: `RE_WIRE_ACK = re.compile(r"wire:\s+(\{.*?\})\s*servos:\s*\[([^\]]+)\]")`.

Daemon emits (robot_daemon.py:896): `wire:   {wire_ack}   servos: {pos}` — but `wire_ack` is often `None` (no ack received from ESP32 within 1.3 s) and `pos` is often `None`. The f-string renders these as literal `None`, which doesn't match `{...}` or `[...]`. Consequence: the web UI "last wire ack" panel and servo display **never update** on any command that fails to ack — which is precisely the state the user most wants to see. The same issue exists for `dry-run` and `no-device` strings.

Proposed fix (two-part):
1. In daemon, log structured values even when ack is missing: `wire: no-ack   servos: none`.
2. Broaden the regex: `r"wire:\s+(\S.*?)\s{2,}servos:\s*(.+)$"`, and have the UI display whatever strings come back.

---

### I3. Vision subprocess stderr is never drained during normal operation — a chatty watcher blocks at the PIPE buffer limit

`demo/robot_daemon.py:621-669`.

`_vision_run_once` spawns the watcher with `stderr=subprocess.PIPE`. The main `while` loop only reads from `stdout`; `stderr` is read only once, as a bounded tail, *after* `terminate()` at line 662-664. Linux default pipe buffer is 64 KiB. If the watcher spews warnings (e.g. `WARN: webcam read failed ...` from vision_watcher.py:231 fires on every frame for a disconnected webcam), stderr fills in ~30 seconds; the watcher's next `print(..., file=sys.stderr)` blocks; the watcher is now stuck and stops emitting JSON on stdout too; our `proc.stdout.readline()` returns empty; we see a "subprocess ended" false-positive and restart. Not catastrophic (supervisor handles it) but masks the real problem and chews CPU on restart loops.

Proposed fix: drain stderr on a side thread, log-throttled.

```diff
+    def _drain_stderr():
+        # Log-throttled stderr pump so the child's PIPE never fills.
+        last_log = 0.0
+        while proc.poll() is None:
+            line = proc.stderr.readline()
+            if not line: break
+            if time.time() - last_log > 5.0:
+                logger(f"[vision-stderr] {line.rstrip()}")
+                last_log = time.time()
+    threading.Thread(target=_drain_stderr, daemon=True).start()
```

---

### I4. `llm_fallback.last_error` attribute pattern is not thread-safe

`demo/robot_daemon.py:544-605`.

`llm_fallback` stores its last error on the function object itself (`llm_fallback.last_error = ...`). Callers read this attribute right after the call. This is fine for the main thread, but `llm_fallback` is *only* called from `one_turn` today, so it's a latent bug. If anyone ever calls it from the behavior tick thread (or from a future async path), two concurrent invocations will overwrite each other's last_error and callers will see the wrong reason.

Proposed fix: return `(cmd, err_reason)` from `llm_fallback`, drop the attribute.

---

### I5. `open()` without `with` in `make_logger` — file handle leaks on daemon restart inside same process

`demo/robot_daemon.py:759`: `fh = open(path, "a", buffering=1) if path else None`.

No matching `.close()` anywhere. The process exits on shutdown so the OS cleans up — but if `main()` is ever invoked twice in the same interpreter (tests, Flask dev reloader equivalent, future REPL), each call leaks a handle. More importantly, **log rotation** (`_maybe_rotate_log`) happens at *open time*: once the daemon is running, the log file will never rotate no matter how large it grows. A 48-hour session easily exceeds the 10 MB threshold.

Proposed fix: rotate on a size check at write time, or size-check every N log lines and re-open if needed.

```diff
 def make_logger(path: str | None):
     if path:
         _maybe_rotate_log(path)
-    fh = open(path, "a", buffering=1) if path else None
+    fh_box = {"fh": open(path, "a", buffering=1) if path else None,
+              "path": path, "written": 0}
     def log(line: str):
         stamp = datetime.datetime.now().strftime("%H:%M:%S")
         text = f"{stamp} {line}"
         print(text)
-        if fh:
-            fh.write(text + "\n")
+        fh = fh_box["fh"]
+        if fh:
+            fh.write(text + "\n")
+            fh_box["written"] += len(text) + 1
+            if fh_box["written"] > LOG_ROTATE_BYTES:
+                fh.close()
+                _maybe_rotate_log(fh_box["path"])
+                fh_box["fh"] = open(fh_box["path"], "a", buffering=1)
+                fh_box["written"] = 0
     return log
```

---

## 3. Nits (cosmetic, hardening, defensive)

- `demo/robot_daemon.py:312` + `demo/vision_watcher.py:50`: `PHONE_SERIALS` hard-coded in two places. Diverge silently if a phone is re-flashed. Factor into a single module.
- `demo/eyes.py:57`: `PHONE_SERIAL = "9WV4C18C11005454"` — third copy. Same divergence risk.
- `demo/robot_daemon.py:1260-1265`: catch-all `except Exception` in the main loop logs and continues. Fine in prod, but masks programming errors during dev. Consider `args.debug` flag to re-raise.
- `demo/robot_daemon.py:1049-1050`: `stdin=subprocess.DEVNULL` is correctly applied to the self-test adb probe, but NOT to the other adb calls (`_phone_transcribe_whisper_cli` push+shell, `parse_intent.py`'s adb shell). In `--mode text` with piped stdin, these can still drain the parent's stdin.
- `demo/wake_listener.py:201-204`: `proc.terminate(); proc.wait(timeout=1); ... proc.kill()` — but if `proc.wait(timeout=1)` raises `TimeoutExpired`, the `except Exception` swallows it and only the nested `proc.kill()` is tried — without a final `proc.wait()` after `.kill()`, the arecord can linger as a zombie until GC.
- `demo/vision_watcher.py:306-311`: `eyes.infer()` exceptions are caught with `print WARN`, but the `sleep(args.interval)` is in the *except* only — on a persistent inference crash the outer loop spins logging WARN every 0.1 s (at webcam cadence). Add a consecutive-failure counter parallel to `webcam_fail_count`.
- `demo/parse_intent.py:229-231`: `subprocess.run(["adb", "shell", shell_cmd], ..., timeout=120)`. 120 s is long, but the TimeoutExpired handler at 236-242 then issues another `adb shell killall llama-cli` synchronously with timeout=10 — if *that* also hangs we're stuck. Consider wrapping in a thread with a hard deadline.
- `demo/parse_intent_api.py:236-252`: `urllib.request.urlopen(req, timeout=timeout)` is correct, but the `RETRY_ON_5XX = 1` constant means a 502 takes `2 * TIMEOUT_S = 20 s` to fall through. Subprocess wrapper in `robot_daemon.py:572` only allots 25 s. If DeepInfra hiccups twice, `subprocess.TimeoutExpired` fires before the retry logic completes. Either lower `TIMEOUT_S` or raise the subprocess timeout.
- `demo/robot_daemon.py:464-466` (drain) *and* `demo/robot_daemon.py:487-490` (read) both catch `except Exception: pass`. At minimum distinguish `USBError` (expected: timeout, NAK) from `AttributeError`/`TypeError` (bug).
- `demo/web_ui.py:147-167` `fire_stop`: no `usb.util.release_interface` call between claim and dispose. On the daemon's subsequent send_wire, the persistent handle may conflict with a still-claimed-by-web_ui device entry in libusb's state. Not usually observable, but worth a `release_interface(dev, 1)` before `dispose_resources`.
- `demo/robot_daemon.py:904`: `elif wire_cmd.get("c") in ("stop", "pose"):` clears walking flag, but an LLM can emit `{"c":"walk", ..., "on": False}` (schema allows on=True only, but a malformed reply slipping through canonicalize would break this; canonicalize forces on=True so OK — but note this invariant for future refactors).
- `scripts/start_llm_server.sh:116`: `adb -s "$SERIAL" shell "tail -f ..."` is the *foreground* process for the launcher. When the laptop USB disconnects, tail exits, trap fires, server is killed. Good. But if the phone-side log file never appears, `tail -f` retries-forever behavior depends on toybox/busybox `tail` flavor; on some builds the launcher silently stalls without `READY`. Poll the log file existence before tailing.
- `demo/robot_daemon.py:1266-1275` shutdown path: `stop_evt.set()` then `eye_thread.join(timeout=3)`. If the eye thread is blocked inside `proc.stdout.readline()` because the vision subprocess has frozen (e.g. kernel lockup), join times out silently and the daemon exits without actually killing the subprocess — which becomes an orphan. Add a final `os.killpg(os.getpgid(proc.pid), signal.SIGKILL)` fallback, or `Popen(..., start_new_session=True)` + kill the process group.

---

## Out-of-scope follow-ups

- No tests exercise `send_wire`'s retry path or the battery watcher under lock contention. Would catch C1, C3 quickly.
- `robot_behaviors.py` has a smoke test (`_smoke_test`) — add equivalent for the supervisor loop (clean exit vs crash path in `vision_loop`) and for the log format the web UI parses.
