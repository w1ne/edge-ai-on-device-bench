# PhoneWalker BLE controller firmware

Minimum-viable BLE port of the live USB-CDC wire protocol that the laptop daemon
speaks today. Target board: **Waveshare ESP32-S3 Zero**; servos: 4x Feetech
STS3032; IMU: MPU-6050.

> **Not flashed yet on this machine.** See *Prerequisite* below — the laptop's
> kernel has `cdc_acm` blacklisted (`/etc/modprobe.d/no-cdc-acm.conf`), which
> prevents `esptool.py` from talking to the chip's native USB-Serial/JTAG. The
> source builds clean under PlatformIO once the prerequisite is met. The
> daemon's existing pyusb bulk path still works for the current firmware; this
> blacklist blocks flashing, not runtime.

## Prerequisite on this laptop

```sh
sudo rm /etc/modprobe.d/no-cdc-acm.conf   # or comment out "blacklist cdc_acm"
sudo modprobe cdc_acm
ls /dev/ttyACM*                           # should now list /dev/ttyACM0
```

After flashing you may re-blacklist it if you want the daemon to retain
exclusive pyusb access, though CDC coexistence is usually fine.

## Build + upload (PlatformIO)

```sh
pip install --user platformio
cd firmware/ble_controller
pio run                          # compile
pio run --target upload          # flash via /dev/ttyACM0
pio device monitor                # watch boot logs at 115200
```

## Back up first — mandatory

`scripts/ble_smoke.py` and this firmware are only safe to flash **after** a
byte-for-byte backup of the currently running binary. From repo root:

```sh
mkdir -p firmware_backup
esptool.py --port /dev/ttyACM0 --baud 460800 read_flash \
    0 0x400000 "firmware_backup/original_$(date -u +%Y%m%d-%H%M%S).bin"
```

A healthy dump is 4 MB on this board. Confirm with `ls -lh firmware_backup/`
before proceeding.

## Restore the original firmware

If this BLE firmware misbehaves, restore the last backup:

```sh
esptool.py --port /dev/ttyACM0 --baud 460800 write_flash 0 \
    firmware_backup/original_<timestamp>.bin
```

Then power-cycle. The laptop daemon's pyusb-based wire protocol should come
back immediately.

## Wire protocol

Unchanged from the live USB-CDC firmware (see `docs/STATUS.md` §"Firmware
command surface"):

- **RX** (BLE write, newline-delimited JSON):
  - `{"c":"ping"}` -> `{"t":"ack","c":"ping","ok":true}`
  - `{"c":"pose","n":"neutral|lean_left|lean_right|bow_front","d":<ms>}`
  - `{"c":"walk","on":true,"stride":150,"step":400}`
  - `{"c":"stop"}` / `{"c":"jump"}`
- **TX** (BLE notify, 10 Hz):
  `{"t":"state","p":[p0..p3],"v":<cV>,"tmp":<C>,"ms":<uptime>,"imu":[ax,ay,az,gx,gy,gz]}`

The BLE service is Nordic UART Service (NUS):

| Role | UUID |
|---|---|
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` |
| RX (write) | `6E400002-B5A3-F393-E0A9-E50E24DCCA9E` |
| TX (notify) | `6E400003-B5A3-F393-E0A9-E50E24DCCA9E` |
| **CFG** (write/read) | `6E400004-B5A3-F393-E0A9-E50E24DCCA9E` |

## Pin remap over the air (no re-flash)

If servos don't move after a first flash, the most likely cause is wrong pin
defaults. Defaults in `src/robot_config.cpp::kDefaultConfig`:

| Signal | Default GPIO |
|---|---|
| Servo UART TX | 17 |
| Servo UART RX | 18 |
| Servo DE/RE | 255 (disabled) |
| I2C SDA | 8 |
| I2C SCL | 9 |
| VBAT ADC | 1 (unverified) |
| Servo IDs | 1, 2, 3, 4 |

Send a `cfg` JSON to either RX or CFG characteristic to persist new values to
NVS and reboot:

```json
{"cfg":{"tx":17,"rx":18,"dir":16,"sda":8,"scl":9,"ids":[1,2,3,4]}}
```

The firmware replies `{"t":"ack","c":"cfg","ok":true}` and
`{"t":"info","msg":"cfg saved; reboot to apply pins"}`. Power-cycle to apply.

## Safety

- **Power-on pose:** neutral, no motion until the first command.
- **BLE disconnect:** servos stop immediately.
- **10 s command timeout** while walking: halt + hold last pose; no autonomous
  motion.

## Smoke test

`scripts/ble_smoke.py` (Bleak) scans for `PhoneWalker-BLE`, connects, pings,
then drives a `pose` command and streams state packets for 5 s. Run it after a
successful flash.

## Porting to a different robot

The firmware is split into modules so adapting to a new robot is a
configuration edit. See `docs/FIRMWARE_PORTING.md` for the checklist;
the short version is "edit `src/robot_profile.{h,cpp}`, maybe touch
`src/gait.cpp`, rebuild."

## Known risks

- The `walk` gait in `src/main.cpp` is a stub (two alternating half-steps).
  The original firmware's timed gait has never been observed in source; we
  will have to tune this on the bench.
- Default pin assignments are best-guesses — verify against the Waveshare
  ESP32-S3 Zero schematic before expecting first-flash success.
- ADC divider ratio for battery voltage is assumed 2:1; if your rig uses a
  different divider the `v` field will be scaled wrong.
