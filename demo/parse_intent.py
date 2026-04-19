#!/usr/bin/env python3
"""
Free-form transcript -> structured robot wire-command via TinyLlama on phone.

Usage:
    python3 demo/parse_intent.py "walk forward please"

Prints a single JSON line on stdout. Everything else (llama banner, timing,
errors) goes to stderr. Output is one of the legal wire commands below or
{"c":"noop"} when the transcript doesn't match any known verb / pose.

Legal schemas (the parser will downgrade anything else to noop):
    {"c":"pose","n":"lean_left","d":1500}
    {"c":"pose","n":"lean_right","d":1500}
    {"c":"pose","n":"bow_front","d":1800}
    {"c":"pose","n":"neutral","d":1500}
    {"c":"walk","on":true,"stride":150,"step":400}
    {"c":"stop"}
    {"c":"jump"}
    {"c":"noop"}

Pipeline:
    build short few-shot prompt
 -> adb shell llama-cli -st -jf schema.json -n 32 --temp 0
 -> slice out the generated region (after banner and echoed prompt)
 -> json.loads first balanced {...}
 -> canonicalize / noop on failure
 -> print one JSON line.

Assumptions: adb-connected phone with /data/local/tmp/{tinyllama.gguf,llama-cli}.
The llama-cli build on this phone (b1-3f7c29d) does NOT honor -no-cnv, so we
use -st (single-turn) + --simple-io. The grammar file is pushed once on the
first call and reused (schema.json).
"""
import sys
import json
import subprocess
import time

MODEL = "tinyllama.gguf"
LLAMA = "./llama-cli"
PHONE_DIR = "/data/local/tmp"
SCHEMA_PHONE = f"{PHONE_DIR}/schema.json"
THREADS = 8
N_PREDICT = 32

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

# JSON schema used as a grammar constraint. Forces llama.cpp to emit
# ONLY one of the legal command shapes. Semantic correctness still
# comes from the few-shot prompt, but syntactic validity is guaranteed.
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

# Short few-shot prompt. Kept small: TinyLlama-Chat's chat template
# wraps this as a single user turn and the grammar forces one JSON object.
# No system preamble -- single-turn chat templates on this build add one.
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
    """Map a parsed dict to a canonical wire command, or noop."""
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


_schema_pushed = False


def ensure_schema_on_phone():
    """Push the grammar schema once per process; it is tiny (<1kB)."""
    global _schema_pushed
    if _schema_pushed:
        return True
    payload = json.dumps(SCHEMA, separators=(",", ":"))
    # Write via shell redirection; payload is ASCII and has no single quotes.
    r = subprocess.run(
        ["adb", "shell", f"cat > {SCHEMA_PHONE}"],
        input=payload, capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        eprint(f"[parse_intent] failed to push schema: {r.stderr!r}")
        return False
    _schema_pushed = True
    return True


def run_llama(prompt):
    """Invoke llama-cli over adb. Returns (stdout, stderr, exit_code)."""
    # Single-quote-wrap the prompt; escape embedded ' as '\''.
    quoted = "'" + prompt.replace("'", "'\\''") + "'"
    shell_cmd = (
        f"cd {PHONE_DIR} && {LLAMA} -m {MODEL} -p {quoted} "
        f"-n {N_PREDICT} -t {THREADS} --no-warmup -st --simple-io "
        f"--log-disable --no-perf --temp 0 --top-k 1 "
        f"-jf {SCHEMA_PHONE} 2>&1"
    )
    try:
        r = subprocess.run(
            ["adb", "shell", shell_cmd],
            capture_output=True, text=True, timeout=90,
        )
        return r.stdout, r.stderr, r.returncode
    except FileNotFoundError:
        eprint("[parse_intent] adb not on PATH")
        return "", "adb-not-found", 127
    except subprocess.TimeoutExpired:
        eprint("[parse_intent] llama-cli timed out")
        subprocess.run(
            ["adb", "shell", "killall llama-cli 2>/dev/null || true"],
            capture_output=True, text=True, timeout=10,
        )
        return "", "timeout", 124


# Generated region is bounded below by the last "-> " from the prompt
# echo, and above by "[ Prompt:" or "Exiting...". We key on "Now: ...
# -> " because our prompt guarantees it.
def slice_generation(raw, transcript):
    # Drop trailer.
    for marker in ("[ Prompt:", "Exiting...", "llama_memory_breakdown"):
        idx = raw.find(marker)
        if idx != -1:
            raw = raw[:idx]
    # The prompt ends with "Now: <transcript> ->"; find that literal
    # (the device echoes the prompt back so this anchor exists).
    anchor = f"Now: {transcript} ->"
    idx = raw.rfind(anchor)
    if idx != -1:
        return raw[idx + len(anchor):].strip()
    # Fallback: last "-> " after the echoed prompt list.
    idx = raw.rfind("-> ")
    if idx != -1:
        return raw[idx + 3:].strip()
    return raw.strip()


def parse_intent(transcript):
    if not ensure_schema_on_phone():
        return NOOP
    prompt = PROMPT_TEMPLATE % transcript
    t0 = time.time()
    stdout, stderr, code = run_llama(prompt)
    dt = time.time() - t0
    eprint(f"[parse_intent] adb exit={code} wall={dt:.2f}s")
    if code != 0:
        eprint(f"[parse_intent] adb/llama failed: {stderr[:200]!r}")
        return NOOP
    gen = slice_generation(stdout, transcript)
    eprint(f"[parse_intent] generated: {gen[:200]!r}")
    obj = extract_first_json(gen)
    if obj is None:
        eprint("[parse_intent] no JSON in generated region")
        return NOOP
    return canonicalize(obj)


def main(argv):
    if len(argv) < 2:
        eprint(__doc__)
        sys.exit(1)
    transcript = " ".join(argv[1:]).strip()
    if not transcript:
        print(json.dumps(NOOP, separators=(",", ":")))
        return
    out = parse_intent(transcript)
    print(json.dumps(out, separators=(",", ":")))


if __name__ == "__main__":
    main(sys.argv)
