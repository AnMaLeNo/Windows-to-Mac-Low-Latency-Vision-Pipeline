import statistics
import time
from collections import deque

import cv2
import numpy as np

from detector import Detector
from sources import STATS_WINDOW, open_source
from trigger import open_trigger

ROI_W, ROI_H = 300, 300  # must match the Windows agent's constants in src/main.cpp

STATS_EVERY = 100  # print the stats line this often


def center_is_covered(result, cx: int, cy: int) -> bool:
    """The trigger rule: is the ROI's centre pixel inside any detected car's box?"""
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return False
    b = boxes.xyxy.cpu().numpy()
    return bool(np.any((b[:, 0] <= cx) & (cx <= b[:, 2]) & (b[:, 1] <= cy) & (cy <= b[:, 3])))


def main():
    # Opened before the detector on purpose. On a classic ESP32 devkit the bridge chip's DTR
    # line is wired to the auto-reset circuit, so merely opening the port reboots the MCU and
    # costs ~1s of boot. Doing it here hides that entirely behind the MPS warmup below.
    trigger = open_trigger()
    cx, cy = ROI_W // 2, ROI_H // 2

    detector = Detector(ROI_W, ROI_H)  # MPS warmup happens here, before the source opens

    # Opened last, after the warmup, so that nothing accumulates while Metal compiles its
    # kernels: a UDP socket bound early would fill its buffer with frames destined to be
    # discarded, and a camera would stream into a slot nobody is reading.
    source = open_source(ROI_W, ROI_H)

    cv2.namedWindow("debug", cv2.WINDOW_NORMAL)

    e2e_samples = deque(maxlen=STATS_WINDOW)
    frames_seen = 0
    stalled = False

    while True:
        frame = source.read()

        if frame is None:
            # Only a camera source returns this, and only when it has genuinely stopped
            # delivering. Release explicitly: trigger.py's keepalive would otherwise keep
            # resending the last decision forever, which is right for a static Windows
            # screen and very wrong for an iPhone that just locked.
            trigger.update(False)
            if not stalled:
                print("[warn] source went silent - trigger released, waiting for frames")
                stalled = True
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
            continue

        if stalled:
            print("[info] frames again")
            stalled = False

        result = detector.infer(frame.image)

        # Fire before drawing anything. result.plot() and imshow() together cost several
        # milliseconds, and none of that work is needed to decide whether to press the key -
        # so the trigger byte goes out the instant the decision exists, not at end of frame.
        hit = center_is_covered(result, cx, cy)
        trigger.update(hit)

        annotated = result.plot()
        # Crosshair on the pixel the rule actually tests, coloured by the decision, so the
        # debug window shows what the Pro Micro is doing without probing the wire.
        cv2.drawMarker(annotated, (cx, cy), (0, 0, 255) if hit else (255, 255, 255),
                       cv2.MARKER_CROSS, 12, 1)

        # This loop's own cost: everything from taking delivery of the frame to having a
        # decision drawn. Single-machine and exact, whichever source is running.
        mac_ms = (time.perf_counter() - frame.recv_t0) * 1000
        # Everything we can account for honestly. Understates true glass-to-glass by
        # whatever the source cannot observe - the one-way network hop for UDP (~1-2ms on
        # this LAN, per ping), the sensor-to-USB path for a camera.
        e2e_ms = frame.upstream_ms + mac_ms
        e2e_samples.append(e2e_ms)
        frames_seen += 1

        cv2.putText(
            annotated,
            f"e2e~{e2e_ms:.0f}ms  {frame.overlay} mac={mac_ms:.1f}  "
            f"{frame.label}  trig={'ON' if hit else 'off'}",
            (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1,
        )

        if frames_seen % STATS_EVERY == 0:
            print(f"[stats] {source.name}  "
                  f"e2e med={statistics.median(e2e_samples):.1f} max={max(e2e_samples):.1f}ms  |  "
                  f"{source.stats()}  |  "
                  f"trigger writes dropped={trigger.dropped_writes}", flush=True)

        cv2.imshow("debug", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    # Releases the key on the way out. A crash or Ctrl-C skips this, which is exactly what
    # the ESP32's watchdog is there for - it fails the GPIO low ~250ms after the byte
    # stream stops, so no key can stay stuck down.
    trigger.close()
    source.close()


if __name__ == "__main__":
    main()
