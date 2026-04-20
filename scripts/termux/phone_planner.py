#!/data/data/com.termux/files/usr/bin/env python3
"""
phone_planner.py  —  Termux-side port of demo/robot_planner.py.

LLM tool-calling goal loop for the phone-brain stack.  Mirrors the laptop
planner almost verbatim so the two stay in sync.  Key differences:

  * No FallbackPlanner import.  The phone always has network (Wi-Fi or
    LTE); if DeepInfra is down, the daemon surfaces a terse "not
    connected" to the user rather than running a rule-based mimic.  The
    wiring is left in place (constructor still accepts `fallback=`), so a
    future rule-based fallback could be plugged in without changes here.

  * API-key lookup adds a ~/.dia_key fallback to match phone_intent.py
    (DEEPINFRA_API_KEY env var still wins if present).

The rest — tool schemas, system prompt, retry schedule, text-format tool
call recovery, Planner.run() shape — is a verbatim port.  Keeping the
surface identical to demo/robot_planner.py is deliberate so GoalKeeper
can be reused without adapter code.

Usage pattern:

    planner = Planner(tools={
        "pose":     _tool_pose,
        "walk":     _tool_walk,
        "stop":     _tool_stop,
        "jump":     _tool_jump,
        "look":     _tool_look,
        "look_for": _tool_look_for,
        "say":      _tool_say,
        "wait":     _tool_wait,
    }, logger=log)

    result = planner.run("walk until you see a person")
    # {"success": bool, "reason": str, "steps": [...], "final_say": str}
"""
from __future__ import annotations

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
    return them in the same shape as OpenAI-native tool_calls."""
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

DEFAULT_MODEL = os.environ.get("PHONE_PLANNER_MODEL",
                               "Qwen/Qwen2.5-72B-Instruct")
DEFAULT_MAX_STEPS = 10
REQUEST_TIMEOUT_S = 20.0
NO_TOOL_CALL_LIMIT = 2

# Retry schedule for transient DeepInfra failures.  Total budget stays under
# ~5 s so a turn doesn't feel wedged.
_RETRY_BACKOFFS_S = (0.5, 1.0, 2.0)
_RETRYABLE_STATUSES = {0, 429, 500, 502, 503, 504}


# ------------------------------------------------------------------ tool schemas

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
                "zero-shot pass on a fresh camera frame and returns whether "
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
    "<X>', 'walk until you see <X>'): do ONE quick check with `look_for` "
    "or `look`.  If the target is NOT already visible, start the ongoing "
    "action if one was requested (e.g. `walk` for 'walk until...') and "
    "then call `finish(reason=\"watching\")` IMMEDIATELY with NO "
    "preparatory `say` about the eventual event.  Do NOT emit the eventual "
    "response (e.g. 'hello' for 'say hello when you see a person') on the "
    "initial turn — the response is only correct AFTER the event fires.  "
    "If the target IS already visible on the first check, execute the "
    "normal response then `finish(reason=\"completed\")`.  The caller "
    "treats reason='waiting' / 'watching' as a STANDING goal and "
    "re-invokes you when a relevant event arrives (at which point you "
    "may `say` the actual response and finish).\n"
    "  - Never invent tools.  Only use the tools provided.\n"
    "  - Keep `say` utterances short and natural (one sentence)."
)


# ------------------------------------------------------------------ helpers

def _eprint(*a, **kw) -> None:
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


def _load_api_key() -> str:
    """Return the DeepInfra API key or exit(2).

    Lookup order (matches phone_intent.py):
      1. $DEEPINFRA_API_KEY
      2. $DIA_KEY_FILE (if set)
      3. ~/.dia_key
    """
    k = (os.environ.get("DEEPINFRA_API_KEY") or "").strip()
    if k:
        return k
    path = os.environ.get("DIA_KEY_FILE") or os.path.expanduser("~/.dia_key")
    try:
        with open(path, "r") as fh:
            k = fh.read().strip()
    except OSError:
        k = ""
    if not k:
        _eprint("ERR: no DEEPINFRA_API_KEY in env and ~/.dia_key missing/empty")
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

class Planner:
    """Goal-driven tool-calling loop over DeepInfra.

    Drop-in compatible with demo/robot_planner.Planner so GoalKeeper works
    unchanged.  See module docstring for construction args.
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
        self._fallback = fallback

    def _post_chat_with_retries(self, payload: dict, api_key: str,
                                 ) -> tuple[int, dict | None, str]:
        last: tuple[int, dict | None, str] = (0, None, "")
        for attempt, backoff in enumerate((0.0,) + _RETRY_BACKOFFS_S):
            if backoff > 0.0:
                time.sleep(backoff)
            status, body, raw = _post_chat(payload, api_key)
            last = (status, body, raw)
            if status == 200 and isinstance(body, dict):
                return last
            if status not in _RETRYABLE_STATUSES:
                return last
            self._log(
                f"[planner] DeepInfra transient failure status={status} "
                f"attempt={attempt + 1}/{len(_RETRY_BACKOFFS_S) + 1} "
                f"body={raw[:200]!r}"
            )
        return last

    def run(self, goal: str, observation: dict | None = None) -> dict:
        api_key = _load_api_key()

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": goal.strip()},
        ]

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
                "If yes, take the one appropriate action (e.g. `say` hello, "
                "or `stop` if the standing goal was 'walk until you see X') "
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
                # Don't exit the whole daemon — planner is one subsystem.
                # Caller treats success=False + reason='auth' as fatal for
                # this goal but keeps the daemon alive.
                return {
                    "success": False,
                    "reason": "auth",
                    "steps": steps,
                    "final_say": final_say,
                }
            if status != 200 or not isinstance(body, dict):
                self._log(
                    f"[planner] DeepInfra failed after "
                    f"{len(_RETRY_BACKOFFS_S) + 1} retries; "
                    f"last status={status} body={raw[:200]!r}"
                )
                if self._fallback is not None and step_idx == 1:
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

            if not tool_calls and text:
                recovered = _extract_text_tool_calls(text)
                if recovered:
                    self._log(
                        f"[planner] recovered {len(recovered)} text-fmt "
                        f"tool_calls from content"
                    )
                    tool_calls = recovered
                    text = _FUNC_TAG_RE.sub("", text).strip()

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
                messages.append({
                    "role": "user",
                    "content": (
                        "Please continue by calling a tool, or call "
                        "`finish` if the goal is complete."
                    ),
                })
                continue
            empty_turns = 0

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
    call_log: list[dict] = []

    def _rec(_tool_name: str, **kw) -> dict:
        call_log.append({"tool": _tool_name, "args": kw})
        print(f"  [stub] {_tool_name}({kw})")
        return {"ok": True}

    def pose(name: str, duration_ms: int = 400) -> dict:
        return _rec("pose", name=name, duration_ms=duration_ms)

    def walk(stride: int = 150, step: int = 400, **_) -> dict:
        return _rec("walk", stride=stride, step=step)

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


if __name__ == "__main__":
    if not (os.environ.get("DEEPINFRA_API_KEY", "").strip()
            or os.path.exists(os.path.expanduser("~/.dia_key"))):
        print("skipped (no API key)")
        sys.exit(0)
    tools, call_log = _build_stub_tools([])
    planner = Planner(tools)
    goal = sys.argv[1] if len(sys.argv) > 1 else "jump once and say hi"
    print(f"--- goal: {goal!r}")
    t0 = time.time()
    result = planner.run(goal)
    dt = time.time() - t0
    print(f"result={result}")
    print(f"wall={dt:.2f}s")
    sys.exit(0 if result.get("success") else 1)
