#!/usr/bin/env python3
"""
fallback_eval.py  —  coverage eval for demo.planner_fallback.FallbackPlanner.

Runs the exact 21 cases from scripts/planner_eval.py (5 groups: A simple,
B multi-step, C conditional/vision, D factual, E edge) against the
rule-based fallback — NO LLM in the loop.  Reports N/21 coverage.

This is the HONEST degradation number for when DeepInfra is unreachable.
Cases the fallback correctly refuses (success=False,
reason='fallback_refused' or similar) still count as FAIL here because
the point is to measure coverage, not correctness of refusal.

Usage:
    python3 scripts/fallback_eval.py
    python3 scripts/fallback_eval.py --subset A
    python3 scripts/fallback_eval.py --json out.json

Exit codes:
    0 — eval ran (coverage may still be partial; this isn't a gate)
    1 — harness error
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from demo.planner_fallback import FallbackPlanner  # noqa: E402
# Reuse the exact cases + stub tools from the real eval so we stay
# honest about "same test suite".
from scripts.planner_eval import (  # noqa: E402
    _build_cases, _build_stub_tools, _tool_names, MAX_STEPS,
)


def _run_case(case: dict) -> dict:
    tools, call_log = _build_stub_tools(
        list(case["look_queue"]),
        list(case.get("look_for_queue") or []),
    )
    fp = FallbackPlanner(tools, max_steps=MAX_STEPS)
    t0 = time.time()
    try:
        result = fp.run(case["goal"])
        err = None
    except Exception as e:
        result = {"success": False, "reason": f"exception:{type(e).__name__}",
                  "steps": [], "final_say": ""}
        err = f"{type(e).__name__}: {e}"
    dt = time.time() - t0

    step_count = len(result.get("steps") or [])
    try:
        expect_pass = bool(case["expect"](call_log, result))
        expect_err = None
    except Exception as e:
        expect_pass = False
        expect_err = f"{type(e).__name__}: {e}"

    # For the fallback eval we require both expect_pass AND a successful
    # plan — a correct refusal is NOT coverage.
    passed = bool(result.get("success") and expect_pass
                  and step_count <= MAX_STEPS + 1)  # +1 for internal 'finish'
    return {
        "id": case["id"],
        "group": case["group"],
        "goal": case["goal"],
        "wall_ms": int(dt * 1000),
        "step_count": step_count,
        "success_flag": bool(result.get("success")),
        "reason": result.get("reason"),
        "final_say": result.get("final_say", ""),
        "tool_calls": _tool_names(call_log),
        "passed": passed,
        "expect_pass": expect_pass,
        "expect_err": expect_err,
        "exec_err": err,
    }


def _print_case(rep: dict) -> None:
    verdict = "PASS" if rep["passed"] else ("REFUSED" if not rep["success_flag"] else "FAIL")
    print(f"[{verdict:<7}] {rep['id']:<22} group={rep['group']} "
          f"wall={rep['wall_ms']:>3}ms tools={rep['tool_calls']}")
    if verdict != "PASS":
        print(f"         goal: {rep['goal']!r}")
        print(f"         reason={rep['reason']!r} final_say={rep['final_say']!r}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--subset", default=None,
                    help="Only run cases in this group letter (A/B/C/D/E)")
    ap.add_argument("--json", dest="json_out", default=None)
    args = ap.parse_args()

    cases = _build_cases()
    if args.subset:
        sub = args.subset.upper()
        cases = [c for c in cases if c["group"] == sub]
        if not cases:
            print(f"no cases in group {sub!r}", file=sys.stderr)
            return 1

    print(f"fallback_eval: zero-LLM rule-based planner, cases={len(cases)}")
    print("-" * 72)

    reports: list[dict] = []
    t_grand = time.time()
    for i, c in enumerate(cases, 1):
        rep = _run_case(c)
        _print_case(rep)
        reports.append(rep)
    total_wall = time.time() - t_grand

    pass_ct = sum(1 for r in reports if r["passed"])
    refuse_ct = sum(1 for r in reports
                    if not r["passed"] and not r["success_flag"])
    wrong_ct = sum(1 for r in reports
                   if not r["passed"] and r["success_flag"])
    latencies = [r["wall_ms"] for r in reports]

    print("\n" + "=" * 72)
    print(f"COVERAGE: {pass_ct}/{len(reports)}  "
          f"(refused: {refuse_ct}, wrong-plan: {wrong_ct})")
    if latencies:
        print(f"  per-goal latency: median {statistics.median(latencies):.1f}ms  "
              f"mean {statistics.mean(latencies):.1f}ms  "
              f"max {max(latencies)}ms  total {total_wall*1000:.0f}ms")

    # Per-group breakdown.
    groups: dict[str, list[dict]] = {}
    for r in reports:
        groups.setdefault(r["group"], []).append(r)
    for g in sorted(groups):
        gp = sum(1 for r in groups[g] if r["passed"])
        print(f"  group {g}: {gp}/{len(groups[g])}")

    # Which patterns the fallback missed (one-liner each).
    missed = [r for r in reports if not r["passed"]]
    if missed:
        print("\nMissed patterns:")
        for r in missed:
            print(f"  - {r['id']:<22} reason={r['reason']!r:<25} "
                  f"goal={r['goal']!r}")

    if args.json_out:
        payload = {
            "total": len(reports),
            "pass_count": pass_ct,
            "refused_count": refuse_ct,
            "wrong_plan_count": wrong_ct,
            "cases": reports,
        }
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote JSON -> {args.json_out}")

    # Don't fail CI on partial coverage — this is a measurement, not a gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
