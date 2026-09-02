"""The 24-byte header the Windows agent puts in front of every JPEG.

This module is the Python half of a contract whose other half is C++, and the two are
compiled and interpreted on different machines with nothing checking them against each
other at run time. tests/test_protocol.py does that check by parsing the C++ header.

See docs/PROTOCOL.md, which quotes this file by path and symbol.
"""

import struct

# The leading "<" does two jobs: little-endian byte order, and standard sizes with no
# alignment padding. That combination is what makes this string exactly equivalent to
# the C++ struct declared under #pragma pack(1) in windows-agent/src/protocol.h:4-13.
HEADER_FORMAT = "<IQIHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# A real raise, not an assert. `python -O` and PYTHONOPTIMIZE strip asserts, and this is
# the only Python-side mirror of the static_assert at windows-agent/src/protocol.h:15.
# Stripped, a typo in HEADER_FORMAT would silently misalign every field - nonsense
# timestamps, and a jpeg_size the clamping slice in payload() quietly absorbs into a
# decode warning - instead of failing loudly at import.
if HEADER_SIZE != 24:
    raise RuntimeError(
        f"HEADER_FORMAT unpacks to {HEADER_SIZE} bytes, not 24 - it no longer matches "
        "the #pragma pack(1) PacketHeader in windows-agent/src/protocol.h")

UDP_PORT = 50505  # mirrors kUdpPort in windows-agent/src/protocol.h:17

# Must match kRoiWidth/kRoiHeight in windows-agent/src/main.cpp:25-26. The header carries
# width/height on every frame, so this is also the place the two can be compared - the
# frame loop does exactly that, once, on the first frame it decodes.
ROI_W, ROI_H = 300, 300

# The maximum UDP payload. windows-agent/src/main.cpp:35 caps kMaxJpegSize at
# 65507 - sizeof(PacketHeader) so a whole frame always fits in one datagram. A smaller
# buffer would silently truncate real frames into the decode-failure path, where the
# symptom is indistinguishable from JPEG corruption.
MAX_DATAGRAM = 65535


def unpack_header(data):
    """-> (sequence, capture_wallclock_us, capture_to_send_us, width, height, jpeg_size).

    Returns None for anything shorter than the header rather than raising struct.error.
    The receive socket is bound to 0.0.0.0, so a stray datagram from a port scanner or a
    second sender must not be able to end the detection loop.

    capture_wallclock_us is Unix epoch microseconds (UTC) taken on the Windows machine at
    capture time. Comparing it against this machine's clock is only meaningful to the
    degree both are NTP-synced, which is why stats.py never does so directly - see
    docs/PROTOCOL.md.
    """
    if len(data) < HEADER_SIZE:
        return None
    return struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])


def payload(data, jpeg_size):
    """The JPEG bytes behind the header.

    Bounds-tolerant on purpose: Python clamps a slice, so a truncated datagram yields a
    short buffer that the decoder rejects rather than an exception. A header claiming
    4,000,000 bytes over a 31-byte datagram returns 7 bytes. Do NOT add validation that
    raises - the caller reports claimed-vs-present when the decode then fails, which is
    the difference between "the JPEG is corrupt" and "that was not our packet".
    """
    return data[HEADER_SIZE:HEADER_SIZE + jpeg_size]


def describe_header(fields):
    """One human line, for tools and test failure messages."""
    seq, capture_us, win_us, width, height, jpeg_size = fields
    return (f"seq={seq} {width}x{height} jpeg={jpeg_size}B "
            f"win={win_us / 1000:.1f}ms capture_wallclock_us={capture_us}")
