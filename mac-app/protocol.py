import struct

HEADER_FORMAT = "<IQIHHI"  # little-endian, standard sizes, zero padding - mirrors the C++ #pragma pack(1) struct exactly
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)
assert HEADER_SIZE == 24

UDP_PORT = 50505


def unpack_header(data: bytes):
    """Returns (sequence, capture_ts_us, capture_to_send_us, width, height, jpeg_size)."""
    return struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])
