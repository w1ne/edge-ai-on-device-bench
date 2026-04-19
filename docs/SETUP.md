# First-time setup

Cold-clone path — from zero laptop to a working robot demo. Ubuntu 22.04 / Debian 12 assumed; other distros are similar.

## 1. Clone and poke at it

```bash
git clone git@github.com:w1ne/edge-ai-on-device-bench.git
cd edge-ai-on-device-bench
python3 demo/robot_daemon.py --mode text --dry-run --no-tts
```

If that runs and you can type `neutral`, `lean left`, `shut down` and it prints decisions — Python is fine, the matcher is fine, and you can stop here for a hardware-less smoke test. Otherwise see *Python deps* below.

## 2. Hardware you need

| Device | Why | Cost |
|---|---|---|
| Laptop running Linux (mic + webcam in, USB-A or USB-C out) | Brain today | whatever you have |
| ESP32-S3 Zero board with the PhoneWalker firmware flashed | Motor controller | ~$5 |
| 4× Feetech STS3032 servos + mount | Legs | ~$40 |
| MPU-6050 on I²C | Balance feedback | ~$2 |
| USB-C cable to ESP32 | Wire protocol | have one |
| 2× Li-ion in series (7.2 V, fused) | Power | ~$20 |
| Google Pixel 6 on ADB | On-device STT + LLM | secondhand ~$180 |
| Optional: Huawei P20 Lite for the "it also runs on a $30 phone" line | benchmarks | ~$30 |

## 3. Python deps (laptop)

```bash
sudo apt install -y python3-pip python3-venv arecord alsa-utils espeak-ng \
                    adb android-sdk-platform-tools ffmpeg sox \
                    libusb-1.0-0 usbutils
pip install --user pyusb opencv-python flask requests \
                   openwakeword piper-tts sounddevice \
                   huggingface_hub
```

Optional, for the TFLite Whisper runner + acceleration bench:

```bash
pip install --user tflite-runtime openai-whisper transformers onnx onnx2tf
```

Optional, for the on-device NCNN vision path (not needed for the default webcam source):

```bash
pip install --user ncnn
```

## 4. Udev for the ESP32

pyusb needs non-root read/write on the ESP32-S3's USB CDC device (VID `303a`, PID `1001`). Drop this file:

```bash
sudo tee /etc/udev/rules.d/99-esp32-s3.rules > /dev/null <<'EOF'
SUBSYSTEM=="usb", ATTR{idVendor}=="303a", ATTR{idProduct}=="1001", MODE="0666"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Confirm: `lsusb | grep 303a:1001` shows the Espressif JTAG/serial device.

## 5. ADB + Pixel 6

Enable Developer Mode + USB Debugging on the phone. Plug it in, authorize the laptop when the trust dialog appears.

```bash
adb devices
# 1B291FDF600260       device
```

The daemon's default `--stt-phone pixel6` maps to that serial (hard-coded in `PHONE_SERIALS`). If your phone has a different serial, pass `--stt-phone <serial>`.

Push the on-phone models once (about 2 GB total; they live on `/data/local/tmp/`):

```bash
bash scripts/push_assets.sh
```

## 6. DeepInfra API key (optional but recommended)

Get a key at <https://deepinfra.com>. Stash it somewhere outside this repo:

```bash
mkdir -p ~/Projects/AIHW
cat >> ~/Projects/AIHW/.env.local <<'EOF'
DEEPINFRA_API_KEY=sk-...
EOF
chmod 600 ~/Projects/AIHW/.env.local
```

`scripts/run_robot.sh` sources this file. Without it, the daemon auto-switches to `--llm-api local` (on-phone Gemma 3). Offline is fine — a bit slower + a bit less accurate.

## 7. Piper voice

First Piper call auto-downloads `en_US-lessac-low` (~61 MB) to `~/.cache/piper/`. If the download is slow or you want to preempt:

```bash
mkdir -p ~/.cache/piper
huggingface-cli download rhasspy/piper-voices \
  en/en_US/lessac/low/en_US-lessac-low.onnx \
  en/en_US/lessac/low/en_US-lessac-low.onnx.json \
  --local-dir /tmp/piper-dl
cp /tmp/piper-dl/en/en_US/lessac/low/*.onnx* ~/.cache/piper/
```

## 8. OpenWakeWord models

First `--mode wake` run triggers an auto-download of pre-trained wake models (~10 MB) into the `openwakeword` pip package's cache dir. If offline, run once with internet first.

## 9. First real run

```bash
scripts/run_robot.sh --mode text --no-tts
```

You should see, in order:

```
[run_robot] starting with defaults: ...
robot_daemon up  mode=text  dry_run=False  with_llm=True ...
[self-test] esp32:   PRESENT (VID 303a / PID 1001)
[self-test] adb:     ok (serial=1B291FDF600260)
[self-test] webcam:  present (/dev/video0)
[self-test] llm-key: set (DEEPINFRA_API_KEY)
=== self-test: PASS ===
```

Type `neutral`, `lean left`, `do it again`, `undo`, `shut down`. Servos should physically move each time.

Switch to voice:

```bash
scripts/run_robot.sh                  # push-to-talk
scripts/run_robot.sh --mode wake      # hands-free ("hey jarvis, <command>")
```

Dashboard (in another terminal):

```bash
python3 demo/web_ui.py
# open http://localhost:5555
```

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| `wire: no-device` | Is `lsusb | grep 303a:1001` present? If yes, udev rule not applied — step 4. |
| `USBError: Resource busy` | Another daemon instance holds the interface. `pgrep -f robot_daemon` and kill the stray. |
| IMU reports all zeros | ESP32 brownout on init; power-cycle the robot (unplug + replug the battery). |
| `adb: unreachable` | Phone screen-off or trust-dialog not accepted. Unlock + replug. |
| Vision reports 2 FPS | You're on `--vision-source phone` (adb screencap). Switch to the default `--source webcam`. |
| Piper misses the download | Your network blocked HuggingFace. Do step 7 manually. |
| Wake word false-triggers in noise | Raise `--wake-threshold 0.65` (default 0.5). |

## 11. What NOT to do

- **Don't send `{"c":"walk"}` while the robot is on a table.** It will step forward and fall. Test walking in an open floor.
- **Don't flash the ESP32 from this repo.** The firmware source in `w1ne/PhoneWalker` is NOT what's running on the live device — flashing would regress the demo. See `docs/FIRMWARE_IMU_AUDIT.md`.
- **Don't commit your `DEEPINFRA_API_KEY` literal anywhere in this repo.** Use the `~/Projects/AIHW/.env.local` path. Key rotation happened once already; don't repeat it.

## 12. Pre-commit smoke (optional but recommended)

`scripts/precommit.sh` is a ~2 s sanity check: compileall + state-server auth tests + demo module imports. Run it manually, or wire it into git:

```bash
ln -sf "$(pwd)/scripts/precommit.sh" .git/hooks/pre-commit
```

Exit code 0 means you're clear to commit; non-zero points at which of the three phases failed.

## 13. Where things live

- `demo/robot_daemon.py` — main loop. Read this first.
- `demo/web_ui.py` — dashboard, independent process.
- `demo/robot_behaviors.py` — idle / walking / paused / following state machine.
- `demo/vision_watcher.py` — webcam → YOLO → JSONL event stream.
- `demo/parse_intent*.py` — intent parser variants (regex / local LLM / API).
- `scripts/run_robot.sh` — the launcher.
- `scripts/whisper_tflite_runner.py` — TFLite STT runner.
- `scripts/stress_test.py` — 30-min live reliability test.
- `docs/STATUS.md` — single source of truth for current state.
- `docs/ARCHITECTURE.md` — end-state (phone as brain) + migration path.
- `docs/RELIABILITY_AUDIT_*.md` — static audit findings, triaged.

Good luck.
