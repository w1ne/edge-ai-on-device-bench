#!/usr/bin/env bash
# push_termux.sh — push the phone-side scripts to the Pixel 6 and (when
# Termux is reachable) smoke-test phone_intent.sh via `adb shell` → Termux
# bash.
#
# Phase 1 layout: we push BOTH to /data/local/tmp/ (so adb-shell paths keep
# working, same as before) AND to /sdcard/Download/edge-ai-phone/ (so the
# user can run scripts/termux/termux_bootstrap.sh inside Termux to copy them
# into $HOME). The F-Droid Termux build is not debuggable, so `run-as` is
# blocked — the user has to do a one-time copy-paste bootstrap.
#
# Usage:
#   bash scripts/push_termux.sh                       # default Pixel 6 serial
#   ANDROID_SERIAL=XXX bash scripts/push_termux.sh    # override
#   DEEPINFRA_API_KEY=sk-... bash scripts/push_termux.sh  # also stages key
#
# Exit codes:
#   0  — push ok (smoke test skipped if Termux $HOME isn't reachable via run-as)
#   10 — Termux packages not installed on the phone (APKs missing)
#   11 — adb not present or phone not reachable
#   12 — push failed

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$REPO_ROOT/scripts/termux"
TERMUX_BASH=/data/data/com.termux/files/usr/bin/bash

log() { printf '[push_termux] %s\n' "$*" >&2; }

command -v adb >/dev/null 2>&1 || { log "adb not installed"; exit 11; }

SERIAL="${ANDROID_SERIAL:-1B291FDF600260}"
ADB=(adb -s "$SERIAL")

if ! "${ADB[@]}" get-state >/dev/null 2>&1; then
  log "adb: device $SERIAL not reachable"
  exit 11
fi

if ! "${ADB[@]}" shell 'pm list packages' 2>/dev/null | grep -qi com.termux; then
  log "Termux NOT installed on $SERIAL — see docs/PHONE_BRAIN_SETUP.md"
  exit 10
fi
log "Termux present on $SERIAL"

# -- 1. legacy /data/local/tmp push (for phone_intent.* via adb shell) -------
for f in phone_intent.sh phone_intent.py; do
  log "pushing $f -> /data/local/tmp/$f"
  "${ADB[@]}" push "$SCRIPT_DIR/$f" "/data/local/tmp/$f" >/dev/null \
    || { log "adb push $f failed"; exit 12; }
done
"${ADB[@]}" shell 'chmod 0755 /data/local/tmp/phone_intent.sh /data/local/tmp/phone_intent.py' >/dev/null

# -- 2. /sdcard push: everything the bootstrap copies into Termux $HOME ------
STAGE="/sdcard/Download/edge-ai-phone"
"${ADB[@]}" shell "mkdir -p $STAGE" >/dev/null

PHONE_FILES=(
  phone_intent.py
  phone_intent.sh
  phone_stt.sh
  phone_tts.sh
  phone_wire.py
  phone_daemon.py
  phone_vision.py
  phone_vision_bootstrap.sh
  mock_wire_server.py
  termux_bootstrap.sh
)
for f in "${PHONE_FILES[@]}"; do
  src="$SCRIPT_DIR/$f"
  if [ ! -f "$src" ]; then
    log "WARN: $src missing — skip"; continue
  fi
  log "pushing $f -> $STAGE/$f"
  "${ADB[@]}" push "$src" "$STAGE/$f" >/dev/null \
    || { log "adb push $f failed"; exit 12; }
done

# -- 3. DeepInfra key staging (optional, 0600 on-device) ---------------------
if [ -n "${DEEPINFRA_API_KEY:-}" ]; then
  log "staging DEEPINFRA_API_KEY in $STAGE/.dia_key (mode 0600)"
  "${ADB[@]}" shell "cat > $STAGE/.dia_key && chmod 600 $STAGE/.dia_key" \
    <<<"$DEEPINFRA_API_KEY" >/dev/null
  # Also drop a /data/local/tmp copy so phone_intent.sh via adb shell works
  # without Termux.
  "${ADB[@]}" shell "cat > /data/local/tmp/.dia_key && chmod 600 /data/local/tmp/.dia_key" \
    <<<"$DEEPINFRA_API_KEY" >/dev/null
else
  log "DEEPINFRA_API_KEY not in env — skip key push"
fi

# -- 4. smoke test via /data/local/tmp (Termux-less path) --------------------
# run-as is blocked on F-Droid Termux builds so we can't shell into Termux
# from adb. The /data/local/tmp path exercises the intent parser with
# system-shell bash (toybox-ish) which is enough for a canary. If it works
# here, phone_intent.py will also work once Termux pkg install completes.
if [ -n "${DEEPINFRA_API_KEY:-}" ]; then
  log "smoke test via /data/local/tmp (curl + jq in android shell — may skip)"
  if "${ADB[@]}" shell 'command -v curl && command -v jq' >/dev/null 2>&1; then
    smoke_out="$(printf 'lean left please' \
      | "${ADB[@]}" shell "DIA_KEY_FILE=/data/local/tmp/.dia_key sh /data/local/tmp/phone_intent.sh" 2>/tmp/phone_intent.stderr || true)"
    log "phone stdout: ${smoke_out:-<empty>}"
    log "phone stderr: $(tail -3 /tmp/phone_intent.stderr 2>/dev/null | tr '\n' '|')"
  else
    log "android shell lacks curl/jq — skip (Termux-only smoke via termux_bootstrap.sh)"
  fi
fi

log "push complete."
log "NEXT: on the phone, open Termux and run:"
log "  bash /sdcard/Download/edge-ai-phone/termux_bootstrap.sh"
log "(F-Droid Termux is not debuggable → run-as is blocked → no way to do"
log " this from adb alone. See docs/PHONE_BRAIN_SETUP.md.)"
exit 0
