#!/usr/bin/env python3
"""
robot_planner.py  —  LLM tool-calling goal loop for our legged robot.

Where this fits in the research stack (docs/RESEARCH_AGENT_STACK.md §11):
    "Single biggest win per day of effort: #2, the tool-calling loop."

Today the robot is reactive: one utterance -> parse_intent -> one wire
command -> done.  This module turns a single spoken goal into a *sequence*
of primitive calls by delegating control to Llama-3.1-8B-Instruct over
DeepInfra's OpenAI-compatible /chat/completions endpoint with `tools=[...]`.

Usage pattern (integration — see `WIRE IN` docstring at the bottom):

    planner = Planner(tools={
        "pose":   robot_pose,
        "walk":   robot_walk,
        "stop":   robot_stop,
        "jump":   robot_jump,
        "look":   robot_look,
        "say":    robot_say,
        "wait":   robot_wait,
        # `finish` is handled internally by Planner.run.
    }, logger=logger)

    result = planner.run("look around for my keys")
    # result is a PlanResult TypedDict-ish dict:
    #   {"success": bool, "reason": str, "steps": [...], "final_say": str}

Auth: reads `DEEPINFRA_API_KEY` from env.  Exits with code 2 if unset —
DO NOT hardcode; the key leaked once already (see STATUS.md security note).
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable


# Llama-3.1-8B sometimes emits tool calls as plaintext in Meta's function-tag
# format ("<function=NAME>{...}") instead of populating the OpenAI-style
# `tool_calls` field.  Match those so we can still execute them.
_FUNC_TAG_RE = re.compile(
    r"<function=([A-Za-z_][A-Za-z0-9_]*)>\s*(\{[^<]*?\})\s*(?:</function>)?",
    re.DOTALL,
)


def _extract_text_tool_calls(text: str) -> list[dict]:
    """Parse `<function=NAME>{...json...}` patterns out of assistant text and
    return them in the same shape as OpenAI-native tool_calls, so the rest
    of the loop can treat them identically."""
    out: list[dict] = []
    if not text:
        return out
    for i, m in enumerate(_FUNC_TAG_RE.finditer(text)):
        name = m.group(1)
        raw_args = m.group(2).strip()
        out.append({
            "id": f"textcall_{i}",
            "type": "function",
            "function": {"name": name, "arguments": raw_args},
        })
    return out


# ------------------------------------------------------------------ config

DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_CHAT_URL = DEEPINFRA_BASE_URL + "/chat/completions"

DEFAULT_MODEL = "Qwen/Qwen2.5-72B-Instruct"
DEFAULT_MAX_STEPS = 10
REQUEST_TIMEOUT_S = 20.0
NO_TOOL_CALL_LIMIT = 2  # consecutive text-only replies before we abort

# Retry schedule for transient DeepInfra failures (5xx / timeout / network).
# Pure 4xx responses (auth, bad request) are NOT retried — they won't recover.
# Keep the total budget under ~5 s so an end-user's turn doesn't feel wedged.
_RETRY_BACKOFFS_S = (0.5, 1.0, 2.0)
_RETRYABLE_STATUSES = {0, 429, 500, 502, 503, 504}


# ------------------------------------------------------------------ tool schemas
#
# OpenAI-format tool specs.  DeepInfra's Llama-3.1-8B endpoint accepts these
# and emits `tool_calls` in the assistant message exactly like the OpenAI API.
# Keep arg names in sync with the callable signatures the caller injects.

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "pose",
            "description": (
                "Command a servo pose.  Use for leaning, bowing, returning "
                "to neutral.  `name` must be one of: neutral, lean_left, "
                "lean_right, bow_front."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": ["neutral", "lean_left", "lean_right", "bow_front"],
                    },
                    "duration_ms": {
                        "type": "integer",
                        "description": "motion duration in ms (default 400)",
                        "default": 400,
                    },
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "walk",
            "description": (
                "Start the walking gait.  This tool ONLY starts walking; "
                "to halt motion (including walking), call `stop` instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stride": {"type": "integer", "default": 150},
                    "step":   {"type": "integer", "default": 400},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop",
            "description": (
                "Halt all motion immediately — use this to stop walking, "
                "stop a pose, or as an emergency halt."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "jump",
            "description": "Perform one jump.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look",
            "description": (
                "Glance in a direction and return recent vision events.  "
                "Use when the user asks you to look around, find something, "
                "or check the scene."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["left", "right", "ahead", "down", "up"],
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_for",
            "description": (
                "Open-vocabulary visual query.  Use when the user asks about "
                "an arbitrary object that may or may not be one of the "
                "robot's known classes (e.g. 'do you see a red mug?', "
                "'is there a laptop on the desk?').  Runs a single CLIP "
                "zero-shot pass on a fresh webcam frame and returns whether "
                "the phrase was seen, a 0-1 confidence score, and the wall "
                "time in ms."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Free-form phrase, e.g. 'a red mug'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "say",
            "description": "Speak a short sentence to the user.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait",
            "description": "Pause for the given number of seconds.",
            "parameters": {
                "type": "object",
                "properties": {"seconds": {"type": "number"}},
                "required": ["seconds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Signal that the goal is complete.  ALWAYS call this as the "
                "last step.  `reason` is a short human-readable summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


SYSTEM_PROMPT = (
    "You are the planner for a small quadruped robot.  The user gives you a "
    "goal in plain English.  You accomplish it by calling the provided "
    "tools, one or a few at a time, reading the tool results, and deciding "
    "what to do next.\n"
    "\n"
    "Rules:\n"
    "  - Prefer the fewest tool calls that accomplish the goal.\n"
    "  - If the user asked you to say something, call `say` with that text.\n"
    "  - For factual or verbal questions with no physical action "
    "(weather, math, trivia, yes/no questions about the world): "
    "answer with `say`, then `finish`.  Do NOT call `look` — `look` is "
    "only for questions about the physical scene around the robot.\n"
    "  - If the goal involves finding / spotting something physically "
    "present, use `look` and then `say` what you saw (or didn't).\n"
    "  - For open-ended visual questions about arbitrary objects "
    "('do you see a red mug?', 'is there a laptop on the desk?'), use "
    "`look_for` with the phrase.  For structured class names already "
    "known to the robot, `look` is cheaper.\n"
    "  - To halt any motion, call `stop`.  Do not call `walk` with an "
    "off/disable argument — `walk` only starts walking.\n"
    "  - Always call `finish` once the goal is done.  Do not keep calling "
    "tools after finishing.\n"
    "  - STANDING GOALS: if the goal requires a FUTURE event or ongoing "
    "observation (phrases like 'wait for', 'when you see', 'watch for', "
    "'let me know if', 'find <X>', 'tell me if <X> happens', 'greet every "
    "<X>'): do ONE quick check with `look_for` or `look`.  If the target "
    "is NOT already visible, call `finish(reason=\"waiting\")` IMMEDIATELY "
    "with NO preparatory `say`.  Do NOT emit the eventual response "
    "(e.g. 'hello' for 'say hello when you see a person') on the initial "
    "turn — the response is only correct AFTER the event fires.  If the "
    "target IS already visible on the first check, execute the normal "
    "response then `finish(reason=\"completed\")`.  The caller treats "
    "reason='waiting' / 'watching' as a STANDING goal and re-invokes you "
    "when a relevant event arrives (at which point you may `say` the "
    "actual response and finish).\n"
    "  - Never invent tools.  Only use the tools provided.\n"
    "  - Keep `say` utterances short and natural (one sentence)."
)


# ------------------------------------------------------------------ helpers

def _eprint(*a, **kw) -> None:
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


def _api_key() -> str:
    """Return the DeepInfra API key or exit(2).  No hardcoded fallback — a
    key leaked publicly once (STATUS.md).  Source it from
    ~/Projects/AIHW/.env.local before invoking."""
    k = os.environ.get("DEEPINFRA_API_KEY", "").strip()
    if not k:
        _eprint("ERR: DEEPINFRA_API_KEY not set in environment.\n"
                "     source ~/Projects/AIHW/.env.local  (or export it)")
        sys.exit(2)
    return k


def _post_chat(payload: dict, api_key: str,
               timeout: float = REQUEST_TIMEOUT_S) -> tuple[int, dict | None, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        DEEPINFRA_CHAT_URL,
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body), body
            except json.JSONDecodeError:
                return resp.status, None, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, None, body
    except urllib.error.URLError as e:
        return 0, None, f"URLError: {e}"
    except TimeoutError:
        return 0, None, "timeout"


def _parse_tool_args(raw: Any) -> dict:
    """DeepInfra returns tool_call arguments as a JSON string (OpenAI spec).
    Some models return an already-decoded dict.  Handle both."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


# ------------------------------------------------------------------ planner

# Type alias for clarity; actual returns are ordinary dicts.
# PlanResult: {"success": bool, "reason": str, "steps": list[dict], "final_say": str}

class Planner:
    """Goal-driven tool-calling loop over DeepInfra Llama-3.1-8B-Instruct.

    Construction:
        tools: dict mapping tool name -> Callable.  The callable takes kwargs
               matching TOOL_SCHEMAS and returns a JSON-serialisable dict
               (usually {"ok": True, ...}).  `finish` is intercepted by the
               planner itself — if the injected dict includes it it's ignored.
        model: DeepInfra model id.
        max_steps: hard cap on LLM turns before we abort.
        logger: optional `callable(str) -> None`; defaults to stderr.
    """

    def __init__(
        self,
        tools: dict[str, Callable[..., dict]],
        *,
        model: str = DEFAULT_MODEL,
        max_steps: int = DEFAULT_MAX_STEPS,
        logger: Callable[[str], None] | None = None,
        fallback: Any | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._model = model
        self._max_steps = int(max_steps)
        self._log: Callable[[str], None] = logger or _eprint
        # Optional FallbackPlanner (duck-typed: must expose .run(goal,
        # observation) -> PlanResult).  Invoked when DeepInfra is still
        # unreachable after the retry budget is exhausted.
        self._fallback = fallback

    def _post_chat_with_retries(self, payload: dict, api_key: str,
                                 ) -> tuple[int, dict | None, str]:
        """Wrap `_post_chat` with backoff-on-5xx so transient DeepInfra
        hiccups don't kill the agent loop.  4xx results (auth, malformed
        request) are NOT retried.  Returns the final (status, body, raw)."""
        last: tuple[int, dict | None, str] = (0, None, "")
        for attempt, backoff in enumerate((0.0,) + _RETRY_BACKOFFS_S):
            if backoff > 0.0:
                time.sleep(backoff)
            status, body, raw = _post_chat(payload, api_key)
            last = (status, body, raw)
            if status == 200 and isinstance(body, dict):
                return last
            if status not in _RETRYABLE_STATUSES:
                # Auth / 400 / 404 won't recover by retrying.
                return last
            self._log(
                f"[planner] DeepInfra transient failure status={status} "
                f"attempt={attempt + 1}/{len(_RETRY_BACKOFFS_S) + 1} "
                f"body={raw[:200]!r}"
            )
        return last

    # -------------------------------------------------------------- public
    def run(self, goal: str, observation: dict | None = None) -> dict:
        api_key = _api_key()

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": goal.strip()},
        ]

        # When the GoalKeeper re-engages us with a fresh event (vision detect,
        # IMU anomaly, battery alert, etc.) it passes `observation` carrying
        # {"event": <event dict>, "prior_result": <previous PlanResult>}.
        # Surface that as a follow-up user-role message so the LLM decides
        # whether to act on the new observation or mark the goal complete.
        if observation:
            ev = observation.get("event") if isinstance(observation, dict) else None
            prior = (observation.get("prior_result")
                     if isinstance(observation, dict) else None)
            lines = [
                f"STANDING GOAL (still active): {goal.strip()}",
                f"NEW OBSERVATION: {json.dumps(ev)}"
                if ev is not None
                else f"NEW OBSERVATION: {json.dumps(observation)}",
            ]
            if isinstance(prior, dict) and prior.get("final_say"):
                lines.append(
                    f"You previously said: {prior.get('final_say')!r}"
                )
            lines.append(
                "Decide: does this observation satisfy the goal? "
                "If yes, take the one appropriate action (e.g. `say` hello) "
                "and then call `finish`.  If it does not satisfy the goal, "
                "you may ignore it by calling `finish` with reason='ignored'."
            )
            messages.append({"role": "user", "content": "\n".join(lines)})

        steps: list[dict] = []
        final_say = ""
        finish_reason: str | None = None
        empty_turns = 0
        stop_called = False
        success = False

        for step_idx in range(1, self._max_steps + 1):
            payload = {
                "model": self._model,
                "messages": messages,
                "tools": TOOL_SCHEMAS,
                "tool_choice": "auto",
                "temperature": 0.0,
                "max_tokens": 512,
            }
            status, body, raw = self._post_chat_with_retries(payload, api_key)
            if status in (401, 403):
                self._log(f"[planner] AUTH FAILURE ({status}): {raw[:300]}")
                sys.exit(2)
            if status != 200 or not isinstance(body, dict):
                self._log(
                    f"[planner] DeepInfra failed after "
                    f"{len(_RETRY_BACKOFFS_S) + 1} retries; "
                    f"last status={status} body={raw[:200]!r}"
                )
                if self._fallback is not None and step_idx == 1:
                    # Only divert to the fallback on the FIRST turn — if
                    # we're already mid-plan the conversation state is
                    # lost and restarting from scratch is safer than
                    # partially executing twice.
                    self._log(
                        "[planner] falling back to rule-based planner"
                    )
                    try:
                        return self._fallback.run(goal, observation)
                    except Exception as e:
                        self._log(
                            f"[planner] fallback raised "
                            f"{type(e).__name__}: {e}"
                        )
                return {
                    "success": False,
                    "reason": f"http_{status}",
                    "steps": steps,
                    "final_say": final_say,
                }

            try:
                choice = body["choices"][0]
                msg = choice["message"]
            except (KeyError, IndexError, TypeError):
                self._log(f"[planner] bad response body: {raw[:300]!r}")
                return {
                    "success": False,
                    "reason": "bad_response",
                    "steps": steps,
                    "final_say": final_say,
                }

            tool_calls = list(msg.get("tool_calls") or [])
            text = msg.get("content") or ""

            # Fallback: Llama-3.1-8B occasionally emits Meta-format
            # "<function=NAME>{...}" tags in `content` instead of proper
            # tool_calls.  Parse them out and promote them so we keep going.
            if not tool_calls and text:
                recovered = _extract_text_tool_calls(text)
                if recovered:
                    self._log(
                        f"[planner] recovered {len(recovered)} text-fmt "
                        f"tool_calls from content"
                    )
                    tool_calls = recovered
                    # Strip the raw tags from the recorded assistant text so
                    # the conversation reads cleanly; keep the cleaned text.
                    text = _FUNC_TAG_RE.sub("", text).strip()

            # Record the assistant turn verbatim (must include tool_calls so
            # the follow-up tool messages reference real call IDs).
            asst_msg: dict = {"role": "assistant", "content": text}
            if tool_calls:
                asst_msg["tool_calls"] = tool_calls
            messages.append(asst_msg)

            if not tool_calls:
                empty_turns += 1
                self._log(
                    f"[planner] step={step_idx} no tool_calls "
                    f"(empty_turns={empty_turns}) text={text[:120]!r}"
                )
                if empty_turns >= NO_TOOL_CALL_LIMIT:
                    return {
                        "success": False,
                        "reason": "no_tool_called",
                        "steps": steps,
                        "final_say": final_say,
                    }
                # Nudge the model to call a tool.
                messages.append({
                    "role": "user",
                    "content": (
                        "Please continue by calling a tool, or call "
                        "`finish` if the goal is complete."
                    ),
                })
                continue
            empty_turns = 0  # got tool calls, reset counter

            # Execute each tool call in order.
            for tc in tool_calls:
                tc_id = tc.get("id") or f"call_{step_idx}"
                fn = (tc.get("function") or {})
                name = fn.get("name") or ""
                args = _parse_tool_args(fn.get("arguments"))

                if name == "finish":
                    finish_reason = str(args.get("reason", "done"))
                    steps.append({
                        "step": step_idx,
                        "tool": "finish",
                        "args": args,
                        "result": {"ok": True, "reason": finish_reason},
                    })
                    self._log(
                        f"[planner] step={step_idx} tool=finish "
                        f"args={args} result={{'ok': True}}"
                    )
                    success = True
                    # Still append a tool message so the transcript stays
                    # well-formed, even though we'll break out.
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": "finish",
                        "content": json.dumps({"ok": True,
                                               "reason": finish_reason}),
                    })
                    break

                impl = self._tools.get(name)
                if impl is None:
                    result = {
                        "ok": False,
                        "error": f"unknown tool {name!r}; valid tools: "
                                 f"{sorted(self._tools.keys())}",
                    }
                else:
                    try:
                        out = impl(**args)
                    except TypeError as e:
                        result = {"ok": False,
                                  "error": f"bad args for {name}: {e}"}
                    except Exception as e:
                        result = {"ok": False,
                                  "error": f"{type(e).__name__}: {e}"}
                    else:
                        result = out if isinstance(out, dict) else {"ok": True,
                                                                    "value": out}

                steps.append({
                    "step": step_idx,
                    "tool": name,
                    "args": args,
                    "result": result,
                })
                self._log(
                    f"[planner] step={step_idx} tool={name} "
                    f"args={args} result={result}"
                )

                # Side-channel bookkeeping.
                if name == "say" and isinstance(args, dict):
                    t = args.get("text")
                    if isinstance(t, str) and t:
                        final_say = t
                if name == "stop":
                    stop_called = True

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": name,
                    "content": json.dumps(result),
                })

            if success:
                break
            if stop_called:
                # The user explicitly told us (via the model) to halt.
                # Treat as a successful terminal state — planner shouldn't
                # keep adding tool calls after a hard stop.
                success = True
                finish_reason = finish_reason or "stop_called"
                break

        if not success and finish_reason is None:
            return {
                "success": False,
                "reason": "max_steps",
                "steps": steps,
                "final_say": final_say,
            }
        return {
            "success": True,
            "reason": finish_reason or "ok",
            "steps": steps,
            "final_say": final_say,
        }


# ================================================================= self-test

def _build_stub_tools(look_queue: list[dict]) -> tuple[dict, list[dict]]:
    """Stub implementations that print + log instead of actuating.
    Returns (tools_dict, call_log)."""
    call_log: list[dict] = []

    def _rec(_tool_name: str, **kw) -> dict:
        call_log.append({"tool": _tool_name, "args": kw})
        print(f"  [stub] {_tool_name}({kw})")
        return {"ok": True}

    def pose(name: str, duration_ms: int = 400) -> dict:
        return _rec("pose", name=name, duration_ms=duration_ms)

    def walk(on: bool, stride: int = 150, step: int = 400) -> dict:
        return _rec("walk", on=on, stride=stride, step=step)

    def stop() -> dict:
        return _rec("stop")

    def jump() -> dict:
        return _rec("jump")

    def look(direction: str) -> dict:
        call_log.append({"tool": "look", "args": {"direction": direction}})
        seen = look_queue.pop(0) if look_queue else {"seen": []}
        print(f"  [stub] look(direction={direction!r}) -> {seen}")
        return {"ok": True, **seen}

    def look_for(query: str) -> dict:
        # Stubbed open-vocab vision — self-tests don't exercise it, but we
        # register it so the planner never hits 'unknown tool' if the model
        # opportunistically picks look_for instead of look.
        call_log.append({"tool": "look_for", "args": {"query": query}})
        print(f"  [stub] look_for(query={query!r}) -> seen=False")
        return {"ok": True, "seen": False, "score": 0.0, "frame_ms": 0}

    def say(text: str) -> dict:
        call_log.append({"tool": "say", "args": {"text": text}})
        print(f"  [stub] say({text!r})")
        return {"ok": True}

    def wait(seconds: float) -> dict:
        call_log.append({"tool": "wait", "args": {"seconds": seconds}})
        print(f"  [stub] wait({seconds}) [not sleeping]")
        return {"ok": True}

    tools = {
        "pose": pose,
        "walk": walk,
        "stop": stop,
        "jump": jump,
        "look": look,
        "look_for": look_for,
        "say":  say,
        "wait": wait,
    }
    return tools, call_log


def _run_self_tests() -> int:
    """Hit the real DeepInfra endpoint with 3 goals.  Returns 0 if all pass."""
    tests: list[dict] = [
        {
            "name": "lean-wait-neutral",
            "goal": "Lean to the left, wait two seconds, then go back to neutral.",
            "look_queue": [],
            "expect": lambda log, res: (
                res["success"]
                and any(c["tool"] == "pose"
                        and c["args"].get("name") == "lean_left" for c in log)
                and any(c["tool"] == "pose"
                        and c["args"].get("name") == "neutral" for c in log)
            ),
        },
        {
            "name": "jump-and-happy",
            "goal": "Jump once and then tell me you're happy.",
            "look_queue": [],
            "expect": lambda log, res: (
                res["success"]
                and any(c["tool"] == "jump" for c in log)
                and any(c["tool"] == "say" for c in log)
            ),
        },
        {
            "name": "look-for-person",
            "goal": "Look around and tell me if you see a person.",
            "look_queue": [{"seen": ["person"]}, {"seen": []}],
            # Accept either `look` (old direction-enumerated scan) or
            # `look_for` (newer open-vocab vision).  Smarter models pick
            # `look_for`; either is a correct tool for this goal.
            "expect": lambda log, res: (
                res["success"]
                and any(c["tool"] in ("look", "look_for") for c in log)
                and any(c["tool"] == "say" for c in log)
                and "person" in (res.get("final_say") or "").lower()
            ),
        },
    ]

    all_pass = True
    grand_t0 = time.time()
    for t in tests:
        print(f"\n--- test: {t['name']} ---")
        print(f"    goal: {t['goal']!r}")
        tools, call_log = _build_stub_tools(list(t["look_queue"]))
        planner = Planner(tools)
        t0 = time.time()
        result = planner.run(t["goal"])
        dt = time.time() - t0
        ok = False
        try:
            ok = bool(t["expect"](call_log, result))
        except Exception as e:
            print(f"    expect-check raised: {type(e).__name__}: {e}")
        verdict = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False
        print(f"    steps: {len(result['steps'])}  "
              f"success={result['success']}  reason={result['reason']!r}")
        print(f"    final_say={result['final_say']!r}")
        print(f"    wall={dt:.2f}s  --> {verdict}")
    print(f"\nGRAND TOTAL: {time.time() - grand_t0:.2f}s  "
          f"  overall={'PASS' if all_pass else 'FAIL'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    if not os.environ.get("DEEPINFRA_API_KEY", "").strip():
        print("skipped (no API key)")
        sys.exit(0)
    sys.exit(_run_self_tests())


# ================================================================= WIRE IN
"""
WIRE IN — how to graft this into demo/robot_daemon.py.

The integrator's job is to (a) construct a single Planner at startup,
(b) build a tools dict that binds to the daemon's existing primitives,
(c) route long-form voice utterances through Planner.run() instead of
parse_intent -> one-shot.  Keep the regex / parse_intent_api path for
short imperative commands; the planner is for anything multi-step.

    # near the other init code in main() — after `engine` is built:
    from demo.robot_planner import Planner

    def _tool_pose(name: str, duration_ms: int = 400) -> dict:
        send_wire({"c": "pose", "n": name, "d": int(duration_ms)},
                  args.dry_run)
        return {"ok": True}

    def _tool_walk(on: bool, stride: int = 150, step: int = 400) -> dict:
        send_wire({"c": "walk", "on": bool(on),
                   "stride": int(stride), "step": int(step)}, args.dry_run)
        return {"ok": True}

    def _tool_stop() -> dict:
        send_wire({"c": "stop"}, args.dry_run); return {"ok": True}

    def _tool_jump() -> dict:
        send_wire({"c": "jump"}, args.dry_run); return {"ok": True}

    def _tool_look(direction: str) -> dict:
        # Optional: bias servo toward `direction` via pose before reading
        # the last few vision events.  Vision events live on event_queue;
        # the daemon already tracks engine._seen_classes, so surface those.
        recent = sorted(engine._seen_classes)  # swap for a real ringbuffer
        return {"ok": True, "direction": direction, "seen": recent}

    def _tool_say(text: str) -> dict:
        speak(text, args.tts); return {"ok": True}

    def _tool_wait(seconds: float) -> dict:
        time.sleep(max(0.0, min(5.0, float(seconds)))); return {"ok": True}

    planner = Planner({
        "pose": _tool_pose, "walk": _tool_walk, "stop": _tool_stop,
        "jump": _tool_jump, "look": _tool_look, "say":  _tool_say,
        "wait": _tool_wait,
    }, logger=logger)

    # routing: in the voice turn handler, replace
    #     cmd = parse_intent(utterance); handle_command(cmd, ...)
    # with a fallback-to-planner path:
    #     cmd = parse_intent(utterance)
    #     if cmd.get("c") == "noop" and len(utterance.split()) >= 4:
    #         planner.run(utterance)   # multi-step goal
    #     else:
    #         handle_command(cmd, ...)  # short imperative
"""
