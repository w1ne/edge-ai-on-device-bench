#!/usr/bin/env python3
"""
Fast variant of parse_intent.py: talks to a persistent on-phone llama-server
(see scripts/start_llm_server.sh) via HTTP instead of spawning llama-cli on
every call.  The model stays resident on the phone, so warm calls are <1 s
instead of ~25 s.

Usage:
    # one-time: start the server in another shell/tab
    scripts/start_llm_server.sh --phone p20 --model gemma

    # then:
    python3 demo/parse_intent_fast.py "walk forward please"
    python3 demo/parse_intent_fast.py --model gemma "jump"

CLI surface and JSON output are identical to demo/parse_intent.py so this file
is a drop-in replacement from the caller's side (robot_daemon.py etc.).

Prints one JSON line on stdout.  Timeouts / HTTP errors fall back to
{"c":"noop"} and log the cause on stderr.

The grammar JSON and few-shot prompt are the SAME as parse_intent.py so the
two scripts produce the same answers modulo decoding noise (which is zero
since we use temp=0, top_k=1).
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# Mirror the constants in parse_intent.py so behaviour matches.  Kept as
# literals (not imported) to keep this file additive and standalone.
SERVER_URL = "http://127.0.0.1:18080"
ENDPOINT_COMPLETION = SERVER_URL + "/completion"
ENDPOINT_HEALTH = SERVER_URL + "/health"

MODELS = {
    "tinyllama": {"n_predict": 32},
    "gemma":     {"n_predict": 64},
    "gemma4":    {"n_predict": 64},  # Gemma 4 E2B Q4_K_S, ~3 GB — Pixel 6 only
}
DEFAULT_MODEL = "tinyllama"

NOOP = {"c": "noop"}
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

# Same grammar schema as parse_intent.py — pinned syntactic validity of the
# emitted wire-command JSON regardless of which model is behind the HTTP API.
SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "properties": {
                "c": {"const": "pose"},
                "n": {"enum": ["lean_left", "lean_right",
                               "bow_front", "neutral"]},
                "d": {"type": "integer"},
            },
            "required": ["c", "n", "d"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {
                "c": {"const": "walk"},
                "on": {"const": True},
                "stride": {"type": "integer"},
                "step": {"type": "integer"},
            },
            "required": ["c", "on", "stride", "step"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"c": {"enum": ["stop", "jump", "noop"]}},
            "required": ["c"],
            "additionalProperties": False,
        },
    ]
}

PROMPT_TEMPLATE = (
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
    "Now: %s ->"
)


def eprint(*a, **kw):
    kw.setdefault("file", sys.stderr)
    kw.setdefault("flush", True)
    print(*a, **kw)


def canonicalize(obj):
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


def http_post_json(url, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def run_llama_server(prompt, model_key, timeout=90):
    """POST a completion request. Returns the generated text (str) or None."""
    cfg = MODELS[model_key]
    body = {
        "prompt": prompt,
        "n_predict": cfg["n_predict"],
        "temperature": 0.0,
        "top_k": 1,
        "cache_prompt": True,     # amortize few-shot prompt across calls
        "stream": False,
        # Server accepts a JSON-schema object via the `json_schema` field and
        # internally converts it to GBNF — same effect as `-jf schema.json`.
        "json_schema": SCHEMA,
        # A short stop list helps cut the generation off after the first
        # balanced object in case the model keeps emitting.
        "stop": ["\nNow:", "\n\n"],
    }
    try:
        reply = http_post_json(ENDPOINT_COMPLETION, body, timeout=timeout)
    except urllib.error.URLError as e:
        eprint(f"[parse_intent_fast] HTTP error: {e}")
        return None
    except TimeoutError:
        eprint("[parse_intent_fast] HTTP timeout")
        return None
    except Exception as e:
        eprint(f"[parse_intent_fast] unexpected error: {e!r}")
        return None
    return reply.get("content", "")


def parse_intent(transcript, model_key=DEFAULT_MODEL):
    prompt = PROMPT_TEMPLATE % transcript
    t0 = time.time()
    gen = run_llama_server(prompt, model_key)
    dt = time.time() - t0
    eprint(f"[parse_intent_fast] model={model_key} wall={dt:.2f}s")
    if gen is None:
        return NOOP
    eprint(f"[parse_intent_fast] generated: {gen[:200]!r}")
    obj = extract_first_json(gen)
    if obj is None:
        eprint("[parse_intent_fast] no JSON in generated region")
        return NOOP
    return canonicalize(obj)


def main(argv):
    ap = argparse.ArgumentParser(
        prog="parse_intent_fast.py",
        description="Same contract as parse_intent.py but talks to the "
                    "persistent on-phone llama-server over HTTP.",
    )
    ap.add_argument(
        "--model",
        choices=sorted(MODELS.keys()),
        default=DEFAULT_MODEL,
        help=f"model the server was started with (default: {DEFAULT_MODEL}). "
             "The HTTP server only serves one model at a time; this flag "
             "only affects n_predict on the client side.",
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
