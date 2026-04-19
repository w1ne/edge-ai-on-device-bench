#!/usr/bin/env python3
"""
robot_behaviors.py  —  state machine that closes the loop between vision,
voice, and the ESP32 wire protocol.

BehaviorEngine consumes:
  * vision events  (see demo/vision_watcher.py:  {"t":"event","class","conf",
                    "bbox":[x,y,w,h],"streak","ts"})
  * voice commands (output of robot_daemon.py's match_command(): dicts like
                    {"c":"walk",...}, {"c":"stop"}, {"c":"pose","n":...})
  * periodic ticks (for idle sway).

and decides what, if anything, to send on the wire.

Behaviors implemented:
  1. idle                   — default state. ~30 s idle sway (neutral pose).
  2. greet-once-per-class   — new class triggers richer greeting, per-class
                              debounce = GREET_DEBOUNCE.
  3. walk-until-obstacle    — user says walk -> walking. Close obstacle
                              (class in OBSTACLE_CLASSES, conf > 0.6,
                              bbox_h > img_h * 0.4) -> stop + "something in
                              front of me" + paused. After PAUSE_RESUME s
                              with no close obstacle -> auto-resume walk.
  4. emergency-stop         — voice "stop"/"halt" always wins.
  5. follow-me              — if class=person centered, hold; drift left/right
                              -> lean_left/lean_right with FOLLOW_COOLDOWN.

Wire protocol (authoritative):
    {"c":"pose","n":<name>,"d":<speed>}
    {"c":"walk","on":true,"stride":150,"step":400}
    {"c":"stop"}   {"c":"jump"}   {"c":"ping"}

The engine never touches USB or TTS itself; callers inject
    speak_fn(text: str)       — say something
    wire_fn(cmd: dict)        — push a wire command (returns anything)
    logger(line: str)         — status log sink
"""
from __future__ import annotations

import time
from typing import Callable, Optional

# ----------------------------------------------------------------- tunables
IDLE_SWAY_INTERVAL  = 30.0   # s between idle sway pokes
GREET_DEBOUNCE      = 30.0   # s per-class debounce for greetings
OBSTACLE_CLASSES    = {"person", "chair", "tv", "bottle", "cup"}
OBSTACLE_CONF_MIN   = 0.6
OBSTACLE_HEIGHT_FRAC = 0.4   # bbox_h / image_h >= this ⇒ "close"
PAUSE_RESUME_S      = 5.0    # resume walking after this much clear time
FOLLOW_COOLDOWN     = 2.0    # s between follow-me turns
FOLLOW_CENTER_BAND  = 0.2    # ±20% of width counts as "centered"

# default image dims — overridable via ctor.
DEFAULT_IMG_W = 640
DEFAULT_IMG_H = 480

WALK_CMD = {"c": "walk", "on": True, "stride": 150, "step": 400}
STOP_CMD = {"c": "stop"}
NEUTRAL_POSE = {"c": "pose", "n": "neutral", "d": 1500}


class BehaviorEngine:
    """State machine.

    States:
      idle       — not moving; periodic sway.
      walking    — last wire was a walk-on; monitor for obstacles.
      paused     — was walking, stopped by obstacle; wait for clear, resume.
      following  — person detected; track left/right drift.

    Transitions driven by on_vision_event, on_voice_command, tick.
    """

    def __init__(
        self,
        speak_fn: Callable[[str], None],
        wire_fn: Callable[[dict], object],
        logger: Callable[[str], None],
        image_w: int = DEFAULT_IMG_W,
        image_h: int = DEFAULT_IMG_H,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._speak  = speak_fn
        self._wire   = wire_fn
        self._log    = logger
        self._now    = time_fn
        self._img_w  = image_w
        self._img_h  = image_h

        self._state: str = "idle"
        self._last_sway: float = self._now()
        self._last_greet: dict[str, float] = {}
        self._seen_classes: set[str] = set()
        self._last_close_ts: float = 0.0
        self._paused_since: float = 0.0
        self._last_follow_turn: float = 0.0

    # ------------------------------------------------------------- api
    def get_state(self) -> str:
        return self._state

    def on_voice_command(self, cmd: Optional[dict]) -> Optional[dict]:
        """Feed a voice-derived wire command. Returns the command that should
        actually be sent on the wire (may differ — e.g. emergency-stop wins
        over everything else, or we enter walking state).  Returns None if
        nothing should be sent."""
        if cmd is None:
            return None
        c = cmd.get("c")
        # (4) emergency-stop ALWAYS wins
        if c == "stop":
            self._enter_idle("voice stop")
            return STOP_CMD
        # (3) walk -> transition to walking
        if c == "walk":
            self._state = "walking"
            self._log(f"[behav] -> walking (voice)")
            return dict(cmd)
        # generic pose/jump/ping/... pass through.  poses/jump break walking.
        if c in ("pose", "jump"):
            if self._state in ("walking", "paused", "following"):
                self._log(f"[behav] {self._state} -> idle (voice {c})")
                self._state = "idle"
        return dict(cmd)

    def on_vision_event(self, event: dict) -> None:
        """Consume a vision watcher event dict. Side-effects: may speak,
        may send wire commands via injected fns."""
        if not isinstance(event, dict) or event.get("t") != "event":
            return
        cls = event.get("class", "?")
        conf = float(event.get("conf", 0.0))
        bbox = event.get("bbox") or [0, 0, 0, 0]
        try:
            _x, _y, bw, bh = bbox[:4]
        except Exception:
            bw = bh = 0.0

        is_new_class = cls not in self._seen_classes
        self._seen_classes.add(cls)

        close = (cls in OBSTACLE_CLASSES
                 and conf >= OBSTACLE_CONF_MIN
                 and bh >= self._img_h * OBSTACLE_HEIGHT_FRAC)

        # (3) walking + close obstacle => stop, pause
        if self._state == "walking" and close:
            self._log(f"[behav] walking->paused: close {cls} "
                      f"conf={conf:.2f} bbox_h={bh:.0f}/{self._img_h}")
            self._wire(STOP_CMD)
            self._speak("something in front of me")
            self._state = "paused"
            self._paused_since = self._now()
            self._last_close_ts = self._now()
            return

        # paused state: refresh the "close" stamp on every close sighting
        if self._state == "paused" and close:
            self._last_close_ts = self._now()
            return

        # (5) follow-me: person and we aren't actively walking/paused
        if (cls == "person" and self._state in ("idle", "following")
                and not close):
            self._maybe_follow(bbox)

        # (2) greet-once-per-class: after we've handled urgent stuff
        self._maybe_greet(cls, is_new_class)

    def tick(self) -> Optional[dict]:
        """Called periodically. May emit an idle sway wire command, may
        auto-resume walking after a pause.  Returns a wire command the
        caller should send, or None."""
        now = self._now()

        # paused -> resume walking after PAUSE_RESUME_S of no close obstacle
        if self._state == "paused":
            if now - self._last_close_ts >= PAUSE_RESUME_S:
                self._log("[behav] paused->walking: auto-resume")
                self._speak("resuming")
                self._state = "walking"
                return dict(WALK_CMD)
            return None

        # idle sway
        if self._state == "idle":
            if now - self._last_sway >= IDLE_SWAY_INTERVAL:
                self._last_sway = now
                self._log("[behav] idle sway")
                return dict(NEUTRAL_POSE)
        return None

    # --------------------------------------------------------- internals
    def _enter_idle(self, reason: str) -> None:
        if self._state != "idle":
            self._log(f"[behav] {self._state}->idle ({reason})")
        self._state = "idle"

    def _maybe_greet(self, cls: str, is_new: bool) -> None:
        now = self._now()
        last = self._last_greet.get(cls, 0.0)
        if now - last < GREET_DEBOUNCE:
            return
        # richer greeting on FIRST sighting, plain on repeat
        if is_new:
            self._speak(f"hi there, I see a {cls}")
        else:
            self._speak(f"hello again, {cls}")
        self._last_greet[cls] = now
        self._log(f"[behav] greet {cls} (new={is_new})")

    def _maybe_follow(self, bbox) -> None:
        now = self._now()
        if now - self._last_follow_turn < FOLLOW_COOLDOWN:
            return
        try:
            x, _y, w, _h = bbox[:4]
        except Exception:
            return
        cx = x + w / 2.0
        norm = (cx / self._img_w) - 0.5   # -0.5 .. +0.5
        if abs(norm) <= FOLLOW_CENTER_BAND / 2.0:
            # centered enough — stay put, but we are "following"
            self._state = "following"
            return
        self._state = "following"
        self._last_follow_turn = now
        if norm < 0:
            cmd = {"c": "pose", "n": "lean_left", "d": 500}
            self._log(f"[behav] follow: lean_left (norm={norm:+.2f})")
        else:
            cmd = {"c": "pose", "n": "lean_right", "d": 500}
            self._log(f"[behav] follow: lean_right (norm={norm:+.2f})")
        self._wire(cmd)


# ================================================================= tests
def _smoke_test() -> tuple[bool, list[str]]:
    """idle -> walk cmd -> close-person vision event -> expect stop.

    Captures wire and speak calls and asserts the key invariants.  Returns
    (passed, transcript_lines).
    """
    transcript: list[str] = []
    wire_log: list[dict] = []
    speak_log: list[str] = []

    clock = [1000.0]
    def now(): return clock[0]

    def speak(t: str):
        speak_log.append(t)
        transcript.append(f"SPEAK {t!r}")
    def wire(c: dict):
        wire_log.append(c)
        transcript.append(f"WIRE  {c}")
        return "ack"
    def log(s: str):
        transcript.append(f"LOG   {s}")

    eng = BehaviorEngine(speak, wire, log, image_w=640, image_h=480, time_fn=now)

    transcript.append(f"state0 = {eng.get_state()}")
    assert eng.get_state() == "idle", "must start idle"

    # 1. voice 'walk'
    out = eng.on_voice_command({"c": "walk", "on": True, "stride": 150, "step": 400})
    transcript.append(f"after walk cmd, state={eng.get_state()} -> wire {out}")
    assert eng.get_state() == "walking"
    assert out and out.get("c") == "walk"

    # simulate daemon actually sending walk
    wire(out)

    # 2. close person vision event: bbox height 50% of image
    clock[0] += 1.0
    ev = {
        "t": "event", "class": "person", "conf": 0.82,
        "bbox": [100.0, 100.0, 120.0, 240.0],  # h=240 of 480 = 50%
        "streak": 5, "ts": "now",
    }
    eng.on_vision_event(ev)
    transcript.append(f"after close-person event, state={eng.get_state()}")
    assert eng.get_state() == "paused", f"expected paused, got {eng.get_state()}"

    # last wire call must be {"c":"stop"}
    assert wire_log[-1] == STOP_CMD, f"expected STOP last, got {wire_log[-1]}"
    # speak must have said "something in front of me"
    assert any("in front of me" in s for s in speak_log), speak_log

    # 3. tick within PAUSE_RESUME_S: no auto-resume
    clock[0] += 2.0
    out = eng.tick()
    transcript.append(f"tick@+2s -> {out}, state={eng.get_state()}")
    assert out is None and eng.get_state() == "paused"

    # 4. tick past PAUSE_RESUME_S -> auto-resume walk
    clock[0] += PAUSE_RESUME_S + 0.5
    out = eng.tick()
    transcript.append(f"tick@+{PAUSE_RESUME_S+2.5:.1f}s -> {out}, "
                      f"state={eng.get_state()}")
    assert eng.get_state() == "walking"
    assert out and out.get("c") == "walk"

    # 5. emergency-stop voice always wins
    clock[0] += 0.1
    out = eng.on_voice_command({"c": "stop"})
    transcript.append(f"after voice stop, state={eng.get_state()}, wire={out}")
    assert eng.get_state() == "idle"
    assert out == STOP_CMD

    # 6. new-class greeting
    clock[0] += 0.1
    before_speak = len(speak_log)
    eng.on_vision_event({
        "t": "event", "class": "chair", "conf": 0.71,
        "bbox": [10.0, 10.0, 30.0, 40.0],  # tiny -> not close
        "streak": 3, "ts": "now",
    })
    transcript.append(f"after new-class chair event, "
                      f"speaks={speak_log[before_speak:]}")
    assert any("I see a chair" in s for s in speak_log[before_speak:]), \
        speak_log[before_speak:]

    return True, transcript


if __name__ == "__main__":
    import datetime, pathlib, sys, traceback
    passed = False
    lines: list[str] = []
    try:
        passed, lines = _smoke_test()
    except AssertionError as e:
        lines.append(f"ASSERT FAILED: {e}")
        lines.append(traceback.format_exc())
    except Exception as e:
        lines.append(f"ERROR: {type(e).__name__}: {e}")
        lines.append(traceback.format_exc())

    verdict = "PASS" if passed else "FAIL"
    day = datetime.date.today().isoformat()
    log_path = pathlib.Path(__file__).resolve().parent.parent / "logs" \
        / f"behaviors_smoke_{day}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(f"\n# robot_behaviors smoke — {datetime.datetime.now().isoformat()}\n")
        for ln in lines:
            fh.write(ln + "\n")
        fh.write(f"{verdict}\n")
    print(f"smoke: {verdict}  (transcript -> {log_path})")
    sys.exit(0 if passed else 1)
