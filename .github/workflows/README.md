# CI pipeline

Kills roast point #19 (we had no CI at all). `ci.yml` is the full
pipeline — all jobs run on every push and pull request, matrixed across
Python 3.10, 3.11, and 3.12 on `ubuntu-latest`.

## Jobs that run

| Job | What | Why |
|---|---|---|
| `lint` | `python3 -m compileall -q demo/ scripts/` (+ optional ruff) | Catches syntax errors before they hit `main`. |
| `state-server-tests` | `python3 scripts/test_state_auth.py` (15 assertions) | Auth + CORS + CSRF are security-load-bearing; these are deterministic and don't touch hardware. |
| `planner-eval-offline` | `--help` + import check for `planner_eval.py`, `planner_eval_holdout.py`, `agent_eval.py` + tool-schema sanity | Prevents a rename in `demo/robot_planner.py` from silently breaking the eval harnesses. **Does not run the real evals** — that would need an API key. |
| `hw-stress-dry-run` | `python3 scripts/hw_stress_test.py --duration 5 --dry-run` | Proves the stress harness still parses args and writes logs without a live ESP32. |
| `ci-passed` | Aggregate gate job | Single required-check pointer for branch protection. |

## Jobs that are DELIBERATELY SKIPPED

These exist as manual / local-only steps — moving them into CI would
either cost money per PR, leak a secret, or depend on hardware that GitHub
Actions runners don't have.

| Skipped check | Why it's skipped | How to run it locally |
|---|---|---|
| Full `planner_eval.py` (21 cases) | Each case burns DeepInfra credits. No CI secret — key leaked once (see `docs/STATUS.md`). | `set -a; source ~/Projects/AIHW/.env.local; set +a; python3 scripts/planner_eval.py` |
| Full `planner_eval_holdout.py` (10 H-cases) | Same reason. | `python3 scripts/planner_eval_holdout.py` |
| Full `agent_eval.py` (10 scenarios) | Same reason, plus 5 min of wall time. | `python3 scripts/agent_eval.py` |
| Full `hw_stress_test.py` (30 min) | Needs a physical ESP32 + USB + power. Runners have none. | `python3 scripts/hw_stress_test.py --duration 1800` |
| `vision_integration_test.py` | Needs a webcam. | `python3 scripts/vision_integration_test.py` |
| ADB / on-phone STT benchmarks | Needs a Pixel 6 over USB. | `scripts/push_assets.sh && scripts/run_robot.sh` |
| Robot daemon end-to-end | Needs mic, speaker, webcam, ESP32. | `scripts/run_robot.sh --mode text --no-tts` |
| Wake word models | Auto-downloads from HuggingFace on first run. | First local run does it. |

## Secrets policy

**This pipeline references ZERO repository secrets.** In particular, we
do **not** mint a CI value for `DEEPINFRA_API_KEY`. The key has leaked
once before; the remediation was "never put it in a place that can be
exfiltrated via a compromised Action or a logged env var". CI sticks to
tests that don't need it.

If you're tempted to add a secret-dependent job later, consider:

1. Can it run locally as a pre-push / pre-merge step instead?
2. Can it be a nightly workflow with `workflow_dispatch` only, not on
   every PR?
3. Does the value of running it in CI outweigh the blast radius if the
   secret leaks? (For API keys: almost never.)

## Extending the pipeline

To add a new job:

1. Make sure the test runs **without** hardware, USB, webcam, mic, ADB,
   network to a paid API, or HuggingFace downloads. If it can't, it's a
   local-only check — document it in the "deliberately skipped" table
   above instead of adding it here.
2. Add a new `jobs.<name>:` block following the pattern of existing
   jobs. Matrix on the same `python-version: ["3.10", "3.11", "3.12"]`
   unless you have a reason not to.
3. Add the new job name to the `needs:` list on `ci-passed` so it
   gates the aggregate.
4. Keep the first run cheap — under 60 s. `ubuntu-latest` starts cold;
   long jobs burn minutes for no benefit on a small repo.

To add a local-only check:

- Put it in `scripts/precommit.sh` if it's fast enough (<10 s).
- Otherwise document it in the "deliberately skipped" table above with
  the exact command.

## Known soft spots

- We don't pin pip dependencies for CI. The tests here intentionally
  only use `stdlib` so there's nothing to `pip install`. If you add a
  test that needs numpy/requests/etc., add a `pip install` step and pin
  the version.
- Ruff runs as non-blocking for now (returns 0 regardless). Once the
  repo is clean enough to gate on it, flip that to a real fail.
- No caching. Setup-python is fast enough that cold runs don't hurt.
  Revisit if the pipeline ever exceeds 2 minutes.
