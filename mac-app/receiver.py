import socket
import time

import cv2
import numpy as np

from detector import Detector
from protocol import HEADER_SIZE, UDP_PORT, unpack_header

ROI_W, ROI_H = 300, 300  # must match the Windows agent's constants in src/main.cpp


def main():
    detector = Detector(ROI_W, ROI_H)  # MPS warmup happens here, before the socket even opens

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", UDP_PORT))
    print(f"Listening on UDP {UDP_PORT}...")

    cv2.namedWindow("debug", cv2.WINDOW_NORMAL)
    last_seq = None

    while True:
        data, _addr = sock.recvfrom(65535)
        recv_t0 = time.perf_counter()

        seq, capture_ts_us, capture_to_send_us, width, height, jpeg_size = unpack_header(data)
        if last_seq is not None and seq != last_seq + 1:
            print(f"[gap] expected {last_seq + 1}, got {seq} ({seq - last_seq - 1} dropped/reordered)")
        last_seq = seq

        jpeg_bytes = data[HEADER_SIZE:HEADER_SIZE + jpeg_size]
        frame = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            print(f"[warn] seq={seq}: failed to decode {jpeg_size}-byte JPEG payload, skipping")
            continue

        result = detector.infer(frame)
        annotated = result.plot()

        infer_done_us = int((time.perf_counter() - recv_t0) * 1_000_000)
        cv2.putText(
            annotated,
            f"seq={seq} win={capture_to_send_us}us mac={infer_done_us}us",
            (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1,
        )

        cv2.imshow("debug", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break


if __name__ == "__main__":
    main()
