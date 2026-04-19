#!/usr/bin/env bash
# Start llama-server on an ADB-connected Android phone for the robot intent parser.
#
# Loads the gguf model once and stays resident so subsequent parse calls over
# HTTP are sub-second (vs ~25 s cold-start per call for `llama-cli`).  The CPU
# backend is used on the phone (`-ngl 0`); a prior bench found Mali Vulkan loses
# for this workload.  Grammar-constrained completions are supported out of the
# box by this build.
#
# Usage:
#   scripts/start_llm_server.sh --phone {pixel6,p20} --model {gemma,tinyllama}
#   scripts/start_llm_server.sh --phone p20 --model gemma
#
# Side effects:
#   - `adb forward tcp:18080 tcp:18080` so the laptop can hit
#     http://127.0.0.1:18080 (curl / Python `requests`).
#   - Kills any prior `llama-server` on the phone (we never want two
#     instances of the same 720 MB model sharing RAM).
#   - Prints "READY" on stdout once /health returns 200.  Runs foreground;
#     Ctrl-C tears the phone-side server down as well.
#
set -euo pipefail

PHONE=""
MODEL="gemma"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --phone) PHONE="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

case "$PHONE" in
  pixel6) SERIAL="1B291FDF600260" ;;
  p20)    SERIAL="9WV4C18C11005454" ;;
  "")     echo "ERR: --phone {pixel6,p20} is required" >&2; exit 2 ;;
  *)      echo "ERR: unknown phone: $PHONE" >&2; exit 2 ;;
esac

case "$MODEL" in
  gemma)     GGUF="/data/local/tmp/gemma3.gguf" ;;
  tinyllama) GGUF="/data/local/tmp/tinyllama.gguf" ;;
  *) echo "ERR: unknown model: $MODEL (use gemma|tinyllama)" >&2; exit 2 ;;
esac

PORT=18080
REMOTE_DIR=/data/local/tmp/vulkan
PID_ON_PHONE=""

cleanup() {
  echo "[start_llm_server] cleanup: stopping phone-side server..." >&2
  adb -s "$SERIAL" shell "pkill -f llama-server 2>/dev/null || true" >/dev/null 2>&1 || true
  adb -s "$SERIAL" forward --remove tcp:${PORT} >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "[start_llm_server] phone=$PHONE ($SERIAL) model=$MODEL gguf=$GGUF port=$PORT" >&2

# Don't allow two inference processes on the same phone.
adb -s "$SERIAL" shell "pkill -f llama-server 2>/dev/null || true" >/dev/null 2>&1 || true
adb -s "$SERIAL" shell "pkill -f llama-cli    2>/dev/null || true" >/dev/null 2>&1 || true
sleep 1

# Forward the TCP port laptop->phone.
adb -s "$SERIAL" forward --remove tcp:${PORT} >/dev/null 2>&1 || true
adb -s "$SERIAL" forward tcp:${PORT} tcp:${PORT} >/dev/null

# Launch in background on phone; log to /data/local/tmp/llama-server.log.
# Use LD_LIBRARY_PATH so the .so deps next to the binary are found.
REMOTE_CMD="cd $REMOTE_DIR && LD_LIBRARY_PATH=$REMOTE_DIR ./llama-server \
  -m $GGUF -ngl 0 -t 4 --no-warmup \
  --host 127.0.0.1 --port ${PORT} \
  > /data/local/tmp/llama-server.log 2>&1 &
echo \$!"

echo "[start_llm_server] launching llama-server on phone..." >&2
PID_ON_PHONE=$(adb -s "$SERIAL" shell "$REMOTE_CMD" | tr -d '\r' | tail -n1)
echo "[start_llm_server] phone pid=$PID_ON_PHONE" >&2

# Poll /health until 200 or 120 s out.
echo "[start_llm_server] waiting for /health on http://127.0.0.1:${PORT} ..." >&2
DEADLINE=$(( $(date +%s) + 120 ))
while :; do
  if [ "$(date +%s)" -gt "$DEADLINE" ]; then
    echo "[start_llm_server] TIMEOUT waiting for /health" >&2
    adb -s "$SERIAL" shell "tail -40 /data/local/tmp/llama-server.log" >&2 || true
    exit 1
  fi
  code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/health" || echo 000)
  if [ "$code" = "200" ]; then
    break
  fi
  sleep 1
done

echo "READY phone=$PHONE model=$MODEL http=http://127.0.0.1:${PORT}"

# Keep script alive; cleanup trap tears the phone-side server down on Ctrl-C.
echo "[start_llm_server] serving. Ctrl-C to stop." >&2
# Tail the phone log so we can see generation activity from the laptop side.
adb -s "$SERIAL" shell "tail -f /data/local/tmp/llama-server.log"
