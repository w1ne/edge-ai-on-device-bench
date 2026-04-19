#!/usr/bin/env bash
# precommit.sh -- 10-second sanity check to run before every commit.
#
# Purpose: kill the "I refactored a demo/ module and broke an import for
# three days before noticing" failure mode.  Fast enough that there's no
# excuse not to run it.
#
# What it checks (NONE of this touches hardware or paid APIs):
#   1. python3 -m compileall demo/ scripts/ -- syntax errors
#   2. python3 scripts/test_state_auth.py -- auth / CORS / CSRF (15 asserts)
#   3. import demo.robot_daemon, .state_server, .goal_keeper,
#      .robot_planner, .voice_pipecat, .vision_query -- module-level
#      import sanity across the core demo surface
#
# Exit codes:
#   0  all checks passed
#   N  N-th step failed (N = 1, 2, or 3)
#
# Wire-in: see docs/SETUP.md for the one-liner that aliases this to a
# git pre-commit hook.
set -eu

# Always run from the repo root, regardless of where the user invoked it.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

step() { printf '[precommit] %s\n' "$*"; }
fail() { printf '[precommit] FAIL: %s\n' "$*" >&2; exit "${2:-1}"; }

step "1/3 compileall demo/ scripts/"
python3 -m compileall -q demo/ scripts/ \
  || fail "compileall failed (syntax error somewhere)" 1

step "2/3 state_server auth tests"
python3 scripts/test_state_auth.py >/dev/null \
  || fail "test_state_auth.py failed (run it directly to see which assertion)" 2

step "3/3 demo module imports"
# demo/ is not a proper package (historical); add it to sys.path first.
python3 -c "
import sys
sys.path.insert(0, 'demo')
import robot_daemon, state_server, goal_keeper, robot_planner, voice_pipecat, vision_query
" >/dev/null || fail "one of demo.{robot_daemon,state_server,goal_keeper,robot_planner,voice_pipecat,vision_query} refused to import" 3

step "OK"
