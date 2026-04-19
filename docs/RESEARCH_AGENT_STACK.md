# Research: the 2026 state of the art for legged robot agents

Scope: what do people *actually ship* when building "voice-in, vision-in, legs-out, acts-on-its-own" robots? Not vapor. Stuff with code or a paper with code. We're going to stand on these shoulders.

Read time: ~15 min. Everything below links to source.

---

## 1. The shape the field has converged on

Strip away the PR and every serious legged/mobile agent ends up with **six layers**. They come in the same order, even when the labels differ:

```
  user goal  ───────────────────────────────────────────────────────
      │  (voice / text / button)
      ▼
  [1] Voice I/O       ── STT + TTS + VAD + interrupt/barge-in
      │
      ▼
  [2] Language + planner  (LLM with tool-use OR hierarchical code-as-policies)
      │         outputs a sequence of calls to…
      ▼
  [3] Behavior primitives  (walk_to, turn, look_for, grasp, release, wait)
      │            each primitive is a policy, scripted OR learned
      ▼
  [4] Spatial / scene memory  (voxel map, 2D occupancy, scene graph)
      │            updated every tick by…
      ▼
  [5] Perception      ── VLM + detector + depth + SLAM pose
      │
      ▼
  [6] Low-level control  ── gait controller + balance + servo driver
                           (the part that crashes the robot if it goes wrong)
```

You can see this in OK-Robot, DynaMem, SayCan/PaLM-SayCan, Stretch AI, LeRobot — the labels vary but the boxes are the same. When we read someone's code we can map their files onto this diagram within ~5 minutes.

The papers that canonicalised this pattern:
- **SayCan** (Google, 2022) — first to put LLM at layer 2 and learn a value function per skill to ground the plan. Planning success 84 %, execution 74 %. [say-can.github.io](https://say-can.github.io/) · [arXiv](https://say-can.github.io/assets/palm_saycan.pdf)
- **Code-as-Policies** (Google, 2022) — LLM emits Python that calls robot primitives. Turns out "plan" and "code" are the same thing.
- **PaLM-E** (Google, 2023) — multimodal LLM directly on the robot; images + text in, plan out.
- **OK-Robot** (NYU + Meta, CoRL 2024) — first credible open modular stack that actually walks around arbitrary homes and achieves 58.5 % on zero-shot pick-and-drop. This is the template we should steal from. [site](https://ok-robot.github.io/) · [arXiv 2401.12202](https://arxiv.org/abs/2401.12202) · [GitHub](https://github.com/ok-robot/ok-robot)
- **DynaMem** (Meta, NeurIPS 2024) — upgrade to OK-Robot's memory: objects move, memory updates. 70 % success on dynamic scenes, 2× the prior SOTA. [site](https://dynamem.github.io/) · [arXiv 2411.04999](https://arxiv.org/abs/2411.04999)

---

## 2. Who's shipping what (open source we can actually use)

| System | Who | What it solves | License | Runs on |
|---|---|---|---|---|
| **LeRobot** | HuggingFace | End-to-end: data collection → training → policy deploy. Ships VLAs (π0, π0.5, SmolVLA), imitation/RL trainers, ROS 2 bridge. First-class Unitree G1 support added in v0.5 | Apache 2 | PyTorch + ROS 2 docker |
| **OK-Robot** | NYU + Meta | Modular: navigation (VoxelMap) + manipulation (AnyGrasp) + language (Lang-SAM, CLIP). No training, zero-shot | MIT | Python 3.9 + ROS on Stretch, GPU workstation |
| **DynaMem** | Meta | OK-Robot's spatial memory, upgraded for dynamic scenes | MIT | Same platform |
| **Stretch AI** | Hello Robot | Voice + VLM + navigation on their Stretch 3 mobile manipulator. Open source, works with LeRobot | Apache 2 | Stretch hardware + GPU desktop |
| **OpenVLA** | Stanford | 7B-param open-source VLA, trained on 970 k real demos. Beats RT-2 by 16 % with 7× fewer params. OFT fine-tune recipe is 25-50× faster | MIT | 15 GB VRAM at fp16, ~6 Hz on RTX 4090 — too big for edge |
| **SmolVLA** | HuggingFace | Small VLA for hobby scale; LoRA-tunable via PEFT. Fits in LeRobot | Apache 2 | Reasonable GPU |
| **π0 / π0.5** | Physical Intelligence | SOTA generalist policy. π0.5 is VLM-init + action expert, more compute-efficient | Apache 2 via LeRobot |  |
| **Pipecat** | Pipecat team | Voice agent orchestration (VAD/STT/LLM/TTS with barge-in). 40+ model plugins. The right shape for layer 1 | BSD-2 | Python, works with LiveKit / WebRTC |
| **LiveKit Agents** | LiveKit | WebRTC transport + agent SDK. Pair with Pipecat for voice layer | Apache 2 |  |
| **Stanford Doggo / Pupper** | Nathan Kau et al. | Open-source quadruped hardware + firmware + IMU balance + trot gait | MIT | Teensy 4.0 + Raspberry Pi |
| **MIT Cheetah 3** | MIT Biomimetics | Proprioceptive balance: IMU orientation + leg-kinematics EKF to estimate base velocity | paper only, no open code |  |

Paid / proprietary (useful for reference, not adoption): Figure 02 (OpenAI VLM), Tesla Optimus (FSD-adjacent stack), Unitree UnifoLM-VLA-0 (weights released but stack is custom), Boston Dynamics Spot + ChatGPT (demo only).

---

## 3. How OK-Robot actually works (the template we should copy)

Because it's the closest in spirit to what we want — modular, open, home-scale, no training — and because the arch is small enough to fit in this doc.

**Input:** "pick up the blue cup and drop it in the sink."

**Pipeline:**

1. **Scan phase (once per environment, ~5 min).** User walks the robot around with a depth camera. System builds a **VoxelMap**:
   - For each frame, run **OWL-ViT** (open-vocab detector) → masks for anything it can name.
   - Back-project masks to 3D using depth + pose → coloured point cloud.
   - Voxelise at **5 cm** resolution.
   - Each voxel stores a **CLIP embedding**, weighted-averaged by detector confidence across frames.
   - Net result: a 3D grid where every cell has a vector "meaning". Ask "blue cup", dot-product similarity returns the voxel(s).

2. **Navigation.** Query → target voxel → A* path planner → send waypoints to the mobile base's `move_base`.

3. **Grasping.** Once at the target:
   - **AnyGrasp** runs on the wrist RGB-D frame → many 6-DoF candidate grasps.
   - **LangSAM** (LangSAM = Language-SAM) segments the image for "blue cup" → object mask.
   - Filter grasps: keep only those whose projected 2D point is inside the mask.
   - Score, pick best, execute via motion planner.

4. **Drop.** "Sink" goes back through LangSAM → median x/y of the mask + 0.2 m height buffer → release.

**Key insights the paper defends (empirically tested):**
- Modular > end-to-end VLA for tasks this simple. 58.5 % zero-shot with no training.
- The VoxelMap is the star. Language → 3D coordinate is the hardest thing to do well, and they do it well.
- CLIP embeddings work fine for objects; they struggle with spatial relations ("the cup *next to* the sink"). Pairing with an LLM helps for compound queries.

**DynaMem's upgrade:** instead of a one-shot scan, the voxel map is **online**. The robot can explore. If a voxel hasn't been observed in N seconds, decay its confidence. If the robot re-sees it, refresh. Dynamic scenes — objects moving — jump from ~30 % success to 70 %.

---

## 4. The voice / conversation layer (layer 1, where most hobby robots botch it)

Most robot demos hard-code "press button, speak, release." That's not a conversational agent. To be a **real** agent you need:
- Low latency (first-word response under 1 s).
- Barge-in: user interrupts the robot; robot shuts up mid-sentence and listens.
- Turn-taking: knows when the user has finished.
- Session memory across turns.

The industry has converged on **Pipecat** and **LiveKit Agents** for this. They solve the same problem differently:

- **Pipecat**: a Python framework that pipes `[VAD] → [STT] → [LLM] → [TTS]` with automatic interruption, 40+ plugins. You bring the transport. BSD-2, cleanest code I've read in this space.
- **LiveKit Agents**: a full platform (WebRTC transport + agent SDK). If you want multiple simultaneous audio sessions, or a web client, this is the answer. Can actually run Pipecat *on top* of LiveKit.

Both handle TTS interrupt, which is the single feature that most makes a robot feel alive.

**What we have today:** `arecord` push-to-talk + `espeak-ng` / Piper / OpenWakeWord — a hand-rolled subset of what Pipecat does out of the box. We should migrate to Pipecat; ~2 days of work, and we get barge-in + session memory for free.

---

## 5. Behavior primitives — the shape of layer 3

Every system above has a short list of "things the robot can do." The LLM/planner composes these. The specific list varies but they all look like:

```python
# OK-Robot-style
navigate_to(voxel_query: str)          # "blue cup"
pick(voxel_query: str)                 # calls AnyGrasp
drop(voxel_query: str)                 # calls LangSAM + release
scan_room()                            # rotates, updates VoxelMap

# Stretch AI style
move_to(x, y, theta)
grasp_at(x, y, z)
look_around()
say(text)

# Code-as-Policies style (Google)
get_object_bbox(name)
pick_up(bbox)
put_down(bbox, location)
describe_scene() -> str
```

**The rule:** every primitive returns a **result dict** `{success: bool, observation: str, error: str | None}`. The LLM reads this back as the next message in its tool-use conversation, then decides whether to continue, replan, or stop.

For us, the initial list maps 1:1 onto our existing wire commands plus vision queries. See §8.

---

## 6. Spatial memory choices

The field's four options, ranked by complexity:

| Option | What | Good for | Open code |
|---|---|---|---|
| **Dead reckoning + flat list** | Cumulative pose from motor encoders + IMU, list of `(object, pose)` | Tiny robots, short sessions, demos | trivial, write our own |
| **2D occupancy grid** | Top-down cell grid, each cell "free / unknown / occupied" | Planar homes, 2D planners | Nav2 from ROS 2; also hand-rollable in ~100 lines of numpy |
| **3D voxel map** (OK-Robot) | 5 cm voxels, each with CLIP embedding | Open-vocab object queries "blue cup" | [OK-Robot](https://github.com/ok-robot/ok-robot) — full implementation |
| **Online sparse voxel** (DynaMem) | Voxel map that expires + re-adds over time | Dynamic scenes, long sessions | [dynamem.github.io](https://dynamem.github.io/), code on their site |
| **Scene graph** | Symbolic graph: rooms → objects → attributes → relationships | Complex reasoning, e.g. "the cup *on* the table" | [ConceptGraphs](https://concept-graphs.github.io/), [SceneGraph-GPT] |
| **NeRF / Gaussian splatting + language** | Dense 3D + queryable features | Research-heavy, great quality | [LeRF](https://www.lerf.io/), [GARField] — slow for online use |

**For a 4-legged, phone-brained robot the right choice is 2D occupancy + dead-reckoning for pose, plus a small flat list of "things I saw recently with rough bearings."** 3D voxel maps from OK-Robot/DynaMem assume a depth camera — we don't have one, and adding one makes the robot bulky. Instead: depth-from-monocular (which we already have via Depth Anything V2 @ 7.9 FPS on Pixel 6) is sufficient as a confidence signal for "is there something blocking me."

---

## 7. Locomotion — the unfixed layer

This is where the "on the shoulders of giants" principle gets harder, because legged locomotion is **sensitive to the exact mechanical robot**. You can't copy the MIT Cheetah gait onto our 4× STS3032 + 3D-printed body. But the algorithms transfer:

- **Open-loop sinusoidal gait** (Stanford Doggo) — trot = two pairs of legs alternate phase. Works on most small quadrupeds with position-controlled servos. Our firmware already has a walk mode that looks like this based on the servo positions we see.
- **State estimation** (MIT Cheetah 3) — fuse IMU (orientation) with leg kinematics (contact / swing phase) to get body velocity. This is the missing piece for our robot to *know if it's walking or not*.
- **Balance feedback** — the simplest useful version: if IMU pitch/roll > 20° from setpoint, clip the gait and send an emergency-pose. Better: PD control on roll/pitch by biasing the gait. Best: RL-trained policy (Spot, Cheetah 3 both do this).

What's on our table (no pun intended):
- IMU works (verified earlier: gravity correctly on Z, gyro bias stable).
- Firmware has `walk` command but we can't see the source.
- No balance loop today. Robot falls off tables.

Fix path: **expose an "abort walk" reflex** in the daemon that watches IMU and sends `{"c":"stop"}` on threshold breach. Doesn't prevent the fall but prevents continued walking into the fall. Proper closed-loop balance needs firmware source (which we don't have).

---

## 8. Concrete proposal for our robot — where each box gets its code

Mapping the standard architecture onto our hardware + code:

| Layer | Standard | Our current | Our upgrade path |
|---|---|---|---|
| **1. Voice I/O** | Pipecat + Piper + OpenWakeWord | arecord + Whisper + Piper + OpenWakeWord (hand-rolled) | Migrate to Pipecat for barge-in + turn-taking. ~2 days. |
| **2. Planner** | OpenAI/Anthropic tool-use on Llama-3.1-8B | Regex matcher + optional DeepInfra Llama-3.1-8B one-shot JSON | Swap to tool-calling mode with a primitive schema; add observation→replan loop. ~3 days. |
| **3. Primitives** | `navigate_to`, `pick`, `drop` | `pose`, `walk`, `stop`, `jump`, `ping` | Wrap wire cmds + vision queries into a thin Python `class RobotTools` with tool-call schemas. ~1 day. |
| **4. Spatial memory** | VoxelMap / DynaMem | None | 2D occupancy + dead-reckoning + short list of recent detections. ~2 days. |
| **5. Perception** | OWL-ViT + CLIP + Lang-SAM + depth | YOLO-Fastest v2 + Depth V2 @ 256 + SmolVLM (on-demand) | Add an "interesting frame" trigger → SmolVLM caption; tie to memory. ~2 days. |
| **6. Low-level** | MIT Cheetah balance + learned gait | ESP32 firmware (unknown source) + STS3032 | IMU-watchdog reflex in daemon; firmware source pending. ~1 day. |

**Net effort to go from reactive → agent:** about **10 focused days** if we adopt where adoption is possible and write only the glue.

---

## 9. What NOT to reinvent

In rough order of regret if we skip them:

1. **Tool-calling agent loop** — Anthropic's `claude` SDK, OpenAI's Agents SDK, Smolagents from HuggingFace all solve this. Use one, don't write our own.
2. **Voice pipeline** — Pipecat. The barge-in code alone is weeks of work.
3. **YOLO + depth + VLM** — LeRobot's perception modules are trivially reusable, or just keep our current stack; this is already good.
4. **Training a VLA** — skip. OpenVLA/π0/SmolVLA all need large-ish GPUs to fine-tune and real hardware to roll out. Not our cost/value ratio right now. We do *not* have the data for a useful one.
5. **3D voxel mapping** — skip unless/until we add a depth sensor. 2D is sufficient for the room-scale, table-scale, "I walked here, saw that" mental model.
6. **Custom wake word** — OpenWakeWord's pretrained set (hey jarvis / hey mycroft) is fine; training our own is a distraction.

---

## 10. What IS worth inventing (our differentiation)

Where existing work does NOT already solve our problem:

1. **Phone-as-brain runtime** — nobody's shipping an Android-hosted mobile-manipulation agent. OK-Robot needs a GPU workstation + ROS; Stretch AI needs a desktop + WiFi. Pixel-6-sized compute is genuinely uncharted. Our `ARCHITECTURE.md` is the sketch; the engineering is ours.
2. **Sub-$300 legged agent** — all the above start at $16k (Unitree G1). Ours is ~$100 hardware + a phone you already own. The chassis + controller + phone combo doesn't exist.
3. **Offline-first voice** — every demo robot needs WiFi. Our Whisper-on-Pixel-6 + on-phone Gemma path works with no internet. Useful for situations where you don't want your TV camera's audio going to the cloud (heydict's thesis).

---

## 11. My ranked adoption list (what to integrate next, in order)

Small → big effort:

1. **Pipecat for the voice layer** (2 days). Unlocks barge-in, a huge felt-quality jump.
2. **LLM tool-calling loop** (3 days). The single step that turns reactive → agent.
3. **2D spatial memory** (2 days). Gives the LLM continuity across commands.
4. **IMU reflex** (1 day). Doesn't fix walking, but makes the robot less likely to keep marching off a surface.
5. **Scene caption on interesting frames** (2 days). Elevates the LLM's understanding from "class 0" to "the cup on the left of the keyboard."
6. **LeRobot adapter for our primitives** (3-5 days). Keeps us positioned to ride LeRobot's momentum later — datasets, policies, Unitree G1 if we upgrade hardware.

Total: ~**13-15 days** of focused work. Meaningful portion parallelizable via subagents.

**Single biggest win per day of effort: #2, the tool-calling loop.** Without it, the other layers don't compose. With it, everything else slots in over the following week.

---

## Sources

- [SayCan (Google, 2022)](https://say-can.github.io/) · [paper](https://say-can.github.io/assets/palm_saycan.pdf)
- [Code-as-Policies (Google)](https://code-as-policies.github.io/)
- [OK-Robot site](https://ok-robot.github.io/) · [paper](https://arxiv.org/abs/2401.12202) · [code](https://github.com/ok-robot/ok-robot)
- [DynaMem](https://dynamem.github.io/) · [paper](https://arxiv.org/abs/2411.04999)
- [OpenVLA](https://openvla.github.io/) · [paper](https://arxiv.org/abs/2406.09246) · [code](https://github.com/openvla/openvla)
- [π0 / π0.5 (Physical Intelligence)](https://www.pi.website/download/pi05.pdf)
- [LeRobot (HuggingFace)](https://github.com/huggingface/lerobot) · [v0.5 release](https://huggingface.co/blog/lerobot-release-v050) · [Unitree G1 guide](https://huggingface.co/docs/lerobot/unitree_g1)
- [SmolVLA training guide](https://docs.phospho.ai/learn/train-smolvla)
- [Stretch AI](https://hello-robot.com/stretch-ai) · [GitHub](https://github.com/hello-robot/stretch_ai) · [Chris Paxton's intro](https://itcanthink.substack.com/p/introducing-stretch-ai)
- [Pipecat](https://github.com/pipecat-ai/pipecat) · [vs LiveKit analysis](https://medium.com/@ggarciabernardo/realtime-ai-agents-frameworks-bb466ccb2a09)
- [Stanford Doggo](https://github.com/Nate711/StanfordDoggoProject) · [Stanford Pupper paper](https://arxiv.org/pdf/2110.00736)
- [MIT Cheetah 3 design + control](https://dspace.mit.edu/bitstream/handle/1721.1/126619/IROS.pdf)
- [LeRF (language-embedded radiance fields)](https://www.lerf.io/) — for NeRF comparison
- [Unitree G1 humanoid](https://www.unitree.com/g1/) · [unitree_lerobot repo](https://github.com/unitreerobotics/unitree_lerobot)
- [Figure AI](https://www.figure.ai/) — proprietary, for context
