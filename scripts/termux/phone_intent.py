#!/data/data/com.termux/files/usr/bin/env python3
"""phone_intent.py — Termux-hosted Python variant of phone_intent.sh.

Same JSON contract as demo/parse_intent_api.py:
  - transcript on stdin
  - one canonical wire-command JSON on stdout
  - diagnostics on stderr
  - noop on API failure, hard-exit 2 on auth failure / missing key

Requires only Python stdlib (urllib, json, os, sys). In Termux:
    pkg install -y python

API key lookup order:
  1. $DEEPINFRA_API_KEY
  2. $DIA_KEY_FILE (if set)
  3. ~/.dia_key  (mode 0600 recommended)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = "https://api.deepinfra.com/v1/openai/chat/completions"
MODEL = os.environ.get(
    "PHONE_INTENT_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct"
)
TIMEOUT_S = 10.0

NOOP = {"c": "noop"}
CANON = {
    ("pose", "lean_left"):  {"c": "pose", "n": "lean_left",  "d": 1500},
    ("pose", "lean_right"): {"c": "pose", "n": "lean_right", "d": 1500},
    ("pose", "bow_front"):  {"c": "pose", "n": "bow_front",  "d": 1800},
    ("pose", "neutral"):    {"c": "pose", "n": "neutral",    "d": 1500},
}

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

FEW_SHOTS = (
    "Map the user robot command to ONE wire command JSON.\n"
    "Examples:\n"
    "lean to the left -> {\"c\":\"pose\",\"n\":\"lean_left\",\"d\":1500}\n"
    "tilt right -> {\"c\":\"pose\",\"n\":\"lean_right\",\"d\":1500}\n"
    "bow forward -> {\"c\":\"pose\",\"n\":\"bow_front\",\"d\":1800}\n"
    "stand up -> {\"c\":\"pose\",\"n\":\"neutral\",\"d\":1500}\n"
    "start walking -> {\"c\":\"walk\",\"on\":true,\"stride\":150,\"step\":400}\n"
    "halt -> {\"c\":\"stop\"}\n"
    "hop -> {\"c\":\"jump\"}\n"
    "tell me a joke -> {\"c\":\"noop\"}\n"
    "turn around -> {\"c\":\"noop\"}\n"
)


def eprint(*a, **kw):
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


def load_key() -> str:
    k = (os.environ.get("DEEPINFRA_API_KEY") or "").strip()
    if k:
        return k
    path = os.environ.get("DIA_KEY_FILE") or os.path.expanduser("~/.dia_key")
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def canonicalize(obj) -> dict:
    if not isinstance(obj, dict):
        return NOOP
    c = obj.get("c")
    if c in ("stop", "jump", "noop"):
        return {"c": c}
    if c == "walk":
        return {"c": "walk", "on": True, "stride": 150, "step": 400}
    if c == "pose":
        return CANON.get(("pose", obj.get("n")), NOOP)
    return NOOP


def extract_first_json(text: str):
    i = text.find("{")
    while i != -1:
        depth = 0
        in_str = False
        esc = False
        for j in range(i, len(text)):
            ch = text[j]
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
                        try:
                            return json.loads(text[i:j + 1])
                        except json.JSONDecodeError:
                            break
        i = text.find("{", i + 1)
    return None


def main() -> int:
    key = load_key()
    if not key:
        eprint("[phone_intent] DEEPINFRA_API_KEY not in env and ~/.dia_key missing/empty")
        return 2
    transcript = sys.stdin.read().strip()
    if not transcript:
        eprint("[phone_intent] empty stdin")
        print(json.dumps(NOOP, separators=(",", ":")))
        return 4
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": FEW_SHOTS + f"Now: {transcript} ->"},
        ],
        "temperature": 0.0,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
    except (urllib.error.URLError, TimeoutError) as e:
        eprint(f"[phone_intent] network error: {e} — noop")
        print(json.dumps(NOOP, separators=(",", ":")))
        return 0
    dt = (time.time() - t0) * 1000
    eprint(f"[phone_intent] model={MODEL} status={status} wall={dt:.0f}ms")
    if status in (401, 403):
        eprint(f"[phone_intent] AUTH FAILURE: {body[:300]}")
        return 2
    if status != 200:
        eprint(f"[phone_intent] non-200 ({status}): {body[:300]} — noop")
        print(json.dumps(NOOP, separators=(",", ":")))
        return 0
    try:
        content = json.loads(body)["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        eprint(f"[phone_intent] bad body: {body[:300]} — noop")
        print(json.dumps(NOOP, separators=(",", ":")))
        return 0
    obj = extract_first_json(content)
    if obj is None:
        eprint(f"[phone_intent] no JSON in content={content[:200]!r} — noop")
        print(json.dumps(NOOP, separators=(",", ":")))
        return 0
    print(json.dumps(canonicalize(obj), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
