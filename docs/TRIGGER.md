# Trigger link: Mac detection → keypress on the Windows PC

The vision pipeline ends with a decision. This is how that decision becomes a real keystroke
on the PC without either machine trusting the other's USB stack.

```
MacBook ──USB serial──> ESP32 ──GPIO (active high)──> Pro Micro ──USB HID──> Windows PC
         1 byte/update                  held while true              held key
```

Two microcontrollers rather than one because the two ends belong to two different USB hosts:
the ESP32 is a USB *device* on the Mac, the Pro Micro is a USB *device* on the PC, and a
single MCU cannot be a device on two hosts at once. The GPIO between them is the crossing
point, and it is a bare wire — nothing to parse, nothing to time out.

## The rule

Per frame, on the Mac: **is the ROI's centre pixel inside any detected car's bounding
box?** Implemented in `center_is_covered()` in [`mac-app/receiver.py`](../mac-app/receiver.py) —
a point-in-box test against `result.boxes.xyxy`, vectorised over all boxes.

True → the key goes down and *stays* down. False → it comes back up. The key is held for
exactly as long as the condition holds, so Windows' own key-repeat decides what a held key
means to whatever application is listening.

The debug window draws a crosshair on that exact pixel — red when the trigger is on, white
when it is off — so the rule is visible without probing the wire.

## Wire protocol (Mac → ESP32)

One byte, no framing: `0x01` = active, `0x00` = idle. 115200 baud.

There is no "on change only" optimisation. The Mac sends the current state after every
inference *and* from a 20ms keepalive thread, and the ESP32 always acts on the newest byte
it has. That makes the stream idempotent — a lost or duplicated byte corrects itself within
20ms — and it removes the class of bug where the two ends disagree about the current state
forever because one edge went missing.

The keepalive is not optional. DXGI Desktop Duplication only produces a frame when the
screen actually changes, so a static screen means no frames, which means no frame-driven
sends. Without a timer independent of the frame stream, the key would release itself the
moment the PC's screen stopped moving.

### Watchdog

If the ESP32 hears nothing for **250ms** it drives the GPIO low regardless of the last state
it was told. That covers the Mac app crashing, being Ctrl-C'd, or the cable being pulled —
all of which would otherwise leave a key physically held down with nothing left running to
release it. At a 20ms keepalive, 250ms is ~12 consecutive missed sends: far outside normal
jitter, and fast enough that a stuck key is never perceptible.

Measured on hardware: the key self-releases **232ms** after the sender goes silent — 250ms
less the ~20ms that had already elapsed since the last keepalive byte. `trigger_selftest.py`
exercises this deliberately, because it is the one path that never runs in normal operation
and would otherwise only be discovered the first time something crashed.

## Wiring

| ESP32 | Pro Micro | |
|---|---|---|
| `GPIO 4` | `D2` | the trigger line, active high |
| `GND` | `GND` | **required** — see below |
| | `D2` → 10k → `GND` | **required** pull-down — see below |

Neither board powers the other. The ESP32 takes 5V from the Mac's USB, the Pro Micro from
the PC's.

**The ground wire is mandatory.** The two boards sit on two different computers' USB
supplies, so without a shared reference "3.3V" on one board means nothing on the other and
the input reads garbage. This does tie the two machines' USB grounds together; on two
machines on the same mains circuit that is normally uneventful, and if it ever isn't, the
clean fix is an optocoupler on the signal line rather than removing the ground.

**The 10k pull-down is mandatory.** The ATmega32U4 has internal pull-*ups* only, so with the
ESP32 unplugged or unpowered `D2` floats, and a floating input on a device whose job is to
type will press keys on its own. The resistor makes "no signal" mean "released" electrically,
not just in firmware.

**Voltage margin, worth knowing about.** The ESP32 drives 3.3V. A 5V/16MHz Pro Micro needs
`0.6 × VCC = 3.0V` to read a logic high, so 3.3V works with 0.3V of margin — reliable in
practice, and the standard way these two boards are paired. If you ever see chatter, the
fixes in order of effort are: a 3.3V/8MHz Pro Micro (no shifting needed at all), a level
shifter, or inverting the logic to `INPUT_PULLUP` on the Pro Micro and having the ESP32 pull
the line *down* to signal — that last one needs no extra parts, but it flips the polarity
this code is written for.

## Flashing

Each folder carries a `platformio.ini` with `src_dir = .`, so the `.ino` opens in the
Arduino IDE and builds under PlatformIO from the same single copy:

```
pio run -d firmware/esp32-link    -t upload --upload-port COM5
pio run -d firmware/pro-micro-hid -t upload --upload-port COM4
```

Change the key in one line: `TRIGGER_KEY` at the top of `pro-micro-hid.ino`. It is a plain
`char`, and the `KEY_*` constants from `Keyboard.h` work there too.

**PlatformIO does not bundle `Keyboard.h`** the way the Arduino IDE does, hence the explicit
`lib_deps` in `pro-micro-hid/platformio.ini`. Same upstream library, just not implicit.

**The board identity matters for the Pro Micro.** This one enumerates as VID 2341 / PID 8037
— a genuine Arduino Micro identity, not SparkFun's 1B4F — so `board = micro`. That choice
sets both the VID/PID compiled into the HID descriptor and the PID the uploader waits for
after the 1200-baud touch reset, so the wrong one breaks the upload handshake.

### This ESP32 board needs the BOOT button held

Uploading fails with `Wrong boot mode detected (0x13)` on every attempt, including esptool
directly. The chip answers the sync, but the auto-reset circuit never pulls IO0 low, so it
boots normally instead of into download mode. Hold `BOOT` (a.k.a. `IO0`/`FLASH`) for the
whole upload — holding it throughout is harmless.

Only the IO0 half is broken: `RTS`→`EN` works, which is why esptool's closing
`Hard resetting via RTS pin` succeeds. The same missing path is why the board does not reset
when a serial port is opened, so it never reprints its ROM banner — do not read that silence
as a dead board.

> A Pro Micro that types on its own is awkward to reflash, since it may fight you for the
> port. The escape hatch is the bootloader: tap RST twice quickly and upload during the
> 8-second window. Worth knowing *before* you need it.

## Testing without the Mac

- [`tools/trigger_selftest.py`](../tools/trigger_selftest.py) — run from the **Windows PC**.
  Drives the ESP32 as the Mac would and watches this machine's real keyboard state, so it
  verifies and times the entire chain with no Mac and nothing to observe by eye. It refuses
  to run if the key is already down, which is what a floating `D2` looks like.
  Note that it makes the Pro Micro genuinely type: put the focus somewhere harmless first.
- [`tools/trigger_bench.py`](../tools/trigger_bench.py) — runs from either machine, drives
  the line manually (`blink` / `on` / `watchdog`) for when you want to watch the LED.

The ESP32 firmware mirrors the trigger line onto the onboard LED (`GPIO 2`). Without it
there is no way to check that half of the link on its own.

## Latency, measured

[`tools/trigger_selftest.py`](../tools/trigger_selftest.py) times the whole link end to end:
it writes the state byte and then watches Windows' own keyboard state via `GetAsyncKeyState`
until the key moves. That single number covers everything after the detection decision —
USB OUT to the bridge, the UART byte, both firmware loops, the GPIO, the HID interrupt
endpoint, and the Windows input stack.

20 trials, ESP32-D0WD-V3 on a CH340 board → Arduino Micro, 2026-08-26:

| | min | median | max |
|---|---|---|---|
| press | 0.75ms | **1.24ms** | 1.61ms |
| release | 0.59ms | **0.64ms** | 1.19ms |

The press spread is 0.86ms wide, which is the 1ms `bInterval` of the HID interrupt endpoint
showing up exactly where it should: the byte arrives at a uniformly random phase within the
host's polling cycle, so the wait for the next IN transfer is uniform over 0–1ms. Release
measures consistently faster than press by about 0.6ms and this is not explained here —
worth knowing before reading anything into the asymmetry.

Per-hop, the parts too small to isolate in that measurement: the UART byte costs 87µs at
115200, and each firmware loop is well under 10µs.

**~1.2ms against a vision pipeline measured in tens of milliseconds.** This link is not
where the latency is, and tuning it further would be wasted effort — raising the baud rate
to 921600, for instance, buys back 78µs of a 1240µs path.

The one place the code does spend care on latency is *ordering*: `trigger.update()` is
called immediately after inference, before `result.plot()` and `imshow()`, which together
cost several milliseconds and are pure debug output. The trigger byte leaves the moment the
decision exists, not at the end of the frame.

## Alternative considered: no ESP32 at all

A plain USB-to-TTL adapter (FTDI/CP2102 breakout) exposes its **DTR** pin on a header, and
DTR is settable directly from the host — `ser.dtr = True` in pyserial is one USB control
transfer that moves a real pin. That removes the ESP32, its firmware, and the UART hop
entirely: the Mac would drive the Pro Micro's input line directly.

It is genuinely simpler, and about 100µs faster, which is nothing here. It was not chosen
because it also gives up the watchdog — a host-driven pin holds its last state forever if
the Mac app dies, so a held key would stay held — and because on an ESP32 devkit the trick
is unavailable anyway: DTR and RTS there are wired to the auto-reset circuit, so toggling
DTR reboots the board instead of signalling. Worth knowing if you ever want to drop a board
from the chain and are willing to handle the stuck-key case another way.
