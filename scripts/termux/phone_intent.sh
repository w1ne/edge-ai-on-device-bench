#!/data/data/com.termux/files/usr/bin/bash
# phone_intent.sh — Termux-hosted intent parser for edge-ai-on-device-bench.
#
# Wire-compatible drop-in for demo/parse_intent_api.py. Reads a transcript
# from stdin, calls DeepInfra's OpenAI-compatible chat/completions endpoint
# with the same system prompt + few-shots, canonicalizes the reply to one
# of the legal {"c":...} shapes, prints one JSON line on stdout.
#
# No Python. Just curl + jq. Both are in the default termux `pkg` set:
#   pkg install -y curl jq
#
# API key: read from $DEEPINFRA_API_KEY if set, else from $HOME/.dia_key
# (mode 0600). Never bake into this script — repo is public.
#
# Exit codes:
#   0  — printed a canonical JSON intent (may be {"c":"noop"} on model miss)
#   2  — missing API key
#   3  — missing curl / jq
#   4  — stdin empty
# Network / HTTP errors collapse to stdout {"c":"noop"} and exit 0, matching
# parse_intent_api.py's noop-on-failure contract so the daemon stays robust.

set -u
set -o pipefail

log() { printf '[phone_intent] %s\n' "$*" >&2; }

command -v curl >/dev/null 2>&1 || { log "curl missing — pkg install curl"; exit 3; }
command -v jq   >/dev/null 2>&1 || { log "jq missing — pkg install jq";     exit 3; }

if [ -z "${DEEPINFRA_API_KEY:-}" ]; then
  key_file="${DIA_KEY_FILE:-$HOME/.dia_key}"
  if [ -r "$key_file" ]; then
    DEEPINFRA_API_KEY="$(tr -d ' \r\n' < "$key_file")"
  fi
fi
if [ -z "${DEEPINFRA_API_KEY:-}" ]; then
  log "DEEPINFRA_API_KEY not in env and \$HOME/.dia_key missing/empty"
  exit 2
fi

transcript="$(cat)"
transcript="$(printf '%s' "$transcript" | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
if [ -z "$transcript" ]; then
  log "empty stdin"
  exit 4
fi

model_id="${PHONE_INTENT_MODEL:-meta-llama/Meta-Llama-3.1-8B-Instruct}"

# Kept in sync with demo/parse_intent_api.py SYSTEM_PROMPT + FEW_SHOTS. If
# that file changes, update here too. A drift-check would be nice but out of
# scope for the first byte.
system_prompt='You translate a spoken English robot command into exactly ONE wire command JSON object.  You must emit only valid JSON, no prose, no markdown, no commentary.
Legal shapes (emit exactly one):
  {"c":"pose","n":"lean_left","d":1500}
  {"c":"pose","n":"lean_right","d":1500}
  {"c":"pose","n":"bow_front","d":1800}
  {"c":"pose","n":"neutral","d":1500}
  {"c":"walk","on":true,"stride":150,"step":400}
  {"c":"stop"}
  {"c":"jump"}
  {"c":"noop"}
Rules:
  - If the command is a pose/walk/stop/jump synonym, map to that.
  - If unknown / chit-chat / unrelated, emit {"c":"noop"}.
  - Never invent new keys or values.  Never explain yourself.'

few_shots='Map the user robot command to ONE wire command JSON.
Examples:
lean to the left -> {"c":"pose","n":"lean_left","d":1500}
tilt right -> {"c":"pose","n":"lean_right","d":1500}
bow forward -> {"c":"pose","n":"bow_front","d":1800}
stand up -> {"c":"pose","n":"neutral","d":1500}
start walking -> {"c":"walk","on":true,"stride":150,"step":400}
halt -> {"c":"stop"}
hop -> {"c":"jump"}
tell me a joke -> {"c":"noop"}
turn around -> {"c":"noop"}
'
user_msg="${few_shots}Now: ${transcript} ->"

payload="$(jq -nc \
  --arg model "$model_id" \
  --arg sys "$system_prompt" \
  --arg usr "$user_msg" \
  '{model:$model,
    messages:[{role:"system",content:$sys},{role:"user",content:$usr}],
    temperature:0.0,
    max_tokens:64,
    response_format:{type:"object"}
   } | .response_format.type = "json_object"')"

t0="$(date +%s%3N 2>/dev/null || date +%s000)"
http_body="$(curl -sS --max-time 10 --retry 1 --retry-delay 1 \
  -H "Authorization: Bearer ${DEEPINFRA_API_KEY}" \
  -H "Content-Type: application/json" \
  -w $'\n__HTTP_STATUS__%{http_code}' \
  -d "$payload" \
  https://api.deepinfra.com/v1/openai/chat/completions 2>/dev/null)"
curl_rc=$?
t1="$(date +%s%3N 2>/dev/null || date +%s000)"

http_status="$(printf '%s' "$http_body" | awk -F'__HTTP_STATUS__' '/__HTTP_STATUS__/{print $2}' | tail -1)"
body="$(printf '%s' "$http_body" | sed -e 's/__HTTP_STATUS__[0-9]*$//')"
log "model=$model_id status=${http_status:-?} curl_rc=$curl_rc wall=$((t1 - t0))ms"

if [ "$curl_rc" -ne 0 ] || [ -z "${http_status:-}" ]; then
  log "curl failed (rc=$curl_rc) — noop"
  echo '{"c":"noop"}'
  exit 0
fi
if [ "$http_status" = "401" ] || [ "$http_status" = "403" ]; then
  log "AUTH FAILURE ($http_status): $(printf '%s' "$body" | head -c 300)"
  exit 2
fi
if [ "$http_status" != "200" ]; then
  log "non-200 ($http_status): $(printf '%s' "$body" | head -c 300) — noop"
  echo '{"c":"noop"}'
  exit 0
fi

content="$(printf '%s' "$body" | jq -r '.choices[0].message.content // empty' 2>/dev/null)"
if [ -z "$content" ]; then
  log "no content in body — noop"
  echo '{"c":"noop"}'
  exit 0
fi

# Pull the first balanced {...} out of content, then canonicalize.
# jq's `fromjson?` returns the parsed obj or null; grep -oP is not portable
# on Termux's busybox-adjacent, so do it with sed+awk.
first_json="$(printf '%s' "$content" | awk '
  BEGIN{depth=0;start=-1;in_str=0;esc=0}
  {
    for (i=1;i<=length($0);i++) {
      c=substr($0,i,1);
      if (in_str) {
        if (esc) esc=0;
        else if (c=="\\") esc=1;
        else if (c=="\"") in_str=0;
      } else {
        if (c=="\"") in_str=1;
        else if (c=="{") { if (depth==0) { start=length(buf)+i; startline=NR; startcol=i } depth++ }
        else if (c=="}") { depth--; if (depth==0) { printf "%s%s", (startline==NR?substr($0,startcol,i-startcol+1):(prevbuf substr($0,1,i))), "\n"; exit } }
      }
    }
    prevbuf=prevbuf $0 "\n"
  }
')"
if [ -z "$first_json" ]; then
  log "no JSON object in content=$(printf '%s' "$content" | head -c 120) — noop"
  echo '{"c":"noop"}'
  exit 0
fi

# Canonicalize via jq. Mirrors parse_intent_api.canonicalize().
canon="$(printf '%s' "$first_json" | jq -cM '
  . as $o
  | if type != "object" then {c:"noop"}
    elif ($o.c == "stop") then {c:"stop"}
    elif ($o.c == "jump") then {c:"jump"}
    elif ($o.c == "noop") then {c:"noop"}
    elif ($o.c == "walk") then {c:"walk",on:true,stride:150,step:400}
    elif ($o.c == "pose") then
      (if $o.n == "lean_left"  then {c:"pose",n:"lean_left",d:1500}
       elif $o.n == "lean_right" then {c:"pose",n:"lean_right",d:1500}
       elif $o.n == "bow_front"  then {c:"pose",n:"bow_front",d:1800}
       elif $o.n == "neutral"    then {c:"pose",n:"neutral",d:1500}
       else {c:"noop"} end)
    else {c:"noop"}
    end
' 2>/dev/null)"

if [ -z "$canon" ]; then
  log "canonicalize failed on $(printf '%s' "$first_json" | head -c 120) — noop"
  echo '{"c":"noop"}'
  exit 0
fi

printf '%s\n' "$canon"
