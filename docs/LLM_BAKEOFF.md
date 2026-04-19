# LLM Bakeoff — Planner Model Selection

**Date:** 2026-04-19
**Harness:** `scripts/planner_eval.py` (21 cases across 5 groups, `max_steps=10`, `temperature=0.0`)
**Endpoint:** DeepInfra OpenAI-compatible `/chat/completions` with `tools=[...]` + `tool_choice="auto"`
**Time cap:** 300 s per full 21-case run. Exceed it = disqualified for latency.
**Result JSON:** `results/<tag>.json` (one per model)

## Why this exists

The Planner defaults to `meta-llama/Meta-Llama-3.1-8B-Instruct` purely because the existing DeepInfra client already pointed at it. That was expedience, not evidence — roast point #7 in the reliability audit. This doc is the evidence.

## Panel

| Model | Invoked id | Status |
|---|---|---|
| Llama-3.1-8B | `meta-llama/Meta-Llama-3.1-8B-Instruct` | ran |
| Llama-3.3-70B | `meta-llama/Llama-3.3-70B-Instruct` (Turbo served) | ran |
| Qwen2.5-72B | `Qwen/Qwen2.5-72B-Instruct` | ran |
| DeepSeek-V3.2-Exp | `deepseek-ai/DeepSeek-V3.2-Exp` | **DNF — >300 s** |
| Gemma-3-27B | `google/gemma-3-27b-it` | ran |
| Mixtral-8x7B | `mistralai/Mixtral-8x7B-Instruct-v0.1` | ran |

DeepSeek-V3.2-Exp is available on DeepInfra (chat completions verified with `curl`) but exceeded the 300 s time cap on the full 21-case run — in both attempts the process was SIGTERMed before reporting results. Disqualified on latency.

### DeepSeek retry — post-DNF probe (2026-04-20)

Followup probe ran a single-turn smoke test with a 3-tool schema:
`tools_calls=1 (jump({}))`, total wall **4.79 s**, prompt=409 / completion=41 tokens. Then ran the full planner loop on "Jump once and tell me you are happy" (8-tool schema): 3 steps, **11.91 s wall** (~4 s per LLM turn).

Verdict: **DeepSeek-V3.2 is not structurally broken** — the DNF was a
scope issue. The model emits one tool call per turn (no parallel calls)
so a 3-step goal costs ~3 × 4 s = 12 s of LLM wall, versus Qwen-2.5-72B
which parallelises and finishes the same goal in ~2 s. Extrapolated, a
21-case run that averages 3 steps/case lands at ~4 minutes — just
barely within the 300 s cap, but with zero headroom.

Not worth retrying at scale: even if it passed every case, the 5-6×
per-goal latency disadvantage vs Qwen makes it a non-starter for an
interactive voice agent. Result stays as DNF with the asterisk that the
cause is "serial tool calls + slow throughput," not "broken
tool-calling."

## Results

| Model | Score | Median latency | Total wall | Failures |
|---|---:|---:|---:|---|
| **Qwen/Qwen2.5-72B-Instruct** | **20/21** | 3.07 s | 71.7 s | C3-find-keys (chose new `look_for` tool; harness still wants `look`) |
| meta-llama/Meta-Llama-3.1-8B-Instruct | 20/21 | **2.18 s** | **53.1 s** | C2-look-person-absent (hallucinated "Hello!" when no person seen) |
| meta-llama/Llama-3.3-70B-Instruct | 13/21 | 4.02 s | 102.7 s | 8 cases, 7 of them `no_tool_called` — model frequently returned empty text instead of invoking a tool |
| google/gemma-3-27b-it | 6/21 | 4.69 s | 100.1 s | 15 `no_tool_called` — Gemma-3 via this endpoint does not reliably emit OpenAI tool_calls |
| mistralai/Mixtral-8x7B-Instruct-v0.1 | 0/21 | 3.48 s | 76.0 s | All 21 — Mixtral-8x7B does not produce tool_calls with this schema at all |
| deepseek-ai/DeepSeek-V3.2-Exp | DNF | — | >300 s | Time-capped |

### Failure notes

- **Llama-3.1-8B / C2** — after 5 consecutive empty `look()` results the 8B planner still says "Hello!" as if it found the person. Classic small-model lack of negation tracking.
- **Qwen2.5-72B / C3** — Qwen picks `look_for("keys")` which *is* the better tool (open-vocab, scored result) and correctly announces "I couldn't find your keys." The C3 expectation is `"look" in tools`, so the harness fails it. Harness quirk, not a Qwen defect. Real robot behaviour is correct.
- **Llama-3.3-70B** — 7 of 8 failures are "no tool called". The Turbo variant silently returns an empty `content` with `tool_calls=null` on some prompts (notably short factual questions in group D and several C cases). Strange for a 70B model; probably an artefact of DeepInfra's Turbo tool-calling template being broken for this size.
- **Gemma-3-27B** — same `no_tool_called` symptom, consistent with Gemma-3's known weakness: its tool-call emission is unreliable without custom formatting. This endpoint does not apply that formatting.
- **Mixtral-8x7B** — clean zero. The 8x7B tokenizer/template on DeepInfra does not route through a tool-call path at all. It emits prose only.

## Cost — rough order of magnitude

Very rough estimate per 1,000 planner runs (assuming ~1,500 prompt tokens summed across turns and ~200 output tokens per run — actual token count depends on the goal; real usage could be half or double this).

| Model | $/1M in | $/1M out | ~$/1k runs | ~$/day @ 1k runs/day |
|---|---:|---:|---:|---:|
| Llama-3.1-8B | 0.02 | 0.05 | 0.04 | **$0.04** |
| Gemma-3-27B | 0.08 | 0.16 | 0.15 | $0.15 |
| Llama-3.3-70B Turbo | 0.10 | 0.32 | 0.21 | $0.21 |
| Qwen2.5-72B | 0.12 | 0.39 | 0.26 | $0.26 |
| Mixtral-8x7B | 0.54 | 0.54 | 0.92 | $0.92 |
| DeepSeek-V3.2 (proxy) | 0.26 | 0.38 | 0.47 | $0.47 |

For a 24/7 robot making ~1 planner call per minute when active (~1k calls/day), Qwen2.5-72B costs roughly **6.5× more than Llama-3.1-8B** ($0.26/day vs $0.04/day — ~$95/yr vs $15/yr). For a hobby project this is a rounding error; for a fleet of 100 robots it starts to matter.

## Winner

**Qwen/Qwen2.5-72B-Instruct.**

Rationale:
- Top score on the panel (20/21; tied with the 8B baseline in raw count but by different failures).
- Its one failure is a **harness expectation bug**, not a model bug. It correctly used the newer `look_for` tool and correctly said "I couldn't find your keys." Realistic robot behaviour is *better* than Llama-3.1-8B's on this case.
- Actually passes C2-look-person-absent — it does not hallucinate a greeting when no one is there, which is the exact safety-critical case the 8B model fails today.
- 3.07 s median vs 2.18 s for the 8B: +0.9 s per planner turn. For a legged robot that typically waits 2–3 s for a voice turn anyway, this is human-imperceptible.
- ~6.5× the cost of the 8B at ~$0.26/day for a busy robot — still trivial in absolute terms.

Runner-up: Llama-3.1-8B is the right default only if you actively need sub-3s responses AND accept the C2-style hallucination as acceptable. For anything goal-oriented involving negation ("if X, else Y") the 8B is a risk.

Notable non-findings: **A bigger Llama didn't help.** Llama-3.3-70B via DeepInfra's Turbo path is *worse* than the 8B at 13/21 because of an endpoint-level tool-calling bug (7 `no_tool_called`). Mixtral-8x7B and Gemma-3-27B likewise simply don't do OpenAI-style tool-calling through this endpoint — they're not replacements at any price.

## Recommendation

Switch `demo/robot_planner.py` default from `Meta-Llama-3.1-8B-Instruct` to `Qwen/Qwen2.5-72B-Instruct`. Signature-compatible — only the `model=` default kwarg changes. Daemon code that passes `Planner(tools, model=...)` continues to work; nothing passes `model=` today.

Cost impact: negligible at current usage. If a cost regression ever matters we can revisit — the bakeoff is easy to rerun.

## Reproduction

```bash
set -a; source ~/Projects/AIHW/.env.local; set +a
mkdir -p results
for m in \
    "meta-llama/Meta-Llama-3.1-8B-Instruct" \
    "meta-llama/Llama-3.3-70B-Instruct" \
    "Qwen/Qwen2.5-72B-Instruct" \
    "google/gemma-3-27b-it" \
    "mistralai/Mixtral-8x7B-Instruct-v0.1" ; do
  tag=$(echo "$m" | tr '/' '-' | tr '[:upper:]' '[:lower:]')
  timeout 300 python3 scripts/planner_eval.py --model "$m" --json "results/${tag}.json"
done
```
