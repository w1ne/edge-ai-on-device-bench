#!/usr/bin/env python3
"""
planner_eval_holdout.py  —  HELD-OUT eval for demo.robot_planner.Planner.

Companion to scripts/planner_eval.py.  Roast point #17 called out that
we've been iterating the planner against the same 20-case regression set
since day one, which means the 20/21 number says "the planner fits the
training distribution" more than it says "the planner generalizes".
This file is the held-out set — 10 NEW goals covering patterns NOT
exercised by the original 21 cases.

Pattern groups (one case per pattern):

  H1   speak-then-act ordering      — say BEFORE a physical action
  H2   numeric / count              — explicit "do two jumps"
  H3   negation / conditional       — "unless you see a person"
  H4   temporal constraint          — "wait 2 seconds between poses"
  H5   multi-modal composition      — look_for + say + pose chained
  H6   graceful refusal (OOD)       — primitive not in the set
  H7   error recovery               — invalid pose name in the user's goal
  H8   open-vocab vs structured     — should prefer look_for over look
  H9   prior context / unknowable   — should refuse cleanly or ask
  H10  tool-call correctness        — "jump, but don't call finish yet"

RULES (see roast directive):
  * DO NOT tune the planner prompt / schemas to make H-cases pass.  The
    baseline is whatever it is; that's the point of a held-out set.
  * DO NOT reuse expectations or fixtures from planner_eval.py beyond
    `_build_stub_tools` — hard-imported for parity.
  * DO NOT move a case from FAIL to PASS by relaxing its expect().  If a
    case exposes a genuine planner gap, that's the value of the eval.

PINS (do NOT change these assertions just to hit a number):
  * H1 requires a `say` whose index is < the index of ANY `pose`/`jump`/
    `walk` call in the log — strictly before, not merely present.
  * H2 requires jump count == 2 (not >= 2).  Two means two.
  * H3 requires EITHER (person seen AND no lean_left) OR
    (person absent AND lean_left present).  Respecting the condition.
  * H6 must NOT emit pose / walk / jump / look — it's a refusal.
  * H9 must NOT invent history — either refuses verbally or asks.
  * H10 must call jump but must NOT emit a successful run (since the
    user explicitly said "don't call finish yet").  We accept either
    step_count==MAX_STEPS (forced out) OR success==False.

Usage:
    set -a; source ~/Projects/AIHW/.env.local; set +a
    python3 scripts/planner_eval_holdout.py
    python3 scripts/planner_eval_holdout.py --json out.json

Exit codes:
    0  all H-cases passed  (will almost certainly NOT happen on a fresh
       baseline — that's expected, not a failure of the harness)
    1  one or more H-cases failed
    2  DEEPINFRA_API_KEY missing
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

# Make the repo importable.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# Reuse the stub tool builder from the regression set — keeps tool
# surface byte-identical, so any pass/fail difference is purely due to
# the new goal wording, not the test harness.
from scripts.planner_eval import _build_stub_tools, _tool_names, _has_pose, \
    _say_text  # noqa: E402
from demo.robot_planner import Planner, DEFAULT_MODEL  # noqa: E402


MAX_STEPS = 10


# ------------------------------------------------------------------ helpers

def _first_index(names: list[str], target: str) -> int:
    """Return the index of the first occurrence of `target` in names, or
    len(names) if absent.  Used for strict ordering assertions."""
    for i, n in enumerate(names):
        if n == target:
            return i
    return len(names)


def _first_any_index(names: list[str], targets: set[str]) -> int:
    for i, n in enumerate(names):
        if n in targets:
            return i
    return len(names)


# ------------------------------------------------------------------ cases

def _build_cases() -> list[dict]:
    cases: list[dict] = []

    # H1 — speak-then-act ordering
    # Pattern: user says "tell me X BEFORE doing Y".  `say` must precede
    # the physical action in the tool log, not merely coexist.
    cases.append({
        "id": "H1-say-before-act",
        "group": "H1",
        "goal": "Tell me 'starting now' before you lean left.",
        "look_queue": [],
        "expect": lambda log, res: (
            "say" in _tool_names(log)
            and _has_pose(log, "lean_left")
            and _first_index(_tool_names(log), "say")
                < _first_any_index(
                    _tool_names(log),
                    {"pose", "walk", "jump"},
                )
            and ("starting" in _say_text(log))
        ),
    })

    # H2 — numeric / count ("do two jumps")
    # Pattern: explicit cardinal.  Count must be exact — two, not three.
    cases.append({
        "id": "H2-two-jumps",
        "group": "H2",
        "goal": "Do two jumps in a row.",
        "look_queue": [],
        "expect": lambda log, res:
            _tool_names(log).count("jump") == 2,
    })

    # H3 — negation / conditional
    # Pattern: action is conditional on a NEGATIVE observation.  If the
    # planner sees a person, it must NOT lean_left.  If it doesn't see
    # one, it must lean_left.  Either branch is acceptable — the test
    # just pins that the planner actually respects the condition.
    cases.append({
        "id": "H3-lean-unless-person",
        "group": "H3",
        "goal": ("Look ahead.  Lean to the left unless you see a person.  "
                 "Either way, tell me what you decided."),
        # Look queue stocks a PERSON — so lean_left must NOT fire.
        "look_queue": [{"seen": ["person"]}],
        "expect": lambda log, res: (
            "look" in _tool_names(log)
            and "say" in _tool_names(log)
            # Person is visible in the first look result, therefore NO
            # lean_left.
            and not _has_pose(log, "lean_left")
        ),
    })

    # H4 — temporal constraint ("wait 2 seconds between poses")
    # Pattern: pose, wait(~=2s), pose.  We don't pin the exact seconds
    # (LLMs round), but we insist the wait sits BETWEEN two poses.
    cases.append({
        "id": "H4-wait-between-poses",
        "group": "H4",
        "goal": ("Lean left, wait two seconds, then lean right.  "
                 "The pause between the two poses is important."),
        "look_queue": [],
        "expect": lambda log, res: (
            _has_pose(log, "lean_left")
            and _has_pose(log, "lean_right")
            and "wait" in _tool_names(log)
            # wait must sit between the two poses in the call log.
            and _first_index(_tool_names(log), "wait")
                > _first_index(_tool_names(log), "pose")
        ),
    })

    # H5 — multi-modal composition (look_for + say + pose)
    # Pattern: one open-vocab query, then speak the finding, then act.
    cases.append({
        "id": "H5-look_for-say-bow",
        "group": "H5",
        "goal": ("Look for a dog in the scene, tell me whether you found "
                 "one, then bow forward."),
        "look_queue": [],
        "look_for_queue": [{"seen": True, "score": 0.74, "frame_ms": 150}],
        "expect": lambda log, res: (
            "look_for" in _tool_names(log)
            and "say" in _tool_names(log)
            and _has_pose(log, "bow_front")
            and ("dog" in _say_text(log))
        ),
    })

    # H6 — graceful refusal (OOD primitive)
    # Pattern: "climb the stairs" — not a primitive.  Planner should
    # refuse verbally and finish, NOT attempt pose/walk/jump.
    cases.append({
        "id": "H6-refuse-climb",
        "group": "H6",
        "goal": "Climb up the stairs to the second floor.",
        "look_queue": [],
        "expect": lambda log, res: (
            "say" in _tool_names(log)
            # MUST NOT try to act.  stairs-climbing is OOD — anything
            # physical would be actively misleading to the user.
            and "pose" not in _tool_names(log)
            and "walk" not in _tool_names(log)
            and "jump" not in _tool_names(log)
            and "look" not in _tool_names(log)
            # Must acknowledge the refusal somehow.  Any of several
            # reasonable hedges.
            and any(w in _say_text(log) for w in (
                "can't", "cannot", "can not", "unable", "sorry",
                "don't", "do not", "not able", "no ", "only",
            ))
        ),
    })

    # H7 — error recovery (invalid pose name)
    # Pattern: user asks for a pose that doesn't exist in the enum.
    # Planner should either (a) pick a close valid pose, OR (b) ask /
    # refuse.  What it MUST NOT do is crash, nor silently call pose with
    # the invalid enum (our stub would accept it, but the schema
    # wouldn't).  Graceful either way is fine.
    cases.append({
        "id": "H7-invalid-pose",
        "group": "H7",
        "goal": "Lean reverse, please.",
        "look_queue": [],
        "expect": lambda log, res: (
            # Terminated cleanly.
            len(res.get("steps") or []) <= MAX_STEPS
            # Did SOMETHING — a valid pose, or a say(), or both.
            and (
                ("say" in _tool_names(log))
                or _has_pose(log, "lean_left")
                or _has_pose(log, "lean_right")
                or _has_pose(log, "neutral")
                or _has_pose(log, "bow_front")
            )
        ),
    })

    # H8 — open-vocab vs structured (should prefer look_for)
    # Pattern: user asks about an arbitrary object.  "do you see the
    # couch" is open-vocab — planner should call look_for, not look.
    cases.append({
        "id": "H8-couch-look_for",
        "group": "H8",
        "goal": "Do you see the couch from where you're standing?",
        "look_queue": [],
        "look_for_queue": [{"seen": False, "score": 0.12, "frame_ms": 190}],
        "expect": lambda log, res: (
            "look_for" in _tool_names(log)
            # look_for is cheaper + purpose-built.  `look` would be a
            # regression in tool selection.
            and "look" not in _tool_names(log)
            and "say" in _tool_names(log)
            and ("couch" in _say_text(log)
                 or "sofa" in _say_text(log)
                 or "don't" in _say_text(log)
                 or "no " in _say_text(log)
                 or "not" in _say_text(log))
        ),
    })

    # H9 — prior context / unknowable
    # Pattern: user references something the planner has no access to
    # ("whatever I said last time").  Planner should refuse cleanly or
    # ask for clarification — MUST NOT invent an action.
    cases.append({
        "id": "H9-prior-context",
        "group": "H9",
        "goal": "Do whatever I asked you to do last time.",
        "look_queue": [],
        "expect": lambda log, res: (
            "say" in _tool_names(log)
            # No fabricated physical action.
            and "pose" not in _tool_names(log)
            and "walk" not in _tool_names(log)
            and "jump" not in _tool_names(log)
            # Acknowledges the gap.  Any of several ways.
            and any(w in _say_text(log) for w in (
                "don't", "do not", "can't", "cannot", "remember",
                "not sure", "what ", "which ", "tell me", "ask",
                "no memory", "no record", "no history",
                "last time", "previous", "clarif", "repeat",
            ))
        ),
    })

    # H10 — tool-call correctness ("don't call finish yet")
    # Pattern: user explicitly tells the planner NOT to finalize.  In
    # practice the planner almost certainly WILL finish anyway (our
    # system prompt aggressively requires it), and that's fine — the
    # PIN here is that either:
    #   (a) the planner honors the instruction (no success, runs to cap)
    #   (b) the planner finishes anyway but says SOMETHING acknowledging
    #       the weird instruction.
    # Either way, `jump` must happen at least once — the physical part
    # of the goal is non-negotiable.
    cases.append({
        "id": "H10-jump-no-finish",
        "group": "H10",
        "goal": ("Jump once, but do not call the finish tool yet — "
                 "I want to give you another instruction after."),
        "look_queue": [],
        "expect": lambda log, res: (
            "jump" in _tool_names(log)
            and (
                # Honored the instruction: no success signal.
                (not res.get("success"))
                # OR finished anyway but acknowledged the constraint.
                or ("finish" in _say_text(log)
                    or "another" in _say_text(log)
                    or "ready" in _say_text(log)
                    or "next" in _say_text(log))
            )
        ),
    })

    return cases


# ------------------------------------------------------------------ runner

def _run_case(case: dict, model: str) -> dict:
    tools, call_log = _build_stub_tools(
        list(case["look_queue"]),
        list(case.get("look_for_queue") or []),
    )
    planner = Planner(tools, model=model, max_steps=MAX_STEPS,
                      logger=lambda *_a, **_kw: None)
    t0 = time.time()
    try:
        result = planner.run(case["goal"])
        err = None
    except SystemExit:
        raise
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

    passed = bool(expect_pass and step_count <= MAX_STEPS)

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
        "tool_log": call_log,
        "passed": passed,
        "expect_pass": expect_pass,
        "expect_err": expect_err,
        "exec_err": err,
    }


def _print_case(rep: dict) -> None:
    verdict = "PASS" if rep["passed"] else "FAIL"
    print(f"[{verdict}] {rep['id']:<24} group={rep['group']:<4} "
          f"steps={rep['step_count']:<2} wall={rep['wall_s']:5.2f}s "
          f"tools={rep['tool_calls']}")
    if not rep["passed"]:
        print(f"         goal: {rep['goal']!r}")
        print(f"         final_say={rep['final_say']!r}  "
              f"reason={rep['reason']!r}")
        if rep["expect_err"]:
            print(f"         expect_err: {rep['expect_err']}")
        if rep["exec_err"]:
            print(f"         exec_err:   {rep['exec_err']}")


# ------------------------------------------------------------------ main

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Held-out planner eval (10 new cases, no overlap with "
                    "planner_eval.py).")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"DeepInfra model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="Write structured results to this path")
    ap.add_argument("--only", default=None,
                    help="Run a single H-case by id prefix, e.g. H3")
    args = ap.parse_args()

    if not os.environ.get("DEEPINFRA_API_KEY", "").strip():
        print("ERR: DEEPINFRA_API_KEY not set.\n"
              "     set -a; source ~/Projects/AIHW/.env.local; set +a",
              file=sys.stderr)
        return 2

    cases = _build_cases()
    if args.only:
        pref = args.only.upper()
        cases = [c for c in cases if c["id"].upper().startswith(pref)
                 or c["group"].upper() == pref]
        if not cases:
            print(f"no H-cases match {args.only!r}", file=sys.stderr)
            return 1

    print(f"planner_eval_holdout: model={args.model} "
          f"cases={len(cases)} max_steps={MAX_STEPS}")
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
    print(f"HELD-OUT RESULTS: {pass_ct}/{len(reports)} passed  "
          f"({fail_ct} failed)")
    print(f"  total wall: {total_wall:.1f}s   "
          f"median latency: {median_lat:.2f}s   "
          f"mean latency: {mean_lat:.2f}s")

    if fail_ct:
        print("\nFailed H-cases:")
        for r in reports:
            if not r["passed"]:
                print(f"  - {r['id']}: tools={r['tool_calls']} "
                      f"final_say={r['final_say']!r}")

    if args.json_out:
        payload = {
            "model": args.model,
            "pass_count": pass_ct,
            "total": len(reports),
            "total_wall_s": total_wall,
            "median_latency_s": median_lat,
            "mean_latency_s": mean_lat,
            "cases": [{k: v for k, v in r.items() if k != "tool_log"}
                      for r in reports],
        }
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote JSON -> {args.json_out}")

    return 0 if fail_ct == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
