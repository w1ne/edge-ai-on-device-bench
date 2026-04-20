#!/data/data/com.termux/files/usr/bin/bash
# termux_bootstrap.sh — one-shot Termux-side setup for the phone-brain port.
#
# Why this exists as a paste-me-into-Termux script: the F-Droid Termux build
# is NOT debuggable, so `adb shell run-as com.termux ...` is blocked. The only
# supported way to execute inside Termux's Linux prefix from the host is to
# have the user open Termux once and run this.
#
# The user can either:
#   a) adb push scripts/termux/termux_bootstrap.sh /sdcard/Download/
#      (then in Termux: `bash /sdcard/Download/termux_bootstrap.sh`)
#   b) copy-paste the one-liner printed by docs/PHONE_BRAIN_SETUP.md, which
#      curl | bash's this file from GitHub.
#
# Safe to re-run. Idempotent.

set -u
set -o pipefail

log() { printf '[bootstrap] %s\n' "$*" >&2; }

# -- 1. storage access (so Termux can read /sdcard for pushed assets) --------
if [ ! -d "$HOME/storage" ]; then
  log "requesting storage permission (accept the Android popup)"
  termux-setup-storage || true
  sleep 2
fi

# -- 2. repos + core packages ------------------------------------------------
log "pkg update"
pkg update -y >/dev/null 2>&1 || true
log "pkg install python curl jq termux-api"
pkg install -y python curl jq termux-api \
  || { log "pkg install failed — check network + Termux version"; exit 1; }

# -- 3. python sanity --------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  log "python3 still missing after pkg install — abort"
  exit 2
fi
python3 --version >&2

# -- 4. pull phone_* scripts from /sdcard into Termux $HOME ------------------
# push_termux.sh drops them in /sdcard/Download/edge-ai-phone/ via `adb push`.
SRC="/sdcard/Download/edge-ai-phone"
if [ -d "$SRC" ]; then
  log "copying phone_* scripts from $SRC -> \$HOME/"
  cp -f "$SRC"/phone_*.py "$HOME/" 2>/dev/null || true
  cp -f "$SRC"/phone_*.sh "$HOME/" 2>/dev/null || true
  cp -f "$SRC"/mock_wire_server.py "$HOME/" 2>/dev/null || true
  chmod 0755 "$HOME"/phone_*.py "$HOME"/phone_*.sh 2>/dev/null || true
else
  log "no $SRC yet — adb push scripts first (scripts/push_termux.sh does this)"
fi

# -- 5. dia key: accept either $HOME/.dia_key (copy-pasted) or sdcard push ---
if [ -r "/sdcard/Download/edge-ai-phone/.dia_key" ]; then
  log "copying dia key from /sdcard into \$HOME/.dia_key (0600)"
  cp -f "/sdcard/Download/edge-ai-phone/.dia_key" "$HOME/.dia_key"
  chmod 600 "$HOME/.dia_key"
  shred -u "/sdcard/Download/edge-ai-phone/.dia_key" 2>/dev/null \
    || rm -f "/sdcard/Download/edge-ai-phone/.dia_key"
fi
if [ ! -r "$HOME/.dia_key" ]; then
  log "WARN: \$HOME/.dia_key missing. Paste it manually:"
  log "  printf 'sk-xxx' > \$HOME/.dia_key && chmod 600 \$HOME/.dia_key"
fi

# -- 6. cache dirs -----------------------------------------------------------
mkdir -p "$HOME/.cache/piper"

log "done. files in \$HOME:"
ls -la "$HOME" >&2

# Drop a world-readable flag so the host (adb) can verify completion,
# since Termux's $HOME is not accessible to the shell uid.
FLAG="/sdcard/Download/edge-ai-phone/bootstrap_ok.flag"
{
  echo "ok ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "python=$(command -v python3 || echo missing)"
  echo "pyver=$(python3 --version 2>&1 || echo missing)"
  echo "daemon=$([ -f "$HOME/phone_daemon.py" ] && echo present || echo missing)"
  echo "wire=$([ -f "$HOME/phone_wire.py" ] && echo present || echo missing)"
  echo "intent=$([ -f "$HOME/phone_intent.py" ] && echo present || echo missing)"
  echo "stt=$([ -f "$HOME/phone_stt.sh" ] && echo present || echo missing)"
  echo "tts=$([ -f "$HOME/phone_tts.sh" ] && echo present || echo missing)"
  echo "dia_key=$([ -f "$HOME/.dia_key" ] && echo present || echo missing)"
} > "$FLAG" 2>/dev/null || true

exit 0
