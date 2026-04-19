#!/usr/bin/env python3
"""
Free-form transcript -> structured robot wire-command via on-phone LLM.

Usage:
    python3 demo/parse_intent.py "walk forward please"
    python3 demo/parse_intent.py --model gemma "walk forward please"
    python3 demo/parse_intent.py --model tinyllama "walk forward please"

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

Assumptions: adb-connected phone with
/data/local/tmp/{tinyllama.gguf,gemma3.gguf,llama-cli}.
The llama-cli build on this phone (b1-3f7c29d) does NOT honor -no-cnv for
either model; -st (single-turn) is required to exit cleanly.  Gemma 3 1B
scored 7/8 on the self-test suite vs TinyLlama's 4/8, so --model gemma is
the better intent parser when the ~25-60 s per-phrase latency is tolerable.
The grammar file is pushed once on the first call and reused (schema.json).
"""
import argparse
import sys
import json
import subprocess
import time

LLAMA = "./llama-cli"
PHONE_DIR = "/data/local/tmp"
SCHEMA_PHONE = f"{PHONE_DIR}/schema.json"
THREADS = 8

# Per-model settings. N_PREDICT is larger for Gemma because the Gemma chat
# template wraps the prompt in role markers and the model sometimes burns a
# couple of tokens before emitting the constrained JSON.
MODELS = {
    "tinyllama": {
        "file": "tinyllama.gguf",
        "n_predict": 32,
    },
    "gemma": {
        "file": "gemma3.gguf",
        "n_predict": 64,
    },
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


def run_llama(prompt, model_key):
    """Invoke llama-cli over adb. Returns (stdout, stderr, exit_code)."""
    cfg = MODELS[model_key]
    model_file = cfg["file"]
    n_predict = cfg["n_predict"]
    # Single-quote-wrap the prompt; escape embedded ' as '\''.
    quoted = "'" + prompt.replace("'", "'\\''") + "'"
    # `-st` single-turn is the key to non-interactive exit on this build
    # for both TinyLlama and Gemma (the Gemma chat template otherwise
    # forces conversation mode and blocks on stdin). `< /dev/null` is
    # defence-in-depth: kills the REPL if -st ever regresses.
    shell_cmd = (
        f"cd {PHONE_DIR} && {LLAMA} -m {model_file} -p {quoted} "
        f"-n {n_predict} -t {THREADS} --no-warmup -st --simple-io "
        f"--log-disable --no-perf --temp 0 --top-k 1 "
        f"-jf {SCHEMA_PHONE} < /dev/null 2>&1"
    )
    try:
        r = subprocess.run(
            ["adb", "shell", shell_cmd],
            capture_output=True, text=True, timeout=120,
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


def parse_intent(transcript, model_key=DEFAULT_MODEL):
    if not ensure_schema_on_phone():
        return NOOP
    prompt = PROMPT_TEMPLATE % transcript
    t0 = time.time()
    stdout, stderr, code = run_llama(prompt, model_key)
    dt = time.time() - t0
    eprint(f"[parse_intent] model={model_key} adb exit={code} wall={dt:.2f}s")
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
    ap = argparse.ArgumentParser(
        prog="parse_intent.py",
        description="Map a free-form transcript to a robot wire command "
                    "via llama-cli on the phone.",
    )
    ap.add_argument(
        "--model",
        choices=sorted(MODELS.keys()),
        default=DEFAULT_MODEL,
        help=(f"on-phone model to use (default: {DEFAULT_MODEL}). "
              "gemma has higher accuracy on single-word and out-of-vocab "
              "inputs but ~2x wall-clock latency."),
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
