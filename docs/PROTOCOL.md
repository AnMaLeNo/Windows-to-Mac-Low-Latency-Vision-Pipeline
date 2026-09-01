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
    uint64_t capture_wallclock_us;
    uint32_t capture_to_send_us;
    uint16_t width;
    uint16_t height;
    uint32_t jpeg_size;
};
#pragma pack(pop)
static_assert(sizeof(PacketHeader) == 24, "PacketHeader size drifted");
```

Python (`mac-app/macvision/protocol.py`):

```python
HEADER_FORMAT = "<IQIHHI"  # little-endian, standard sizes, zero padding - matches #pragma pack(1)
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 24
```

## Latency measurement, and why it does not trust the clocks

The debug overlay shows:

- **`win`** — Windows-side capture → encode → send. One machine, one monotonic clock. Exact.
- **`mac`** — Mac-side packet received → detection drawn. One machine, one monotonic clock. Exact.
  Sampled after the boxes and the crosshair are drawn and *before* the overlay text, so it
  excludes `imshow` and `waitKey`. With `--no-display` it collapses to the decision time,
  and headless numbers are then not comparable to windowed ones.
- **`decide`** — packet received → trigger byte written. Printed in the `[stats]` line
  rather than the overlay, and unaffected by the display — so this is the figure to quote
  when comparing runs.

Everything above assumes the Windows agent is the source. A camera attached to the Mac
has no second clock, so there is nothing to calibrate and `net+` is not reported; and it
cannot know how long the photons took to become an array, so `win` becomes `cam` and is
reported as unmeasured. The overlay then reads `e2e>` instead of `e2e~`: a lower bound by
an unknown amount, rather than an understatement by a known one. The two are not
comparable, and `decide` is the only figure that is.
- **`net+`** — network delay *above the best case seen recently* (see below). Exact.
- **`e2e~`** — the sum of the three. The tilde is deliberate: it understates true
  glass-to-glass by exactly the irreducible one-way network hop, which on this LAN is
  1-2ms (measured by `ping`).

The obvious way to get end-to-end latency is to timestamp on Windows, subtract on the Mac,
and be done. That does not work here. Measured on this pair: the Windows clock was **238ms
behind** its own NTP server, stable across samples. A raw wall-clock delta would have
reported ~240ms of "latency" that was purely clock skew — and indeed did, before this was
diagnosed.

This is not a misconfiguration to fix. Stock Windows `w32time` targets roughly one-second
accuracy, not milliseconds; it reports its own error bound in `w32tm /query /status` as
`Root Dispersion` (8s here). It also *slews* rather than steps offsets of this size, so
`w32tm /resync` does not promptly correct it.

So the receiver self-calibrates instead, in
[`mac-app/macvision/stats.py`](../mac-app/macvision/stats.py). Queueing delay varies
frame to frame and occasionally clears; a clock offset is constant. Therefore over a rolling window of frames:

```
min(transit)  ~=  clock_offset + best-case network hop
net+          =   transit - min(transit)        # excess delay, offset-free
```

Subtracting the rolling minimum cancels the offset entirely and leaves the excess delay,
which is what actually matters — it is where lag buildup under load shows up. The rolling
window also tracks the offset as `w32time` slowly slews the clock mid-run.

The residual error is one-way hop time (1-2ms on a wired/local LAN), stated rather than
hidden. To check it, `ping` the other machine and halve the round-trip.

To diagnose a suspected clock problem directly:
`w32tm /stripchart /computer:time.windows.com /samples:6 /dataonly` on Windows prints the
live offset against a reference server.

## Deliberately out of scope for v1

No app-level packet fragmentation/reassembly, retries, or forward error correction. A JPEG
payload above ~1472 bytes will be fragmented at the IP layer on the way out, which is a
mature, fast kernel path — on a dedicated direct Ethernet link between two machines with no
other traffic, packet loss is expected to be rare in practice. `sequence` exists so this
assumption can actually be checked once the pipeline is running; add mitigation only if
measurement shows real, meaningful loss.
