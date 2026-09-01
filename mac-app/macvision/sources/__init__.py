"""Where frames come from. Exactly one source runs; the two are never mixed.

    udp      the Windows agent captures a region of its own screen, JPEG-encodes it
             and sends it over the wire. Two machines, two clocks.
    camera   a camera attached to this Mac. One machine, no clock problem, but an
             unmeasurable sensor latency (see Capture.upstream_ms).

Both hand back the same thing, so the detector, the rule and the action never learn
which one is running - swapping `--source` is the whole change.

The two look unrelated and are not. Both can produce frames faster than inference
consumes them, and both must therefore answer "give me the NEWEST frame, and tell me
how many you threw away" rather than "give me the next frame". A queue of stale frames
is the failure this whole package is built to avoid: nothing is lost, but everything
falls further behind, and the debug window lags more the longer motion continues.
"""

import time
from urllib.parse import parse_qs, urlparse

DEFAULT_KIND = "udp"


def _int(text):
    """int(text), or None. Convert and catch - never str.isdigit() as a proxy.

    isdigit() is True for characters int() refuses: "2".isdigit() and "\u00b2".isdigit()
    are both True, but int("\u00b2") raises ValueError. Every unicode digit-like
    character is such a hole, and so is a leading "--". Trusting isdigit() is what let
    five hostile values raise out of parse_source, whose whole contract is that it
    cannot.
    """
    try:
        return int(text.strip(), 10)
    except (ValueError, AttributeError):
        return None


def _float(text):
    """float(text), or None. Same reasoning as _int."""
    try:
        return float(text.strip())
    except (ValueError, AttributeError):
        return None


class Capture:
    """One frame, and everything the loop needs to reason about it.

    `frame` is a reference to the source's own array, never a copy - nothing on the
    path ahead of the trigger write may copy the pixels.

    frame        BGR ndarray, or None when this tick produced no usable image. None
                 means HOLD the current trigger state: no new information is not the
                 same as "no person". The far end's watchdog is the only thing
                 entitled to clear a state without new information.
    t0           perf_counter() at acquisition. The origin for decide_ms and mac_ms,
                 and it is taken inside the source because that is the only place that
                 knows when the frame actually arrived.
    seq          monotonic frame counter. From the wire for udp, local for camera.
    width/height the frame's own dimensions, after any crop.
    upstream_ms  time spent before this process could see the pixels, when it is
                 measurable at all. udp: the Windows capture->send span, exact.
                 camera: None - the delay between photons and AVFoundation cannot be
                 measured without a hardware reference, so it is not reported rather
                 than guessed.
    transit_ms   cross-machine delay, contaminated by clock offset; None for any
                 single-machine source, which removes the calibration entirely.
    dropped      frames this source discarded as stale on this tick.
    note         a diagnostic line for the caller to print AFTER the trigger byte has
                 gone out, or None. Sources never print it themselves: gaps happen
                 precisely when the pipeline is already behind.
    """

    __slots__ = ("frame", "t0", "seq", "width", "height", "upstream_ms", "transit_ms",
                 "dropped", "note")

    def __init__(self, frame, t0, seq, width, height, upstream_ms=None,
                 transit_ms=None, dropped=0, note=None):
        self.frame = frame
        self.t0 = t0
        self.seq = seq
        self.width = width
        self.height = height
        self.upstream_ms = upstream_ms
        self.transit_ms = transit_ms
        self.dropped = dropped
        self.note = note


class Source:
    """Interface.

    Lifecycle is deliberately three steps, not one, because the startup order matters:

        open()    acquire the device or the socket, and learn the frame geometry. On
                  macOS this is also where the camera permission prompt appears.
        flush()   discard everything that piled up while the model was warming. Called
                  immediately before the loop, so the first frame processed is fresh.
        recv()    block for the newest frame.

    Merging open() and flush() would put a multi-second MPS warmup between acquiring
    the device and the first read, and the first frame the loop ever saw would be
    seconds stale.
    """

    name = "none"
    description = "?"
    # Every source counts what it threw away for staleness. Both of them can outrun
    # inference, so this is the number that separates "my Mac is too slow" from "the
    # link is losing frames" - opposite causes, opposite fixes.
    stale_dropped = 0
    # The frame geometry, IF the source knows it after open(). A camera does - it is the
    # crop it was configured with. A socket does not: nothing about binding a port says
    # what the far end will send, so it leaves these 0 and the configured --roi-w/--roi-h
    # stands until the first frame arrives and the loop checks it. Anything that reads
    # these must therefore tolerate 0, which is what `source.width or args.roi_w` means.
    width = 0
    height = 0
    # The label this source's upstream time carries in the debug overlay. "win" is
    # defined by docs/PROTOCOL.md; a source that cannot measure an upstream span
    # leaves upstream_ms None and the label is never rendered.
    upstream_label = "up"

    def open(self):
        raise NotImplementedError

    def flush(self):
        """Discard accumulated frames. Must never block waiting for a new one."""

    def recv(self):
        raise NotImplementedError

    def close(self):
        pass

    def status(self):
        return {"kind": self.name, "description": self.description}


def parse_source(target):
    """--source / $MACVISION_SOURCE -> a plain dict. Pure, and it never raises.

    Same contract as trigger.parse_target, for the same reason: urlparse defers port
    validation to attribute access, so a malformed port must not become a traceback out
    of the one function whose job is to make sense of user input.

        udp://[host][:port]
        camera://<index or device path>?crop=x,y,w,h&size=WxH&fps=N

    -> {"kind", "host", "port", "device", "crop", "size", "fps", "raw", "reason"}
    """
    out = {"kind": "unknown", "host": None, "port": None, "device": None,
           "crop": None, "size": None, "fps": None, "raw": target, "reason": None}

    text = (target or "").strip()
    if not text:
        out["kind"] = DEFAULT_KIND
        return out

    bare = text.casefold()
    if bare in ("udp", "camera"):
        out["kind"] = bare
        return out

    if "://" not in text:
        out["reason"] = ("expected udp://host:port or camera://0 "
                         "(no scheme in this value)")
        return out

    scheme = bare.split("://", 1)[0]
    if scheme not in ("udp", "camera"):
        out["reason"] = f"unknown scheme {scheme!r} (expected udp or camera)"
        return out

    try:
        parsed = urlparse(text)
        port = parsed.port if scheme == "udp" else None
    except ValueError as exc:
        out["reason"] = str(exc)
        return out

    out["kind"] = scheme
    if scheme == "udp":
        out["host"] = parsed.hostname or None
        out["port"] = port
        return out

    # camera://<device>?crop=...&size=...&fps=...
    # A bare index lands in netloc ("0"); an absolute device path lands in path
    # ("/dev/video0"), and its leading slash is part of the path - stripping it would
    # turn an absolute path into a relative one that opens nothing.
    device = parsed.netloc or parsed.path
    if not device:
        out["kind"] = "unknown"
        out["reason"] = "camera:// names no device (try camera://0)"
        return out
    # A bare index becomes an int; anything else is a device path, left as typed.
    index = _int(device)
    out["device"] = index if index is not None and device.strip().isdigit() else device

    query = parse_qs(parsed.query)

    def one(key):
        values = query.get(key)
        return values[0] if values else None

    crop = one("crop")
    if crop:
        parts = [_int(p) for p in crop.split(",")]
        if len(parts) != 4 or any(p is None for p in parts):
            out["reason"] = f"crop={crop!r}: expected four integers x,y,w,h"
            out["kind"] = "unknown"
            return out
        if parts[2] <= 0 or parts[3] <= 0:
            out["reason"] = f"crop={crop!r}: width and height must be positive"
            out["kind"] = "unknown"
            return out
        out["crop"] = tuple(parts)

    size = one("size")
    if size:
        parts = [_int(p) for p in size.lower().split("x")]
        if len(parts) != 2 or any(p is None or p <= 0 for p in parts):
            out["reason"] = (f"size={size!r}: expected WxH of positive integers, "
                             f"e.g. 1280x720")
            out["kind"] = "unknown"
            return out
        out["size"] = tuple(parts)

    fps = one("fps")
    if fps:
        value = _float(fps)
        if value is None or value <= 0:
            out["reason"] = f"fps={fps!r}: expected a positive number"
            out["kind"] = "unknown"
            return out
        out["fps"] = value

    return out


def build_source(kind, **kwargs):
    """The factory. RAISES on bad input, unlike parse_source.

    Unlike the trigger, a source that cannot be opened IS fatal: a vision pipeline with
    no images is not a degraded mode, it is nothing at all. That asymmetry is the same
    one pi-agent/piproxy draws between build_sink (fatal) and a missing keyboard.
    """
    if kind == "udp":
        from .udp import UdpSource
        return UdpSource(host=kwargs.get("host") or "0.0.0.0",
                         port=kwargs.get("port"))
    if kind == "camera":
        from .camera import CameraSource
        return CameraSource(device=kwargs.get("device", 0),
                            crop=kwargs.get("crop"),
                            size=kwargs.get("size"),
                            fps=kwargs.get("fps"))
    raise ValueError(f"unknown source {kind!r} (expected: udp, camera)")
