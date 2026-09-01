"""Parse one captured datagram and ask the trigger rule about it - no detector needed.

Exercises the wire format and the trigger geometry with no cv2, no MPS and no Windows
agent, so "is the header being read correctly?" and "would this box have pressed the
key?" can be answered on any machine, including the Pi.

    python3 -m tools.frame_replay capture.bin
    python3 -m tools.frame_replay capture.bin --box 100,100,200,200 --box 0,0,10,10

Capture a datagram with tools/udp_test_listener.py, or build one with --synth.
"""

import argparse
import struct
import sys

from macvision.protocol import (HEADER_FORMAT, HEADER_SIZE, ROI_H, ROI_W,
                                describe_header, payload, unpack_header)
from macvision.rule import center_is_covered, roi_center

JPEG_SOI = b"\xff\xd8"


def parse_box(text):
    parts = text.split(",")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(f"{text!r}: expected x1,y1,x2,y2")
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: expected four numbers")


def main():
    p = argparse.ArgumentParser(prog="frame_replay",
                                description=__doc__.splitlines()[0])
    p.add_argument("path", nargs="?", help="a file holding one raw datagram")
    p.add_argument("--synth", action="store_true",
                   help="build a well-formed datagram instead of reading one")
    p.add_argument("--box", type=parse_box, action="append", dest="boxes",
                   metavar="X1,Y1,X2,Y2",
                   help="a detection box to test the rule against; repeatable")
    p.add_argument("--roi-w", type=int, default=ROI_W)
    p.add_argument("--roi-h", type=int, default=ROI_H)
    args = p.parse_args()

    if args.synth:
        body = JPEG_SOI + b"synthetic"
        data = struct.pack(HEADER_FORMAT, 1, 1_700_000_000_000_000, 1234,
                           args.roi_w, args.roi_h, len(body)) + body
    elif args.path:
        with open(args.path, "rb") as fh:
            data = fh.read()
    else:
        p.error("give a path, or --synth")

    print(f"--- datagram: {len(data)} bytes ---")
    header = unpack_header(data)
    if header is None:
        print(f"\nFAIL: {len(data)} bytes is shorter than the {HEADER_SIZE}-byte header.")
        print("  1. is this really one datagram, and not a truncated capture?")
        print("  2. did the capture tool strip or add framing?")
        return 1

    print("    " + describe_header(header))
    seq, capture_us, win_us, width, height, jpeg_size = header
    body = payload(data, jpeg_size)
    print(f"    payload: {jpeg_size} claimed, {len(body)} present")

    problems = []
    if len(body) != jpeg_size:
        problems.append(f"the payload is truncated: {len(body)} of {jpeg_size} bytes "
                        "arrived. The receiver would report a decode failure here.")
    if not body.startswith(JPEG_SOI):
        problems.append(f"the payload does not start with a JPEG SOI marker "
                        f"({body[:2].hex() or 'nothing'} instead of ffd8).")
    if (width, height) != (args.roi_w, args.roi_h):
        problems.append(f"the sender says {width}x{height}, this ROI is configured for "
                        f"{args.roi_w}x{args.roi_h} - the rule would test the wrong "
                        "pixel.")

    # The rule, against the sender's own dimensions, which is what the receiver checks.
    cx, cy = roi_center(args.roi_w, args.roi_h)
    print(f"--- the rule: is ({cx}, {cy}) covered? ---")
    boxes = args.boxes or []
    if not boxes:
        print("    no --box given, so nothing to test")
    else:
        for box in boxes:
            covered = center_is_covered([box], cx, cy)
            print(f"    {box} -> {'COVERS' if covered else 'misses'} the centre")
        hit = center_is_covered(boxes, cx, cy)
        print(f"    trigger would be: {'ON' if hit else 'off'}")
        print("    note: these boxes stand in for a person-filtered model. The real "
              "detector passes classes=[0], so an unfiltered model's boxes would "
              "include chairs and fire on them.")

    if problems:
        print(f"\nFAIL: {len(problems)} problem(s) with this datagram:")
        for i, problem in enumerate(problems, 1):
            print(f"  {i}. {problem}")
        return 1
    print(f"\nPASS: header parses, {len(body)} payload bytes present, ROI matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
