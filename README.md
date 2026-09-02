# Windows-to-Mac Low-Latency Vision Pipeline

Captures a small, fixed region of interest (ROI) from this Windows PC's screen in real time
and streams it over a direct wired Ethernet link to a MacBook Air M5, where YOLO
(Ultralytics) runs object detection on each frame. End-to-end latency is the one thing this
project optimizes for — not throughput, not image fidelity.

- [`windows-agent/`](windows-agent/) — C++ capture + send agent (DXGI Desktop Duplication → GPU-side crop → JPEG → UDP). See [`windows-agent/README.md`](windows-agent/README.md) to build and run.
- [`mac-app/`](mac-app/) — Python + Ultralytics. Takes a frame stream — from the Windows agent over UDP, or from a camera on this Mac (the iPhone over Continuity Camera), selected with `FRAME_SOURCE` — runs YOLO on it, and turns the decision into one byte of trigger state. Acquisition, processing and action are three swappable blocks. See [`mac-app/README.md`](mac-app/README.md) to set up and run. Optional, and outside the pipeline: a local web page on the Mac that starts and stops `macvision`, shows the debug image, the latency numbers and the trigger state, and builds its launch form from `macvision`'s own parser — see [`docs/DASHBOARD.md`](docs/DASHBOARD.md).
- [`pi-agent/`](pi-agent/) — Raspberry Pi keyboard proxy. The real keyboard plugs into the Pi, which forwards it to the PC and injects the trigger key into the same stream, so **the PC sees exactly one keyboard**. See [`pi-agent/README.md`](pi-agent/README.md).
- [`firmware/`](firmware/) — the two Arduino sketches that turn a detection back into a keypress on the PC: `esp32-link/` (USB serial from the Mac → GPIO) and `pro-micro-hid/` (GPIO → USB HID keyboard).
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the wire format shared by both sides.
- [`docs/TRIGGER.md`](docs/TRIGGER.md) — the trigger rule, the Mac→ESP32→Pro Micro link, and its wiring.

## Status

Bring-up milestones, in order:

0. Windows agent builds and runs.
1. Windows agent captures the ROI and writes JPEGs to disk (`--dump N`), no networking — verifies capture/crop/encode correctness in isolation.
2. Windows agent sends over loopback UDP to a local test listener (`tools/udp_test_listener.py`) — verifies the wire protocol end-to-end on one machine.
3. Real hand-off: Windows agent points at the Mac's actual IP over the direct Ethernet link; `mac-app/macvision/` runs on the Mac (`python3 -m macvision`).
4. Trigger loop closed: a car on the ROI's centre pixel holds a key down on the PC, via ESP32 → Pro Micro. See [`docs/TRIGGER.md`](docs/TRIGGER.md).
5. **One keyboard, not two.** The PC must not see a second keyboard alongside the real one, so the real keyboard's receiver moves to a Raspberry Pi that forwards it *and* the trigger as a single HID stream. Mac → Pi is one UDP byte, same protocol as the ESP32 link. See [`pi-agent/README.md`](pi-agent/README.md).

   Verified on hardware: a Logitech MX Keys captured via evdev and `EVIOCGRAB`, the trigger arriving from the Mac, the two merged into one report, the keepalive holding a key through a static screen, and the watchdog releasing it 292ms after the Mac goes silent. Still untested: the output sinks — nothing is wired to the PC yet, so `pro-micro-proxy` has compiled but never run.

6. **A second eye.** The Mac can run the same detector on a camera of its own instead of the Windows stream — an iPhone over Continuity Camera, wired or wireless — chosen with `FRAME_SOURCE`. One source at a time; the trigger rule, the detector and the trigger link are untouched. See [`mac-app/README.md`](mac-app/README.md#frame-source).

   Verified on hardware: an iPhone 15 Pro at 1920×1080, 24fps measured, YOLO running on its native 300×300 centre crop at a 19.5ms median end-to-end, stale frames correctly discarded in favour of newer ones, and the 150ms staleness release firing when the camera stops.

7. **A dashboard.** A web page, on the Mac, for the Mac: it starts and stops `macvision`, shows the debug image, the latency numbers and the trigger state as they happen, and builds its launch form from `macvision`'s own parser. `macvision` gains only an off-by-default `--telemetry` tap, whose one call sits after the trigger byte is written; the page is a separate process (`python3 -m dashboard` from `mac-app/`) and can do nothing the command line cannot. See [`docs/DASHBOARD.md`](docs/DASHBOARD.md).

   Not yet run on the Mac: everything is tested with fakes on a machine with no camera and no model. The cost check (`mac-app/tools/telemetry_tap.py`, see the doc) is the first thing to run there.
