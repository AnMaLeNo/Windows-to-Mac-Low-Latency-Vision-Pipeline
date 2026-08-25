# Wire protocol

One UDP packet per frame: a 24-byte header followed immediately by `jpeg_size` bytes of JPEG
data. Total UDP payload = `24 + jpeg_size` bytes. Port: **50505**.

All multi-byte fields are little-endian (both an x86_64 Windows machine and Apple Silicon are
little-endian, so no byte-swapping is needed — just explicit, padding-free packing on both
sides).

| Offset | Size | Field | Type | Meaning |
|---|---|---|---|---|
| 0 | 4 | `sequence` | uint32 | Increments once per packet actually **sent**. Cursor-only DXGI updates are never sent, so this is not a raw capture-loop counter. Lets the receiver detect drops/reordering. |
| 4 | 8 | `capture_ts_us` | uint64 | Windows-local **monotonic** microseconds since the agent process started. Not wall-clock, not comparable to anything on the Mac — only useful for computing Windows-side inter-frame cadence from consecutive deltas. |
| 12 | 4 | `capture_to_send_us` | uint32 | Windows-local elapsed time from a successful frame acquire to immediately before `send()` — i.e. GPU crop + map + JPEG encode cost. Clock-sync-free and directly meaningful on its own. |
| 16 | 2 | `width` | uint16 | ROI width in pixels. |
| 18 | 2 | `height` | uint16 | ROI height in pixels. |
| 20 | 4 | `jpeg_size` | uint32 | Byte length of the JPEG payload that follows the header. |

## Reference layouts

C++ (`windows-agent/src/protocol.h`):

```cpp
#pragma pack(push, 1)
struct PacketHeader {
    uint32_t sequence;
    uint64_t capture_ts_us;
    uint32_t capture_to_send_us;
    uint16_t width;
    uint16_t height;
    uint32_t jpeg_size;
};
#pragma pack(pop)
static_assert(sizeof(PacketHeader) == 24, "PacketHeader size drifted");
```

Python (`mac-app/protocol.py`):

```python
HEADER_FORMAT = "<IQIHHI"  # little-endian, standard sizes, zero padding - matches #pragma pack(1)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 24
```

## Clock-sync caveat

`capture_ts_us` and any timestamp the Mac records on receive are **not** a valid basis for a
true one-way glass-to-glass latency number unless both machines' wall clocks are synced (e.g.
both on NTP). Without that, they're only useful for measuring jitter/inter-arrival gaps on
each side independently.

v1 approach: each side measures and logs only its own local processing duration —
`capture_to_send_us` on Windows, an equivalent receive-to-detection-done duration computed
locally on the Mac. Network transit time on the direct Gigabit link is treated as a small,
separately-estimated constant (e.g. a quick `ping` once the cable is up) rather than derived
from a cross-machine timestamp delta. Revisit only if a real cross-machine number turns out to
be needed.

## Deliberately out of scope for v1

No app-level packet fragmentation/reassembly, retries, or forward error correction. A JPEG
payload above ~1472 bytes will be fragmented at the IP layer on the way out, which is a
mature, fast kernel path — on a dedicated direct Ethernet link between two machines with no
other traffic, packet loss is expected to be rare in practice. `sequence` exists so this
assumption can actually be checked once the pipeline is running; add mitigation only if
measurement shows real, meaningful loss.
