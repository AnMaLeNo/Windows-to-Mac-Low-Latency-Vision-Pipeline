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
python receiver.py
```

A window titled "debug" shows the received ROI feed with detection boxes and a per-frame
timing overlay. Press `q` in that window to quit. The first run downloads `yolov8n.pt`
automatically (stock COCO weights, just to validate the pipeline end to end — swap the
`WEIGHTS_PATH` constant in `detector.py` for your own trained model later).

## Networking

With a direct Ethernet cable between this Mac and the Windows PC and no DHCP server on
either end, both machines self-assign link-local (`169.254.x.x`) addresses automatically
within about a minute of the cable going up — that's enough for the UDP traffic to work with
no manual IP configuration. Find this Mac's address with `ifconfig | grep 169.254`, then set
that as the target IP constant in the Windows agent's `src/main.cpp` and rebuild it.

The first time `receiver.py` runs, macOS will likely prompt for a firewall/network
permission for incoming connections — allow it, or the Windows agent's packets won't arrive.
