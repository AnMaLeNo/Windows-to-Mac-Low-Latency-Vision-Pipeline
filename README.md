# Windows-to-Mac Low-Latency Vision Pipeline

Captures a small, fixed region of interest (ROI) from this Windows PC's screen in real time
and streams it over a direct wired Ethernet link to a MacBook Air M5, where YOLO
(Ultralytics) runs object detection on each frame. End-to-end latency is the one thing this
project optimizes for — not throughput, not image fidelity.

- [`windows-agent/`](windows-agent/) — C++ capture + send agent (DXGI Desktop Duplication → GPU-side crop → JPEG → UDP). See [`windows-agent/README.md`](windows-agent/README.md) to build and run.
- [`mac-app/`](mac-app/) — Python + Ultralytics receiver (UDP → JPEG decode → YOLO → debug visualization). See [`mac-app/README.md`](mac-app/README.md) to set up and run.
- [`pi-agent/`](pi-agent/) — Raspberry Pi keyboard proxy. The real keyboard plugs into the Pi, which forwards it to the PC and injects the trigger key into the same stream, so **the PC sees exactly one keyboard**. See [`pi-agent/README.md`](pi-agent/README.md).
- [`firmware/`](firmware/) — the two Arduino sketches that turn a detection back into a keypress on the PC: `esp32-link/` (USB serial from the Mac → GPIO) and `pro-micro-hid/` (GPIO → USB HID keyboard).
- [`docs/PROTOCOL.md`](docs/PROTOCOL.md) — the wire format shared by both sides.
- [`docs/TRIGGER.md`](docs/TRIGGER.md) — the trigger rule, the Mac→ESP32→Pro Micro link, and its wiring.

## Status

Bring-up milestones, in order:

0. Windows agent builds and runs.
1. Windows agent captures the ROI and writes JPEGs to disk (`--dump N`), no networking — verifies capture/crop/encode correctness in isolation.
2. Windows agent sends over loopback UDP to a local test listener (`tools/udp_test_listener.py`) — verifies the wire protocol end-to-end on one machine.
3. Real hand-off: Windows agent points at the Mac's actual IP over the direct Ethernet link; `mac-app/receiver.py` runs on the Mac.
4. Trigger loop closed: a person on the ROI's centre pixel holds a key down on the PC, via ESP32 → Pro Micro. See [`docs/TRIGGER.md`](docs/TRIGGER.md).
5. **One keyboard, not two.** The PC must not see a second keyboard alongside the real one, so the real keyboard's receiver moves to a Raspberry Pi that forwards it *and* the trigger as a single HID stream. Mac → Pi is one UDP byte, same protocol as the ESP32 link. See [`pi-agent/README.md`](pi-agent/README.md).

   Verified on hardware: a Logitech MX Keys captured via evdev and `EVIOCGRAB`, the trigger arriving from the Mac, the two merged into one report, the keepalive holding a key through a static screen, and the watchdog releasing it 292ms after the Mac goes silent. Still untested: the output sinks — nothing is wired to the PC yet, so `pro-micro-proxy` has compiled but never run.
