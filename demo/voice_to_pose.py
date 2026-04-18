#!/usr/bin/env python3
"""
Minimal voice → robot pose demo. No LLM, no cloud. Just:

  audio file → Whisper Base on phone → keyword match → pose → ESP32

Usage:
    demo/voice_to_pose.py path/to/command.wav [--tiny]

    --tiny : use Whisper Tiny (smaller, faster, worse at short commands;
             default is Whisper Base which reliably transcribes 1–2 word
             robot commands on a Kirin 659 / Tensor G1).

Valid command phrases (any one is enough):
    "stand" / "neutral"               → pose neutral
    "bow" / "bow front" / "go front"  → pose bow_front
    "lean left"                       → pose lean_left
    "lean right"                      → pose lean_right
    "walk"                            → walk
    "stop"                            → stop
    "jump"                            → jump

Prereqs: adb-connected phone with /data/local/tmp/whisper-cli plus
ggml-base.en.bin (default) or ggml-tiny.bin. Push via
../scripts/push_assets.sh. ESP32 on USB (303a:1001).
"""
import sys, subprocess, time, json, re
import usb.core, usb.util

# Each rule: (list-of-substrings-any-match, command).
# Order matters — first match wins. "lean" rules must come before "walk"
# because a misheard "walking left" shouldn't fire walk before lean.
KEYWORDS = [
    (["lean left"],                          {"c":"pose","n":"lean_left","d":1500}),
    (["lean right"],                         {"c":"pose","n":"lean_right","d":1500}),
    (["bow", "go front", "go forward"],      {"c":"pose","n":"bow_front","d":1800}),
    (["neutral", "stand"],                   {"c":"pose","n":"neutral","d":1500}),
    (["emergency", "estop", "e-stop"],       {"c":"estop"}),
    (["stop"],                               {"c":"stop"}),
    (["walk"],                               {"c":"walk","on":True,"stride":150,"step":400}),
    (["jump"],                               {"c":"jump"}),
]

def main(argv):
    args = [a for a in argv[1:] if not a.startswith("-")]
    flags = [a for a in argv[1:] if a.startswith("-")]
    if not args:
        print(__doc__); sys.exit(1)
    wav_path = args[0]
    model = "ggml-tiny.bin" if "--tiny" in flags else "ggml-base.en.bin"

    # 1) push + transcribe on phone
    r = subprocess.run(["adb","push",wav_path,"/data/local/tmp/cmd.wav"],
                       capture_output=True, text=True, check=False)
    if r.returncode != 0:
        print(f"adb push failed: {r.stderr}"); sys.exit(2)

    t0 = time.time()
    r = subprocess.run(
        ["adb","shell",f"cd /data/local/tmp && ./whisper-cli -m {model} -f cmd.wav --no-timestamps -l en -t 8 2>&1"],
        capture_output=True, text=True, timeout=60,
    )
    t_whisper = time.time() - t0
    transcript = ""
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith(("whisper_","main:","system_info","[","#")): continue
        transcript = s
        break
    print(f"[1] Whisper {model} ({t_whisper*1000:.0f} ms): {transcript!r}")

    # 2) keyword match
    low = re.sub(r"[^\w\s]", " ", transcript.lower()).strip()
    cmd = None
    for triggers, c in KEYWORDS:
        if any(t in low for t in triggers):
            cmd = c; break
    if cmd is None:
        print(f"[2] No command keyword matched in {transcript!r}"); sys.exit(1)
    print(f"[2] Matched → {json.dumps(cmd)}")

    # 3) send to ESP32
    dev = usb.core.find(idVendor=0x303a, idProduct=0x1001)
    if dev is None:
        print("ESP32 not found on USB (303a:1001)"); sys.exit(3)
    try: usb.util.claim_interface(dev, 1)
    except Exception: pass
    try:
        while True: dev.read(0x81, 4096, timeout=60)
    except Exception: pass

    t0 = time.time()
    dev.write(0x01, (json.dumps(cmd)+"\n").encode(), timeout=500)
    buf = bytearray(); end = time.time() + 1.3
    while time.time() < end:
        try: buf.extend(dev.read(0x81, 4096, timeout=100))
        except Exception: pass
    t_act = time.time() - t0

    ack, pos = None, None
    for line in buf.decode("utf-8","replace").splitlines():
        if '"ack"' in line or '"err"' in line: ack = line.strip()
        m = re.search(r'"p":\[([\-\d,]+)\]', line)
        if m: pos = [int(x) for x in m.group(1).split(',')]
    print(f"[3] Actuate ({t_act*1000:.0f} ms): {ack}")
    print(f"[3] servos: {pos}")
    print(f"\ntotal: {(t_whisper + t_act)*1000:.0f} ms")

if __name__ == "__main__":
    main(sys.argv)
