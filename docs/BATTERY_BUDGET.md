# Battery budget — the hw_stress_test voltage gate

## What we measured

First live 30-second run of `scripts/hw_stress_test.py` on 2026-04-20
(see `logs/hw_stress_20260420-005037.*`):

- Resting voltage under light load: **6.90 V** (2S Li-ion pack nominally 7.4 V)
- Low-voltage alert threshold: **6.50 V** (`VOLT_LOW_V` in both daemon
  and stress test)
- Headroom at start of that run: **0.40 V**

That is not a lot.

## Why it matters

The ESP32 draws bursts through the 3.3 V LDO, and the STS3032 servos pull
~180 mA nominal and 800 mA+ under torque. Each pose command is a burst;
walking (not something we do on a table) would be a sustained draw.

Observed failure mode from earlier sessions: the MPU-6050 `begin()` inside
the ESP32 firmware silently fails on boot when the rail briefly drops
under LDO dropout, and the chip then streams all-zero IMU data until a
power cycle. A starting pack at 6.90 V can hit dropout on the first pose
command.

## Rule encoded in `hw_stress_test.py`

Pre-flight gate runs 1.5 s of telemetry before the main loop starts:

| Start voltage | Behavior |
|---|---|
| `v < 6.80 V` | **refuse to run** — return exit code 3 and tell user to charge |
| `6.80 V ≤ v < 7.20 V` | **cap duration at 600 s (10 min)** with a log warning |
| `v ≥ 7.20 V` | full `--duration` allowed |

Override available: `--ignore-battery-budget` skips the gate entirely.
Use only for diagnostic runs on a pack you've validated elsewhere.

## What we still don't know

- The **exact** voltage trajectory over a 30-minute stress run on a
  freshly charged pack. We've measured 30 seconds. Linear extrapolation
  is unreliable for Li-ion SOC curves — they're flat then fall off a
  cliff near empty.
- Whether the 2S pack can sustain 30 min of pose+jump cycling without
  tripping the 6.5 V alert. This is a **hardware test the user must
  schedule** — see `docs/AGENT_ROADMAP.md` "What's NOT compressible by
  agents."
- Whether a 3S pack would fix this (higher starting voltage at the cost
  of more weight). Design decision, not software.

## How to unblock the 30-minute test

1. Fully charge the pack (per-cell to 4.2 V → total ~8.4 V at rest).
2. Wait 10 minutes for surface charge to dissipate (resting voltage is
   what matters, not charger-off voltage).
3. Start `python3 scripts/hw_stress_test.py --duration 1800` under
   supervision.
4. If the pre-flight refuses, the pack isn't ready — recharge or
   replace.

If refusal persists with fully-charged cells: the pack has degraded and
should be replaced. 2S Li-ion packs at age > 1 year with heavy servo
usage commonly drop their resting voltage by 0.3–0.5 V per year.

## Next steps when we have real data

Replace the empirical numbers in this doc and in `VOLT_SAFE_FULL_V` /
`VOLT_MIN_START_V` with a measured discharge curve for this specific
pack. Until then, the thresholds are conservative guesses.
