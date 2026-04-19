#!/usr/bin/env bash
# run_robot.sh — one-shot launcher for the full robot stack.
#
# Sources the DeepInfra key, defaults to webcam vision + API-backed LLM
# fallback + BehaviorEngine, and hands off to demo/robot_daemon.py.
# Any extra args passed to this script are forwarded to the daemon, so you
# can override flags without editing the script:
#
#   scripts/run_robot.sh                     # full voice + vision
#   scripts/run_robot.sh --mode text         # typed commands
#   scripts/run_robot.sh --dry-run           # no ESP32 wire writes
#   scripts/run_robot.sh --llm-api local --llm-model gemma   # offline
#   scripts/run_robot.sh --vision-source phone --vision-phone pixel6
#
# Env var DEEPINFRA_API_KEY is sourced from ~/Projects/AIHW/.env.local if it
# isn't already set.  No keys are ever written here.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
ENV_FILE="${HOME}/Projects/AIHW/.env.local"

if [ -z "${DEEPINFRA_API_KEY:-}" ]; then
  if [ -r "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; source "$ENV_FILE"; set +a
  else
    echo "WARN: $ENV_FILE not readable and DEEPINFRA_API_KEY is unset." >&2
    echo "      API-backed LLM fallback will not work; consider --llm-api local." >&2
  fi
fi

# Defaults — can be overridden via the extra-args tail.
DEFAULT_ARGS=(
  --with-vision person
  --vision-source webcam
  --vision-interval 0.1
  --with-llm --llm-api api
  --log "$REPO_DIR/logs/robot-$(date -u +%Y%m%d-%H%M%S).log"
)

echo "[run_robot] starting with defaults: ${DEFAULT_ARGS[*]}" >&2
echo "[run_robot] extra args: $*" >&2
echo "[run_robot] hit Enter to speak a command; say 'shut down' or Ctrl-C to exit." >&2

cd "$REPO_DIR"
exec python3 demo/robot_daemon.py "${DEFAULT_ARGS[@]}" "$@"
