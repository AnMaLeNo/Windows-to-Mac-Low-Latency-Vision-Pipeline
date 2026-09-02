"""The newest raw frame becomes one image, at most --fps times a second, only if watched.

The subscriber thread hands every frame to offer(), which does one tuple assignment
and a counter increment - the same "latest slot" idea the publisher uses on the other
side of the socket, for the same reason: the producer must never wait for the
consumer. A ticker thread wakes 1/fps later, and if a browser is connected and a new
frame is pending it encodes that one frame and publishes it with its header, so the
boxes and the pixels travel in the same event and cannot drift apart. Frames offered
between ticks are superseded, and counted as skipped rather than silently vanishing.

Two encoders, chosen once. With opencv importable the frame becomes a JPEG at quality
80 (~15KB for a 300x300 ROI, a few hundred microseconds). Without it - this package
must run on the stdlib alone - the frame becomes a PNG written here by hand: zlib at
level 1, filter 0 on every row. It is larger and slower than the JPEG, and it is still
well under a millisecond per frame at 15fps on anything that runs a browser. Neither
opencv nor numpy is imported at module level; both are optional accelerators reached
from inside functions, and a missing one is probed once and remembered.
"""

import base64
import struct
import sys
import threading
import zlib

JPEG_QUALITY = 80
PNG_LEVEL = 1
DEFAULT_FPS = 15

_UNPROBED = object()
_cv2_cache = _UNPROBED


def _load_cv2():
    """(cv2, numpy) or None. Probed once: a failed import is not cached by Python, and
    walking sys.path for a module that is not there, 15 times a second, is silly."""
    global _cv2_cache
    if _cv2_cache is _UNPROBED:
        try:
            import cv2
            import numpy
            _cv2_cache = (cv2, numpy)
        except Exception:
            _cv2_cache = None
    return _cv2_cache


def _load_numpy():
    try:
        import numpy
        return numpy
    except Exception:
        return None


# --- pure encoders ----------------------------------------------------------------------
def encode_image(payload, w, h, c, fmt="bgr8", backend=None):
    """Raw pixels -> (mime, bytes). Pure.

    backend None tries opencv (JPEG) and falls back to PNG; "png" is the stdlib path;
    "jpeg" insists on opencv and raises RuntimeError without it. fmt names the channel
    order as contract 1 does: "bgr8" is swapped to RGB for the PNG and handed to
    opencv as is; anything else is taken to already be RGB (or gray when c == 1).
    """
    w, h, c = int(w), int(h), int(c)
    if w <= 0 or h <= 0:
        raise ValueError(f"bad geometry {w}x{h}")
    if c not in (1, 3, 4):
        raise ValueError(f"unsupported channel count {c} (expected 1, 3 or 4)")
    if len(payload) != w * h * c:
        raise ValueError(f"payload is {len(payload)} bytes, expected {w}x{h}x{c}="
                         f"{w * h * c}")
    fmt = (fmt or "bgr8").lower()

    if backend in (None, "jpeg"):
        mods = _load_cv2()
        if mods is not None:
            try:
                return "image/jpeg", _encode_jpeg(mods, payload, w, h, c, fmt)
            except Exception:
                if backend == "jpeg":
                    raise
        elif backend == "jpeg":
            raise RuntimeError("backend 'jpeg' needs opencv, which is not importable")
    elif backend != "png":
        raise ValueError(f"unknown backend {backend!r} (expected None, 'jpeg' or 'png')")

    pixels = payload
    if c >= 3 and fmt.startswith("bgr"):
        pixels = _bgr_to_rgb(payload, c)
    return "image/png", encode_png(pixels, w, h, c)


def _encode_jpeg(mods, payload, w, h, c, fmt):
    cv2, np = mods
    arr = np.frombuffer(payload, dtype=np.uint8)
    if c == 1:
        arr = arr.reshape(h, w)
    else:
        arr = arr.reshape(h, w, c)
        if not fmt.startswith("bgr"):
            # opencv wants BGR; an RGB(A) frame gets its colour channels reversed.
            arr = np.concatenate([arr[:, :, 2::-1], arr[:, :, 3:]], axis=2)
        if c == 4:
            arr = arr[:, :, :3]          # JPEG has no alpha
        arr = np.ascontiguousarray(arr)
    ok, buf = cv2.imencode(".jpg", arr, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
    if not ok:
        raise RuntimeError("cv2.imencode refused the frame")
    return buf.tobytes()


def _bgr_to_rgb(payload, c):
    """Swap the first and third channel of every pixel. numpy if it is there; otherwise
    three extended-slice assignments on a bytearray, which are C loops too."""
    np = _load_numpy()
    if np is not None:
        arr = np.frombuffer(payload, dtype=np.uint8).reshape(-1, c)
        return np.concatenate([arr[:, 2::-1], arr[:, 3:]], axis=1).tobytes()
    out = bytearray(len(payload))
    out[0::c] = payload[2::c]
    out[1::c] = payload[1::c]
    out[2::c] = payload[0::c]
    if c == 4:
        out[3::c] = payload[3::c]
    return bytes(out)


def _png_chunk(kind, data):
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def encode_png(pixels, w, h, channels):
    """A minimal, valid PNG: 8-bit, colour type 0 (gray), 2 (RGB) or 6 (RGBA), one
    IDAT, filter type 0 on every row. `pixels` is row-major, already in RGB order."""
    colour = {1: 0, 3: 2, 4: 6}[channels]
    stride = w * channels
    view = memoryview(pixels)
    # One filter byte per row, then the row. A join of h slices: no per-byte Python.
    raw = b"".join(b"\x00" + view[y * stride:(y + 1) * stride] for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, colour, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", ihdr)
            + _png_chunk(b"IDAT", zlib.compress(raw, PNG_LEVEL))
            + _png_chunk(b"IEND", b""))


def data_url(mime, data):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


# --- the rate limiter -------------------------------------------------------------------
class FrameEncoder:
    def __init__(self, bus, fps=DEFAULT_FPS, backend=None):
        if fps <= 0:
            raise ValueError("fps must be > 0")
        self.bus = bus
        self.fps = fps
        self.interval = 1.0 / fps
        if backend is None:
            backend = "jpeg" if _load_cv2() is not None else "png"
        self.backend = backend

        self._pending = None          # (header, payload), replaced whole
        self.offered = 0
        self._taken = 0               # offered count at the last tick that looked
        self.encoded = 0
        self.skipped = 0
        self.errors = 0

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="frame-encoder")

    def offer(self, header, payload):
        """The subscriber thread's whole cost: one assignment, one increment."""
        self._pending = (header, payload)
        self.offered += 1

    def start(self):
        if not self._thread.is_alive() and not self._stop.is_set():
            self._thread.start()

    def stop(self, timeout=2.0):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def alive(self):
        return self._thread.is_alive()

    def status(self):
        return {
            "backend": self.backend,
            "fps": self.fps,
            "offered": self.offered,
            "encoded": self.encoded,
            "skipped": self.skipped,
            "errors": self.errors,
            "alive": self.alive,
        }

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.tick()

    def tick(self):
        """One tick of the rate limiter. Public so a test can drive it without waiting."""
        # Nobody watching: leave the slot alone rather than consuming it, so the frame
        # goes out on the first tick after a browser connects. With a stalled source
        # (udp waiting for the Windows agent) that held frame is the only picture a
        # reloaded page will ever get.
        if not self.bus.wants_frames():
            return
        # Count first, then take the slot. If a frame lands between the two reads it is
        # the one encoded now and counted again next tick - a duplicate at worst,
        # never a lost newest.
        n = self.offered
        fresh = n - self._taken
        if fresh <= 0:
            return
        self._taken = n
        pending = self._pending
        if pending is None:
            self.skipped += fresh
            return
        self.skipped += fresh - 1
        header, payload = pending
        try:
            mime, data = encode_image(payload, header["w"], header["h"], header["c"],
                                      header.get("fmt", "bgr8"), backend=self.backend)
        except Exception as exc:
            self.errors += 1
            if self.errors in (1, 10) or self.errors % 500 == 0:
                print(f"[frames] could not encode frame ({exc}); continuing "
                      f"[{self.errors} so far]", file=sys.stderr, flush=True)
            return
        self.bus.publish("frame", dict(header, image=data_url(mime, data)))
        self.encoded += 1
