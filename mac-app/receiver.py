import socket
import statistics
import time
from collections import deque

import cv2
import numpy as np

from detector import Detector
from protocol import HEADER_SIZE, UDP_PORT, unpack_header
from trigger import open_trigger

ROI_W, ROI_H = 300, 300  # must match the Windows agent's constants in src/main.cpp

STATS_WINDOW = 200   # frames kept for the rolling stats line
STATS_EVERY = 100    # print the stats line this often


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

    detector = Detector(ROI_W, ROI_H)  # MPS warmup happens here, before the socket even opens

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"Listening on UDP {UDP_PORT}...")

    cv2.namedWindow("debug", cv2.WINDOW_NORMAL)
    last_seq = None

    # transit = this machine's clock minus the Windows capture timestamp. It contains
    # the real network delay AND the offset between the two machines' clocks, which is
    # not small: stock Windows w32time targets ~1s accuracy, and this pair was measured
    # 238ms apart. Taking transit at face value would report latency that is almost
    # entirely clock skew.
    #
    # So we self-calibrate instead of trusting the clocks. Queueing delay varies frame
    # to frame and occasionally clears; a clock offset is constant. Therefore
    # min(transit) over a few hundred frames ~= clock_offset + best-case network hop,
    # and subtracting it leaves the *excess* delay above best case - exact, and immune
    # to any offset. A rolling window keeps this correct even as w32time slowly slews
    # the Windows clock during a run.
    transit_samples = deque(maxlen=STATS_WINDOW)
    e2e_samples = deque(maxlen=STATS_WINDOW)
    frames_seen = 0
    stale_dropped_total = 0

    while True:
        # Block until at least one datagram is available, then drain any backlog that
        # piled up in the kernel socket buffer while we were busy decoding/inferring the
        # previous frame - keep only the newest one. Without this, a plain recvfrom() loop
        # processes every frame in arrival order: if inference is slower than the arrival
        # rate, nothing is ever lost, but everything falls further and further behind, so
        # the debug window visibly lags more the longer motion continues. Dropping stale
        # frames trades "see every frame" for "always see the most recent one", which is
        # what a latency-critical detector wants.
        data, _addr = sock.recvfrom(65535)
        dropped = 0
        sock.setblocking(False)
        while True:
            try:
                data, _addr = sock.recvfrom(65535)
                dropped += 1
            except BlockingIOError:
                break
        sock.setblocking(True)

        recv_t0 = time.perf_counter()
        recv_wallclock_us = time.time() * 1_000_000

        seq, capture_wallclock_us, capture_to_send_us, width, height, jpeg_size = unpack_header(data)
        if last_seq is not None and seq != last_seq + 1:
            missing = seq - last_seq - 1
            lost_in_transit = missing - dropped
            print(f"[gap] {missing} missing (seq {last_seq + 1}..{seq - 1}): "
                  f"{dropped} dropped here for staleness, {lost_in_transit} lost in transit")
        last_seq = seq

        jpeg_bytes = data[HEADER_SIZE:HEADER_SIZE + jpeg_size]
        frame = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            print(f"[warn] seq={seq}: failed to decode {jpeg_size}-byte JPEG payload, skipping")
            continue

        result = detector.infer(frame)

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

        mac_ms = (time.perf_counter() - recv_t0) * 1000
        # True glass-to-glass: capture on Windows -> detection drawn here. Spans two
        # machines' clocks, so it is only as accurate as their NTP sync (see
        # docs/PROTOCOL.md); win/mac below are each single-machine and always exact.
        transit_ms = (recv_wallclock_us - capture_wallclock_us) / 1000
        win_ms = capture_to_send_us / 1000

        transit_samples.append(transit_ms)
        stale_dropped_total += dropped
        frames_seen += 1

        # Excess network delay above the best case seen recently - this is where lag
        # buildup shows up, and it carries no clock-offset error.
        queue_ms = transit_ms - min(transit_samples)
        # Everything we can account for honestly. Understates true glass-to-glass by
        # exactly the irreducible one-way hop (~1-2ms on this LAN, per ping), which is
        # far better than the ~238ms of clock skew a raw wall-clock delta would inject.
        e2e_ms = win_ms + queue_ms + mac_ms
        e2e_samples.append(e2e_ms)

        cv2.putText(
            annotated,
            f"e2e~{e2e_ms:.0f}ms  win={win_ms:.1f} net+={queue_ms:.1f} mac={mac_ms:.1f}  "
            f"seq={seq}  trig={'ON' if hit else 'off'}",
            (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1,
        )

        if frames_seen % STATS_EVERY == 0:
            print(f"[stats] n={len(transit_samples)}  "
                  f"e2e med={statistics.median(e2e_samples):.1f} max={max(e2e_samples):.1f}ms  |  "
                  f"clock offset+hop={min(transit_samples):.1f}ms (calibrated out)  |  "
                  f"stale dropped={stale_dropped_total}  |  "
                  f"trigger writes dropped={trigger.dropped_writes}", flush=True)

        cv2.imshow("debug", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            # Releases the key on the way out. A crash or Ctrl-C skips this, which is
            # exactly what the ESP32's watchdog is there for - it fails the GPIO low
            # ~250ms after the byte stream stops, so no key can stay stuck down.
            trigger.close()
            break


if __name__ == "__main__":
    main()
