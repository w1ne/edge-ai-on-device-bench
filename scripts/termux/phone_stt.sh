#!/data/data/com.termux/files/usr/bin/bash
# phone_stt.sh — Termux-side STT. Records ~3 s of mic audio via termux-api,
# transcribes with whisper-cli + ggml-tiny. Prints transcript on stdout,
# diagnostics on stderr.
#
# Requires (on phone):
#   - termux-api pkg + com.termux.api APK installed
#   - microphone permission granted to Termux:API (user must accept the
#     Android dialog the first time)
#   - whisper-cli at /data/local/tmp/whisper-cli (docs/SETUP.md pushes this)
#   - whisper model at /data/local/tmp/ggml-tiny.bin
#
# Usage:
#   bash phone_stt.sh              # record 3 s and transcribe
#   RECORD_SEC=5 bash phone_stt.sh # longer capture
#   WAV=/tmp/cap.wav bash phone_stt.sh < /dev/null  # reuse an existing wav
#
# Exit codes:
#   0  — transcript printed (may be empty if nothing was said)
#   2  — missing binaries / model
#   3  — termux-microphone-record failed (permission not granted, mic busy)
#   4  — whisper-cli failed

set -u
set -o pipefail

log() { printf '[phone_stt] %s\n' "$*" >&2; }

RECORD_SEC="${RECORD_SEC:-3}"
WAV="${WAV:-$PREFIX/tmp/phone_stt_cap.wav}"
WHISPER_BIN="${WHISPER_BIN:-/data/local/tmp/whisper-cli}"
WHISPER_MODEL="${WHISPER_MODEL:-/data/local/tmp/ggml-tiny.bin}"

mkdir -p "$(dirname "$WAV")" 2>/dev/null || true

# -- dep check ---------------------------------------------------------------
[ -x "$WHISPER_BIN" ] || { log "missing whisper-cli at $WHISPER_BIN"; exit 2; }
[ -r "$WHISPER_MODEL" ] || { log "missing model at $WHISPER_MODEL"; exit 2; }

# -- record (skip if caller pre-provided a wav at $WAV) ---------------------
if [ ! -s "$WAV" ] || [ "${REUSE_WAV:-0}" != "1" ]; then
  if ! command -v termux-microphone-record >/dev/null 2>&1; then
    log "termux-microphone-record missing — pkg install termux-api"
    exit 2
  fi
  log "recording ${RECORD_SEC}s -> $WAV"
  # -d = default mic, -l = limit seconds, -f = output path
  termux-microphone-record -d -l "$RECORD_SEC" -f "$WAV" >/dev/null 2>&1 &
  rec_pid=$!
  # termux-microphone-record returns immediately; wait for the capture window.
  sleep $((RECORD_SEC + 1))
  termux-microphone-record -q >/dev/null 2>&1 || true
  wait "$rec_pid" 2>/dev/null || true

  if [ ! -s "$WAV" ]; then
    log "no audio captured (permission not granted? run termux-microphone-record manually once)"
    exit 3
  fi
fi

# -- transcribe --------------------------------------------------------------
# whisper-cli prints transcript lines on stdout with timestamps. We strip
# timestamps + diagnostic banner lines and emit just the text.
log "transcribing $WAV"
raw="$("$WHISPER_BIN" -m "$WHISPER_MODEL" -f "$WAV" -l en --no-timestamps -nt 2>/dev/null)" || {
  log "whisper-cli failed (rc=$?)"
  exit 4
}

# Collapse whitespace, drop any residual leading/trailing noise.
printf '%s\n' "$raw" \
  | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
  | awk 'NF' \
  | head -n 10
exit 0
