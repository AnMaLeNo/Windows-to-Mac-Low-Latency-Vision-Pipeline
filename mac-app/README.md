# mac-app

Python + Ultralytics: a frame stream → YOLO inference (MPS) → one byte of trigger state
out to whatever holds the key down on the PC.

Three blocks, and each is swappable without touching the others:

    1. acquisition            2. processing              3. action
       macvision/sources/        detector.py  (YOLO)         trigger.py
         udp.py     ──────→      rule.py      (decision) ──→   serial → ESP32
         camera.py                                             udp    → Pi proxy

Whichever source runs, the detector, the rule and the action never learn which — and
`--source` is the whole change.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# frames from the Windows agent over the wire
TRIGGER_TARGET=udp://raspberrypi.local:48010 python3 -m macvision

# frames from a camera on this Mac, cropped to a 300x300 region
TRIGGER_TARGET=udp://raspberrypi.local:48010 \
  python3 -m macvision --source "camera://0?crop=100,100,300,300&size=1280x720&fps=60"
```

`python receiver.py` still works — it is a shim that calls the same `main()`.

**Run it from `mac-app/`, not from inside `macvision/`.** Ultralytics downloads
`yolov8n.pt` into the process working directory on first run, and `.gitignore` only
covers it there; running from elsewhere quietly starts committing 6MB of weights.

`TRIGGER_TARGET` says where a detection should go. **Without it nothing gets pressed** —
the default (`auto`) looks for a USB serial device on this Mac, which is the original
ESP32-on-the-Mac wiring, not the keyboard proxy on the Pi. Running it bare is a
working vision pipeline attached to nothing, so it prints a loud banner rather than
letting you find out by watching a key that never arrives.

| value | meaning |
|---|---|
| `udp://host[:48010]` | the Raspberry Pi keyboard proxy — see [`pi-agent/README.md`](../pi-agent/README.md) |
| `serial:///dev/cu.usbserial-0001` | an ESP32 plugged into this Mac |
| `auto` (default) | first serial port found on this Mac, else nothing |
| `none` | vision only, no trigger, no banner |

`--source` chooses where frames come from, and follows the same idiom:

| value | meaning |
|---|---|
| `udp://[host][:50505]` (default) | the Windows agent over the wire |
| `camera://0` | camera index 0 on this Mac, whole frame |
| `camera://0?crop=x,y,w,h&size=WxH&fps=N` | a region of a camera, at a chosen mode |
| `camera:///dev/video0` | a camera by device path |

`--list-cameras` probes which indices answer. `$MACVISION_SOURCE` is the environment
equivalent. The crop is validated against the camera's real frame at startup and
refused if it does not fit — numpy would otherwise clamp it silently and hand the
detector a frame of the wrong shape.

The same values are accepted as `--trigger-target`, which wins over the environment.
The env var is what the READMEs and the muscle memory use; the flag is an extra door.
`python3 -m macvision --help` lists everything else (`--no-display`, `--weights`,
`--roi-w/--roi-h`, `--udp-port`, `--list-ports`, …) and works on a machine with neither
opencv nor ultralytics installed.

A window titled "debug" shows the received ROI feed with detection boxes and a per-frame
timing overlay. A crosshair marks the ROI's centre pixel — the pixel the trigger rule tests —
and turns red when the trigger is firing. Press `q` in that window to quit. The first run downloads `yolov8n.pt`
automatically (stock COCO weights, just to validate the pipeline end to end — swap the
`WEIGHTS_PATH` constant in [`macvision/detector.py`](macvision/detector.py), or pass
`--weights` / set `MACVISION_WEIGHTS`, for your own trained model later).

## What runs where

| module | block | role | needs |
|---|---|---|---|
| [`macvision/sources/__init__.py`](macvision/sources/__init__.py) | 1 | the `Source` contract, `Capture`, and `--source` parsing | — |
| [`macvision/sources/udp.py`](macvision/sources/udp.py) | 1 | the Windows agent's link: drain, header, sequence accounting | opencv (to decode) |
| [`macvision/sources/camera.py`](macvision/sources/camera.py) | 1 | a camera on this Mac: device, crop, size, fps | opencv |
| [`macvision/protocol.py`](macvision/protocol.py) | 1 | the 24-byte wire header, shared with the Windows agent | — |
| [`macvision/codec.py`](macvision/codec.py) | 1 | JPEG bytes → a BGR frame | opencv |
| [`macvision/detector.py`](macvision/detector.py) | 2a | the Ultralytics adapter and the MPS warmup | ultralytics |
| [`macvision/rule.py`](macvision/rule.py) | 2b | the decision: is the centre pixel inside any box | — |
| [`macvision/trigger.py`](macvision/trigger.py) | 3 | the one-byte level link, its transports and the keepalive | pyserial (serial only) |
| [`macvision/stats.py`](macvision/stats.py) | — | the self-calibrating latency window and the overlay vocabulary | — |
| [`macvision/display.py`](macvision/display.py) | — | the debug window | opencv (HighGUI) |
| [`macvision/loop.py`](macvision/loop.py) | — | the frame loop — the ordering *is* the design | — |
| [`macvision/__main__.py`](macvision/__main__.py) | — | argparse, and the startup order | — |

Blocks 2a and 2b are split because they answer different questions. `detector.py` turns
pixels into boxes and is the only module that may import ultralytics; `rule.py` turns
boxes into a decision and imports *nothing*, which is what makes the rule that presses a
key readable and testable on any machine.

Everything in the "—" rows imports with nothing installed. That is what lets the wire
format, the trigger rule, the latency arithmetic and the frame ordering be tested on any
machine, and [`tests/test_imports.py`](tests/test_imports.py) fails if a stray
third-party import ever creeps into one of them.

## Tests

Stdlib only, no pytest, and they run anywhere — including a Raspberry Pi with no opencv:

```bash
python3 -m tests.test_protocol      # the header, checked against the C++ declaration
python3 -m tests.test_rule          # the trigger rule's edges
python3 -m tests.test_stats         # the clock-skew calibration
python3 -m tests.test_sources       # both sources: newest-wins, drops, cropping
python3 -m tests.test_trigger       # TRIGGER_TARGET parsing, and the release-last race
python3 -m tests.test_imports       # the core stays dependency-free
python3 -m tests.test_loop_order    # trigger.update() fires before any drawing
```

## Tools

```bash
TRIGGER_TARGET=udp://raspberrypi.local:48010 python3 -m tools.trigger_check
python3 -m tools.frame_replay capture.bin --box 100,100,200,200
```

`trigger_check` drives the real trigger module at the real 50Hz and reads the Pi's own
packet counter over HTTP, so it answers "did the byte arrive?" from the far end — with
no Windows PC and no MPS in the picture. `frame_replay` parses one captured datagram and
asks the rule about it, with no detector at all.

## Trigger hardware

Whatever `TRIGGER_TARGET` points at, the protocol is the same and the failure modes are
the same: one byte of state, re-sent after every inference *and* from a 20ms keepalive,
so the stream is idempotent and a lost packet corrects itself. If the far end stops
hearing from this Mac for 250ms it releases the key on its own — no key survives the
process that was holding it.

A missing or unreachable target never stops the detector: the vision pipeline runs
alone and the crosshair still shows what the rule decided.

Wiring and the reasoning behind the numbers are in
[`docs/TRIGGER.md`](../docs/TRIGGER.md) for the direct ESP32 link, and in
[`pi-agent/README.md`](../pi-agent/README.md) for the Raspberry Pi proxy.

## Things that will bite

- **The two machines' clocks disagree by ~238ms.** That is why `e2e~` is a *sum* of
  three separately-measured spans rather than a wall-clock delta, and why the tilde is
  there. See [`docs/PROTOCOL.md`](../docs/PROTOCOL.md).
- **`auto` finds nothing once the link moved to the Pi** — there is no serial device on
  this Mac any more. Worse, if there *is* one, `auto` will happily stream 0x01/0x00 at
  50Hz into it whatever it is: a Pro Micro, a 3D printer, a debug probe. Point at the
  Pi explicitly.
- **Always the `cu.` device node, never `tty.`.** Opening `tty.*` blocks until DCD is
  asserted, which a USB-serial bridge never does, so the open hangs forever.
- **`--no-display` changes what `mac` measures.** It collapses to the decision time, so
  headless and windowed numbers are not comparable. `decide` — packet received to
  trigger byte written — is printed in the `[stats]` line and *is* comparable across
  both; quote that one.
- **The UDP trigger host is resolved once, at startup.** A DHCP change mid-session
  needs a restart. This is deliberate: resolving `raspberrypi.local` per datagram meant
  an mDNS round trip 50 times a second on the latency-critical path.
- **A camera's own latency is invisible and probably dominates.** Between the photons
  and AVFoundation there are typically tens of milliseconds, and no way to measure it
  from software — it needs a hardware reference (film a running timer, or flash an LED).
  So the camera source reports its upstream time as unknown and the overlay shows
  `e2e>` — a lower bound — instead of `e2e~`. Do not compare a camera run's `e2e` to a
  UDP run's.
- **A camera queues frames.** `cv2.VideoCapture.read()` hands back the *oldest* frame it
  holds, and `CAP_PROP_BUFFERSIZE=1` is not honoured by AVFoundation. The camera source
  therefore reads on its own thread and keeps only the newest, counting the rest into
  `stale dropped` — the same trade the UDP drain makes.
- **A dead Windows agent can leave the key held.** If the sender dies while a person is
  on the centre pixel, this Mac blocks in `recvfrom` forever while the keepalive keeps
  asserting the last state — so the far end's watchdog is fed and never fires. Ctrl-C
  releases it. Releasing on idle instead would be wrong: DXGI produces no frames on a
  static screen, which is the whole reason the keepalive exists.

## Networking

With a direct Ethernet cable between this Mac and the Windows PC and no DHCP server on
either end, both machines self-assign link-local (`169.254.x.x`) addresses automatically
within about a minute of the cable going up — that's enough for the UDP traffic to work with
no manual IP configuration. Find this Mac's address with `ifconfig | grep 169.254`, then set
that as the target IP constant in the Windows agent's `src/main.cpp` and rebuild it.

The first time the receiver runs, macOS will likely prompt for a firewall/network
permission for incoming connections — allow it, or the Windows agent's packets won't arrive.
