#!/usr/bin/env python3
"""
agent_eval.py  —  end-to-end regression harness for GoalKeeper+Planner.

Kills roast points #12 and #13.  planner_eval.py tests the planner in
isolation; goal_keeper's self-test runs ONE scripted scenario.  This
harness drives 10 timeline scenarios through the REAL DeepInfra
tool-calling loop with inert stub tools, verifying (tool history, final
state, followup count, final say) against per-scenario expectations.

Usage:
    set -a; source ~/Projects/AIHW/.env.local; set +a
    python3 scripts/agent_eval.py
    python3 scripts/agent_eval.py --json out.json
    python3 scripts/agent_eval.py --only S4   # single scenario

Ground rules:
    - No modifications to GoalKeeper or Planner to make tests pass.
    - Inert stub tools only; no hardware, camera, or mic.
    - 30 s wall cap per scenario; whole eval should be <5 min.

Exit codes:
    0  all scenarios passed
    1  one or more scenarios failed
    2  DEEPINFRA_API_KEY missing
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from typing import Any, Callable

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from demo.robot_planner import Planner, DEFAULT_MODEL  # noqa: E402
from demo.goal_keeper import GoalKeeper  # noqa: E402


SCENARIO_WALL_CAP_S = 30.0
MAX_STEPS = 8  # per planner turn — keep each turn cheap
MAX_FOLLOWUPS = 5


# ----------------------------------------------------------------- stub tools

def _build_stub_tools() -> tuple[dict, list[dict]]:
    """Inert tool stubs: just append to call_log and return {ok:True}.
    Identical surface to planner_eval.py — critical for parity."""
    call_log: list[dict] = []

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
        # No scripted queue — look returns empty; if a scenario needs vision
        # data it delivers it via the event timeline, not via look().
        return {"ok": True, "seen": []}

    def look_for(query: str) -> dict:
        call_log.append({"tool": "look_for", "args": {"query": query}})
        return {"ok": True, "seen": False, "score": 0.0, "frame_ms": 0}

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


# ----------------------------------------------------------------- helpers

def _tool_names(log: list[dict]) -> list[str]:
    return [c["tool"] for c in log]


def _has_pose(log: list[dict], name: str) -> bool:
    return any(c["tool"] == "pose" and c["args"].get("name") == name
               for c in log)


def _say_text(log: list[dict]) -> str:
    bits = [c["args"].get("text", "") for c in log if c["tool"] == "say"]
    return " ".join(bits).lower()


def _initial_say_text(log: list[dict], cutoff_count: int) -> str:
    """Concatenate every say() utterance that happened during the first
    `cutoff_count` tool calls (the initial planner turn).  Used by S9 to
    inspect the very first say() before any event fires."""
    bits = []
    for c in log[:cutoff_count]:
        if c["tool"] == "say":
            bits.append(c["args"].get("text", ""))
    return " ".join(bits).lower()


# ----------------------------------------------------------------- scenarios

def _build_scenarios() -> list[dict]:
    """Each scenario:
        id, desc, goal, events, expect(ctx) -> (bool, msg)
    Events are list of (delay_s, event_dict | special).  Specials:
        {"cancel": True}         -> call keeper.cancel()
        {"new_goal": "..."}      -> call keeper.set_goal(new)
        {"type": ..., ...}       -> keeper.on_event(dict)
    """
    scenarios: list[dict] = []

    # --- S1 ------------------------------------------------------------
    scenarios.append({
        "id": "S1",
        "desc": "Goal completes on initial run, no events needed",
        "goal": "bow once and say hello",
        "events": [],
        "expect": lambda ctx: (
            _has_pose(ctx["tool_log"], "bow_front")
            and "say" in _tool_names(ctx["tool_log"])
            and ctx["state"] == "done"
            and ctx["followups"] == 0,
            "state={state!r} followups={followups} "
            "tools={tools}".format(
                state=ctx["state"], followups=ctx["followups"],
                tools=_tool_names(ctx["tool_log"]))
        ),
    })

    # --- S2 ------------------------------------------------------------
    def _s2_expect(ctx):
        tools = _tool_names(ctx["tool_log"])
        saytext = _say_text(ctx["tool_log"])
        # At least 1 followup triggered, or state done.  Accept either:
        # model may have preempted by saying hello on initial turn if the
        # event fired quickly enough, but we require the say()-with-hello.
        ok = (
            ctx["followups"] >= 1
            and "say" in tools
            and ("hello" in saytext or "hi" in saytext or "hey" in saytext)
            and ctx["state"] in ("done", "active", "capped")
        )
        return ok, (f"state={ctx['state']!r} followups={ctx['followups']} "
                    f"say={saytext!r} tools={tools}")

    scenarios.append({
        "id": "S2",
        "desc": "Standing goal triggers on relevant event",
        "goal": "say hello when you see a person",
        "events": [(1.0, {"type": "vision", "class": "person",
                          "conf": 0.9})],
        "expect": _s2_expect,
    })

    # --- S3 ------------------------------------------------------------
    def _s3_expect(ctx):
        ok = ctx["followups"] == 0
        return ok, (f"followups={ctx['followups']} state={ctx['state']!r} "
                    f"tools={_tool_names(ctx['tool_log'])}")

    scenarios.append({
        "id": "S3",
        "desc": "Irrelevant event ignored (bicycle, goal is person)",
        "goal": "say hello when you see a person",
        "events": [(0.5, {"type": "vision", "class": "bicycle",
                          "conf": 0.8})],
        "expect": _s3_expect,
    })

    # --- S4 ------------------------------------------------------------
    def _s4_expect(ctx):
        ok = (ctx["followups"] <= MAX_FOLLOWUPS
              and ctx["state"] in ("capped", "done", "active"))
        return ok, (f"state={ctx['state']!r} followups={ctx['followups']} "
                    f"(cap={MAX_FOLLOWUPS})")

    scenarios.append({
        "id": "S4",
        "desc": "Followup cap enforced against burst of 10 events",
        "goal": "greet every person you see",
        "events": [(0.2 + 0.15 * i,
                    {"type": "vision", "class": "person", "conf": 0.9,
                     "idx": i})
                   for i in range(10)],
        "expect": _s4_expect,
    })

    # --- S5 ------------------------------------------------------------
    def _s5_expect(ctx):
        # Record the followup count at cancel time so a late person event
        # after cancel doesn't look like a followup from the first goal.
        post_cancel_followups = ctx.get("_s5_followups_after_cancel")
        cur = ctx["followups"]
        late_fired = (post_cancel_followups is not None
                      and cur > post_cancel_followups)
        ok = ctx["state"] == "cancelled" and not late_fired
        return ok, (f"state={ctx['state']!r} followups={cur} "
                    f"post_cancel_followups={post_cancel_followups}")

    scenarios.append({
        "id": "S5",
        "desc": "Cancel mid-goal halts followups",
        "goal": "wait for a person and say hello",
        "events": [
            (0.5, {"cancel": True}),
            (1.5, {"type": "vision", "class": "person", "conf": 0.9}),
        ],
        "expect": _s5_expect,
    })

    # --- S6 ------------------------------------------------------------
    def _s6_expect(ctx):
        ok = ctx["followups"] == 1
        return ok, (f"followups={ctx['followups']} (expected 1) "
                    f"state={ctx['state']!r}")

    scenarios.append({
        "id": "S6",
        "desc": "Mixed events: only laptop matches",
        "goal": "look for a laptop",
        "events": [
            (0.3, {"type": "vision", "class": "bicycle", "conf": 0.8}),
            (0.9, {"type": "vision", "class": "laptop", "conf": 0.9}),
            (1.5, {"type": "vision", "class": "chair", "conf": 0.8}),
        ],
        "expect": _s6_expect,
    })

    # --- S7 ------------------------------------------------------------
    def _s7_expect(ctx):
        saytext = _say_text(ctx["tool_log"])
        ok = (ctx["followups"] >= 1
              and any(w in saytext for w in
                      ("battery", "voltage", "power", "low", "charge")))
        return ok, (f"followups={ctx['followups']} say={saytext!r}")

    scenarios.append({
        "id": "S7",
        "desc": "Battery event triggers followup",
        "goal": "tell me if battery gets low",
        "events": [(0.5, {"type": "battery", "voltage": 6.4,
                          "low": True})],
        "expect": _s7_expect,
    })

    # --- S8 ------------------------------------------------------------
    def _s8_expect(ctx):
        cur_goal = ctx["current_goal"]
        ok = (cur_goal == "jump once and finish"
              and ctx["state"] == "done"
              and "jump" in _tool_names(ctx["tool_log"]))
        return ok, (f"state={ctx['state']!r} goal={cur_goal!r} "
                    f"tools={_tool_names(ctx['tool_log'])}")

    scenarios.append({
        "id": "S8",
        "desc": "New goal replaces active goal",
        "goal": "wait for a person to arrive",
        "events": [(0.5, {"new_goal": "jump once and finish"})],
        "expect": _s8_expect,
    })

    # --- S9 ------------------------------------------------------------
    def _s9_expect(ctx):
        # No events ever fire.  The initial say() must NOT greet.
        saytext = _say_text(ctx["tool_log"])
        greeted = any(w in saytext for w in ("hello", "hi ", "hey"))
        ok = (ctx["followups"] == 0
              and not greeted)
        return ok, (f"initial_say={saytext!r} greeted={greeted} "
                    f"followups={ctx['followups']} state={ctx['state']!r}")

    scenarios.append({
        "id": "S9",
        "desc": "Planner does NOT hallucinate hello without seeing person",
        "goal": "say hello when you see a person",
        "events": [],
        "expect": _s9_expect,
    })

    # --- S10 -----------------------------------------------------------
    def _s10_expect(ctx):
        saytext = _say_text(ctx["tool_log"])
        # Check that the followup references mug/cup/blue somehow.
        references_object = any(w in saytext for w in
                                ("mug", "cup", "blue", "ceramic"))
        # Alternatively, tool args may reference it (e.g. look_for query).
        args_refs = False
        for c in ctx["tool_log"]:
            if c["tool"] in ("look_for", "say"):
                text = " ".join(str(v).lower() for v in c["args"].values())
                if any(w in text for w in ("mug", "cup", "blue", "ceramic")):
                    args_refs = True
                    break
        ok = (ctx["followups"] >= 1
              and (references_object or args_refs))
        return ok, (f"followups={ctx['followups']} say={saytext!r} "
                    f"refs_object={references_object} refs_args={args_refs}")

    scenarios.append({
        "id": "S10",
        "desc": "Observation description passes through to followup",
        "goal": "find the blue coffee mug",
        "events": [(0.5, {"type": "vision", "class": "cup",
                          "description": "a blue ceramic mug",
                          "conf": 0.85})],
        "expect": _s10_expect,
    })

    return scenarios


# ----------------------------------------------------------------- runner

def _run_scenario(scn: dict, model: str) -> dict:
    """Run one scenario end-to-end.  Returns a report dict."""
    tools, call_log = _build_stub_tools()
    planner = Planner(tools, model=model, max_steps=MAX_STEPS,
                      logger=lambda *_a, **_kw: None)
    log_lines: list[str] = []
    keeper = GoalKeeper(
        planner,
        logger=lambda s: log_lines.append(s),
        max_followups=MAX_FOLLOWUPS,
    )

    # Scenario-local mutable state (for the special "cancel" / "new_goal"
    # events — they capture this dict via closure on the driver thread).
    state: dict = {
        "current_goal": scn["goal"],
        "followups_at_cancel": None,
    }

    # --- schedule events on a driver thread ---------------------------
    stop_driver = threading.Event()

    def _driver():
        t0 = time.time()
        for delay, ev in scn["events"]:
            # Sleep in small chunks so we can bail on stop_driver.
            target = t0 + delay
            while True:
                now = time.time()
                if now >= target or stop_driver.is_set():
                    break
                time.sleep(min(0.05, target - now))
            if stop_driver.is_set():
                return
            if not isinstance(ev, dict):
                continue
            if ev.get("cancel"):
                state["followups_at_cancel"] = keeper.status()["followups"]
                keeper.cancel()
            elif ev.get("new_goal"):
                ng = ev["new_goal"]
                state["current_goal"] = ng
                # Blocking first turn of the new goal (replaces active).
                keeper.set_goal(ng)
            else:
                keeper.on_event(ev)

    driver = threading.Thread(target=_driver, name=f"driver-{scn['id']}",
                              daemon=True)

    # --- run initial goal + driver + wait -----------------------------
    t0 = time.time()
    initial_tool_count_marker = {"count": 0}
    timed_out = False
    exec_err = None
    try:
        driver.start()
        initial = keeper.set_goal(scn["goal"])
        # Snapshot tool count right after initial turn — S9 inspects this.
        initial_tool_count_marker["count"] = len(call_log)

        # Wait until driver finishes scripting events, then wait_idle.
        driver_deadline = t0 + SCENARIO_WALL_CAP_S - 2.0
        driver.join(timeout=max(0.1, driver_deadline - time.time()))
        if driver.is_alive():
            stop_driver.set()
            driver.join(timeout=1.0)
            timed_out = True

        # Quiesce in-flight followups up to remaining budget.
        remaining = max(0.5, (t0 + SCENARIO_WALL_CAP_S) - time.time())
        keeper.wait_idle(timeout=remaining)
    except SystemExit:
        raise  # auth failure
    except Exception as e:
        exec_err = f"{type(e).__name__}: {e}"

    wall = time.time() - t0
    st = keeper.status()

    ctx = {
        "tool_log": call_log,
        "initial_say": _initial_say_text(call_log,
                                         initial_tool_count_marker["count"]),
        "state": st["state"],
        "followups": st["followups"],
        "current_goal": state["current_goal"],
        "_s5_followups_after_cancel": state["followups_at_cancel"],
        "last_result": st["last_result"],
        "initial_result": initial,
    }

    try:
        expect_result = scn["expect"](ctx)
        if isinstance(expect_result, tuple):
            passed, msg = expect_result
        else:
            passed, msg = bool(expect_result), ""
        expect_err = None
    except Exception as e:
        passed = False
        msg = ""
        expect_err = f"{type(e).__name__}: {e}"

    return {
        "id": scn["id"],
        "desc": scn["desc"],
        "goal": scn["goal"],
        "wall_s": wall,
        "timed_out": timed_out,
        "state": st["state"],
        "followups": st["followups"],
        "current_goal": state["current_goal"],
        "tool_calls": _tool_names(call_log),
        "tool_log": call_log,
        "final_say": (st["last_result"] or {}).get("final_say", ""),
        "initial_say": ctx["initial_say"],
        "passed": bool(passed) and not exec_err,
        "msg": msg,
        "exec_err": exec_err,
        "expect_err": expect_err,
        "logs_tail": log_lines[-8:],
    }


def _print_report(rep: dict) -> None:
    verdict = "PASS" if rep["passed"] else "FAIL"
    print(f"[{verdict}] {rep['id']:<3} followups={rep['followups']:<2} "
          f"state={rep['state']:<10} wall={rep['wall_s']:5.2f}s  "
          f"{rep['desc']}")
    if not rep["passed"]:
        print(f"       goal: {rep['goal']!r}")
        print(f"       msg : {rep['msg']}")
        print(f"       tools: {rep['tool_calls']}")
        print(f"       final_say: {rep['final_say']!r}")
        if rep["initial_say"]:
            print(f"       initial_say: {rep['initial_say']!r}")
        if rep["exec_err"]:
            print(f"       exec_err: {rep['exec_err']}")
        if rep["expect_err"]:
            print(f"       expect_err: {rep['expect_err']}")
        if rep["logs_tail"]:
            print(f"       logs tail:")
            for ln in rep["logs_tail"]:
                print(f"         {ln}")


def main() -> int:
    ap = argparse.ArgumentParser(description="End-to-end GoalKeeper eval")
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"DeepInfra model id (default: {DEFAULT_MODEL})")
    ap.add_argument("--only", default=None,
                    help="Only run this scenario id (e.g. S4)")
    ap.add_argument("--json", dest="json_out", default=None,
                    help="Write structured results to this path")
    args = ap.parse_args()

    if not os.environ.get("DEEPINFRA_API_KEY", "").strip():
        print("ERR: DEEPINFRA_API_KEY not set.\n"
              "     set -a; source ~/Projects/AIHW/.env.local; set +a",
              file=sys.stderr)
        return 2

    scenarios = _build_scenarios()
    if args.only:
        scenarios = [s for s in scenarios if s["id"] == args.only.upper()]
        if not scenarios:
            print(f"no scenario matches id {args.only!r}", file=sys.stderr)
            return 1

    print(f"agent_eval: model={args.model} scenarios={len(scenarios)} "
          f"max_followups={MAX_FOLLOWUPS} wall_cap={SCENARIO_WALL_CAP_S}s")
    print("-" * 72)

    reports: list[dict] = []
    t_grand = time.time()
    for i, scn in enumerate(scenarios, 1):
        print(f"\n({i}/{len(scenarios)}) {scn['id']}: {scn['desc']}")
        rep = _run_scenario(scn, args.model)
        _print_report(rep)
        reports.append(rep)
    total_wall = time.time() - t_grand

    pass_ct = sum(1 for r in reports if r["passed"])
    fail_ct = len(reports) - pass_ct
    latencies = [r["wall_s"] for r in reports]
    median_lat = statistics.median(latencies) if latencies else 0.0

    print("\n" + "=" * 72)
    print(f"RESULTS: {pass_ct}/{len(reports)} passed  ({fail_ct} failed)")
    print(f"  total wall: {total_wall:.1f}s   "
          f"median scenario: {median_lat:.2f}s")

    if fail_ct:
        print("\nFailed scenarios:")
        for r in reports:
            if not r["passed"]:
                print(f"  - {r['id']}: followups={r['followups']} "
                      f"state={r['state']} tools={r['tool_calls']}")
                print(f"    msg: {r['msg']}")

    if args.json_out:
        payload = {
            "model": args.model,
            "pass_count": pass_ct,
            "total": len(reports),
            "total_wall_s": total_wall,
            "median_scenario_s": median_lat,
            "max_followups": MAX_FOLLOWUPS,
            "scenarios": [
                {k: v for k, v in r.items()
                 if k not in ("tool_log",)}  # trim verbose
                for r in reports
            ],
        }
        with open(args.json_out, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nWrote JSON -> {args.json_out}")

    return 0 if fail_ct == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
