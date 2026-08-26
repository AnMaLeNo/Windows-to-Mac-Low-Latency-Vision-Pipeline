# Wire protocol

One UDP packet per frame: a 24-byte header followed immediately by `jpeg_size` bytes of JPEG
data. Total UDP payload = `24 + jpeg_size` bytes. Port: **50505**.

All multi-byte fields are little-endian (both an x86_64 Windows machine and Apple Silicon are
little-endian, so no byte-swapping is needed — just explicit, padding-free packing on both
sides).

| Offset | Size | Field | Type | Meaning |
|---|---|---|---|---|
| 0 | 4 | `sequence` | uint32 | Increments once per packet actually **sent**. Cursor-only DXGI updates are never sent, so this is not a raw capture-loop counter. Lets the receiver detect drops/reordering. |
| 4 | 8 | `capture_wallclock_us` | uint64 | **Unix epoch microseconds (UTC)** read on the Windows machine at capture time. The receiver subtracts this from its own clock to get true end-to-end latency — accurate only to the degree both machines are NTP-synced (see below). |
| 12 | 4 | `capture_to_send_us` | uint32 | Windows-local elapsed time from a successful frame acquire to immediately before `send()` — i.e. GPU crop + map + JPEG encode cost. Clock-sync-free and directly meaningful on its own. Measured from *after* `AcquireNextFrame` returns, so the blocking wait for the desktop to change is excluded. |
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

## Clock sync — what the `e2e` number is worth

The debug overlay shows three timings:

- **`win`** — Windows-side capture → encode → send. Single machine, one monotonic clock. Exact.
- **`mac`** — Mac-side packet received → detection drawn. Single machine, one monotonic clock. Exact.
- **`e2e`** — capture on Windows → detection drawn on the Mac, i.e. the real glass-to-glass
  number. Computed as `mac_clock_now - capture_wallclock_us + mac`. This one spans **two
  machines' clocks**, so any offset between them lands directly in the result.

`e2e` is therefore only as trustworthy as the NTP sync between the two machines. Both should
be actively syncing (`w32tm /resync` on Windows, System Settings → General → Date & Time →
"Set time and date automatically" on macOS). A typical well-synced pair lands within a few
milliseconds of each other; a machine that hasn't synced in a while can be off by tens or
hundreds of milliseconds, which would swamp the measurement entirely.

**Sanity check:** `e2e` should be a bit larger than `win + mac` — the difference is network
transit plus kernel queueing. If `e2e` comes out *smaller* than `win + mac`, or negative, the
clocks are out of sync and the number is meaningless until they're resynced. Compare the gap
against a quick `ping` round-trip if you want to confirm.

`win` and `mac` never have this problem, so when in doubt they remain the reliable numbers.

## Deliberately out of scope for v1

No app-level packet fragmentation/reassembly, retries, or forward error correction. A JPEG
payload above ~1472 bytes will be fragmented at the IP layer on the way out, which is a
mature, fast kernel path — on a dedicated direct Ethernet link between two machines with no
other traffic, packet loss is expected to be rare in practice. `sequence` exists so this
assumption can actually be checked once the pipeline is running; add mitigation only if
measurement shows real, meaningful loss.
