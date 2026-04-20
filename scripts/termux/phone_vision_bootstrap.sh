#!/data/data/com.termux/files/usr/bin/bash
# phone_vision_bootstrap.sh — one-shot Termux-side setup for phone_vision.py.
#
# Runs inside Termux on the Pixel 6. Safe to re-run.
#
# What it does:
#   1. Ensures termux-api (`termux-camera-photo`) is installed.
#   2. Copies phone_vision.py from /sdcard/Download/edge-ai-phone/ to $HOME.
#   3. Verifies CAMERA permission is granted to com.termux.api by attempting
#      a throwaway frame; if it fails we log a clear "open Termux:API and
#      accept the camera dialog" hint. (Hosts can also grant it head-less:
#      `adb shell pm grant com.termux.api android.permission.CAMERA`.)
#   4. Warms the hosted-VLM path by doing a zero-shot query against the
#      freshly-captured frame. Result is written to
#      /sdcard/Download/edge-ai-phone/vision_test.flag so the host can pull
#      it via adb.
#
# We do NOT `pip install onnxruntime` because phone_vision.py ships the
# hosted-VLM path only (see the module docstring for the rationale).
#
# Usage (paste in a Termux session):
#   bash /sdcard/Download/edge-ai-phone/phone_vision_bootstrap.sh

set -u
set -o pipefail

log() { printf '[vision_bootstrap %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }

STAGE="/sdcard/Download/edge-ai-phone"
FLAG="$STAGE/vision_bootstrap.flag"
VISION_FLAG="$STAGE/vision_test.flag"

: > "$FLAG"
exec > >(tee -a "$FLAG") 2>&1

log "== phone_vision bootstrap =="

# -- 1. ensure termux-api + curl + jq are installed -------------------------
if ! command -v termux-camera-photo >/dev/null 2>&1; then
  log "termux-camera-photo missing -> pkg install termux-api"
  pkg install -y termux-api || { log "pkg install termux-api failed"; exit 2; }
fi
for b in curl jq python3; do
  command -v "$b" >/dev/null 2>&1 || { log "missing $b (run termux_bootstrap.sh first)"; exit 2; }
done

# -- 2. copy phone_vision.py into $HOME -------------------------------------
SRC_PY="$STAGE/phone_vision.py"
if [ ! -r "$SRC_PY" ]; then
  log "no $SRC_PY — run scripts/push_termux.sh on the host first"
  exit 2
fi
cp -f "$SRC_PY" "$HOME/phone_vision.py"
chmod 0755 "$HOME/phone_vision.py"
log "copied phone_vision.py -> \$HOME ($(wc -c <"$HOME/phone_vision.py") bytes)"

# -- 3. probe camera --------------------------------------------------------
TMP_JPG="$PREFIX/tmp/phone_vision_probe.jpg"
mkdir -p "$(dirname "$TMP_JPG")"
rm -f "$TMP_JPG"
log "probing back camera (termux-camera-photo -c 0)"
termux-camera-photo -c 0 "$TMP_JPG" 2>&1 | sed 's/^/  cam: /' || true
if [ -s "$TMP_JPG" ]; then
  sz=$(wc -c <"$TMP_JPG")
  log "camera probe OK: $sz bytes @ $TMP_JPG"
else
  log "CAMERA probe FAILED. Options:"
  log "  a) from host: adb shell pm grant com.termux.api android.permission.CAMERA"
  log "  b) open Termux:API once, run \`termux-camera-photo -c 0 /tmp/x.jpg\`,"
  log "     accept the camera permission dialog, then re-run this script."
fi

# -- 4. warm the hosted-VLM path via the CLI --------------------------------
if [ ! -s "$HOME/.dia_key" ]; then
  log "WARN: \$HOME/.dia_key missing — VLM calls will fail with no_api_key"
fi

log "running phone_vision CLI self-test (3 runs, back camera)"
python3 "$HOME/phone_vision.py" \
  --camera-id 0 \
  --runs 3 \
  --threshold 0.2 \
  --flag-path "$VISION_FLAG" \
  --query "a person" "a laptop" "a red mug" \
  2>&1 | sed 's/^/  cli: /' || log "CLI self-test non-zero (check flag)"

log "vision_test flag: $VISION_FLAG"
[ -r "$VISION_FLAG" ] && log "flag bytes: $(wc -c <"$VISION_FLAG")"
log "== bootstrap done =="
exit 0
