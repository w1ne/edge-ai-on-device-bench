# Morning 3 — full stack running

Autonomous run while you were at the grocery store. Scope executed: test all reasoning paths, pick the one that works, wire the whole robot stack end-to-end, don't force slow hardware.

## TL;DR

**The full stack is wired and end-to-end green in dry-run.** Voice → intent (keyword matcher primary, DeepInfra API fallback for out-of-vocab phrases) → wire command → servos → spoken ack. Vision → webcam at 20 FPS → BehaviorEngine state machine → auto-stop on close obstacle, greet on new class, follow-me on person drift.

Run it:

```bash
source ~/Projects/AIHW/.env.local        # export DEEPINFRA_API_KEY
python3 demo/robot_daemon.py --with-vision person --vision-phone pixel6 \
                             --with-llm --llm-api api --log logs/robot.log
```

## What I tested, what won

**Reasoning / intent parsing — 8-phrase self-test:**

| backend | accuracy | median latency | notes |
|---|---:|---:|---|
| keyword matcher (regex) | 14/14 of its set | <1 ms | primary path, zero cost |
| TinyLlama 1.1B Q4_0 (local) | 4/8 | ~25 s/call (reload), ~3.5 s (server) | collapses to first schema option |
| Gemma 3 1B Q4_0 (local) | 7/8 | ~27 s (reload), ~2 s (server, Pixel 6) | misses "tell me a joke" → "stop" |
| Gemma 4 E2B Q4_K_S (local, Pixel 6 only) | 5/8 | 25-90 s/call | more conservative, too cautious for our use; 3 GB doesn't fit P20 |
| **DeepInfra Llama-3.1-8B (API)** | **8/8** | **1.18 s/call** | **winner** |

**Verdict:** keyword matcher handles the vast majority; when it misses, hit the API. Local LLM paths stay behind `--llm-api local` for offline use but are no longer the default.

**Vision source:**

| source | FPS | latency | ready? |
|---|---:|---:|---|
| `adb screencap` (default) | 2 | ~400 ms | fine for debug, useless for reaction |
| **USB webcam (`--source webcam`)** | **19.9** | **p50 11 ms** | **winner** |

## Files that matter

- `demo/robot_daemon.py` — main loop. Flags: `--mode {voice,text}`, `--with-llm --llm-api {local,api}`, `--llm-model {gemma,gemma4,tinyllama}`, `--llm-fast`, `--with-vision <class>`, `--vision-phone {pixel6,p20}`, `--vision-interval`, `--dry-run`, `--no-tts`, `--log`.
- `demo/robot_behaviors.py` — `BehaviorEngine` state machine (idle / walking / paused / following). Greets new classes once per 30 s, stops when a close obstacle (`bbox_h >= img_h*0.4` at `conf >= 0.6`) appears while walking, auto-resumes after 5 s clear, follows a person via `lean_left` / `lean_right` when they drift off center.
- `demo/vision_watcher.py` — JSONL stream of `tick` + `event` lines. `--source {phone,webcam}`, `--watch-for <class>`, `--min-streak`, `--threshold`.
- `demo/parse_intent_api.py` — DeepInfra path. Default model `meta-llama/Meta-Llama-3.1-8B-Instruct`. Requires `DEEPINFRA_API_KEY` env var.
- `demo/parse_intent_fast.py` — persistent on-phone llama-server path (CPU-only, via `scripts/start_llm_server.sh --phone pixel6 --model gemma`).
- `demo/parse_intent.py` — reload-per-call local path (kept for reference).
- `demo/eyes.py` — one-shot YOLO, still useful for ad-hoc detection.
- `scripts/start_llm_server.sh` — `--phone {pixel6,p20} --model {gemma,gemma4,tinyllama}`. `gemma4` refuses P20 (3 GB won't fit 3.7 GB RAM).

## Security note

**⚠️ DeepInfra API key rotation required.** I briefly committed the literal key (`kbW…`) to this public repo while wiring the API path. Caught it within minutes, purged the string from `HEAD` and every historical commit with `git-filter-repo`, force-pushed `main`. But GitHub caches public objects for a short window — any attacker cloning between the push and the filter-repo has the key.

**Action:** rotate the DeepInfra key (dashboard → settings → API keys → revoke + regenerate), update `~/Projects/AIHW/.env.local` with the new value. The repo code already reads from env only.

## What I didn't do (and why)

- **TFLite + NNAPI / Edge TPU port.** Days of work; the carousel / benchmarks are honest without it. Noted as the next real-hardware investment.
- **Firmware IMU fix.** `docs/FIRMWARE_TODO.md` is the plan — you said "next iteration", and it's a different repo.
- **Android Camera2 feed.** Webcam was fast enough; scrcpy was a fallback I didn't need. Termux + Camera2 stays in reserve for when the robot carries a phone as its only camera.
- **Gemma 4 on P20.** It's 3 GB Q4_K_S — would OOM on P20's 3.7 GB. `start_llm_server.sh` refuses with a helpful error message.

## Good to know

- Keyword matcher + API fallback is both fast *and* accurate. The local LLM paths are essentially offline-only now — they'd be the right choice if we lose internet.
- `drain_vision_events` now delegates to `BehaviorEngine` when `--with-vision` is on. The old inline greeting logic is still there for `--with-vision` off, for backward compatibility.
- Everything is on `main` at `7f9ee44`. Commit email is still correct (`14119286+w1ne@users.noreply.github.com`).
