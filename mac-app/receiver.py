import socket
import statistics
import time
from collections import deque

import cv2
import numpy as np

from detector import Detector
from protocol import HEADER_SIZE, UDP_PORT, unpack_header

ROI_W, ROI_H = 300, 300  # must match the Windows agent's constants in src/main.cpp

STATS_WINDOW = 200   # frames kept for the rolling stats line
STATS_EVERY = 100    # print the stats line this often


def main():
    detector = Detector(ROI_W, ROI_H)  # MPS warmup happens here, before the socket even opens

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"Listening on UDP {UDP_PORT}...")

    cv2.namedWindow("debug", cv2.WINDOW_NORMAL)
    last_seq = None

    # Rolling diagnostics. The key number is min(transit): transit is
    # (this machine's clock - the Windows capture timestamp), so it equals
    # true network transit PLUS whatever offset exists between the two clocks.
    # Queueing delay varies frame to frame and occasionally clears, but a clock
    # offset is constant - so the *minimum* transit over a few hundred frames is
    # essentially the clock offset plus the best-case hop (~1-2ms on a LAN).
    # min(transit) near zero => clocks agree, and any large e2e is real queueing.
    # min(transit) large and steady => that much of e2e is just clock skew.
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
        annotated = result.plot()

        mac_ms = (time.perf_counter() - recv_t0) * 1000
        # True glass-to-glass: capture on Windows -> detection drawn here. Spans two
        # machines' clocks, so it is only as accurate as their NTP sync (see
        # docs/PROTOCOL.md); win/mac below are each single-machine and always exact.
        transit_ms = (recv_wallclock_us - capture_wallclock_us) / 1000
        e2e_ms = transit_ms + mac_ms
        win_ms = capture_to_send_us / 1000

        transit_samples.append(transit_ms)
        e2e_samples.append(e2e_ms)
        stale_dropped_total += dropped
        frames_seen += 1

        cv2.putText(
            annotated,
            f"e2e={e2e_ms:.0f}ms (min {min(e2e_samples):.0f})  win={win_ms:.1f} mac={mac_ms:.1f}  seq={seq}",
            (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1,
        )

        if frames_seen % STATS_EVERY == 0:
            print(f"[stats] n={len(transit_samples)}  "
                  f"transit min={min(transit_samples):.1f} med={statistics.median(transit_samples):.1f} "
                  f"max={max(transit_samples):.1f}ms  |  "
                  f"e2e min={min(e2e_samples):.1f} med={statistics.median(e2e_samples):.1f}ms  |  "
                  f"stale dropped={stale_dropped_total}", flush=True)

        cv2.imshow("debug", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break


if __name__ == "__main__":
    main()
