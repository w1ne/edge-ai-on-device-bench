# PHONE_BRAIN_BLOCKED

> **SUPERSEDED 2026-04-19 evening.** Termux + Termux:API are now installed
> on the Pixel 6, the Python stack is ported, and the wire layer talks to
> a mock TCP server. See `docs/PHONE_BRAIN_SETUP.md` for the current
> instructions and Phase-1 file layout. This file kept for history.

Status as of 2026-04-19 (morning): phone-as-brain port was blocked on **one
package install**.

## Probe result

```
$ adb shell pm list packages | grep -i termux
(no output)
```

Device `1B291FDF600260` (Pixel 6, Android 15, arm64-v8a) has **no Termux
installed**. Termux is the one and only supported userland on unrooted Android
for running `python`, `curl`, `jq`, etc. without a custom app. Without it the
intent-parse wrapper has nothing to run.

Android-shell-only fallback won't work either: `adb shell` exposes toybox
(which has `nc`, `base64`, `wget`) but no `curl`, no `jq`, no TLS-capable HTTP
client. DeepInfra requires HTTPS. Hard stop.

## Unblock (one person, ~10 min)

1. Install Termux from **F-Droid**, not the Play Store. The Play Store build
   is outdated and the auth/permission dance differs.
   - <https://f-droid.org/packages/com.termux/>
   - Mirror: <https://github.com/termux/termux-app/releases> (grab the
     `termux-app_v0.118.*_arm64-v8a.apk`)
   - `adb install termux-app_v0.118.*_arm64-v8a.apk`

2. Also install **Termux:API** (companion APK, separate install — needed later
   for mic / USB intents):
   - <https://f-droid.org/packages/com.termux.api/>
   - `adb install termux-api_v0.50.*.apk`

3. First launch Termux on the phone, then:
   ```
   pkg update && pkg upgrade -y
   pkg install -y curl jq python termux-api
   ```

4. Drop the DeepInfra key in a phone-local dotfile:
   ```
   adb push /home/andrii/Projects/AIHW/.env.local /data/local/tmp/.dia_key.tmp
   adb shell 'run-as com.termux sh -c "cat /data/local/tmp/.dia_key.tmp > \$HOME/.dia_key && chmod 600 \$HOME/.dia_key"'
   ```
   (If `run-as` is denied — Play Store builds block it — instead open Termux
   on-device and paste the key into `~/.dia_key` manually, then
   `chmod 600 ~/.dia_key`.)

5. Verify end-to-end from the laptop:
   ```
   bash scripts/push_termux.sh
   ```
   That script pushes `phone_intent.sh` / `.py` to `/data/local/tmp/` and runs
   a smoke transcript through them.

## What ships right now (pre-unblock)

- `scripts/termux/phone_intent.sh` — curl+jq wrapper, zero Python deps.
- `scripts/termux/phone_intent.py` — Python stdlib variant, same JSON contract.
- `scripts/push_termux.sh` — pushes both + smoke tests.
- `demo/robot_daemon.py --intent-on-phone` flag — routes intent through
  `adb shell` once Termux is ready. With Termux absent it logs the blocker and
  falls through to noop (does NOT silently fall back to the laptop path — the
  whole point is to expose the missing byte).

All four are ready. None will run successfully until step 1 above happens.

## Why not bake curl into the Android shell?

Tried. Android `adb shell` has no TLS HTTP client. Static-linked curl binaries
exist for aarch64 Android but (a) need to be pushed to `/data/local/tmp/`, (b)
still need CA bundle pushing, (c) duplicate half of what Termux gives you for
free. Not worth the 2-day rabbit hole.
