#!/usr/bin/env bash
# push_termux.sh — push the phone-side intent parser to the Pixel 6 and
# smoke-test it via `adb shell` → Termux bash.
#
# Usage:
#   bash scripts/push_termux.sh                       # default Pixel 6 serial
#   ANDROID_SERIAL=XXX bash scripts/push_termux.sh    # override
#   DEEPINFRA_API_KEY=sk-... bash scripts/push_termux.sh  # also pushes key
#
# Exit codes:
#   0  — push ok, smoke test produced a valid canonical wire JSON
#   10 — Termux not installed on the phone (see docs/PHONE_BRAIN_BLOCKED.md)
#   11 — adb not present or phone not reachable
#   12 — push failed
#   13 — smoke test failed (phone ran but returned garbage)

set -u
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT_DIR="$REPO_ROOT/scripts/termux"
TERMUX_BASH=/data/data/com.termux/files/usr/bin/bash
TERMUX_PYTHON=/data/data/com.termux/files/usr/bin/python3

log() { printf '[push_termux] %s\n' "$*" >&2; }

command -v adb >/dev/null 2>&1 || { log "adb not installed"; exit 11; }

SERIAL="${ANDROID_SERIAL:-1B291FDF600260}"
ADB=(adb -s "$SERIAL")

if ! "${ADB[@]}" get-state >/dev/null 2>&1; then
  log "adb: device $SERIAL not reachable"
  exit 11
fi

# Probe Termux presence before doing anything else.
if ! "${ADB[@]}" shell 'pm list packages' 2>/dev/null | grep -qi com.termux; then
  log "Termux NOT installed on $SERIAL — see docs/PHONE_BRAIN_BLOCKED.md"
  exit 10
fi
log "Termux present on $SERIAL"

# Push the artifacts. /data/local/tmp is world-readable and survives between
# adb sessions; doesn't need root.
log "pushing scripts/termux/phone_intent.sh -> /data/local/tmp/phone_intent.sh"
"${ADB[@]}" push "$SCRIPT_DIR/phone_intent.sh" /data/local/tmp/phone_intent.sh >/dev/null \
  || { log "adb push .sh failed"; exit 12; }
log "pushing scripts/termux/phone_intent.py -> /data/local/tmp/phone_intent.py"
"${ADB[@]}" push "$SCRIPT_DIR/phone_intent.py" /data/local/tmp/phone_intent.py >/dev/null \
  || { log "adb push .py failed"; exit 12; }
"${ADB[@]}" shell 'chmod 0755 /data/local/tmp/phone_intent.sh /data/local/tmp/phone_intent.py' >/dev/null

# Optionally push the DeepInfra key. Never hardcode.
# We stash it in /data/local/tmp/.dia_key (mode 0600) AND try to copy it into
# the Termux home via run-as if unlocked. Falls back to /data/local/tmp on
# play-store builds where run-as is denied.
if [ -n "${DEEPINFRA_API_KEY:-}" ]; then
  log "pushing DEEPINFRA_API_KEY via stdin (not logged)"
  "${ADB[@]}" shell "cat > /data/local/tmp/.dia_key && chmod 600 /data/local/tmp/.dia_key" \
    <<<"$DEEPINFRA_API_KEY" >/dev/null
  # Try to copy to Termux $HOME so the scripts find it without DIA_KEY_FILE.
  "${ADB[@]}" shell "$TERMUX_BASH -lc 'cat /data/local/tmp/.dia_key > \$HOME/.dia_key && chmod 600 \$HOME/.dia_key && echo OK'" \
    2>/dev/null | grep -q OK && log "copied key into Termux \$HOME" \
                             || log "could not copy into Termux \$HOME (run-as blocked?) — using /data/local/tmp/.dia_key via DIA_KEY_FILE"
fi

# Smoke test. Runs phone_intent.sh under Termux bash with "lean left please"
# on stdin. Any non-JSON stdout, or a non-{"c":"pose",...} response, fails.
log "smoke test: 'lean left please' -> phone_intent.sh"
smoke_out="$(printf 'lean left please' \
  | "${ADB[@]}" shell "DIA_KEY_FILE=/data/local/tmp/.dia_key $TERMUX_BASH /data/local/tmp/phone_intent.sh" \
    2>/tmp/phone_intent.stderr)"
printf '%s' "$smoke_out" > /tmp/phone_intent.stdout
log "phone stdout: $smoke_out"
log "phone stderr: $(tail -3 /tmp/phone_intent.stderr 2>/dev/null | tr '\n' '|')"

# The last line of stdout should parse as a dict with c==pose or c==noop.
last_line="$(printf '%s' "$smoke_out" | awk 'NF{line=$0} END{print line}')"
if command -v python3 >/dev/null 2>&1; then
  if ! python3 -c "
import json,sys
s=sys.stdin.read().strip()
try:
  o=json.loads(s)
except Exception as e:
  print('bad json:',e,repr(s)); sys.exit(1)
if not isinstance(o,dict) or 'c' not in o:
  print('no c:',o); sys.exit(1)
if o['c']=='pose' and o.get('n')=='lean_left':
  print('OK:',o); sys.exit(0)
print('unexpected:',o); sys.exit(2)
" <<<"$last_line"; then
    log "smoke test FAILED — see /tmp/phone_intent.stderr for phone-side reason"
    exit 13
  fi
fi

log "smoke test PASSED"
exit 0
