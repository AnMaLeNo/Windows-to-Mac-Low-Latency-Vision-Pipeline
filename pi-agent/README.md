# Keyboard proxy on the Raspberry Pi

The Windows PC must see **exactly one keyboard**. So the real keyboard stops being
plugged into the PC: its Logitech receiver moves to the Raspberry Pi, the Pi forwards
every keystroke onward, and the Pi injects one extra key into that same stream when
the Mac's vision pipeline reports a person on the ROI's centre pixel.

```
Clavier ──2.4GHz──> Receiver ──USB-A──> Raspberry Pi ──sink──> Windows PC
                                             ↑                (one keyboard)
                        Mac ──UDP/WiFi───────┘
                    (vision trigger, 1 byte, 50Hz)
```

The PC cannot tell the two sources apart, because at the HID layer there is nothing
to tell apart: both end up as key usages in the same 8-byte report.

## Why the Pi and not the Mac

The Mac cannot be a USB keyboard. macOS has no USB device/gadget mode — every port
is host-only — and Apple has blocked publishing a Bluetooth HID Device profile since
Catalina, on Classic (SDP) and LE (the 0x1812 GATT service) alike. Linux has neither
restriction.

Putting the proxy on the Pi rather than the Mac also buys something worth more than
convenience: **your keystrokes never touch Python or the YOLO loop.** A 30ms
inference cannot delay your typing, because typing is not on that machine at all.

## What runs where

| | |
|---|---|
| [`piproxy/keyboard.py`](piproxy/keyboard.py) | reads the receiver via evdev, `EVIOCGRAB`-ed |
| [`piproxy/keymap.py`](piproxy/keymap.py) | evdev key codes → USB HID usages |
| [`piproxy/report.py`](piproxy/report.py) | merges both sources into one 8-byte report |
| [`piproxy/api.py`](piproxy/api.py) | UDP trigger (hot path) + HTTP API (control) |
| [`piproxy/sinks.py`](piproxy/sinks.py) | where reports go, plus emitter and watchdog |

## Setup

```bash
git clone https://github.com/AnMaLeNo/Windows-to-Mac-Low-Latency-Vision-Pipeline.git
cd Windows-to-Mac-Low-Latency-Vision-Pipeline/pi-agent
./setup.sh --check     # report what is missing, change nothing
./setup.sh             # install deps + the systemd unit
```

`setup.sh` is idempotent — re-running it is also how you upgrade. It installs
`python3-evdev` and `python3-serial` from apt rather than pip, because Bookworm marks
the system Python externally-managed (PEP 668) and both are packaged by Debian anyway.

It deliberately **does not touch `/boot/firmware/config.txt`.** That file is where USB
gadget mode is enabled, it needs a reboot, and on a Pi that is also a server that is a
human's decision. See [Output option C](#c-hidg--the-pi-is-the-keyboard) below.

Then choose an output in `/etc/default/piproxy` and start it:

```bash
sudo systemctl enable --now piproxy
curl -s localhost:48011/status | python3 -m json.tool
```

## Choosing the output

The output is the one piece of hardware still open, so it sits behind a pluggable
sink. Everything upstream is written once and does not care which is attached.

### A. `log` — no hardware at all

```bash
python3 -m piproxy --sink log
```

Prints reports instead of emitting them. **Start here**: it exercises the entire
pipeline — capture, grab, merge, trigger, watchdog — with nothing wired up, so when
you do attach hardware you already know everything above it works.

### B. `serial` — a Pro Micro is the keyboard

```bash
python3 -m piproxy --sink serial --serial-port /dev/ttyUSB0
```

The Pi sends framed reports to a Pro Micro plugged into the PC, which enumerates as
the USB HID keyboard. **This is the recommended path on a Pi that is already doing
other work**, because it changes nothing about the Pi: no `config.txt` edit, no
reboot, no change to how the Pi is powered. The receiver goes in a USB-A port, which
is a host port — exactly what USB-A is for.

The two cannot be wired directly: the Pro Micro is a USB *device* on the PC and the
Pi's USB-A ports are hosts, so an ESP32 bridges USB serial to a UART.

```
Pi ──USB (CH340)──> ESP32 ──GPIO4 (UART TX)──> Pro Micro D0/RX
                      GND ────────────────────> Pro Micro GND
```

Both firmwares are in [`firmware/`](../firmware): `esp32-proxy/` and
`pro-micro-proxy/`. They replace `esp32-link/` and `pro-micro-hid/`, which
implemented the older 1-bit trigger link and cannot carry an 8-byte report.

> **Moving from the old wiring.** The ESP32 end stays on GPIO 4 — `esp32-proxy`
> remaps its UART TX onto that pin precisely so an existing wire does not move. The
> Pro Micro end **must** move from `D2` to `D0/RX`: `D2` is not a UART pin on the
> ATmega32U4, and no firmware can make it one.
>
> **Never wire the Pro Micro's TX back to anything here.** It is a 5V output; this
> link only ever needs to run one way.

Framing matters in a way it did not for the old one-byte link: a dropped byte would
desynchronise every following report. Each is wrapped in a `0xAB` start byte and an
XOR checksum, and both firmwares resynchronise on the next valid header.

Baud is **115200 across all three ends**, set by the CH340 between the Pi and the
ESP32 — clones of it get unreliable well before the AVR does, as `docs/TRIGGER.md`
already recorded. A 10-byte frame costs 868µs, which is not where the latency is. If
you ever raise it, note the AVR is *exact* at 1000000 and 8.5% off at 921600: the
faster-looking rate is the broken one.

Send `?` to the ESP32 on the USB serial line and it reports frame counts, so link
quality is measurable without a logic analyser.

### C. `hidg` — the Pi is the keyboard

```bash
python3 -m piproxy --sink hidg --hid-device /dev/hidg0
```

No Pro Micro at all: the Pi's own USB-C port becomes a USB HID keyboard. Cleanest on
a **dedicated** Pi, and a poor fit for one that is also a server, because:

- The Pi 5 has exactly one dual-role port (`dwc2`, the USB-C one). All four USB-A
  ports are behind `xhci-hcd` host controllers, and XHCI has no device mode — no
  cable or adapter changes that.
- That same USB-C port is how the Pi is powered. Making it the data link to the PC
  means the PC powers the Pi, and a PC USB port cannot feed a Pi 5 under load. The
  documented answer is to power via the GPIO 5V pins and use a USB-C→USB-A adapter
  at the PC end to drop the CC lines, so PD never negotiates.
- If the PC is off, the Pi is off — fatal for a Pi running anything else.

Enable with [`setup-gadget.sh`](setup-gadget.sh), which edits `config.txt` and needs
a reboot.

## The trigger from the Mac

Two doors, on purpose.

**UDP, port 48010 — the hot path.** One byte: `0x01` active, `0x00` idle. This is
byte-for-byte the protocol the ESP32 link already spoke, so
[`mac-app/trigger.py`](../mac-app/trigger.py) kept its logic and swapped only its
transport:

```bash
TRIGGER_TARGET=udp://raspberrypi.local:48010 python3 receiver.py
```

There is no "on change only" optimisation, and no retransmission. The Mac sends the
current state after every inference *and* from a 20ms keepalive, and the Pi always
acts on the newest datagram. That makes the stream idempotent — a lost or duplicated
packet corrects itself within 20ms — and removes the class of bug where the two ends
disagree forever because one edge went missing.

The keepalive is not optional. DXGI Desktop Duplication only produces a frame when
the screen changes, so a static screen means no frames and no frame-driven sends.
Without a timer independent of the frame stream, the key would release itself the
moment the PC's screen stopped moving.

**HTTP, port 48011 — control and inspection.**

```bash
curl -s localhost:48011/status | python3 -m json.tool
curl -s -X POST localhost:48011/trigger -d '{"active":true}'
curl -s -X POST localhost:48011/key -d '{"key":"a","action":"press"}'
curl -s localhost:48011/keyboards
```

Fine at human rates. Not for the 50Hz stream — it is a request/response round trip on
a fresh TCP connection, where the UDP path is one datagram.

### Watchdog

If no trigger update arrives for **250ms**, the Pi releases the trigger's keys
regardless of the last state it was told. That covers the Mac app crashing, being
Ctrl-C'd, or the WiFi dropping — all of which would otherwise leave a key held down
with nothing left running to release it. At a 20ms keepalive, 250ms is ~12
consecutive missed sends: far outside normal jitter.

Measured against the real Pi: **292ms** from the Mac going silent to the key coming
up (250ms timeout plus the watchdog's check granularity).

Note what it does *not* touch: keys held on the **physical** keyboard. Those are
released by their own key-up events, or wholesale if the receiver disappears.

## Verified on hardware

Pi 5 + CH340 ESP32 + Arduino Micro, 2026-08-27. The Pro Micro was plugged into the
**Pi** rather than the PC for these, so its HID output came back to the same machine
and every hop became observable from one place — see
[`tools/loopback_check.py`](tools/loopback_check.py).

| | |
|---|---|
| Whole chain, trigger → keypress | `KEY_K` down, held, up. The host's auto-repeat fires while held, which is what proves it is *held* and not re-typed |
| Both sources merged | `a` held → `KEY_A`; person arrives → `KEY_K` joins it; `a` released → only `KEY_A` goes up |
| Key sequence | `a`, `b`, `c` each down-then-up, in order |
| Pi → ESP32 link, 2000 frames | **2000/2000 received, 0 checksum failures**, driven at 936 frames/s — 81% of the line's capacity and ~19× the operational rate |

That last row is what settles 115200 on this CH340: the rate the project already
distrusted turns out to be error-free well past what this system asks of it.

Two behaviours worth knowing, both benign:

- **Closing the serial port resets the ESP32.** RTS is wired to `EN`, so the board
  reboots whenever piproxy stops, taking its frame counters with it. The link is back
  in ~300ms, and the Pro Micro's watchdog releases every key in the meantime.
- **The `?` statistics only survive an unbroken connection**, for the same reason.

## Measured latency

Trigger path, Mac → Pi (ICMP RTT, 25 samples each). The Pi answers on both of its
interfaces, which turns out to be the useful experiment:

| target | min | avg | max | σ |
|---|---|---|---|---|
| `eth0` — the Pi is **wired** | 2.66ms | 8.12ms | 50.74ms | 9.29ms |
| `wlan0` — the Pi on WiFi | 5.26ms | 7.11ms | 13.46ms | 1.87ms |

They are the same, and the wired one has the worse maximum. **The jitter is not the
Pi's**: it is the Mac's, which reaches the LAN over `en0` (WiFi) because its Ethernet
is already committed to the direct link to the Windows PC. Pointing `TRIGGER_TARGET`
at the Pi's wired address — which is what you should do — fixes only half the path,
because the other half was never the problem.

So roughly **1.3–25ms one way**, against **1.24ms** for the old wired ESP32 → Pro Micro
link. To actually remove the wireless hop, the *Mac* needs a wired path to the Pi: put
a small switch on the Mac↔PC link and hang the Pi off it. All three machines go wired,
and a 1-byte trigger at 50Hz costs nothing next to the video already on that segment.
Nothing in the code changes, only `TRIGGER_TARGET`.

Keystroke path (not yet measured on hardware): receiver → Pi USB poll (1ms) →
userspace → sink → PC. Expect ~2–4ms added over a directly-connected keyboard.

## Things that will bite

**`EVIOCGRAB` is what stops the Pi typing too.** Without the grab, every keystroke you
forward is *also* delivered to the Pi's own consoles and sessions. The proxy grabs by
default and refuses a device it cannot grab, rather than half-working. `--no-grab`
exists for testing and prints a warning.

**Auto-repeat must not be forwarded.** evdev emits `value=2` for the kernel's software
repeat; USB keyboards never send repeats, they report a key as continuously held and
the *host* decides the rate. Forwarding them would double up with Windows' own repeat.
[`keyboard.py`](piproxy/keyboard.py) drops them.

**A Logitech receiver is not one device.** The kernel's `hid-logitech-dj` driver
demultiplexes the proprietary HID++ frames into one input node per paired device, and
a receiver typically exposes several `/dev/hidraw*` nodes carrying HID++ rather than
clean boot-keyboard reports. That is why this reads evdev and translates, instead of
passing raw HID reports through. `--list` shows what is actually there.

**Six keys is the boot-protocol limit.** If more are held, the trigger key takes a
slot — it is the one key in this system that must never be lost. A trigger key of
`f13`–`f15` cannot collide with anything you physically press, since no real keyboard
has those.

## Running it by hand

```bash
python3 -m piproxy --list                        # what keyboards can I see?
python3 -m piproxy --sink log                    # full pipeline, no hardware
python3 -m piproxy --sink log --no-keyboard      # trigger only
python3 -m piproxy --sink log --echo-repeats     # log keepalives too
python3 -m piproxy --help
```

`--no-keyboard` is the right first run on a machine you are logged into over SSH: it
brings up the trigger path without grabbing any input device.
