#!/usr/bin/env python3
"""
DeepInfra API variant of parse_intent.py: drop-in replacement with the same
CLI surface and JSON output, but the intent is parsed by a hosted instruction-
tuned model (default: meta-llama/Meta-Llama-3.1-8B-Instruct) reached through
DeepInfra's OpenAI-compatible chat/completions endpoint.

Why: on-device LLMs (Gemma 3 1B / TinyLlama / Gemma 3N E2B) are either too
slow on Pixel 6 (~25-60 s cold, 2-8 s warm for Gemma; 20-28 s for TinyLlama)
or too inaccurate (TinyLlama scored 4/8 on the self-test set; see
logs/parse_intent_tests.log).  A fast hosted endpoint ~0.3-1.0 s/call is the
better default when laptop has internet.

Usage:
    python3 demo/parse_intent_api.py "walk forward please"
    python3 demo/parse_intent_api.py --model llama31-8b "jump"

Prints one JSON line on stdout.  API failures / timeouts / parse errors
fall back to {"c":"noop"} — the daemon treats noop as "matcher didn't
recognize".  Everything else (banner, timing, errors) goes to stderr.

Legal schemas (canonicalize() downgrades anything else to noop):
    {"c":"pose","n":"lean_left","d":1500}
    {"c":"pose","n":"lean_right","d":1500}
    {"c":"pose","n":"bow_front","d":1800}
    {"c":"pose","n":"neutral","d":1500}
    {"c":"walk","on":true,"stride":150,"step":400}
    {"c":"stop"} | {"c":"jump"} | {"c":"noop"}

API key: required via $DEEPINFRA_API_KEY (do NOT hardcode in this repo — it is
public). Source the key from ~/Projects/AIHW/.env.local locally or set it in
your shell before running.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

# ------------------------------------------------------------------ config

DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
DEEPINFRA_CHAT_URL = DEEPINFRA_BASE_URL + "/chat/completions"

# CLI alias -> DeepInfra model id.  Pick fast + cheap + JSON-capable.
# Llama-3.1-8B-Instruct is the default — it's the right size for this
# intent-parse task (sub-second, ~$0.03/Mtok input) and reliably respects
# `response_format={"type":"json_object"}`.  Bigger models listed for
# escalation if ever needed; IDs are the canonical DeepInfra-hosted names.
MODEL_ALIASES = {
    "llama31-8b":  "meta-llama/Meta-Llama-3.1-8B-Instruct",
    "llama33-70b": "meta-llama/Llama-3.3-70B-Instruct",
    "gemma3-27b":  "google/gemma-3-27b-it",
    "qwen25-72b":  "Qwen/Qwen2.5-72B-Instruct",
}
DEFAULT_MODEL_ID = "meta-llama/Meta-Llama-3.1-8B-Instruct"
DEFAULT_ALIAS = "llama31-8b"

TIMEOUT_S = 10.0
RETRY_ON_5XX = 1

# ------------------------------------------------------------------ canon

NOOP = {"c": "noop"}
# Identical to parse_intent.py / parse_intent_fast.py.
CANON = {
    ("pose", "lean_left"):  {"c": "pose", "n": "lean_left",  "d": 1500},
    ("pose", "lean_right"): {"c": "pose", "n": "lean_right", "d": 1500},
    ("pose", "bow_front"):  {"c": "pose", "n": "bow_front",  "d": 1800},
    ("pose", "neutral"):    {"c": "pose", "n": "neutral",    "d": 1500},
    ("walk",  None):        {"c": "walk", "on": True, "stride": 150, "step": 400},
    ("stop",  None):        {"c": "stop"},
    ("jump",  None):        {"c": "jump"},
    ("noop",  None):        NOOP,
}

# Strict system prompt — the model must emit ONLY one legal wire command.
# We reuse the same few-shot examples as parse_intent.py's PROMPT_TEMPLATE
# (read from that module so we don't drift).  The system message nails the
# schema + fallback behaviour; user message carries the few-shots + the
# transcript.
SYSTEM_PROMPT = (
    "You translate a spoken English robot command into exactly ONE wire "
    "command JSON object.  You must emit only valid JSON, no prose, no "
    "markdown, no commentary.\n"
    "Legal shapes (emit exactly one):\n"
    '  {"c":"pose","n":"lean_left","d":1500}\n'
    '  {"c":"pose","n":"lean_right","d":1500}\n'
    '  {"c":"pose","n":"bow_front","d":1800}\n'
    '  {"c":"pose","n":"neutral","d":1500}\n'
    '  {"c":"walk","on":true,"stride":150,"step":400}\n'
    '  {"c":"stop"}\n'
    '  {"c":"jump"}\n'
    '  {"c":"noop"}\n'
    "Rules:\n"
    "  - If the command is a pose/walk/stop/jump synonym, map to that.\n"
    '  - If unknown / chit-chat / unrelated, emit {"c":"noop"}.\n'
    "  - Never invent new keys or values.  Never explain yourself."
)


def _load_few_shots() -> str:
    """Reuse PROMPT_TEMPLATE's few-shot block from parse_intent.py so the
    on-device and API parsers stay semantically aligned.  Falls back to a
    hardcoded copy if the sibling module is unavailable (e.g. renamed)."""
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        from parse_intent import PROMPT_TEMPLATE  # type: ignore
        # PROMPT_TEMPLATE ends with "Now: %s ->"; strip that tail so we can
        # splice in our own user phrasing.
        marker = "Now: %s ->"
        idx = PROMPT_TEMPLATE.rfind(marker)
        if idx != -1:
            return PROMPT_TEMPLATE[:idx].rstrip() + "\n"
        return PROMPT_TEMPLATE
    except Exception:
        # Fallback — keep in sync with parse_intent.py manually if this fires.
        return (
            "Map the user robot command to ONE wire command JSON.\n"
            "Examples:\n"
            "lean to the left -> {\"c\":\"pose\",\"n\":\"lean_left\",\"d\":1500}\n"
            "tilt right -> {\"c\":\"pose\",\"n\":\"lean_right\",\"d\":1500}\n"
            "bow forward -> {\"c\":\"pose\",\"n\":\"bow_front\",\"d\":1800}\n"
            "stand up -> {\"c\":\"pose\",\"n\":\"neutral\",\"d\":1500}\n"
            "start walking -> "
            "{\"c\":\"walk\",\"on\":true,\"stride\":150,\"step\":400}\n"
            "halt -> {\"c\":\"stop\"}\n"
            "hop -> {\"c\":\"jump\"}\n"
            "tell me a joke -> {\"c\":\"noop\"}\n"
            "turn around -> {\"c\":\"noop\"}\n"
        )


FEW_SHOTS = _load_few_shots()


# ------------------------------------------------------------------ helpers

def eprint(*a, **kw):
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


def canonicalize(obj):
    """Map a parsed dict to a canonical wire command, or noop.

    Identical to parse_intent.py's canonicalize(): guarantees the daemon
    never sees an off-schema command.  Any mismatch -> noop.
    """
    if not isinstance(obj, dict):
        return NOOP
    c = obj.get("c")
    if c in ("stop", "jump", "noop"):
        return CANON[(c, None)]
    if c == "walk":
        return CANON[("walk", None)]
    if c == "pose":
        key = ("pose", obj.get("n"))
        if key in CANON:
            return CANON[key]
    return NOOP


def extract_first_json(text):
    """First balanced {...} object in text, json.loads'd, or None."""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None


def _api_key() -> str:
    k = os.environ.get("DEEPINFRA_API_KEY", "").strip()
    if not k:
        print("ERR: DEEPINFRA_API_KEY not set in environment.\n"
              "     source ~/Projects/AIHW/.env.local  (or export it manually)",
              file=sys.stderr)
        sys.exit(2)
    return k


def _resolve_model(arg: str | None) -> str:
    if not arg:
        return DEFAULT_MODEL_ID
    if arg in MODEL_ALIASES:
        return MODEL_ALIASES[arg]
    # Allow passing a full DeepInfra model id directly.
    return arg


def _post_chat(payload: dict, api_key: str, timeout: float) -> tuple[int, dict | None, str]:
    """POST to DeepInfra chat/completions. Returns (status, json_or_None, raw_body_for_errors)."""
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


def run_api(transcript: str, model_id: str, timeout: float = TIMEOUT_S) -> dict:
    """One hosted call.  Always returns a canonical wire command dict."""
    user_msg = FEW_SHOTS + f"Now: {transcript} ->"
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
    }
    api_key = _api_key()

    attempt = 0
    last_err = ""
    while attempt <= RETRY_ON_5XX:
        t0 = time.time()
        status, body, raw = _post_chat(payload, api_key, timeout)
        dt = time.time() - t0
        eprint(
            f"[parse_intent_api] model={model_id} attempt={attempt} "
            f"status={status} wall={dt*1000:.0f}ms"
        )
        if status in (401, 403):
            eprint(f"[parse_intent_api] AUTH FAILURE ({status}): {raw[:500]}")
            # Hard stop per spec — never silently noop on auth errors.
            sys.exit(2)
        if status == 200 and body is not None:
            try:
                content = body["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                eprint(f"[parse_intent_api] unexpected body: {raw[:300]}")
                return NOOP
            eprint(f"[parse_intent_api] content={content[:200]!r}")
            obj = extract_first_json(content)
            if obj is None:
                eprint("[parse_intent_api] no JSON in content")
                return NOOP
            return canonicalize(obj)
        # retryable?
        last_err = raw[:300] if raw else f"status={status}"
        if 500 <= status < 600 or status == 0:
            attempt += 1
            continue
        eprint(f"[parse_intent_api] non-retryable {status}: {last_err}")
        return NOOP

    eprint(f"[parse_intent_api] retries exhausted; last_err={last_err!r}")
    return NOOP


def parse_intent(transcript: str, model_key: str | None = None) -> dict:
    model_id = _resolve_model(model_key)
    return run_api(transcript, model_id)


# ------------------------------------------------------------------ cli

def main(argv):
    ap = argparse.ArgumentParser(
        prog="parse_intent_api.py",
        description="DeepInfra-hosted intent parser. Same CLI as "
                    "demo/parse_intent.py / parse_intent_fast.py.",
    )
    ap.add_argument(
        "--model",
        default=DEFAULT_ALIAS,
        help=(
            "DeepInfra model: alias from "
            f"{sorted(MODEL_ALIASES.keys())!r} or a full model id. "
            f"Default: {DEFAULT_ALIAS} ({DEFAULT_MODEL_ID})."
        ),
    )
    ap.add_argument("transcript", nargs=argparse.REMAINDER,
                    help="the utterance to parse")
    ns = ap.parse_args(argv[1:])
    transcript = " ".join(ns.transcript).strip()
    if not transcript:
        eprint(__doc__)
        print(json.dumps(NOOP, separators=(",", ":")))
        return
    out = parse_intent(transcript, model_key=ns.model)
    print(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv)
