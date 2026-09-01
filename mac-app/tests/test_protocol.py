"""Checks the wire header against the C++ declaration it claims to mirror.

The format exists in three transcriptions - windows-agent/src/protocol.h,
macvision/protocol.py, and tools/udp_test_listener.py - compiled and interpreted on
different machines with nothing comparing them at run time. A single wrong code
misparses every frame: nonsense timestamps, and a jpeg_size the clamping slice quietly
absorbs into a decode warning that looks like a corrupt image.

So this compares against the AUTHORITY rather than against a second copy of the same
assumption: it parses the struct members out of the C++ header and reconstructs the
format string from them.

    python3 -m tests.test_protocol      (from mac-app/)

Needs the windows-agent sources present; skips the cross-language half without them.
"""

import os
import re
import struct
import sys

from macvision.protocol import (HEADER_FORMAT, HEADER_SIZE, MAX_DATAGRAM, ROI_H, ROI_W,
                                UDP_PORT, payload, unpack_header)

# repo root: mac-app/tests/ -> mac-app/ -> the repository
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CPP_HEADER = os.path.join(HERE, "windows-agent", "src", "protocol.h")
CPP_MAIN = os.path.join(HERE, "windows-agent", "src", "main.cpp")

# C type -> struct code, for the types PacketHeader actually uses.
CODES = {"uint8_t": "B", "uint16_t": "H", "uint32_t": "I", "uint64_t": "Q"}


def cpp_struct_members(text):
    """The PacketHeader members, in declaration order."""
    body = re.search(r"struct\s+PacketHeader\s*\{(.*?)\}", text, re.S)
    if not body:
        return []
    return re.findall(r"\b(uint\d+_t)\s+(\w+)\s*;", body.group(1))


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    # --- behaviour, always runs -----------------------------------------------------
    check("HEADER_SIZE", HEADER_SIZE, 24)

    fields = (7, 1_700_000_000_000_000, 4321, 300, 300, 9)
    packed = struct.pack(HEADER_FORMAT, *fields)
    check("round trip", unpack_header(packed), fields)
    check("round trip with a payload tail",
          unpack_header(packed + b"\xff" * 100), fields)

    # A stray datagram from a port scanner must not end the detection loop.
    for n in (0, 1, 23):
        try:
            got = unpack_header(b"\x00" * n)
        except Exception as exc:
            failures.append(f"unpack_header({n} bytes) raised {exc!r}, expected None")
        else:
            check(f"unpack_header({n} bytes)", got, None)

    # The clamping slice: claimed far beyond what arrived must not raise.
    check("payload clamps a truncated datagram",
          len(payload(b"\x00" * 31, 4_000_000)), 7)
    check("payload of a well-formed datagram",
          len(payload(b"\x00" * HEADER_SIZE + b"\xab" * 40, 40)), 40)

    check("MAX_DATAGRAM", MAX_DATAGRAM, 65535)

    # The size guard must survive `python -O`, which strips asserts and would leave the
    # only Python-side mirror of the C++ static_assert unenforced.
    import macvision.protocol as mod
    with open(mod.__file__) as fh:
        source = fh.read()
    if re.search(r"^\s*assert\s+HEADER_SIZE", source, re.M):
        failures.append("protocol.py guards HEADER_SIZE with an assert; `python -O` "
                        "strips it. Use a real raise.")

    # --- against the C++ authority --------------------------------------------------
    if not os.path.exists(CPP_HEADER):
        print(f"skip: {CPP_HEADER} not found; ran the behavioural half only")
    else:
        with open(CPP_HEADER) as fh:
            header_src = fh.read()

        members = cpp_struct_members(header_src)
        if not members:
            failures.append("could not parse struct PacketHeader out of protocol.h")
        else:
            rebuilt = "<" + "".join(CODES.get(t, "?") for t, _ in members)
            check("format rebuilt from protocol.h", rebuilt, HEADER_FORMAT)
            names = [n for _, n in members]
            check("field order",
                  names, ["sequence", "capture_wallclock_us", "capture_to_send_us",
                          "width", "height", "jpeg_size"])
            # The offsets docs/PROTOCOL.md publishes as a table.
            offsets, at = [], 0
            for t, _ in members:
                offsets.append(at)
                at += struct.calcsize("<" + CODES.get(t, "x"))
            check("field offsets", offsets, [0, 4, 12, 16, 18, 20])

        sa = re.search(r"static_assert\s*\(\s*sizeof\(PacketHeader\)\s*==\s*(\d+)",
                       header_src)
        check("C++ static_assert size", int(sa.group(1)) if sa else None, 24)

        port = re.search(r"kUdpPort\s*=\s*(\d+)", header_src)
        check("UDP_PORT vs kUdpPort", UDP_PORT, int(port.group(1)) if port else None)

        if os.path.exists(CPP_MAIN):
            with open(CPP_MAIN) as fh:
                main_src = fh.read()
            w = re.search(r"kRoiWidth\s*=\s*(\d+)", main_src)
            h = re.search(r"kRoiHeight\s*=\s*(\d+)", main_src)
            check("ROI_W vs kRoiWidth", ROI_W, int(w.group(1)) if w else None)
            check("ROI_H vs kRoiHeight", ROI_H, int(h.group(1)) if h else None)
        else:
            print(f"skip: {CPP_MAIN} not found; ROI constants unchecked")

        print(f"checked {HEADER_FORMAT!r} against {os.path.relpath(CPP_HEADER, HERE)}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("header format, field order, offsets, port, ROI, short and truncated "
          "datagrams: all consistent")
    return 0


if __name__ == "__main__":
    sys.exit(run())
