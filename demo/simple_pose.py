#!/usr/bin/env python3
"""
Zero-complication robot demo: send a single named pose to the ESP32 and exit.

Usage:
    demo/simple_pose.py <pose>

Valid poses (from PhoneWalker firmware):
    neutral, bow_front, lean_left, lean_right,
    stretch_diagonal1, stretch_diagonal2,
    twist_front_left, twist_front_right

Additional commands:
    demo/simple_pose.py walk        # start walking
    demo/simple_pose.py stop        # stop walking, return to neutral
    demo/simple_pose.py jump        # jump
    demo/simple_pose.py estop       # emergency stop

Requires: pyusb. The ESP32-S3 should be on USB (VID 303a, PID 1001).
"""
import sys, time, json, re
import usb.core, usb.util

POSES = {"neutral","bow_front","lean_left","lean_right",
         "stretch_diagonal1","stretch_diagonal2",
         "twist_front_left","twist_front_right"}

def main(argv):
    if len(argv) < 2:
        print(__doc__); sys.exit(1)
    arg = argv[1]
    if arg in POSES:
        cmd = {"c":"pose","n":arg,"d":1500}
    elif arg == "walk":
        cmd = {"c":"walk","on":True,"stride":150,"step":400}
    elif arg == "stop":
        cmd = {"c":"stop"}
    elif arg == "jump":
        cmd = {"c":"jump"}
    elif arg == "estop":
        cmd = {"c":"estop"}
    else:
        print(f"unknown: {arg}\n"); print(__doc__); sys.exit(1)

    dev = usb.core.find(idVendor=0x303a, idProduct=0x1001)
    if dev is None:
        print("ESP32 not found on USB"); sys.exit(2)
    try: usb.util.claim_interface(dev, 1)
    except Exception: pass

    # drain
    try:
        while True: dev.read(0x81, 4096, timeout=60)
    except Exception: pass

    dev.write(0x01, (json.dumps(cmd)+"\n").encode(), timeout=500)
    time.sleep(1.5)
    buf = bytearray(); end = time.time() + 1.0
    while time.time() < end:
        try: buf.extend(dev.read(0x81, 4096, timeout=100))
        except Exception: pass

    ack, pos = None, None
    for line in buf.decode('utf-8','replace').splitlines():
        if '"ack"' in line or '"err"' in line:
            ack = line.strip()
        m = re.search(r'"p":\[([\-\d,]+)\]', line)
        if m: pos = [int(x) for x in m.group(1).split(',')]

    print(f"sent: {json.dumps(cmd)}")
    if ack: print(f"ack : {ack}")
    if pos: print(f"servo positions: {pos}")

if __name__ == "__main__":
    main(sys.argv)
