# ARCHITECTURE — phone is the brain, ESP32 is the body

**Mental model.** The Pixel 6 is the compute/sensor package (mic, camera,
CPU+Edge-TPU+GPU, battery, screen). The ESP32-S3 is the motor controller
(servos, IMU, battery monitor). The laptop is a dev workstation, not a runtime
dependency. Today most code runs on the laptop — that's transitional.

---

## 1. Current state (laptop-hosted, ~2026-04-19)

```
                        +-------------------------- LAPTOP (runtime host) --------------------------+
USB mic                 |                                                                           |
  |                     |  demo/wake_listener.py   --(arecord 16k mono, 80ms frames)->             |
  |  analog audio       |    OpenWakeWord "hey jarvis" (ONNX CPU)                                   |
  v                     |                                                                           |
 arecord  -(stdin)->    |  demo/robot_daemon.py                                                     |
                        |    | 1. writes 3s WAV                                                     |
                        |    | 2. adb push  wav  ------------------------------+                    |
                        |    | 3. adb shell whisper-cli <wav>  <--------+       \                   |
                        |    | 4. regex-parse stdout, strip banners     |        v                  |
                        |    | 5. parse_intent_api.py  --https-->  DeepInfra Llama-3.1-8B           |
                        |    |    (OR adb shell llama-cli <-- local fallback)                       |
                        |    | 6. map intent -> wire JSON                                           |
                        |    | 7. pyusb write  --USB CDC-->  ESP32   (VID 303a / PID 1001)          |
                        |    | 8. Piper TTS (~/.cache/piper/en_US-lessac-low.onnx) -> aplay         |
                        |                                                                           |
USB webcam /dev/video0  |  demo/vision_watcher.py                                                   |
  |                     |    cv2.VideoCapture(0) -> YOLO-Fastest v2 (ncnn CPU) -> JSONL -> daemon   |
  v                     |                                                                           |
  cv2  ---------------->|                                                                           |
                        |  demo/web_ui.py (Flask :5555, direct pyusb STOP)                          |
                        +---------------------------------------------------------------------------+
                                          |  adb-usb                           |  USB CDC 303a:1001
                                          v                                    v
                 +----------- PIXEL 6 (as coprocessor) -----------+     +------- ESP32-S3 -------+
                 |  whisper-cli  (tiny/base, ggml)                |     |  firmware (no source)   |
                 |  llama-cli / llama-server (TinyLlama/Gemma)    |     |  wire protocol {c:...}  |
                 |  SmolVLM, YOLO/benchncnn on /data/local/tmp    |     |  MPU-6050 @ 10 Hz       |
                 |  Edge TPU (Tensor G1) for int8 classifiers     |     |  4x STS3032 servos      |
                 +------------------------------------------------+     +-------------------------+
```

**Hops for one "walk forward" voice command (count the latency tax):**
1. mic → `arecord` (laptop)
2. `arecord` → WAV on laptop disk
3. `adb push` WAV laptop → phone
4. `adb shell whisper-cli` (phone CPU)
5. stdout → adb → laptop regex
6. laptop → DeepInfra over HTTPS (or `adb shell llama-cli` — another round trip)
7. laptop maps intent → wire JSON
8. pyusb → USB CDC → ESP32
9. ESP32 state packet → pyusb read → laptop
10. laptop → Piper synth → `aplay` to laptop speakers

**Ten hops, three machines, two USB cables.** Every hop is a failure point.

---

## 2. End-state: phone as brain

```
                       +------------------- PIXEL 6 (runtime host, battery-powered) -----------------+
onboard mic  --------->|  mic_capture (Android MediaRecorder  OR  Termux termux-microphone-record)   |
                       |    -> whisper.cpp / whisper-cli  (already on /data/local/tmp)                |
                       |    -> intent parser (parse_intent.py, keyword-first, DeepInfra/local fallback)|
                       |    -> BehaviorEngine tick (state: idle/walking/paused/following)             |
                       |    -> wire JSON                                                              |
Camera2 (onboard)  OR  |  vision loop: frame -> YOLO-Fastest v2 (ncnn CPU) OR MobileNet int8 (EdgeTPU)|
UVC cam via USB-C hub->|    -> detections -> BehaviorEngine                                           |
                       |  TTS: Piper (AArch64 build) OR Android TextToSpeech                         |
                       |                                                                              |
                       |  --USB-C OTG--> USB serial (303a:1001)  via Android UsbManager / usbserial   |
                       +-----------------------------------+------------------------------------------+
                                                           | USB CDC
                                                           v
                                                   +----- ESP32-S3 -----+
                                                   |  same firmware     |
                                                   |  {c:ping|pose|...} |
                                                   |  MPU-6050, servos  |
                                                   +--------------------+

              (optional, dev only)
   LAPTOP  <--- adb reverse / Wi-Fi  ---  dashboard, log tail, log capture, model push
```

**Hops for the same "walk forward":** mic → Whisper → parser → BehaviorEngine →
USB-serial → ESP32. **Five in-process hops, one machine, one USB cable.** No
`adb push`, no laptop, no WAV round trips.

---

## 3. Migration path (fastest win first)

| # | Piece | On phone today? | How to move | Concrete blocker |
|---|---|---|---|---|
| 1 | **STT (whisper-cli)** | YES — runs under adb | Invoke locally via Termux instead of adb shell. `pkg install termux-api`, call `whisper-cli` directly from a Python script inside Termux. | None — binary already on `/data/local/tmp`, just needs a PATH entry. |
| 2 | **Intent parsing (regex + DeepInfra)** | NO — runs on laptop | Copy `demo/parse_intent.py` + `demo/parse_intent_api.py` into Termux. `pkg install python`, `pip install requests`. Read `$DEEPINFRA_API_KEY` from Termux env. | Keyword path is pure Python stdlib; API path needs HTTPS (Termux has it). No blocker. |
| 3 | **Wire I/O (pyusb -> ESP32)** | NO — laptop pyusb | **Hardest step.** Two options: (a) Termux + pyusb — **does not work out of the box**: Android denies direct libusb access to non-rooted apps, and the Espressif CDC (303a:1001) needs explicit `UsbManager` permission grant via an Android Intent. (b) Thin Kotlin/Java app that owns `UsbManager` + `UsbSerial` (mik3y/usb-serial-for-android), exposes a local TCP socket or Unix socket on `127.0.0.1:7070`, and Termux Python talks to that socket. | **Android sees 303a:1001 as a generic CDC device — yes, verified in `logs/web_ui_smoke_2026-04-19.log` line 53 on laptop lsusb; Android `UsbManager.getDeviceList()` will surface it, but pyusb in Termux will `Permission denied` without root or a companion app.** Plan: write the ~200-line Kotlin bridge app. |
| 4 | **Wake word (OpenWakeWord)** | NO — laptop | `pip install openwakeword onnxruntime` in Termux. Feed it `termux-microphone-record -l 0 -b 16000 -c 1` output. | Termux mic access requires granting the Termux:API microphone permission once. Works. |
| 5 | **TTS (Piper)** | NO — uses `~/.cache/piper/` on laptop | Piper has an AArch64 build. Either (a) run `piper` binary in Termux, push the `en_US-lessac-low.onnx` + `.json` to `$HOME/.cache/piper/` in Termux; or (b) fall back to Android `TextToSpeech` via a tiny Kotlin wrapper that the daemon shells out to. | (a) is lower effort; (b) sounds better on Pixel 6 default voices. |
| 6 | **Vision (cv2.VideoCapture on /dev/video0)** | Partial — `--vision-source phone` uses `adb screencap` at 2 FPS | Two real options: **(i) UVC webcam via USB-C hub** — Android 10+ supports UVC with apps like `UVC Camera`, but `cv2.VideoCapture` in Termux will not open it (no V4L2 layer on Android). Need a Kotlin camera service that grabs UVC frames → shared memory / socket → Termux Python. **(ii) Camera2 via companion app** — same Kotlin service, uses the onboard Pixel 6 rear camera, posts JPEG frames to `127.0.0.1:8080`. Termux `vision_watcher` polls that URL. | No native `cv2.CAP_V4L2` on Android. Camera2 is the realistic path; UVC adds a USB mux problem (hub has to share bandwidth with ESP32 CDC). |
| 7 | **BehaviorEngine (pure Python)** | NO — laptop | Copy `demo/robot_behaviors.py` to Termux. Zero native deps. | None. |
| 8 | **Web UI (Flask)** | NO — laptop | Keep on laptop. Point at phone's IP. Phone exposes `/state` and `/stop` over Wi-Fi. | None. |

**Suggested migration order:**
1. **Week 1** — steps 1, 2, 7 in Termux. Phone now does STT + intent + behavior, still shells out over adb for wire I/O. **Kills the `adb push WAV` hop immediately.**
2. **Week 2** — step 3 (Kotlin USB bridge app). Phone now drives ESP32 directly. Laptop no longer needed at runtime.
3. **Week 3** — step 4 (wake word in Termux), step 5 (Piper in Termux or Android TTS). Hands-free autonomous operation.
4. **Week 4** — step 6 (Camera2 companion app). Full end-state.

---

## 4. What stays on the laptop forever

- **Dev loop.** `git`, editor, `cargo`, `tsc`, model cross-compilation. The phone is not a dev box.
- **Web dashboard** (`demo/web_ui.py`). Better on a 27" screen than a 6" one.
- **Log inspection.** `logs/*.log` rotated at 10 MB; grepping 10 MB on a phone is awful.
- **Data capture for training.** Recording mic + camera + IMU traces to produce new YOLO/wake-word datasets. Laptop has the disk and the ffmpeg pipeline.
- **One-shot benchmarks.** `scripts/run_full_suite.sh`, `scripts/benchmark_moonshine.py`, `scripts/tflite_bench.sh` — these are dev tooling, not robot runtime.
- **Model pushing.** `scripts/push_assets.sh` stays laptop-side.

**The goal is a phone-only _robot_, not a phone-only _dev environment_.**

---

## Migration warts ("this assumes laptop" coupling)

Found by `grep` across `demo/` and `scripts/`:

- **`arecord`** — hardcoded in `demo/robot_daemon.py:162` and `demo/wake_listener.py:91,142`. ALSA-only. No ALSA on Android; needs `termux-microphone-record` or Android AudioRecord.
- **`cv2.VideoCapture(args.webcam_index, cv2.CAP_V4L2)`** — `demo/vision_watcher.py:158,161,189,191`. V4L2 does not exist on Android. Also hardcoded `/dev/video0` in `demo/robot_daemon.py:1073` and the help text at `:1158`.
- **Piper voice files at `~/.cache/piper/`** — `demo/robot_daemon.py:182,1173`. Path is `os.path.expanduser`, so it _will_ resolve under Termux, but the `.onnx` + `.json` assets have to be pushed there manually. Not blocking, just a setup step.
- **`adb push` / `adb shell`** — `demo/robot_daemon.py:162-320`, `demo/voice_to_pose.py:56`, `demo/eyes.py:138`, `demo/pipeline_demo.py:5`. Every one of these becomes a local subprocess or local socket call once the code runs _on_ the phone.
- **`pyusb` to 303a:1001** — `demo/robot_daemon.py:421,1028`, `demo/simple_pose.py:45`, `demo/voice_to_pose.py:83`, `demo/pipeline_demo.py:57`, `demo/web_ui.py:41`. Will not work from Termux without the Kotlin `UsbManager` bridge. This is the single biggest block to a laptop-free runtime.
- **Log paths (`logs/...`)** — relative to repo root. Fine in Termux if we `cd` to a phone-local clone, but `--log PATH` should probably default to `$HOME` on Android.
- **`$DEEPINFRA_API_KEY` sourced from `~/Projects/AIHW/.env.local`** — `scripts/run_robot.sh`. Termux has no such dir. Needs a phone-local `.env` convention.
- **Web UI binds `0.0.0.0:5555`** (`demo/web_ui.py`) — fine, but the laptop currently assumes `localhost`. When moved, we point browsers at `phone_ip:5555`.

No blockers without a known fix. The pyusb-on-Termux gap is the only one that requires real new code (the Kotlin bridge).
