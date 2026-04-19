#!/usr/bin/env python3
"""
goal_keeper.py  —  persistent-goal wrapper around robot_planner.Planner.

Kills "the planner returns and the goal is gone" (roast point #11): the
reactive planner runs once per utterance and then forgets.  GoalKeeper
parks the current goal in a standing slot, keeps state across turns,
and re-engages the planner when a vision / IMU / battery event arrives
that's relevant to the goal.

Design choices worth calling out:

 1. on_event() is event-driven, NOT polled.  Callers (drain_vision_events,
    the battery watcher) hand us a single event each time one shows up;
    there is no background "anything new?" loop.

 2. Re-plans run on a DAEMON THREAD so the main loop's event pump
    (drain_vision_events in robot_daemon.py) is not blocked for the
    5-30 s the LLM can take.  A single in-flight slot with a lock — no
    queue — because chasing every event would thrash the planner.
    Events arriving while a follow-up is running are dropped; the model
    already has the goal context and we'll pick up the next relevant one.

 3. Relevance scoring for v1 is KEYWORD OVERLAP between the event's
    class/phrase and the goal text.  Cheap, deterministic, zero API
    calls.  An LLM-based relevance check would double cost and round-trip
    latency on every frame.  v2 can upgrade this behind the same
    interface if needed.

 4. `max_followups` is a HARD CAP (default 5).  Exceeding it sets
    state='capped' and further events are ignored — prevents a stuck
    goal from burning the API budget indefinitely.

Public API:

    gk = GoalKeeper(planner, logger=print)
    gk.set_goal("wait for a person and say hello")  # blocking first turn
    gk.on_event({"type": "vision", "class": "person", ...})  # returns quick
    gk.status()   # {"goal":..., "state":..., "followups":..., "last_result":...}
    gk.cancel()   # user said "never mind"
"""
from __future__ import annotations

import re
import sys
import threading
import time
from typing import Any, Callable


# Tokens that are too common to be useful for keyword relevance.  Matching
# "the" or "is" to every event would fire follow-ups on every vision frame.
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
    """Lowercase alphanum tokens minus stopwords.  Short tokens (<3 chars) are
    dropped so "a" / "to" / "of" don't accidentally count as relevant."""
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
    """Wraps a Planner with a single-slot standing-goal state machine.

    States:
        "idle"      — no goal set yet (or cleared).
        "active"    — goal set, watching for relevant events.
        "done"      — planner called finish(reason=...) — goal complete.
        "cancelled" — user called cancel().
        "capped"    — hit max_followups without finishing.
        "error"     — planner raised on first turn.
    """

    # States where on_event() does nothing.
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
        # Single in-flight follow-up slot.  We drop concurrent events
        # rather than queue them up.
        self._inflight: bool = False
        # Change counter so daemon can cheaply tell if status changed.
        self._version: int = 0

    # ---------------------------------------------------------------- public

    def set_goal(self, text: str) -> dict:
        """Install a new standing goal and run the planner once synchronously.

        Returns the PlanResult from that first turn (also stored internally).
        Replaces any previously-active goal without warning — the daemon's
        voice loop hands us whatever the user just said.
        """
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

        # First turn runs synchronously so callers see the initial result
        # (matches the prior reactive behaviour for the first utterance).
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
        # If the planner explicitly finished (completed / cancelled / refused),
        # the goal is done.  Otherwise we stay 'active' and watch for events.
        terminal = (result.get("success")
                    and reason.lower() not in ("", "ok", "watching", "waiting"))
        with self._lock:
            if terminal:
                self._state = "done"
            # success==False + reason=='max_steps' is left as 'active' so a
            # follow-up event can rescue it.
            self._version += 1
        self._log(f"[goal] initial run done: success={result.get('success')} "
                  f"reason={reason!r} state={self._state}")
        return result

    def cancel(self) -> None:
        """User said 'never mind' / 'cancel'.  Goes inert immediately."""
        with self._lock:
            if self._state == "idle":
                return
            prior_goal = self._goal
            self._state = "cancelled"
            self._version += 1
        self._log(f"[goal] cancelled: {prior_goal!r}")

    def on_event(self, event: dict) -> None:
        """Called from the daemon's event hot path for every vision / IMU /
        battery event.  Cheap and non-blocking: relevance check runs inline,
        actual planner re-engagement runs on a background thread."""
        if not isinstance(event, dict):
            return
        with self._lock:
            if self._state in self._INERT:
                return
            if self._goal is None:
                return
            if self._inflight:
                # Already re-planning — drop this event rather than queue.
                return
            if self._followups >= self._max:
                self._log("[goal] follow-up cap reached")
                self._state = "capped"
                self._version += 1
                return
            if not self._is_relevant(event):
                return
            # Arm the in-flight slot before we release the lock.
            self._inflight = True
            self._followups += 1
            goal = self._goal
            prior = self._last_result
            fcount = self._followups
            self._version += 1

        # Launch the follow-up off the hot path.  Daemon thread so it never
        # blocks shutdown.
        th = threading.Thread(
            target=self._run_followup,
            args=(goal, event, prior, fcount),
            name=f"goalkeeper-followup-{fcount}",
            daemon=True,
        )
        th.start()

    def status(self) -> dict:
        """Snapshot for the UI / state publisher.  Cheap, lock-guarded."""
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
        """Monotonic counter — daemon can compare to detect status changes
        without diffing the whole status dict every frame."""
        with self._lock:
            return self._version

    # --------------------------------------------------------------- internals

    def _is_relevant(self, event: dict) -> bool:
        """Keyword-overlap relevance.  Battery / IMU events are always
        relevant when a goal is active — the operator almost certainly wants
        to know (e.g. 'battery low while walking' means the planner might
        decide to stop).  Vision events have to mention a class/phrase the
        goal text references."""
        etype = str(event.get("type", "")).lower()
        if etype in ("battery", "imu"):
            return True
        # Extract any class/label/phrase fields vision events carry.
        bits: list[str] = []
        for key in ("class", "label", "phrase", "query", "text"):
            v = event.get(key)
            if isinstance(v, str) and v:
                bits.append(v)
            elif isinstance(v, (list, tuple)):
                bits.extend(str(x) for x in v)
        if not bits:
            return False
        ev_tokens = _tokens(" ".join(bits))
        overlap = self._goal_tokens & ev_tokens
        # Also accept surface substring matches for multi-word goal phrases
        # that tokenise oddly (e.g. 'red mug' vs vision event {"phrase":"red mug"}).
        if not overlap:
            goal_lc = (self._goal or "").lower()
            for b in bits:
                if b and b.lower() in goal_lc:
                    return True
            return False
        return True

    def _run_followup(self, goal: str, event: dict, prior: dict | None,
                      fcount: int) -> None:
        """Thread target — runs planner.run(goal, observation={...}) and
        updates state.  Never raises; all errors are logged + recorded."""
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
            # If the planner terminated on this turn with anything other
            # than a soft "keep watching" reason, mark done.
            terminal = (result.get("success")
                        and reason.lower() not in ("", "ok",
                                                   "watching", "waiting"))
            # "ignored" is a terminal reason — the model decided this event
            # didn't satisfy the goal.  We still stay active for the next
            # relevant event (don't mark done, don't mark capped), but DO
            # decrement against the cap since we used a slot.
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
    """Build a fake Planner whose .run() returns scripted results so the
    self-test doesn't hit the network.  Not used by the real self-test below
    (which does hit DeepInfra if the key is set), but useful for debugging."""
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
    """Scripted integration test.  Uses a deterministic scripted stub planner
    so the test is hermetic — no network, no API key needed, passes every
    time provided the GoalKeeper wiring is sound.  Also verifies against the
    real DeepInfra planner when DEEPINFRA_API_KEY is set (printed separately
    so a transient API hiccup doesn't cause the whole script to fail)."""
    import os

    # ---------- Part 1: scripted stub path (always runs, hermetic) ----------
    print("=== GoalKeeper self-test (scripted stub) ===")
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

    # t=0: no one visible.  Irrelevant event (bicycle) should be ignored.
    gk.on_event({"type": "vision", "class": "bicycle",
                 "ts": time.time(), "conf": 0.9})
    # tiny wait to let any accidental thread start (should not happen).
    time.sleep(0.2)
    st1 = gk.status()
    assert st1["followups"] == 0, \
        f"bicycle event should not have triggered a follow-up; got {st1}"

    # t=2: person arrives — should re-engage planner and produce hello+finish.
    gk.on_event({"type": "vision", "class": "person",
                 "ts": time.time(), "conf": 0.95})
    # Wait for the daemon thread to complete.
    deadline = time.time() + 5.0
    while time.time() < deadline:
        st = gk.status()
        if st["state"] in ("done", "capped", "error") and not gk._inflight:
            break
        time.sleep(0.05)
    final = gk.status()
    last_say = (final["last_result"] or {}).get("final_say", "")
    print(f"final: state={final['state']} followups={final['followups']} "
          f"last_say={last_say!r}")
    stub_ok = (final["state"] == "done"
               and final["followups"] == 1
               and "hello" in last_say.lower())
    print("scripted-stub: " + ("PASS" if stub_ok else "FAIL"))
    if not stub_ok:
        return 1

    # ---------- Part 2: live DeepInfra (optional, informational only) --------
    have_key = bool(os.environ.get("DEEPINFRA_API_KEY", "").strip())
    if not have_key:
        print("\n(live DeepInfra path skipped — no DEEPINFRA_API_KEY)")
        return 0

    # Build a planner with stubbed tools — we don't want to actuate real
    # hardware from a self-test, and the planner's tool callables must exist.
    print("\n=== GoalKeeper self-test (live DeepInfra, informational) ===")
    if have_key:
        # Use the real tool-calling loop against DeepInfra.
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
        from robot_planner import Planner  # noqa: E402

        call_log: list[dict] = []

        def _rec(name, **kw):
            call_log.append({"tool": name, "args": kw})
            print(f"  [tool] {name}({kw})")
            return {"ok": True}

        tools = {
            "pose": lambda name, duration_ms=400: _rec("pose", name=name,
                                                      duration_ms=duration_ms),
            "walk": lambda stride=150, step=400, **_: _rec("walk", stride=stride,
                                                           step=step),
            "stop": lambda: _rec("stop"),
            "jump": lambda: _rec("jump"),
            "look": lambda direction: _rec("look", direction=direction),
            "look_for": lambda query: {"ok": True, "seen": False,
                                       "score": 0.0, "frame_ms": 0},
            "say":  lambda text: _rec("say", text=text),
            "wait": lambda seconds: _rec("wait", seconds=seconds),
        }
        planner = Planner(tools)
    else:
        return 0

    logs_live: list[str] = []
    gk = GoalKeeper(planner,
                    logger=lambda s: (logs_live.append(s), print(s))[-1],
                    max_followups=3)

    goal = "wait for a person to arrive and say hello when they do"
    print(f"\n--- goal: {goal!r} ---")
    initial = gk.set_goal(goal)
    print(f"initial result: success={initial.get('success')} "
          f"reason={initial.get('reason')!r} "
          f"final_say={initial.get('final_say')!r}")

    st0 = gk.status()
    print(f"status after set_goal: state={st0['state']} "
          f"followups={st0['followups']}")

    if st0["state"] == "done":
        # Model already said hello on the first turn (some models interpret
        # the goal optimistically); this is a valid outcome.  We already
        # verified the wiring in Part 1 so this path exits 0 regardless.
        print("live: goal completed on initial turn (no event needed)")
        return 0

    # Simulate an irrelevant event first — should be ignored.
    print("\n--- live irrelevant event: bicycle ---")
    gk.on_event({"type": "vision", "class": "bicycle",
                 "ts": time.time(), "conf": 0.9})
    time.sleep(0.3)

    # Relevant event: person arrives.
    print("\n--- relevant event: person ---")
    gk.on_event({"type": "vision", "class": "person",
                 "ts": time.time(), "conf": 0.95})

    # Wait for the follow-up thread to finish (max 40s — planner can take
    # ~15 s per turn).
    deadline = time.time() + 40.0
    while time.time() < deadline:
        st = gk.status()
        if st["state"] in ("done", "capped", "error") and not gk._inflight:
            break
        time.sleep(0.25)

    final = gk.status()
    print(f"\nfinal status: state={final['state']} "
          f"followups={final['followups']} "
          f"last_say={(final['last_result'] or {}).get('final_say')!r}")

    said = " ".join(
        c["args"].get("text", "") for c in call_log if c["tool"] == "say"
    ).lower()
    greeted = any(w in said for w in ("hello", "hi ", "hi!", "hey"))
    print(f"\nlive: state={final['state']} followups={final['followups']} "
          f"greeted={greeted} say={said!r}")
    # The live DeepInfra path is informational: a transient API hiccup or
    # a differently-phrased model response must not fail the whole
    # self-test.  Part 1 already proved the wiring.
    return 0


if __name__ == "__main__":
    sys.exit(_run_self_test())
