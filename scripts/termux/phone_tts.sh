#!/data/data/com.termux/files/usr/bin/bash
# phone_tts.sh — Termux-side TTS. Reads text on stdin, synthesizes speech
# and plays it through the phone speaker.
#
# Strategy: prefer Piper (natural voice) if `piper` binary + voice are
# present in $HOME/.cache/piper/. Fallback to termux-tts-speak (Android's
# built-in TTS via termux-api). Last fallback: espeak-ng if user installed
# it via pkg.
#
# Usage:
#   echo "acknowledged, leaning left" | bash phone_tts.sh
#
# Env knobs:
#   PIPER_BIN   — path to piper (default: $PREFIX/bin/piper, else none)
#   PIPER_VOICE — path to .onnx voice (default: $HOME/.cache/piper/en_US-lessac-low.onnx)
#   TTS_OFF=1   — no-op (just echo the text to stderr)
#
# Exit codes:
#   0 — something played (or TTS_OFF=1)
#   2 — stdin empty
#   3 — no working TTS backend

set -u
set -o pipefail

log() { printf '[phone_tts] %s\n' "$*" >&2; }

text="$(cat)"
text="${text#"${text%%[![:space:]]*}"}"
text="${text%"${text##*[![:space:]]}"}"
[ -n "$text" ] || { log "empty stdin — nothing to say"; exit 2; }

if [ "${TTS_OFF:-0}" = "1" ]; then
  log "TTS_OFF=1 — would have said: $text"
  exit 0
fi

PIPER_BIN="${PIPER_BIN:-$PREFIX/bin/piper}"
PIPER_VOICE="${PIPER_VOICE:-$HOME/.cache/piper/en_US-lessac-low.onnx}"

# -- 1. piper path -----------------------------------------------------------
if [ -x "$PIPER_BIN" ] && [ -r "$PIPER_VOICE" ]; then
  log "using piper ($PIPER_VOICE)"
  tmp_wav="$(mktemp -t phone_tts.XXXXXX.wav)"
  if printf '%s' "$text" | "$PIPER_BIN" --model "$PIPER_VOICE" --output_file "$tmp_wav" 2>/dev/null; then
    if command -v play-audio >/dev/null 2>&1; then
      play-audio "$tmp_wav" >/dev/null 2>&1 && { rm -f "$tmp_wav"; exit 0; }
    fi
    if command -v termux-media-player >/dev/null 2>&1; then
      termux-media-player play "$tmp_wav" >/dev/null 2>&1 && { rm -f "$tmp_wav"; exit 0; }
    fi
    if command -v aplay >/dev/null 2>&1; then
      aplay -q "$tmp_wav" >/dev/null 2>&1 && { rm -f "$tmp_wav"; exit 0; }
    fi
    log "piper synth ok but no player (play-audio/termux-media-player/aplay) — falling through"
  else
    log "piper synth failed — falling through"
  fi
  rm -f "$tmp_wav"
fi

# -- 2. termux-tts-speak (Android built-in) ---------------------------------
if command -v termux-tts-speak >/dev/null 2>&1; then
  log "using termux-tts-speak"
  printf '%s' "$text" | termux-tts-speak && exit 0
  log "termux-tts-speak failed"
fi

# -- 3. espeak-ng last resort ------------------------------------------------
if command -v espeak-ng >/dev/null 2>&1; then
  log "using espeak-ng"
  espeak-ng -v en-us -s 160 "$text" >/dev/null 2>&1 && exit 0
fi

log "no working TTS backend (piper/termux-tts-speak/espeak-ng all absent or failed)"
exit 3
