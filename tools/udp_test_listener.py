"""Throwaway local UDP listener for Milestone 2 (loopback bring-up test).

Run this first, then run capture_agent.exe (pointed at 127.0.0.1) in another window. Not
part of the shipped product - mac-app/macvision/ is the real thing. Kept stdlib-only and
free of any 3.8+-only syntax so it also runs as-is with this Windows machine's existing
Python 3.7.
"""
import os
import socket
import struct

HEADER_FORMAT = "<IQIHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
UDP_PORT = 50505
OUT_DIR = "received"


def main():
    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", UDP_PORT))
    print("Listening on 127.0.0.1:%d ... Ctrl+C to stop" % UDP_PORT)

    last_seq = None
    count = 0
    try:
        while True:
            data, _addr = sock.recvfrom(65535)
            sequence, capture_ts_us, capture_to_send_us, width, height, jpeg_size = \
                struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])

            gap = ""
            if last_seq is not None and sequence != last_seq + 1:
                gap = "  [GAP: expected %d, got %d]" % (last_seq + 1, sequence)
            last_seq = sequence

            jpeg_bytes = data[HEADER_SIZE:HEADER_SIZE + jpeg_size]
            out_path = os.path.join(OUT_DIR, "received_%04d.jpg" % count)
            with open(out_path, "wb") as f:
                f.write(jpeg_bytes)

            print("seq=%d capture_to_send_us=%d size=%dx%d jpeg_size=%d actual=%d -> %s%s" % (
                sequence, capture_to_send_us, width, height, jpeg_size, len(jpeg_bytes), out_path, gap))
            count += 1
    except KeyboardInterrupt:
        print("\nStopped after %d packets." % count)


if __name__ == "__main__":
    main()
