#!/usr/bin/env python3
"""
Minimal voice → robot pose demo. No LLM, no cloud. Just:

  audio file → Whisper Tiny on phone → keyword match → pose → ESP32

Usage:
    demo/voice_to_pose.py path/to/command.wav

Valid command phrases (any one is enough):
    "neutral" / "stand"                   → pose neutral
    "bow" / "bow front"                   → pose bow_front
    "lean left"                           → pose lean_left
    "lean right"                          → pose lean_right
    "walk"                                → walk
    "stop"                                → stop
    "jump"                                → jump

Prereqs: adb-connected phone with /data/local/tmp/whisper-cli +
ggml-tiny.bin (push via ../scripts/push_assets.sh). ESP32 on USB.
"""
import sys, subprocess, time, json, re
import usb.core, usb.util

KEYWORDS = [
    (["neutral", "stand"],         {"c":"pose","n":"neutral","d":1500}),
    (["bow front", "bow"],         {"c":"pose","n":"bow_front","d":1800}),
    (["lean left"],                {"c":"pose","n":"lean_left","d":1500}),
    (["lean right"],               {"c":"pose","n":"lean_right","d":1500}),
    (["walk"],                     {"c":"walk","on":True,"stride":150,"step":400}),
    (["stop"],                     {"c":"stop"}),
    (["jump"],                     {"c":"jump"}),
    (["emergency", "estop"],       {"c":"estop"}),
]

def main(argv):
    if len(argv) < 2:
        print(__doc__); sys.exit(1)
    wav_path = argv[1]

    # 1) push audio + transcribe on phone
    subprocess.run(["adb", "push", wav_path, "/data/local/tmp/cmd.wav"], check=True,
                   capture_output=True)
    t0 = time.time()
    r = subprocess.run(
        ["adb","shell","cd /data/local/tmp && ./whisper-cli -m ggml-tiny.bin -f cmd.wav --no-timestamps -l en -t 8 2>&1"],
        capture_output=True, text=True, timeout=60,
    )
    t_whisper = time.time() - t0
    transcript = ""
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith(("whisper_","main:","system_info","[","#")): continue
        transcript = s
        break
    print(f"[1] Whisper ({t_whisper*1000:.0f} ms): {transcript!r}")

    # 2) keyword match → command
    low = transcript.lower()
    cmd = None
    for triggers, c in KEYWORDS:
        if any(t in low for t in triggers):
            cmd = c; break
    if cmd is None:
        print(f"[2] No command keyword matched in {transcript!r}")
        sys.exit(1)
    print(f"[2] Matched → {json.dumps(cmd)}")

    # 3) send to ESP32
    dev = usb.core.find(idVendor=0x303a, idProduct=0x1001)
    if dev is None:
        print("ESP32 not found on USB (303a:1001)"); sys.exit(2)
    try: usb.util.claim_interface(dev, 1)
    except Exception: pass
    try:
        while True: dev.read(0x81, 4096, timeout=60)
    except Exception: pass

    t0 = time.time()
    dev.write(0x01, (json.dumps(cmd)+"\n").encode(), timeout=500)
    buf = bytearray(); end = time.time() + 1.2
    while time.time() < end:
        try: buf.extend(dev.read(0x81, 4096, timeout=100))
        except Exception: pass
    t_act = time.time() - t0

    ack, pos = None, None
    for line in buf.decode('utf-8','replace').splitlines():
        if '"ack"' in line or '"err"' in line:
            ack = line.strip()
        m = re.search(r'"p":\[([\-\d,]+)\]', line)
        if m: pos = [int(x) for x in m.group(1).split(',')]
    print(f"[3] Actuate ({t_act*1000:.0f} ms): {ack}")
    print(f"[3] servos: {pos}")
    print(f"\ntotal: {(t_whisper + t_act)*1000:.0f} ms")

if __name__ == "__main__":
    main(sys.argv)
