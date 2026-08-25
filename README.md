# Windows-to-Mac Low-Latency Vision Pipeline

Captures a small, fixed region of interest (ROI) from this Windows PC's screen in real time
and streams it over a direct wired Ethernet link to a MacBook Air M5, where YOLO
(Ultralytics) runs object detection on each frame. End-to-end latency is the one thing this
project optimizes for — not throughput, not image fidelity.

- [`windows-agent/`](windows-agent/) — C++ capture + send agent (DXGI Desktop Duplication → GPU-side crop → JPEG → UDP). See [`windows-agent/README.md`](windows-agent/README.md) to build and run.
- [`mac-app/`](mac-app/) — Python + Ultralytics receiver (UDP → JPEG decode → YOLO → debug visualization). See [`mac-app/README.md`](mac-app/README.md) to set up and run.
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the wire format shared by both sides.

## Status

Bring-up milestones, in order:

0. Windows agent builds and runs.
1. Windows agent captures the ROI and writes JPEGs to disk (`--dump N`), no networking — verifies capture/crop/encode correctness in isolation.
2. Windows agent sends over loopback UDP to a local test listener (`tools/udp_test_listener.py`) — verifies the wire protocol end-to-end on one machine.
3. Real hand-off: Windows agent points at the Mac's actual IP over the direct Ethernet link; `mac-app/receiver.py` runs on the Mac.
