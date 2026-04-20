#!/data/data/com.termux/files/usr/bin/env python3
"""
phone_goal_keeper.py  —  Termux port of demo/goal_keeper.py.

Persistent-goal wrapper around phone_planner.Planner.  Verbatim port of
the laptop-side GoalKeeper; the only delta is the live-self-test import
(phone_planner instead of robot_planner).

Public API (unchanged):

    gk = GoalKeeper(planner, logger=print)
    gk.set_goal("walk until you see a person")
    gk.on_event({"type": "vision", "class": "person", ...})
    gk.status()       # snapshot dict
    gk.cancel()
    gk.wait_idle(30)  # block until no follow-up is in flight
"""
from __future__ import annotations

import re
import sys
import threading
import time
from typing import Any, Callable


_STOPWORDS: set[str] = {
    "the", "a", "an", "and", "or", "but", "if", "then", "when", "to", "for",
    "of", "in", "on", "at", "by", "with", "from", "up", "down", "is", "are",
    "was", "were", "be", "been", "being", "do", "does", "did", "have", "has",
    "had", "it", "its", "this", "that", "these", "those", "i", "you", "we",
    "they", "he", "she", "them", "his", "her", "my", "your", "our", "their",
    "me", "him", "us", "here", "there", "now", "tell", "say", "said",
    "around", "about", "please", "just", "let", "go", "see", "look", "watch",
    "wait", "something", "someone", "anyone", "anything", "one", "two",
    "back", "forward", "some", "any", "all", "no", "not", "out", "so",
    "also", "will", "can", "should", "could", "would", "get", "got", "want",
}


def _tokens(text: str) -> set[str]:
    out: set[str] = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_-]*", (text or "").lower()):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        out.add(tok)
    return out


def _eprint(*a, **kw) -> None:
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


class GoalKeeper:
    """Single-slot standing-goal state machine around a Planner.

    States:
        "idle"      — no goal set yet (or cleared).
        "active"    — goal set, watching for relevant events.
        "done"      — planner called finish() — goal complete.
        "cancelled" — user called cancel().
        "capped"    — hit max_followups without finishing.
        "error"     — planner raised on first turn.
    """

    _INERT: frozenset[str] = frozenset({"idle", "done", "cancelled", "capped",
                                        "error"})

    def __init__(
        self,
        planner: Any,
        *,
        logger: Callable[[str], None] | None = None,
        max_followups: int = 5,
    ) -> None:
        self._planner = planner
        self._log: Callable[[str], None] = logger or _eprint
        self._max = int(max_followups)

        self._lock = threading.Lock()
        self._goal: str | None = None
        self._goal_tokens: set[str] = set()
        self._state: str = "idle"
        self._followups: int = 0
        self._last_result: dict | None = None
        self._set_at: float = 0.0
        self._inflight: bool = False
        self._version: int = 0

    # ---------------------------------------------------------------- public

    def set_goal(self, text: str) -> dict:
        """Install a new standing goal and run the planner once synchronously."""
        goal = (text or "").strip()
        if not goal:
            self._log("[goal] set_goal('') ignored")
            return {"success": False, "reason": "empty_goal",
                    "steps": [], "final_say": ""}

        with self._lock:
            prior = self._goal
            self._goal = goal
            self._goal_tokens = _tokens(goal)
            self._state = "active"
            self._followups = 0
            self._last_result = None
            self._set_at = time.time()
            self._version += 1
        if prior:
            self._log(f"[goal] replaced prior goal: {prior!r} -> {goal!r}")
        else:
            self._log(f"[goal] set: {goal!r}  tokens={sorted(self._goal_tokens)}")

        try:
            result = self._planner.run(goal)
        except Exception as e:
            self._log(f"[goal] initial planner error: {type(e).__name__}: {e}")
            result = {"success": False, "reason": f"error:{type(e).__name__}",
                      "steps": [], "final_say": ""}
            with self._lock:
                self._last_result = result
                self._state = "error"
                self._version += 1
            return result

        with self._lock:
            self._last_result = result
        reason = str(result.get("reason") or "")
        terminal = (result.get("success")
                    and reason.lower() not in ("", "ok", "watching", "waiting"))
        with self._lock:
            if terminal:
                self._state = "done"
            self._version += 1
        self._log(f"[goal] initial run done: success={result.get('success')} "
                  f"reason={reason!r} state={self._state}")
        return result

    def cancel(self) -> None:
        with self._lock:
            if self._state == "idle":
                return
            prior_goal = self._goal
            self._state = "cancelled"
            self._version += 1
        self._log(f"[goal] cancelled: {prior_goal!r}")

    def on_event(self, event: dict) -> None:
        """Event-driven entry point from vision / battery / IMU watchers."""
        if not isinstance(event, dict):
            return
        with self._lock:
            if self._state in self._INERT:
                return
            if self._goal is None:
                return
            if self._inflight:
                return
            if self._followups >= self._max:
                self._log("[goal] follow-up cap reached")
                self._state = "capped"
                self._version += 1
                return
            if not self._is_relevant(event):
                return
            self._inflight = True
            self._followups += 1
            goal = self._goal
            prior = self._last_result
            fcount = self._followups
            self._version += 1

        th = threading.Thread(
            target=self._run_followup,
            args=(goal, event, prior, fcount),
            name=f"goalkeeper-followup-{fcount}",
            daemon=True,
        )
        th.start()

    def status(self) -> dict:
        with self._lock:
            return {
                "goal": self._goal,
                "state": self._state,
                "followups": self._followups,
                "max_followups": self._max,
                "last_result": self._last_result,
                "set_at": self._set_at,
                "version": self._version,
            }

    def version(self) -> int:
        with self._lock:
            return self._version

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Block until no follow-up is in flight (or timeout elapses)."""
        deadline = time.time() + max(0.0, float(timeout))
        while time.time() < deadline:
            with self._lock:
                if not self._inflight:
                    return True
            time.sleep(0.05)
        with self._lock:
            return not self._inflight

    # --------------------------------------------------------------- internals

    def _is_relevant(self, event: dict) -> bool:
        etype = str(event.get("type", "")).lower()
        if etype in ("battery", "imu"):
            return True
        bits: list[str] = []
        for key in ("class", "label", "phrase", "query", "text", "description"):
            v = event.get(key)
            if isinstance(v, str) and v:
                bits.append(v)
            elif isinstance(v, (list, tuple)):
                bits.extend(str(x) for x in v)
        if not bits:
            return False
        ev_tokens = _tokens(" ".join(bits))
        overlap = self._goal_tokens & ev_tokens
        if not overlap:
            goal_lc = (self._goal or "").lower()
            for b in bits:
                if b and b.lower() in goal_lc:
                    return True
            return False
        return True

    def _run_followup(self, goal: str, event: dict, prior: dict | None,
                      fcount: int) -> None:
        self._log(f"[goal] follow-up #{fcount} for goal={goal!r} "
                  f"event={event}")
        try:
            result = self._planner.run(
                goal, observation={"event": event, "prior_result": prior},
            )
        except Exception as e:
            self._log(f"[goal] follow-up planner error: "
                      f"{type(e).__name__}: {e}")
            result = {"success": False, "reason": f"error:{type(e).__name__}",
                      "steps": [], "final_say": ""}

        reason = str(result.get("reason") or "")
        with self._lock:
            self._last_result = result
            self._inflight = False
            terminal = (result.get("success")
                        and reason.lower() not in ("", "ok",
                                                   "watching", "waiting"))
            if terminal and reason.lower() != "ignored":
                self._state = "done"
            elif self._followups >= self._max and self._state == "active":
                self._state = "capped"
                self._log("[goal] follow-up cap reached")
            self._version += 1
        self._log(f"[goal] follow-up #{fcount} done: "
                  f"success={result.get('success')} reason={reason!r} "
                  f"state={self._state}")


# =============================================================== self-test

def _stub_planner_factory():
    """Scripted stub planner for hermetic self-test."""
    calls: list[dict] = []

    class _Stub:
        def run(self, goal: str, observation: dict | None = None) -> dict:
            calls.append({"goal": goal, "observation": observation})
            if observation and isinstance(observation.get("event"), dict):
                ev = observation["event"]
                if ev.get("class") == "person":
                    return {"success": True, "reason": "completed",
                            "steps": [{"tool": "say",
                                       "args": {"text": "hello"}}],
                            "final_say": "hello"}
            return {"success": False, "reason": "watching",
                    "steps": [{"tool": "say",
                               "args": {"text": "I am watching"}}],
                    "final_say": "I am watching"}

    return _Stub(), calls


def _run_self_test() -> int:
    print("=== GoalKeeper (phone) self-test (scripted stub) ===")
    stub, calls = _stub_planner_factory()
    logs: list[str] = []
    gk = GoalKeeper(stub, logger=lambda s: (logs.append(s), print(s))[-1],
                    max_followups=3)
    goal = "wait for a person to arrive and say hello when they do"
    print(f"goal: {goal!r}")
    r0 = gk.set_goal(goal)
    st0 = gk.status()
    print(f"initial: state={st0['state']} final_say={r0.get('final_say')!r}")
    assert st0["state"] == "active", f"expected active, got {st0['state']!r}"

    gk.on_event({"type": "vision", "class": "bicycle",
                 "ts": time.time(), "conf": 0.9})
    time.sleep(0.2)
    st1 = gk.status()
    assert st1["followups"] == 0, \
        f"bicycle event should not have triggered a follow-up; got {st1}"

    gk.on_event({"type": "vision", "class": "person",
                 "ts": time.time(), "conf": 0.95})
    gk.wait_idle(timeout=5.0)
    final = gk.status()
    last_say = (final["last_result"] or {}).get("final_say", "")
    print(f"final: state={final['state']} followups={final['followups']} "
          f"last_say={last_say!r}")
    ok = (final["state"] == "done"
          and final["followups"] == 1
          and "hello" in last_say.lower())
    print("scripted-stub: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_run_self_test())
