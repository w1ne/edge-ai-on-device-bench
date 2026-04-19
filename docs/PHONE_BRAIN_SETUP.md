# Phone-brain setup (Pixel 6)

Status: Phase 1 — Termux installed + Python stack ported. Wire layer stubbed
against a mock TCP server; real ESP32 connectivity waits on the Android BLE
companion app (sibling agent's work, exposes a socket at `127.0.0.1:5557`).

## What's on the phone after Phase 1

| Package | Version | Installed |
|---|---|---|
| com.termux       | 0.118.3  (F-Droid versionCode 1002) | via `adb install` |
| com.termux.api   | 0.53.0   (F-Droid versionCode 1002) | via `adb install` |

## Re-running the install from scratch

```bash
mkdir -p /tmp/termux_apks
cd /tmp/termux_apks
curl -fL -o termux.apk     "https://f-droid.org/repo/com.termux_1002.apk"
curl -fL -o termux-api.apk "https://f-droid.org/repo/com.termux.api_1002.apk"
adb -s 1B291FDF600260 install termux.apk
adb -s 1B291FDF600260 install termux-api.apk
```

Versions above are the current **stable** (non-beta) builds per
`https://f-droid.org/api/v1/packages/com.termux` as of 2026-04-19. Newer
betas exist; stay on 1002/1002 unless there's a specific reason not to.

## Why there's a manual one-time step

The F-Droid Termux APKs are **not** built with `android:debuggable="true"`,
so `adb shell run-as com.termux …` is rejected by the OS. There is no way
to drive `pkg install` from the host without either (a) rooting the phone,
(b) installing an older/debug Termux (deprecated), or (c) the user running
one command inside a Termux session. Option (c) is by far the least painful.

## One-time bootstrap (user does this ONCE)

1. Host (laptop):

   ```bash
   DEEPINFRA_API_KEY="$(cat ~/Projects/AIHW/.env.local | awk -F= '/DEEPINFRA_API_KEY/{print $2}')" \
     bash scripts/push_termux.sh
   ```

   This stages `phone_*.sh/.py`, `mock_wire_server.py`, `termux_bootstrap.sh`,
   and `.dia_key` (mode 0600) under `/sdcard/Download/edge-ai-phone/` on the
   phone. Nothing sensitive is committed.

2. Phone: open the **Termux** app (first launch; accept the storage
   permission dialog if asked). Then paste:

   ```
   bash /sdcard/Download/edge-ai-phone/termux_bootstrap.sh
   ```

   The bootstrap:

   - runs `termux-setup-storage` (accept the dialog)
   - `pkg install -y python curl jq termux-api`
   - copies `phone_*.{py,sh}` + `mock_wire_server.py` into `$HOME`
   - moves `.dia_key` into `$HOME/.dia_key` (0600) and shreds the staging copy

3. Phone: open **Termux:API** once and accept microphone permission. (The
   STT script will fail until this is done. This is a one-time Android
   permission grant; Termux:API's `termux-microphone-record` can't work
   without it.)

## Post-bootstrap smoke

From the host:

```bash
adb -s 1B291FDF600260 shell '/data/data/com.termux/files/usr/bin/python3 --version'
```

Expected: `Python 3.12.x` (or whatever Termux currently ships). If you see
`inaccessible or not found`, the bootstrap didn't run yet.

On the phone, inside Termux:

```bash
# --- intent ---
echo "lean left please" | python3 ~/phone_intent.py
# -> {"c":"pose","n":"lean_left","d":1500}

# --- wire (mock loopback) ---
python3 ~/mock_wire_server.py &
echo '{"c":"noop"}' | python3 ~/phone_wire.py
# -> {"ok":true,"echo":{"c":"noop"},"ts":...}
kill %1

# --- daemon end-to-end ---
echo "bow forward" | python3 ~/phone_daemon.py --mode text --mock-wire --no-tts
```

Expected daemon output (stderr):

```
[phone_daemon HH:MM:SS] scripts dir: /data/data/com.termux/files/home
[phone_daemon HH:MM:SS] wire: spawning mock_wire_server on 127.0.0.1:5557
[phone_daemon HH:MM:SS] transcript: 'bow forward'
[phone_daemon HH:MM:SS] intent: {'c': 'pose', 'n': 'bow_front', 'd': 1800}
[phone_daemon HH:MM:SS] wire ack: {'ok': True, 'echo': {...}, 'ts': ...}
[phone_daemon HH:MM:SS] decision: {'c': 'pose', ...}
```

## What's still blocked without the companion app

- **Real wire to ESP32.** `phone_wire.py` talks to `127.0.0.1:5557`. The
  sibling agent is building the Android BLE companion app that will expose
  that socket. Until it ships, `--mock-wire` is the only way to see the
  daemon drive wire commands. **Does not block** any other layer — intent,
  STT, TTS, and the daemon loop all work independently.
- **Camera / vision.** Deferred to Phase 2. Nothing in Phase 1 needs it.
- **Web UI / state server.** Phone has no good Flask story inside Termux
  (no reverse proxy, mic permissions hold the audio layer). Laptop still
  serves `demo/web_ui.py` if you want a dashboard.
- **Wake word.** Same reason — deferred until Phase 2.

## What's still manual for the user

1. Open Termux once and run `bash /sdcard/Download/edge-ai-phone/termux_bootstrap.sh`.
2. Open Termux:API once to grant microphone permission.

Everything else (host-side push, keys, ack smoke) is scripted.

## File layout on the phone

| On-phone path | Source | Purpose |
|---|---|---|
| `/data/local/tmp/phone_intent.{sh,py}` | `scripts/termux/` | legacy adb-shell smoke path |
| `/sdcard/Download/edge-ai-phone/*` | `scripts/termux/` | bootstrap staging |
| `$HOME/phone_intent.py` | bootstrap copies from staging | intent parser |
| `$HOME/phone_stt.sh` | bootstrap | record + whisper-cli |
| `$HOME/phone_tts.sh` | bootstrap | piper → termux-tts-speak → espeak-ng |
| `$HOME/phone_wire.py` | bootstrap | TCP client to 127.0.0.1:5557 |
| `$HOME/phone_daemon.py` | bootstrap | one_turn loop |
| `$HOME/mock_wire_server.py` | bootstrap | dev mock for wire |
| `$HOME/.dia_key` (0600) | bootstrap | DeepInfra API key |
