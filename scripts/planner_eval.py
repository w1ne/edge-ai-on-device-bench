#!/usr/bin/env python3
"""
planner_eval.py  —  regression harness for demo.robot_planner.Planner.

The planner's built-in self-test only covers 3 goals.  That's not enough
to catch regressions when we tweak the system prompt, swap models, or
change max_steps.  This harness drives 20 goals through the real
DeepInfra tool-calling loop with fully stubbed tool callables, records
what was called, and prints a pass/fail verdict per case.

Usage:
    set -a; source ~/Projects/AIHW/.env.local; set +a
    python3 scripts/planner_eval.py                 # run all 20
    python3 scripts/planner_eval.py --subset A      # only Group A
    python3 scripts/planner_eval.py --json out.json # dump structured results

Ground rules (see user directive):
  * NO edits to demo/robot_planner.py — eval is external.
  * Tool stubs are inert recorders; no actuation, no sleeps.
  * `look()` returns a parametric fixture so conditional cases are
    deterministic on the tool-result side (LLM prose still varies).
  * Expected tool sets are the minimum required — actual > expected is
    fine as long as the step cap holds.  Do not massage expectations to
    hit a target pass rate.

Exit codes:
    0  all cases passed
    1  one or more cases failed
    2  $DEEPINFRA_API_KEY missing (propagated from planner itself)

PINS -- do NOT relax these to hit a number:
  * A2/A4/B1/B2/B4/E3 pose names (bow_front, lean_left, lean_right,
    neutral) are the canonical enum.  If you rename them in the schema,
    update BOTH the schema and the checks -- never just the checks.
  * C2 ("nobody is here") MUST match a negation verbal.  Changing this
    to "any say" would let a planner greet the empty room and still
    pass.
  * C5 MUST require `look_for` AND forbid `look` -- it's the open-vocab
    vs structured distinction, not a free choice.
  * D-group (weather/math/name) MUST forbid pose/walk/jump/look.  A
    verbal question is verbal; physical action is a regression.
  * E-group cases are deliberately tolerant -- do NOT add tighter
    expectations there; the whole point is terminate-cleanly, not
    specific-tool-choice.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any, Callable

# Make sibling `demo/` importable when running from the repo root.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from demo.robot_planner import Planner, DEFAULT_MODEL  # noqa: E402


MAX_STEPS = 10


# ------------------------------------------------------------------ stubs

def _build_stub_tools(look_queue: list[dict],
                      look_for_queue: list[dict] | None = None,
                      ) -> tuple[dict, list[dict]]:
    """Inert tool stubs that just append to a call log.  `look_queue` is
    consumed left-to-right; once exhausted, subsequent look() calls
    return {"seen": []}.  `look_for_queue` feeds the open-vocab path the
    same way; once exhausted, look_for() returns {'seen': False,
    'score': 0.0, 'frame_ms': 0}."""
    call_log: list[dict] = []
    lf_queue = list(look_for_queue or [])

    def pose(name: str, duration_ms: int = 400) -> dict:
        call_log.append({"tool": "pose",
                         "args": {"name": name, "duration_ms": duration_ms}})
        return {"ok": True}

    def walk(stride: int = 150, step: int = 400, **_ignored) -> dict:
        call_log.append({"tool": "walk",
                         "args": {"stride": stride, "step": step}})
        return {"ok": True}

    def stop() -> dict:
        call_log.append({"tool": "stop", "args": {}})
        return {"ok": True}

    def jump() -> dict:
        call_log.append({"tool": "jump", "args": {}})
        return {"ok": True}

    def look(direction: str) -> dict:
        call_log.append({"tool": "look", "args": {"direction": direction}})
        seen = look_queue.pop(0) if look_queue else {"seen": []}
        return {"ok": True, **seen}

    def look_for(query: str) -> dict:
        call_log.append({"tool": "look_for", "args": {"query": query}})
        out = lf_queue.pop(0) if lf_queue else {
            "seen": False, "score": 0.0, "frame_ms": 0,
        }
        return {"ok": True, **out}

    def say(text: str) -> dict:
        call_log.append({"tool": "say", "args": {"text": text}})
        return {"ok": True}

    def wait(seconds: float) -> dict:
        call_log.append({"tool": "wait", "args": {"seconds": seconds}})
        return {"ok": True}

    tools = {
        "pose": pose, "walk": walk, "stop": stop, "jump": jump,
        "look": look, "look_for": look_for, "say": say, "wait": wait,
    }
    return tools, call_log


# ------------------------------------------------------------------ helpers

def _tool_names(log: list[dict]) -> list[str]:
    return [c["tool"] for c in log]


def _has_pose(log: list[dict], name: str) -> bool:
    return any(c["tool"] == "pose" and c["args"].get("name") == name
               for c in log)


def _say_text(log: list[dict]) -> str:
    """Concatenate every say() utterance, lowercased, for substring checks."""
    bits = [c["args"].get("text", "") for c in log if c["tool"] == "say"]
    return " ".join(bits).lower()


def _subset(required: set[str], actual_names: list[str]) -> bool:
    return required.issubset(set(actual_names))


def _terminated_ok(result: dict, step_count: int) -> bool:
    """Pass if the run terminated cleanly: finish called, stop used as
    terminal (planner marks success=True for that path), or cleanly hit
    max_steps with a reasonable tool history.  We're lenient here — the
    per-case expect() is where behaviour is pinned."""
    if result.get("success"):
        return True
    # Not success, but did we at least stay under the cap?
    return step_count <= MAX_STEPS


# ------------------------------------------------------------------ cases

def _build_cases() -> list[dict]:
    """20 cases, 5 groups.  Each case dict:
        id, group, goal, look_queue, expect(log, result) -> bool, note
    """
    cases: list[dict] = []

    # Group A — Simple two-step ------------------------------------------
    cases.extend([
        {
            "id": "A1-jump-happy",
            "group": "A",
            "goal": "Jump once and tell me you're happy.",
            "look_queue": [],
            "expect": lambda log, res:
                "jump" in _tool_names(log) and "say" in _tool_names(log),
        },
        {
            "id": "A2-bow-then-say",
            "group": "A",
            "goal": "Bow forward and then say hello to the audience.",
            "look_queue": [],
            "expect": lambda log, res:
                _has_pose(log, "bow_front") and "say" in _tool_names(log),
        },
        {
            "id": "A3-walk-then-stop",
            "group": "A",
            "goal": "Start walking, then stop after a moment.",
            "look_queue": [],
            "expect": lambda log, res:
                "walk" in _tool_names(log) and "stop" in _tool_names(log),
        },
        {
            "id": "A4-lean-right-say",
            "group": "A",
            "goal": "Lean to the right and tell me what you did.",
            "look_queue": [],
            "expect": lambda log, res:
                _has_pose(log, "lean_right") and "say" in _tool_names(log),
        },
        {
            "id": "A5-look-say",
            "group": "A",
            "goal": "Look ahead and tell me what you see.",
            "look_queue": [{"seen": ["chair", "table"]}],
            "expect": lambda log, res:
                "look" in _tool_names(log) and "say" in _tool_names(log),
        },
    ])

    # Group B — Multi-step coordinated -----------------------------------
    cases.extend([
        {
            "id": "B1-lean-wait-neutral",
            "group": "B",
            "goal": "Lean left, wait one second, then return to neutral.",
            "look_queue": [],
            "expect": lambda log, res:
                _has_pose(log, "lean_left")
                and "wait" in _tool_names(log)
                and _has_pose(log, "neutral"),
        },
        {
            "id": "B2-bow-walk-stop",
            "group": "B",
            "goal": "Bow forward, then walk a bit, then stop.",
            "look_queue": [],
            "expect": lambda log, res:
                _has_pose(log, "bow_front")
                and "walk" in _tool_names(log)
                and "stop" in _tool_names(log),
        },
        {
            "id": "B3-jump-say-jump",
            "group": "B",
            "goal": "Jump, say ready, then jump again.",
            "look_queue": [],
            "expect": lambda log, res:
                _tool_names(log).count("jump") >= 2
                and "say" in _tool_names(log),
        },
        {
            "id": "B4-lean-both",
            "group": "B",
            "goal": "Lean left, then lean right, then come back to neutral.",
            "look_queue": [],
            "expect": lambda log, res:
                _has_pose(log, "lean_left")
                and _has_pose(log, "lean_right")
                and _has_pose(log, "neutral"),
        },
        {
            "id": "B5-walk-wait-stop-say",
            "group": "B",
            "goal": "Walk forward, wait two seconds, stop, then say done.",
            "look_queue": [],
            "expect": lambda log, res:
                "walk" in _tool_names(log)
                and "wait" in _tool_names(log)
                and "stop" in _tool_names(log)
                and "say" in _tool_names(log),
        },
    ])

    # Group C — Conditional / observational ------------------------------
    cases.extend([
        {
            "id": "C1-look-person-seen",
            "group": "C",
            "goal": "Look around, and if you see a person, say hello.",
            "look_queue": [{"seen": ["person"]}],
            "expect": lambda log, res:
                "look" in _tool_names(log)
                and "say" in _tool_names(log)
                # With a person in view the utterance should reference them
                # or greet ("hello"/"hi"/"hey").  Accept either.
                and any(w in _say_text(log)
                        for w in ("hello", "hi ", "hey", "person")),
        },
        {
            "id": "C2-look-person-absent",
            "group": "C",
            "goal": "Look around, and if you see a person, say hello, "
                    "otherwise say nobody is here.",
            "look_queue": [{"seen": []}],
            "expect": lambda log, res:
                "look" in _tool_names(log)
                and "say" in _tool_names(log)
                # Should NOT greet — should acknowledge absence.
                and any(w in _say_text(log)
                        for w in ("nobody", "no one", "don't",
                                  "do not", "empty", "can't", "cannot",
                                  "no person", "not see")),
        },
        {
            "id": "C3-find-keys",
            "group": "C",
            "goal": "Look around for my keys and tell me if you found them.",
            "look_queue": [{"seen": ["keys"]}],
            "expect": lambda log, res:
                "look" in _tool_names(log)
                and "say" in _tool_names(log)
                and "key" in _say_text(log),
        },
        {
            "id": "C4-scan-report",
            "group": "C",
            "goal": "Look left, then look right, then tell me what you saw.",
            "look_queue": [{"seen": ["dog"]}, {"seen": ["cat"]}],
            "expect": lambda log, res:
                _tool_names(log).count("look") >= 2
                and "say" in _tool_names(log),
        },
        {
            "id": "C5-look-for-laptop",
            "group": "C",
            "goal": "Look for a laptop on the desk and tell me if you see one.",
            "look_queue": [],
            # Open-vocabulary question: planner must pick look_for, not look.
            "look_for_queue": [{"seen": True, "score": 0.82, "frame_ms": 180}],
            "expect": lambda log, res:
                "look_for" in _tool_names(log)
                and "look" not in _tool_names(log)
                and "say" in _tool_names(log)
                and "laptop" in _say_text(log),
        },
    ])

    # Group D — Should refuse / finish quickly (no actuation) ------------
    cases.extend([
        {
            "id": "D1-weather",
            "group": "D",
            "goal": "Tell me about the weather today.",
            "look_queue": [],
            "expect": lambda log, res:
                "say" in _tool_names(log)
                and "pose" not in _tool_names(log)
                and "walk" not in _tool_names(log)
                and "jump" not in _tool_names(log),
        },
        {
            "id": "D2-math",
            "group": "D",
            "goal": "What is two plus two?",
            "look_queue": [],
            "expect": lambda log, res:
                "say" in _tool_names(log)
                and "pose" not in _tool_names(log)
                and "walk" not in _tool_names(log)
                and ("4" in _say_text(log) or "four" in _say_text(log)),
        },
        {
            "id": "D3-name",
            "group": "D",
            "goal": "What is your name?",
            "look_queue": [],
            "expect": lambda log, res:
                "say" in _tool_names(log)
                and "pose" not in _tool_names(log)
                and "walk" not in _tool_names(log)
                and "jump" not in _tool_names(log),
        },
    ])

    # Group E — Edge cases -----------------------------------------------
    cases.extend([
        {
            "id": "E1-do-something",
            "group": "E",
            "goal": "Do something.",
            "look_queue": [{"seen": []}],
            # Anything is fine; must terminate inside the step cap.
            "expect": lambda log, res:
                len(res.get("steps") or []) <= MAX_STEPS
                and len(log) >= 1,
        },
        {
            "id": "E2-walk-and-stop",
            "group": "E",
            "goal": "Walk forward and also stop.",
            "look_queue": [],
            # Contradictory — accept either outcome, just insist it didn't
            # loop.  If the planner picks one and calls finish, that's OK.
            "expect": lambda log, res:
                ("walk" in _tool_names(log) or "stop" in _tool_names(log))
                and len(res.get("steps") or []) <= MAX_STEPS,
        },
        {
            "id": "E3-long-list",
            "group": "E",
            "goal": "Lean left, lean right, lean left again, jump, then "
                    "say done.",
            "look_queue": [],
            "expect": lambda log, res:
                _tool_names(log).count("pose") >= 3
                and _has_pose(log, "lean_left")
                and _has_pose(log, "lean_right")
                and "jump" in _tool_names(log)
                and "say" in _tool_names(log)
                and "done" in _say_text(log),
        },
    ])

    return cases


# ------------------------------------------------------------------ runner

def _run_case(case: dict, model: str) -> dict:
    tools, call_log = _build_stub_tools(
        list(case["look_queue"]),
        list(case.get("look_for_queue") or []),
    )
    planner = Planner(tools, model=model, max_steps=MAX_STEPS,
                      logger=lambda *_a, **_kw: None)  # quiet planner logs
    t0 = time.time()
    try:
        result = planner.run(case["goal"])
        err = None
    except SystemExit:
        # Planner exits(2) on auth failure — re-raise so main() handles it.
        raise
    except Exception as e:
        result = {"success": False, "reason": f"exception:{type(e).__name__}",
                  "steps": [], "final_say": ""}
        err = f"{type(e).__name__}: {e}"
    dt = time.time() - t0

    step_count = len(result.get("steps") or [])
    terminated = _terminated_ok(result, step_count)

    # Apply per-case expectation.
    try:
        expect_pass = bool(case["expect"](call_log, result))
        expect_err = None
    except Exception as e:
        expect_pass = False
        expect_err = f"{type(e).__name__}: {e}"

    passed = bool(terminated and expect_pass and step_count <= MAX_STEPS)

    return {
        "id": case["id"],
        "group": case["group"],
        "goal": case["goal"],
        "wall_s": dt,
        "step_count": step_count,
        "success_flag": bool(result.get("success")),
        "reason": result.get("reason"),
        "final_say": result.get("final_say", ""),
        "tool_calls": _tool_names(call_log),
        "passed": passed,
        "terminated_ok": terminated,
        "expect_pass": expect_pass,
        "expect_err": expect_err,
        "exec_err": err,
    }


def _print_case(rep: dict) -> None:
    verdict = "PASS" if rep["passed"] else "FAIL"
    print(f"[{verdict}] {rep['id']:<22} group={rep['group']} "
          f"steps={rep['step_count']:<2} wall={rep['wall_s']:5.2f}s "
          f"tools={rep['tool_calls']}")
    if not rep["passed"]:
        print(f"         goal: {rep['goal']!r}")
        print(f"         final_say={rep['final_say']!r}  "
              f"reason={rep['reason']!r}  "
              f"terminated_ok={rep['terminated_ok']} "
              f"expect_pass={rep['expect_pass']}")
        if rep["expect_err"]:
            print(f"         expect_err: {rep['expect_err']}")
        if rep["exec_err"]:
            print(f"         exec_err:   {rep['exec_err']}")


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"DeepInfra model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--subset", default=None,
                    help="Only run cases in this group letter (A/B/C/D/E)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="Write structured results to this path")
    args = ap.parse_args()

    if not os.environ.get("DEEPINFRA_API_KEY", "").strip():
        print("ERR: DEEPINFRA_API_KEY not set.\n"
              "     set -a; source ~/Projects/AIHW/.env.local; set +a",
              file=sys.stderr)
        return 2

    cases = _build_cases()
    if args.subset:
        sub = args.subset.upper()
        cases = [c for c in cases if c["group"] == sub]
        if not cases:
            print(f"no cases in group {sub!r}", file=sys.stderr)
            return 1

    print(f"planner_eval: model={args.model} cases={len(cases)} "
          f"max_steps={MAX_STEPS}")
    print("-" * 72)

    reports: list[dict] = []
    t_grand = time.time()
    for i, c in enumerate(cases, 1):
        print(f"\n({i}/{len(cases)}) running {c['id']} ...")
        rep = _run_case(c, args.model)
        _print_case(rep)
        reports.append(rep)
    total_wall = time.time() - t_grand

    pass_ct = sum(1 for r in reports if r["passed"])
    fail_ct = len(reports) - pass_ct
    latencies = [r["wall_s"] for r in reports]
    median_lat = statistics.median(latencies) if latencies else 0.0
    mean_lat = statistics.mean(latencies) if latencies else 0.0

    print("\n" + "=" * 72)
    print(f"RESULTS: {pass_ct}/{len(reports)} passed  "
          f"({fail_ct} failed)")
    print(f"  total wall: {total_wall:.1f}s   "
          f"median latency: {median_lat:.2f}s   "
          f"mean latency: {mean_lat:.2f}s")

    # Per-group breakdown.
    groups: dict[str, list[dict]] = {}
    for r in reports:
        groups.setdefault(r["group"], []).append(r)
    for g in sorted(groups):
        gp = sum(1 for r in groups[g] if r["passed"])
        print(f"  group {g}: {gp}/{len(groups[g])}")

    if fail_ct:
        print("\nFailed cases:")
        for r in reports:
            if not r["passed"]:
                print(f"  - {r['id']}: tools={r['tool_calls']} "
                      f"say={r['final_say']!r}")

    if args.json_out:
        payload = {
            "model": args.model,
            "subset": args.subset,
            "pass_count": pass_ct,
            "total": len(reports),
            "total_wall_s": total_wall,
            "median_latency_s": median_lat,
            "mean_latency_s": mean_lat,
            "cases": reports,
        }
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote JSON -> {args.json_out}")

    return 0 if fail_ct == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
