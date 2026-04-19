#!/usr/bin/env python3
"""
planner_fallback.py  —  zero-LLM rule-based fallback for `robot_planner.Planner`.

Why this exists
    Today the robot's multi-step planner is 100% dependent on DeepInfra
    (`Qwen/Qwen2.5-72B-Instruct` over their OpenAI-compatible endpoint).
    If DeepInfra 5xx's, rate-limits, or the network is down, the agent
    loop vanishes and every long utterance ("lean left, wait, then
    neutral") returns `noop`.  See docs/RELIABILITY_AUDIT point #18.

    On-phone Gemma-3 is NOT a credible tool-caller on our 9-tool schema
    (docs/LLM_BAKEOFF.md — 6/21 on DeepInfra, worse on-device).  Bundling
    a local CPU LLM (llama-cpp-python + Qwen-2.5-3B-GGUF) was considered
    but rejected for this pass: the install surface is real, and the
    honest MVP is a deterministic rule-based decomposer that handles
    common goal shapes and refuses everything else.

    This module is that decomposer.  Stdlib only.  <10 ms per goal.

Shape
    Returns the same PlanResult shape as Planner.run:
        {"success": bool, "reason": str, "steps": list[dict], "final_say": str}

    On genuine refusal (goal doesn't match any pattern), returns
    success=False, reason='fallback_refused'.  The daemon treats that as
    "say 'I can only handle simple commands right now'" or queues the
    utterance for retry when DeepInfra comes back.

Coverage
    Designed against `scripts/planner_eval.py`'s 21 cases.  Run
    `scripts/fallback_eval.py` for the measured number.  Do not claim
    100%.  Groups D (factual/verbal) and part of C (conditional /
    open-vocab) are intentionally left to the real planner.

Non-goals
    - Sentiment-based "do the right thing" inference.  That's the LLM's job.
    - Handling novel phrasings we haven't seen in the eval set.
    - Any kind of ML or learning.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable


# ------------------------------------------------------------------ regexes
#
# Pose lookup.  Keys are canonical servo poses; values are regex alternatives
# that trigger them.  We tokenize the goal and match phrases rather than
# substrings so "lean right" is NOT mis-matched for "lean" in "leaning
# forward doesn't count".  Order matters: longer matches first.

_POSE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Canonical two-word phrases first so "lean left" beats "left" alone.
    ("lean_left",  re.compile(r"\blean(?:ing)?\s+(?:to\s+the\s+)?left\b", re.I)),
    ("lean_right", re.compile(r"\blean(?:ing)?\s+(?:to\s+the\s+)?right\b", re.I)),
    ("bow_front",  re.compile(r"\b(?:bow(?:ing)?(?:\s+(?:forward|front|down))?|take\s+a\s+bow)\b", re.I)),
    ("neutral",    re.compile(r"\b(?:go\s+back\s+to\s+neutral|come\s+back\s+to\s+neutral|return\s+to\s+neutral|back\s+to\s+neutral|neutral(?:\s+pose)?|stand(?:\s+up)?\s+straight|reset\s+pose)\b", re.I)),
]

_JUMP_RE = re.compile(r"\bjump(?:\s+(?:once|again|now))?\b", re.I)
_STOP_RE = re.compile(r"\b(?:stop(?:\s+(?:walking|moving|now))?|halt|freeze)\b", re.I)
_WALK_RE = re.compile(r"\b(?:walk|start\s+walking|go\s+forward|walk\s+(?:forward|a\s+bit))\b", re.I)

_LOOK_DIR_RE = re.compile(
    r"\blook\s+(left|right|ahead|forward|up|down|around)\b", re.I
)
_LOOK_GENERIC_RE = re.compile(
    r"\b(?:look\s+around|what\s+do\s+you\s+see|scan(?:\s+the\s+room)?)\b", re.I
)

# "look for a <thing>", "do you see a <thing>", "is there a <thing>"
# We only capture the noun phrase loosely — it's used as the CLIP query.
_LOOK_FOR_RE = re.compile(
    r"\b(?:look\s+for|search\s+for|find|do\s+you\s+see|can\s+you\s+see|"
    r"is\s+there|are\s+there|spot)\s+(?:a\s+|an\s+|any\s+|some\s+|my\s+|the\s+)?"
    r"([A-Za-z][A-Za-z0-9 _\-]{0,40})",
    re.I,
)

_WAIT_RE = re.compile(
    r"\bwait\s+(?:for\s+)?(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|a\s+moment|a\s+bit|a\s+second)"
    r"(?:\s+(second|seconds|sec|secs|s|minute|minutes|min|m))?",
    re.I,
)

# Bare "say <text>" or "tell me <text>" patterns.  Capture the payload.
_SAY_RE = re.compile(
    r"\b(?:say|tell\s+me|announce|greet\s+me\s+with|speak)\s+(?:that\s+|me\s+)?"
    r"[\"']?([^,.!?\"']+?)[\"']?(?:[.!?]|$)",
    re.I,
)

# Word → number for wait durations.  Covers 1–10; anything else = 1.0s.
_WORD_NUMS: dict[str, float] = {
    "a moment": 1.0, "a bit": 1.0, "a second": 1.0,
    "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0,
    "six": 6.0, "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0,
}


# ------------------------------------------------------------------ helpers

def _parse_wait_seconds(m: re.Match[str]) -> float:
    raw = m.group(1).strip().lower()
    unit = (m.group(2) or "second").lower()
    try:
        n = float(raw)
    except ValueError:
        n = _WORD_NUMS.get(raw, 1.0)
    # Clamp to 0–5s like the daemon's _tool_wait does.
    if unit.startswith("min") or unit == "m":
        n = n * 60.0
    return max(0.0, min(5.0, float(n)))


def _split_segments(goal: str) -> list[str]:
    """Split the goal on ", then", "; then", " and then", " then ", ", and",
    and bare commas.  Normalise whitespace.  Segments keep their raw text
    so downstream regexes still work."""
    # Normalise the "and then" / "then" connectors into commas so a single
    # split on , gives us our segments.  Order of replacements matters:
    # longer first so we don't double-replace.
    s = goal.strip()
    if not s:
        return []
    # Handle "and also" as a light connector too (E2 'walk forward and also stop').
    s = re.sub(r"\s*,?\s*(?:and\s+then|then|and\s+also|also)\s+", ", ", s, flags=re.I)
    s = re.sub(r"\s+and\s+", ", ", s, flags=re.I)
    # Split and drop empties / trailing punctuation.
    parts = [p.strip(" .!?;:") for p in s.split(",")]
    return [p for p in parts if p]


def _extract_say_text(segment: str) -> str | None:
    m = _SAY_RE.search(segment)
    if not m:
        return None
    txt = m.group(1).strip().strip("\"'")
    if not txt:
        return None
    # If the captured payload is itself an action verb ("say hello",
    # payload='hello' — fine; "tell me what you saw" — payload='what you
    # saw', which we DON'T want to parrot back verbatim).  Bail if the
    # payload contains action-like stems so the caller falls to look+say.
    if re.search(r"\b(?:what\s+you\s+saw|what\s+you\s+see|what\s+you\s+did|"
                 r"if\s+you|when\s+you|you\s+saw|you\s+see|"
                 r"found\s+them|found\s+it)\b", txt, re.I):
        return None
    return txt


# Reflective "tell me what you did / what you saw / how you're feeling"
# phrases — the planner should emit a canned self-narration after the
# action rather than parrot the phrase.
_REFLECT_DID_RE = re.compile(
    r"\btell\s+me\s+(?:what\s+you\s+did|how\s+that\s+went)\b", re.I,
)
_REFLECT_SAW_RE = re.compile(
    r"\btell\s+me\s+what\s+you\s+(?:see|saw)\b", re.I,
)
_REFLECT_FOUND_RE = re.compile(
    r"\btell\s+me\s+if\s+you\s+(?:found|see|spot)\s+"
    r"(?:them|it|one|any|a\s+|an\s+)", re.I,
)
# "if you see <X>, say <Y>" and "otherwise say <Z>" branches.
_COND_IF_RE = re.compile(
    r"\bif\s+you\s+(?:see|spot|find)\s+(?:a\s+|an\s+|any\s+|the\s+)?"
    r"([A-Za-z][A-Za-z0-9 _-]{0,30}?)\s*,?\s*"
    r"(?:then\s+)?(?:say|tell\s+me|announce|greet\s+me\s+with|greet\s+them)\s+"
    r"[\"']?([^,.\"']+)",
    re.I,
)
_COND_ELSE_RE = re.compile(
    r"\botherwise\s+(?:say|tell\s+me|announce)\s+[\"']?([^,.\"']+)",
    re.I,
)
_ACT_HAPPY_RE = re.compile(r"\btell\s+me\s+(?:you(?:'re|\s+are)?\s+happy|how\s+happy)\b", re.I)
_ACT_READY_RE = re.compile(r"\btell\s+me\s+(?:you(?:'re|\s+are)?\s+ready)\b", re.I)


def _handle_primitive(segment: str, steps: list[dict], final_say: list[str],
                      tools: dict[str, Callable[..., dict]],
                      step_idx_ref: list[int]) -> bool:
    """Try to match one segment against a single primitive tool call.
    Returns True if we handled it (added to `steps`), False if we couldn't."""
    # Order: stop > jump > pose > walk > look_for > look > wait > say.
    # "stop" beats "walk" if both appear in one segment (E2 'walk and stop').
    # Actually for E2 we treat the WHOLE GOAL as a compound (split earlier),
    # so each segment is already one action. Per-segment: match first hit.

    # Pose takes priority — "lean left", "bow", "neutral" should not fall
    # through to bare verb matches.
    for name, pat in _POSE_PATTERNS:
        if pat.search(segment):
            return _emit(steps, tools, step_idx_ref, "pose", {"name": name})

    if _STOP_RE.search(segment):
        return _emit(steps, tools, step_idx_ref, "stop", {})

    if _JUMP_RE.search(segment):
        return _emit(steps, tools, step_idx_ref, "jump", {})

    # look_for has to come BEFORE look so "look for a cup" doesn't land on
    # the bare "look" path.
    mlf = _LOOK_FOR_RE.search(segment)
    if mlf:
        q = mlf.group(1).strip()
        # Strip trailing filler ("on the desk", "in the room") for a cleaner query.
        q = re.sub(r"\s+(?:on|in|at|near|by)\s+.*$", "", q, flags=re.I).strip()
        if q:
            return _emit(steps, tools, step_idx_ref, "look_for", {"query": q})

    mdir = _LOOK_DIR_RE.search(segment)
    if mdir:
        d = mdir.group(1).lower()
        if d == "forward":
            d = "ahead"
        return _emit(steps, tools, step_idx_ref, "look", {"direction": d})

    if _LOOK_GENERIC_RE.search(segment):
        return _emit(steps, tools, step_idx_ref, "look", {"direction": "ahead"})

    mw = _WAIT_RE.search(segment)
    if mw:
        sec = _parse_wait_seconds(mw)
        return _emit(steps, tools, step_idx_ref, "wait", {"seconds": sec})

    if _WALK_RE.search(segment):
        return _emit(steps, tools, step_idx_ref, "walk",
                     {"stride": 150, "step": 400})

    # Say-only.  Keep LAST because "tell me what you saw" would otherwise
    # absorb segments that we really wanted to route to look+say.
    txt = _extract_say_text(segment)
    if txt is not None:
        ok = _emit(steps, tools, step_idx_ref, "say", {"text": txt})
        if ok:
            final_say.append(txt)
        return ok

    return False


def _emit(steps: list[dict],
          tools: dict[str, Callable[..., dict]],
          step_idx_ref: list[int],
          name: str,
          args: dict[str, Any]) -> bool:
    """Invoke a tool and record the step.  Returns True on success."""
    fn = tools.get(name)
    if fn is None:
        steps.append({
            "step": step_idx_ref[0],
            "tool": name,
            "args": args,
            "result": {"ok": False, "error": f"unknown tool {name!r}"},
        })
        step_idx_ref[0] += 1
        return False
    try:
        out = fn(**args)
    except TypeError as e:
        out = {"ok": False, "error": f"bad args for {name}: {e}"}
    except Exception as e:
        out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    result = out if isinstance(out, dict) else {"ok": True, "value": out}
    steps.append({
        "step": step_idx_ref[0],
        "tool": name,
        "args": args,
        "result": result,
    })
    step_idx_ref[0] += 1
    return True


# ------------------------------------------------------------------ planner

class FallbackPlanner:
    """Zero-LLM emergency planner.

    Pattern-matches common goal shapes onto tool-call sequences.  Returns
    the same PlanResult shape as robot_planner.Planner.run so the daemon
    can drop it in when DeepInfra is unreachable.

    Coverage target: ~60–70 % of real-user goals.  Goals it can't match
    return success=False, reason='fallback_refused' so the caller can
    speak a degraded-mode apology or queue for retry.
    """

    def __init__(
        self,
        tools: dict[str, Callable[..., dict]],
        *,
        logger: Callable[[str], None] | None = None,
        max_steps: int = 8,
    ) -> None:
        self._tools = dict(tools)
        self._log: Callable[[str], None] = logger or (lambda *_a, **_kw: None)
        self._max_steps = int(max_steps)

    # -------------------------------------------------------------- public
    def run(self, goal: str, observation: dict | None = None) -> dict:
        """Turn `goal` into a sequence of tool calls.  `observation` is
        accepted for API compatibility with the real planner but the
        fallback always re-plans from the goal text — no event-aware
        re-engagement."""
        t0 = time.time()
        goal = (goal or "").strip()
        if not goal:
            return self._refuse("empty_goal", t0)

        segments = _split_segments(goal)
        if not segments:
            return self._refuse("empty_goal", t0)

        steps: list[dict] = []
        final_say: list[str] = []
        step_idx = [1]

        # Whole-goal conditional shape: "look around, if you see a person,
        # say hello (, otherwise say nobody)."  Detect BEFORE segment split
        # because the conditional spans commas.
        cond_handled = self._try_conditional_look(goal, steps, final_say,
                                                  step_idx)
        if cond_handled:
            return self._finish(steps, final_say, t0, reason="completed")

        # Special: if the WHOLE goal is a single "say X" / "tell me X" /
        # "what is your name" etc.  We handle a tiny set of factual
        # self-referential questions explicitly.  Anything else factual
        # goes to the refused path so the daemon can decide.
        if len(segments) == 1:
            handled = self._handle_whole_goal_special(
                segments[0], steps, final_say, step_idx,
            )
            if handled == "refused":
                return self._refuse("unknown_factual", t0)
            if handled == "done":
                # Already emitted say + finish.
                return self._finish(steps, final_say, t0, reason="completed")

        # Compound chain: one primitive per segment.  Refuse if any segment
        # fails to match — partial execution of an ambiguous multi-step
        # goal is worse than not trying.
        prior_action: tuple[str, dict] | None = None  # for reflective "tell me what you did"
        prior_look_seen: list[str] | None = None      # for "tell me what you saw"
        prior_lookfor: tuple[str, bool] | None = None # for "tell me if you found them"
        implicit_target: str | None = _extract_implicit_target(goal)  # "keys" etc.
        for seg in segments:
            if step_idx[0] > self._max_steps:
                self._log(f"[fallback] max_steps exceeded at seg={seg!r}")
                return self._refuse("max_steps", t0)

            # Reflective segments — they generate a say() derived from the
            # prior tool result rather than requiring the LLM's prose.
            if _REFLECT_SAW_RE.search(seg):
                text = self._narrate_look(prior_look_seen)
                _emit(steps, self._tools, step_idx, "say", {"text": text})
                final_say.append(text)
                continue
            if _REFLECT_FOUND_RE.search(seg) and prior_lookfor is not None:
                q, seen = prior_lookfor
                text = (f"Yes, I can see a {q}."
                        if seen else f"No, I don't see a {q}.")
                _emit(steps, self._tools, step_idx, "say", {"text": text})
                final_say.append(text)
                continue
            # "tell me if you found them" / "tell me if you see one" after a
            # plain look() — narrate using the implicit target + last seen.
            if (_REFLECT_FOUND_RE.search(seg)
                    and implicit_target
                    and prior_look_seen is not None):
                key = implicit_target.lower()
                hit = any(key in str(x).lower() for x in prior_look_seen)
                text = (f"Yes, I found your {implicit_target}."
                        if hit else f"No, I don't see your {implicit_target}.")
                _emit(steps, self._tools, step_idx, "say", {"text": text})
                final_say.append(text)
                continue
            if _REFLECT_DID_RE.search(seg) and prior_action is not None:
                text = self._narrate_action(prior_action)
                _emit(steps, self._tools, step_idx, "say", {"text": text})
                final_say.append(text)
                continue
            if _ACT_HAPPY_RE.search(seg):
                text = "I'm happy!"
                _emit(steps, self._tools, step_idx, "say", {"text": text})
                final_say.append(text)
                continue
            if _ACT_READY_RE.search(seg):
                text = "I'm ready."
                _emit(steps, self._tools, step_idx, "say", {"text": text})
                final_say.append(text)
                continue

            ok = _handle_primitive(seg, steps, final_say, self._tools, step_idx)
            if not ok:
                # Per-segment refusal bubbles up: we don't want to half-
                # execute "lean left, then do something weird".
                self._log(f"[fallback] unhandled segment: {seg!r}")
                return self._refuse("fallback_refused", t0,
                                    partial_steps=steps,
                                    partial_say=final_say)

            # Cache the last tool-bearing step so reflective segments later
            # in the chain can narrate it.
            last = steps[-1] if steps else None
            if last:
                t = last.get("tool")
                a = last.get("args") or {}
                r = last.get("result") or {}
                if t == "look":
                    prior_look_seen = list(r.get("seen") or [])
                elif t == "look_for":
                    prior_lookfor = (a.get("query", ""), bool(r.get("seen")))
                elif t in ("pose", "jump", "walk", "stop"):
                    prior_action = (t, dict(a))

        if not steps:
            return self._refuse("no_primitive_matched", t0)

        return self._finish(steps, final_say, t0, reason="completed")

    def _narrate_look(self, seen: list[str] | None) -> str:
        if not seen:
            return "I don't see anything notable."
        if len(seen) == 1:
            return f"I see a {seen[0]}."
        return "I see " + ", ".join(str(x) for x in seen) + "."

    def _narrate_action(self, action: tuple[str, dict]) -> str:
        name, args = action
        if name == "pose":
            pose = str(args.get("name", "")).replace("_", " ")
            if not pose:
                return "I moved."
            if pose.startswith("lean"):
                return f"I leaned to the {pose.split()[-1]}."
            if pose == "bow front":
                return "I bowed forward."
            if pose == "neutral":
                return "I returned to neutral."
            return f"I moved to {pose}."
        if name == "jump":
            return "I jumped."
        if name == "walk":
            return "I started walking."
        if name == "stop":
            return "I stopped."
        return "Done."

    def _try_conditional_look(
        self,
        goal: str,
        steps: list[dict],
        final_say: list[str],
        step_idx: list[int],
    ) -> bool:
        """Handle "look around, if you see a <X>, say <Y> (, otherwise say <Z>)".
        Returns True if the shape matched and was executed."""
        s = goal
        if not (_LOOK_GENERIC_RE.search(s) or _LOOK_DIR_RE.search(s)
                or re.search(r"\blook\s+around\b", s, re.I)):
            return False
        mif = _COND_IF_RE.search(s)
        if not mif:
            return False

        # Capture query and branches.
        target = mif.group(1).strip().lower()
        # Strip trailing filler words from the target ("a person," → "person").
        target = re.sub(r"[\s,]+$", "", target)
        if_text = mif.group(2).strip().strip("\"'")
        melse = _COND_ELSE_RE.search(s)
        else_text = melse.group(1).strip().strip("\"'") if melse else None

        # Execute look() first.  The stub returns {"ok":True,"seen":[...]}.
        _emit(steps, self._tools, step_idx, "look", {"direction": "ahead"})
        seen_list = []
        if steps and isinstance(steps[-1].get("result"), dict):
            seen_list = steps[-1]["result"].get("seen") or []

        # Classes in seen_list may be exact tokens like "person", or noun
        # phrases.  Match loosely: did any token contain our target word?
        target_key = target.split()[-1]  # "a person" → "person"
        hit = any(target_key in str(x).lower() for x in seen_list)

        if hit:
            text = if_text if if_text else f"Yes, I see a {target_key}."
        else:
            text = (else_text if else_text
                    else f"I don't see a {target_key}.")
        _emit(steps, self._tools, step_idx, "say", {"text": text})
        final_say.append(text)
        return True

    # -------------------------------------------------------------- helpers
    def _handle_whole_goal_special(
        self,
        seg: str,
        steps: list[dict],
        final_say: list[str],
        step_idx: list[int],
    ) -> str:
        """Handle whole-goal shapes that don't decompose into segments:
            * "say hi", "tell me you're ready"  — say-only
            * "what is your name" — hardcoded reply + finish
            * "look around and tell me what you see" — look + say

        Returns one of:
            "unhandled" — caller should try segment-wise decomposition
            "done"      — say + finish emitted, plan is complete
            "refused"   — this was clearly factual and we won't guess
        """
        s = seg.lower()

        # "what is your name?"  The daemon can override by wiring a
        # real identity string, but for fallback we say something honest.
        if re.search(r"\bwhat(?:'s| is)\s+your\s+name\b", s):
            _emit(steps, self._tools, step_idx, "say",
                  {"text": "I'm your robot. Name me whatever you like."})
            final_say.append("I'm your robot. Name me whatever you like.")
            return "done"

        # "how are you" — friendly cheap reply.
        if re.search(r"\bhow\s+are\s+you\b", s):
            _emit(steps, self._tools, step_idx, "say",
                  {"text": "I'm good, thanks. How can I help?"})
            final_say.append("I'm good, thanks. How can I help?")
            return "done"

        # "look around and tell me what you see" / "look + report" shapes.
        if (_LOOK_GENERIC_RE.search(s)
                and re.search(r"\btell\s+me\s+what\s+you\s+(?:see|saw)\b", s)):
            _emit(steps, self._tools, step_idx, "look", {"direction": "ahead"})
            # Build a say from whatever look returned in the last step.
            seen = []
            if steps and isinstance(steps[-1].get("result"), dict):
                seen = steps[-1]["result"].get("seen") or []
            if seen:
                text = "I see " + ", ".join(str(x) for x in seen) + "."
            else:
                text = "I don't see anything notable."
            _emit(steps, self._tools, step_idx, "say", {"text": text})
            final_say.append(text)
            return "done"

        # "do you see a <X>" / "is there a <X>" — look_for + say.
        mlf = _LOOK_FOR_RE.search(s)
        if mlf and not _contains_action_verb(s):
            q = mlf.group(1).strip()
            q = re.sub(r"\s+(?:on|in|at|near|by)\s+.*$", "", q, flags=re.I).strip()
            if q:
                _emit(steps, self._tools, step_idx, "look_for", {"query": q})
                r = steps[-1].get("result") or {}
                seen = bool(r.get("seen"))
                text = (f"Yes, I can see a {q}."
                        if seen else f"No, I don't see a {q}.")
                _emit(steps, self._tools, step_idx, "say", {"text": text})
                final_say.append(text)
                return "done"

        # Obvious factual / world-knowledge refusals.  Check BEFORE the
        # generic "say X" path because "tell me the weather" would
        # otherwise be mis-parroted as say("the weather").
        if re.search(r"\b(?:weather|temperature|forecast|news|time|date|"
                     r"plus|minus|times|divided|equals|"
                     r"capital\s+of|president\s+of|population)\b", s):
            return "refused"
        if re.search(r"^\s*(?:what|who|when|where|why|how)\b", s):
            # Open-ended interrogatives we didn't catch above — refuse.
            return "refused"

        # Bare "say X" / "tell me X" — one segment say-only.
        if re.search(r"^\s*(?:say|tell\s+me|announce|greet)\b", s):
            txt = _extract_say_text(seg)
            if txt:
                _emit(steps, self._tools, step_idx, "say", {"text": txt})
                final_say.append(txt)
                return "done"

        return "unhandled"

    def _refuse(self, reason: str, t0: float,
                partial_steps: list[dict] | None = None,
                partial_say: list[str] | None = None) -> dict:
        return {
            "success": False,
            "reason": reason,
            "steps": partial_steps or [],
            "final_say": (partial_say[-1] if partial_say else ""),
            "fallback": True,
            "wall_ms": int((time.time() - t0) * 1000),
        }

    def _finish(self, steps: list[dict], final_say: list[str],
                t0: float, *, reason: str) -> dict:
        # Append a finish pseudo-step for parity with Planner.run output.
        steps.append({
            "step": len(steps) + 1,
            "tool": "finish",
            "args": {"reason": reason},
            "result": {"ok": True, "reason": reason},
        })
        return {
            "success": True,
            "reason": reason,
            "steps": steps,
            "final_say": final_say[-1] if final_say else "",
            "fallback": True,
            "wall_ms": int((time.time() - t0) * 1000),
        }


_IMPLICIT_TARGET_RE = re.compile(
    r"\blook(?:ing)?\s+(?:around\s+)?for\s+"
    r"(?:a\s+|an\s+|any\s+|some\s+|my\s+|the\s+)?"
    r"([A-Za-z][A-Za-z0-9 _\-]{0,30})",
    re.I,
)


def _extract_implicit_target(goal: str) -> str | None:
    """Pull the noun phrase out of 'look (around) for <X>' so reflective
    'tell me if you found them' can refer back to it.  Returns None if no
    such phrase is present."""
    m = _IMPLICIT_TARGET_RE.search(goal or "")
    if not m:
        return None
    q = m.group(1).strip()
    # Stop at conjunctions / prepositions that introduce trailing clauses.
    q = re.split(r"\s+(?:on|in|at|near|by|and|,)\s+", q, maxsplit=1)[0]
    return q or None


def _contains_action_verb(s: str) -> bool:
    """True if the segment has an action verb (jump/walk/lean/pose/stop).
    Used to disambiguate 'look for a cup' (look_for-only) from 'lean left
    and look for a cup' (compound — caller should split on ',')."""
    return bool(re.search(
        r"\b(?:jump|walk|lean|bow|stop|halt|freeze|neutral|wait)\b", s, re.I
    ))


# ================================================================= self-test
#
# Run `python3 demo/planner_fallback.py` for a quick smoke check without
# DeepInfra.  Not a replacement for scripts/fallback_eval.py.

def _smoke() -> int:
    call_log: list[dict] = []

    def _mk(name: str) -> Callable[..., dict]:
        def f(**kw: Any) -> dict:
            call_log.append({"tool": name, "args": kw})
            if name == "look":
                return {"ok": True, "seen": ["chair", "table"]}
            if name == "look_for":
                return {"ok": True, "seen": True, "score": 0.9, "frame_ms": 120}
            return {"ok": True}
        return f

    tools = {n: _mk(n) for n in
             ("pose", "walk", "stop", "jump", "look", "look_for", "say", "wait")}

    cases = [
        ("lean left", True),
        ("jump", True),
        ("say hi", True),
        ("lean left, wait 1 second, then neutral", True),
        ("lean left, then lean right, then neutral", True),
        ("look around", True),
        ("do you see a cup", True),
        ("wait 2 seconds", True),
        ("tell me the weather", False),
        ("compute the square root of pi and recite it", False),
    ]

    fp = FallbackPlanner(tools)
    ok_n = 0
    for goal, expect_ok in cases:
        call_log.clear()
        r = fp.run(goal)
        got_ok = bool(r["success"])
        verdict = "PASS" if got_ok == expect_ok else "FAIL"
        if verdict == "PASS":
            ok_n += 1
        print(f"[{verdict}] goal={goal!r}  "
              f"success={got_ok} reason={r['reason']!r}  "
              f"tools={[c['tool'] for c in call_log]}")
    print(f"\nsmoke: {ok_n}/{len(cases)}")
    return 0 if ok_n == len(cases) else 1


if __name__ == "__main__":
    import sys
    sys.exit(_smoke())
