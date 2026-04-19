#!/usr/bin/env python3
"""BLE smoke test for PhoneWalker-BLE firmware.

Scans for the advertisement, connects, pings, then issues a neutral pose and
logs the 10 Hz state-packet stream for 5 seconds.

Usage:
  python3 scripts/ble_smoke.py
  python3 scripts/ble_smoke.py --name PhoneWalker-BLE --duration 5

Exit code:
  0 - ping ack received and state packets flowed
  1 - device not found
  2 - connected but no ack within timeout
  3 - unexpected error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from typing import Optional

try:
    from bleak import BleakClient, BleakScanner
except ImportError:
    print("pip install --user bleak", file=sys.stderr)
    sys.exit(3)

NUS_SVC  = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX   = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # we write here
NUS_TX   = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # we notify from here

LOG = logging.getLogger("ble_smoke")


class SmokeState:
    def __init__(self) -> None:
        self.ack_ping = asyncio.Event()
        self.ack_pose = asyncio.Event()
        self.state_count = 0
        self.last_p: Optional[list[int]] = None
        self.first_p: Optional[list[int]] = None


def make_notification_handler(state: SmokeState):
    def _handler(_sender, data: bytearray) -> None:
        for line in data.split(b"\n"):
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                LOG.warning("non-JSON notify: %r", line)
                continue
            t = msg.get("t")
            if t == "ack":
                if msg.get("c") == "ping" and msg.get("ok"):
                    state.ack_ping.set()
                if msg.get("c") == "pose" and msg.get("ok"):
                    state.ack_pose.set()
                LOG.info("ack: %s", msg)
            elif t == "state":
                state.state_count += 1
                p = msg.get("p")
                if isinstance(p, list):
                    if state.first_p is None:
                        state.first_p = list(p)
                    state.last_p = list(p)
                if state.state_count <= 3 or state.state_count % 5 == 0:
                    LOG.info("state#%d p=%s v=%s imu=%s",
                             state.state_count, p, msg.get("v"), msg.get("imu"))
            elif t == "err":
                LOG.warning("err: %s", msg)
            else:
                LOG.info("msg: %s", msg)
    return _handler


async def find_device(name: str, timeout: float) -> Optional[str]:
    LOG.info("scanning for '%s' (%.1fs)...", name, timeout)
    dev = await BleakScanner.find_device_by_name(name, timeout=timeout)
    if dev is None:
        return None
    LOG.info("found %s @ %s", dev.name, dev.address)
    return dev.address


async def run(args) -> int:
    state = SmokeState()
    address = await find_device(args.name, args.scan_timeout)
    if address is None:
        LOG.error("device '%s' not found", args.name)
        return 1

    async with BleakClient(address) as client:
        LOG.info("connected: %s", client.is_connected)
        await client.start_notify(NUS_TX, make_notification_handler(state))

        LOG.info("writing ping...")
        await client.write_gatt_char(NUS_RX, b'{"c":"ping"}\n', response=False)
        try:
            await asyncio.wait_for(state.ack_ping.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            LOG.error("no ping ack within 2s")
            return 2

        LOG.info("writing pose neutral d=800...")
        await client.write_gatt_char(
            NUS_RX, b'{"c":"pose","n":"neutral","d":800}\n', response=False
        )
        try:
            await asyncio.wait_for(state.ack_pose.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            LOG.warning("no pose ack within 2s (continuing to observe stream)")

        LOG.info("streaming state packets for %.1fs...", args.duration)
        t_end = time.monotonic() + args.duration
        while time.monotonic() < t_end:
            await asyncio.sleep(0.1)

        await client.stop_notify(NUS_TX)

    LOG.info("done: %d state packets, first p=%s last p=%s",
             state.state_count, state.first_p, state.last_p)
    if state.state_count == 0:
        LOG.error("ping ack OK but no state packets received")
        return 2
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="PhoneWalker-BLE")
    ap.add_argument("--scan-timeout", type=float, default=10.0)
    ap.add_argument("--duration", type=float, default=5.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        LOG.exception("fatal: %s", exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
