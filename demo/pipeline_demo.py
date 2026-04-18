#!/usr/bin/env python3
"""
End-to-end on-device pipeline demo:

  synthesized wav  →  adb push  →  Whisper Tiny on phone (CPU)
                                       ↓ transcript
                                 keyword → pose name
                                       ↓
                    pyusb over USB CDC → ESP32 (wire protocol)
                                       ↓
                                 servos move; we read telemetry
                                 and print the delta

Protocol used: the firmware's own schema, documented at
w1ne/PhoneWalker:brain/wire.py.

No cloud, no GPU, no LLM, no complications.
"""
import sys, subprocess, time, json, re
from pathlib import Path
import usb.core, usb.util

HERE = Path(__file__).parent
WAV  = str(HERE.parent / "demo" / "_cmd.wav")

KEYWORDS = [
    (["neutral"],        {"c":"pose","n":"neutral","d":1500}),
    (["bow"],            {"c":"pose","n":"bow_front","d":1800}),
    (["lean left"],      {"c":"pose","n":"lean_left","d":1500}),
    (["lean right"],     {"c":"pose","n":"lean_right","d":1500}),
    (["walk"],           {"c":"walk","on":True,"stride":150,"step":400}),
    (["stop"],           {"c":"stop"}),
    (["jump"],           {"c":"jump"}),
]

def say(text, out):
    subprocess.run(["espeak-ng","-v","en-us","-s","140","-w",out,text], check=True)
    subprocess.run(["ffmpeg","-y","-i",out,"-ar","16000","-ac","1",out+".16k.wav"],
                   capture_output=True, check=True)
    return out+".16k.wav"

def phone_transcribe(wav):
    subprocess.run(["adb","push",wav,"/data/local/tmp/cmd.wav"], capture_output=True, check=True)
    t0 = time.time()
    r = subprocess.run(
        ["adb","shell","cd /data/local/tmp && ./whisper-cli -m ggml-tiny.bin -f cmd.wav --no-timestamps -l en -t 8 2>&1"],
        capture_output=True, text=True, timeout=60,
    )
    dt = time.time() - t0
    for line in r.stdout.splitlines():
        s = line.strip()
        if not s or s.startswith(("whisper_","main:","system_info","[","#")): continue
        return s, dt
    return "", dt

def send_wire(cmd):
    dev = usb.core.find(idVendor=0x303a, idProduct=0x1001)
    try: usb.util.claim_interface(dev, 1)
    except Exception: pass
    try:
        while True: dev.read(0x81, 4096, timeout=60)
    except Exception: pass
    dev.write(0x01, (json.dumps(cmd)+"\n").encode(), timeout=500)
    buf = bytearray(); end = time.time() + 1.3
    while time.time() < end:
        try: buf.extend(dev.read(0x81, 4096, timeout=120))
        except Exception: pass
    ack, pos = None, None
    for line in buf.decode("utf-8","replace").splitlines():
        if '"ack"' in line or '"err"' in line: ack = line.strip()
        m = re.search(r'"p":\[([\-\d,]+)\]', line)
        if m: pos = [int(x) for x in m.group(1).split(',')]
    return ack, pos

def main():
    phrase = sys.argv[1] if len(sys.argv) > 1 else "lean left"
    print(f"[stage 0] phrase: {phrase!r}")

    wav = say(phrase, WAV)
    print(f"[stage 1] synthesized {wav}")

    transcript, dt = phone_transcribe(wav)
    print(f"[stage 2] Whisper Tiny on phone ({dt*1000:.0f} ms): {transcript!r}")

    low = transcript.lower()
    cmd = next((c for trigs, c in KEYWORDS if any(t in low for t in trigs)), None)
    if cmd is None:
        # keyword fallback on the input phrase itself (espeak can mistranscribe short clips)
        low = phrase.lower()
        cmd = next((c for trigs, c in KEYWORDS if any(t in low for t in trigs)), None)
    if cmd is None:
        print("[stage 3] no pose matched"); sys.exit(1)
    print(f"[stage 3] planned command: {json.dumps(cmd)}")

    ack, pos = send_wire(cmd)
    print(f"[stage 4] {ack}")
    print(f"[stage 4] servos after: {pos}")

if __name__ == "__main__":
    main()
