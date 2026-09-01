# mac-app

Python + Ultralytics receiver: frames → YOLO inference (MPS) → trigger + debug window.

Frames come from one of two sources, chosen at startup — either the Windows agent over
the Ethernet link, or a camera on this Mac (the iPhone over Continuity Camera). The
trigger rule, the detector and the debug window are identical either way.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
TRIGGER_TARGET=udp://raspberrypi.local:48010 python receiver.py
```

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

## Frame source

`FRAME_SOURCE` picks where frames come from. Unlike `TRIGGER_TARGET`, a source that
will not open is fatal — there is no useful degraded mode when there are no frames.

| value | meaning |
|---|---|
| `udp` (default) | the Windows agent, over the direct Ethernet link |
| `camera://N` | camera index `N` on this Mac — see below |

```bash
FRAME_SOURCE=camera://0 TRIGGER_TARGET=udp://raspberrypi.local:48010 python receiver.py
```

### Finding the camera index

**Indices are not stable, and they cannot be derived.** Plugging in the iPhone inserts it
at index 0 and pushes the built-in camera to 1 — and `system_profiler` lists the two in
the *opposite* order, so there is no name-to-index mapping to rely on. OpenCV's
AVFoundation backend addresses cameras by number and cannot report their names at all.

So find it by looking:

```bash
python tools/list_cameras.py 6
```

It probes each index, reports the resolution and the *measured* frame rate, and writes a
preview per camera with the 300×300 crop marked. Identify the iPhone from the previews.
(Measured, because the advertised rate is fiction: the iPhone reports 1 fps and delivers 24.)

Two failures it is built to tell apart, since both look like a camera that will not open:

- **No macOS camera permission.** It is granted to the app that owns the process —
  Terminal, iTerm, your editor — never to `python`. Run from a real terminal and answer
  the prompt, or check System Settings > Privacy & Security > Camera. Denied, frames
  arrive all-black rather than failing.
- **Continuity Camera not offered.** The iPhone must be locked, stationary, rear camera
  facing the scene, same Apple ID. *Unlocking it ends the session.* USB removes the
  wireless latency but none of those conditions. If `system_profiler` does not list the
  iPhone, no index will find it.

### Field of view

The crop is native pixels, **never resized** — a 1:1 window on the sensor output. So the
capture resolution is the only thing that decides how much of the scene those 300×300
pixels cover: 28% of frame height at 1080p, 42% at 720p, 62% at 480p. To see more of the
world at the same sharpness, lower `CAPTURE_SIZE` in [`sources.py`](sources.py).

### Why silence means different things

A camera that stops delivering releases the trigger after 150ms; a silent Windows source
does not, and holds the key. That asymmetry is deliberate: DXGI only produces a frame when
the screen changes, so silence there means "nothing moved". A camera always produces
frames, so silence means the iPhone locked or the link dropped — and holding the last
decision would leave a key pressed until the process died. The camera source also reopens
its handle on its own when Continuity Camera drops.

## Debug window

A window titled "debug" shows the current frame with detection boxes and a per-frame
timing overlay. A crosshair marks the ROI's centre pixel — the pixel the trigger rule tests —
and turns red when the trigger is firing. Press `q` in that window to quit. The first run downloads `yolov8n.pt`
automatically (stock COCO weights, just to validate the pipeline end to end — swap the
`WEIGHTS_PATH` constant in `detector.py` for your own trained model later).

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

## Networking

With a direct Ethernet cable between this Mac and the Windows PC and no DHCP server on
either end, both machines self-assign link-local (`169.254.x.x`) addresses automatically
within about a minute of the cable going up — that's enough for the UDP traffic to work with
no manual IP configuration. Find this Mac's address with `ifconfig | grep 169.254`, then set
that as the target IP constant in the Windows agent's `src/main.cpp` and rebuild it.

The first time `receiver.py` runs, macOS will likely prompt for a firewall/network
permission for incoming connections — allow it, or the Windows agent's packets won't arrive.
