#!/usr/bin/env python3
"""
BLE peripheral mock: exposes Nordic UART Service on the laptop's Bluetooth
adapter so the Android companion app (which normally connects to the ESP32
running PhoneWalker-BLE) can be exercised end-to-end without real hardware.

Uses BlueZ via D-Bus (`bluez-peripheral`). This is Linux-only and assumes a
functioning BlueZ >= 5.43 experimental BLE stack.

Contract we mimic (see android_companion/ + docs/PHONE_BRAIN_*.md):
  service UUID : 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
  RX (write)   : 6E400002-B5A3-F393-E0A9-E50E24DCCA9E
  TX (notify)  : 6E400003-B5A3-F393-E0A9-E50E24DCCA9E
  adv name     : PhoneWalker-BLE

Behavior: any newline-delimited JSON line the phone writes to RX is echoed
back on TX with a server timestamp wrapper. Unknown 'c' values get a simple
"ack" reply so Python clients see a full roundtrip.

Install:
    pip install --user bluez-peripheral dbus-next

Run:
    sudo systemctl start bluetooth
    python3 scripts/ble_peripheral_mock.py

Note: `bluez-peripheral` changes API occasionally. If this breaks, fall back
to `bleak` peripheral branch or a node.js (`bleno`) variant — the wire
contract is what matters, not this shim.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

try:
    from bluez_peripheral.gatt.service import Service
    from bluez_peripheral.gatt.characteristic import characteristic, CharacteristicFlags as CF
    from bluez_peripheral.advert import Advertisement
    from bluez_peripheral.util import Adapter, get_message_bus
    from bluez_peripheral.agent import NoIoAgent
except ImportError:
    sys.stderr.write(
        "missing dependency: pip install --user bluez-peripheral dbus-next\n"
    )
    sys.exit(1)


NUS_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_RX = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
NUS_TX = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
ADV_NAME = "PhoneWalker-BLE"


class NusService(Service):
    def __init__(self):
        super().__init__(NUS_SERVICE, True)
        self._rx_buf = bytearray()

    @characteristic(NUS_TX, CF.NOTIFY)
    def tx(self, options):  # pragma: no cover - notify-only, no read path
        return bytes()

    @characteristic(NUS_RX, CF.WRITE | CF.WRITE_WITHOUT_RESPONSE)
    def rx(self, options):  # pragma: no cover - write-only, read unused
        return bytes()

    @rx.setter
    def rx_setter(self, value, options):
        self._rx_buf.extend(bytes(value))
        while b"\n" in self._rx_buf:
            nl = self._rx_buf.index(b"\n")
            line = bytes(self._rx_buf[:nl]).rstrip(b"\r").decode("utf-8", "replace")
            del self._rx_buf[: nl + 1]
            if line.strip():
                asyncio.get_event_loop().create_task(self._handle(line))

    async def _handle(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except Exception:
            obj = {"raw": line}
        reply = {
            "t": "mock_echo",
            "ts": round(time.time(), 3),
            "got": obj,
        }
        payload = (json.dumps(reply) + "\n").encode("utf-8")
        # chunk to <=180 bytes to stay under a negotiated 247-MTU write
        for i in range(0, len(payload), 180):
            self.tx.changed(payload[i : i + 180])
        print(f"[mock] rx={line!r}  tx={reply!r}")


async def main() -> None:
    bus = await get_message_bus()
    svc = NusService()
    await svc.register(bus)

    agent = NoIoAgent()
    await agent.register(bus)

    adapter = await Adapter.get_first(bus)
    advert = Advertisement(ADV_NAME, [NUS_SERVICE], 0, 60)
    await advert.register(bus, adapter)

    try:
        adapter_name = await adapter.get_name()
    except Exception:
        adapter_name = "(unknown adapter)"
    print(f"[mock] advertising as {ADV_NAME} on {adapter_name}, ctrl-C to stop")
    try:
        await bus.wait_for_disconnect()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    asyncio.run(main())
