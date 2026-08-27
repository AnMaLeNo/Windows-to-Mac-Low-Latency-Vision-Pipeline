# mac-app

Python + Ultralytics receiver: UDP → JPEG decode → YOLO inference (MPS) → debug window.

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

A window titled "debug" shows the received ROI feed with detection boxes and a per-frame
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
