"""A camera attached to this Mac.

Parameterised on purpose - which device, what resolution, what frame rate, and which
part of the picture to keep. That last one is the same job the Windows agent does with
kRoiX/kRoiY/kRoiWidth/kRoiHeight before it encodes: both sources hand the detector a
crop of a larger picture, and only the place the crop happens differs.

    camera://0
    camera://0?crop=100,100,300,300&size=1280x720&fps=60
    camera:///dev/video0?crop=0,0,640,480

Why a reader thread rather than calling read() from the loop. cv2.VideoCapture queues
frames: read() hands back the OLDEST one it holds, not the newest. If inference is
slower than the camera's frame rate the backlog grows without bound and the debug window
falls further behind the longer it runs - the identical failure the UDP drain exists to
prevent (commit 8ff3ae1). CAP_PROP_BUFFERSIZE=1 is the documented remedy and AVFoundation
on macOS does not honour it, so the only reliable fix is to keep reading on a thread and
keep just the latest frame. Everything that thread discards is counted, so "the camera
is faster than my Mac" is visible rather than felt.

What this source cannot tell you: how long the photons took to become an ndarray. A
webcam's sensor-to-AVFoundation delay is typically tens of milliseconds - larger than
anything else this pipeline measures - and there is no way to measure it from software.
It is reported as unknown (upstream_ms is None) rather than guessed, which is why the
overlay shows e2e> (a lower bound) rather than e2e~ on this source. Measuring it needs a
hardware reference: film a running millisecond timer, or flash an LED.
"""

import sys
import threading
import time

from . import Capture, Source

# How long recv() waits for a frame before giving up on this tick. A camera that is
# alive produces frames at its own rate, so silence this long means it is gone - not
# that nothing is moving. Long enough to never trip on a slow first frame, short enough
# that a dead camera does not hang the process.
READ_TIMEOUT_S = 2.0


def list_cameras(limit=8):
    """Indices that answer with a frame. Probing is the only way to ask.

    AVFoundation exposes no enumeration through cv2, so each index has to be opened and
    read. That is slow and it makes the camera's activity light blink, which is why this
    is a query-and-exit command rather than something the startup path does.
    """
    import cv2

    backend = getattr(cv2, "CAP_AVFOUNDATION", 0) if sys.platform == "darwin" \
        else getattr(cv2, "CAP_ANY", 0)
    found = []
    for index in range(limit):
        cap = cv2.VideoCapture(index, backend)
        try:
            ok, frame = cap.read()
            if ok and frame is not None:
                found.append({"index": index,
                              "size": [int(frame.shape[1]), int(frame.shape[0])]})
        except Exception:
            pass
        finally:
            cap.release()
    return found


class CameraSource(Source):
    name = "camera"
    upstream_label = "cam"

    def __init__(self, device=0, crop=None, size=None, fps=None, capture=None):
        # `capture` is a plug point with the same purpose as UdpSource's decoder:
        # anything exposing read()/release() drives this class, which is what lets the
        # threading, the cropping and the drop accounting be tested with no opencv.
        self._cap = capture
        self.device = device
        self.crop = crop
        self.size = size
        self.fps = fps
        self.description = f"camera {device}"
        self.width = self.height = 0
        self.frames_read = 0
        self.stale_dropped = 0
        self.read_errors = 0
        self.stalls = 0
        self._seq = 0
        self._pending = None
        self._dropped_since_recv = 0
        self._last_frame = 0.0
        self._lock = threading.Lock()
        self._new = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._reader, daemon=True,
                                        name="camera-reader")

    # --- lifecycle -------------------------------------------------------------------

    def open(self):
        """Acquire the device and learn the geometry.

        On macOS this is where the Camera permission prompt appears, and where a wrong
        index actually surfaces: cv2.VideoCapture(3) happily constructs an object for a
        device that does not exist, and only the first read() fails. So one frame is
        read here - a bad device must fail at startup, not silently produce nothing.
        """
        if self._cap is None:
            import cv2  # lazy: importing this module must not need opencv

            # CAP_AVFOUNDATION explicitly: the default backend on macOS can resolve to
            # something slower, and being explicit makes a backend problem legible.
            backend = getattr(cv2, "CAP_AVFOUNDATION", 0) if sys.platform == "darwin" \
                else getattr(cv2, "CAP_ANY", 0)
            self._cap = cv2.VideoCapture(self.device, backend)
            if self.size:
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.size[0])
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.size[1])
            if self.fps:
                self._cap.set(cv2.CAP_PROP_FPS, self.fps)
            # Asked for even though AVFoundation ignores it: on a backend that does
            # honour it, it shortens the queue the reader thread has to keep draining.
            self._cap.set(getattr(cv2, "CAP_PROP_BUFFERSIZE", 38), 1)

        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise OSError(
                f"camera {self.device!r} opened but produced no frame. Check the device "
                f"index (try --list-cameras), and that this terminal has Camera "
                f"permission in System Settings -> Privacy & Security -> Camera.")

        full_h, full_w = frame.shape[0], frame.shape[1]
        self._validate_crop(full_w, full_h)
        cropped = self._apply_crop(frame)
        self.height, self.width = cropped.shape[0], cropped.shape[1]
        self.description = (f"camera {self.device} {full_w}x{full_h}"
                            + (f" crop {self.width}x{self.height}"
                               f"@{self.crop[0]},{self.crop[1]}" if self.crop else ""))
        self._thread.start()

    def _validate_crop(self, full_w, full_h):
        if not self.crop:
            return
        x, y, w, h = self.crop
        if w <= 0 or h <= 0:
            raise ValueError(f"crop {self.crop}: width and height must be positive")
        if x < 0 or y < 0 or x + w > full_w or y + h > full_h:
            # Numpy would clamp silently, handing the detector a frame of the wrong
            # shape - which the warmup was not built for, and which moves the centre
            # pixel the rule tests. Refuse instead.
            raise ValueError(
                f"crop {self.crop} does not fit inside the camera's {full_w}x{full_h} "
                f"frame. Reduce the crop, or raise the resolution with size=WxH.")

    def _apply_crop(self, frame):
        if not self.crop:
            return frame
        x, y, w, h = self.crop
        # A numpy slice is a view, not a copy - nothing on the path ahead of the trigger
        # write may copy the pixels.
        return frame[y:y + h, x:x + w]

    def flush(self):
        """Drop whatever the reader thread accumulated during the model warmup."""
        with self._lock:
            discarded = self._dropped_since_recv + (1 if self._pending is not None else 0)
            self._pending = None
            self._dropped_since_recv = 0
        return discarded

    # --- the reader thread -----------------------------------------------------------

    def _reader(self):
        while not self._stop.is_set():
            try:
                ok, frame = self._cap.read()
            except Exception as exc:
                self.read_errors += 1
                if self.read_errors in (1, 10) or self.read_errors % 500 == 0:
                    print(f"[camera] read failed ({exc}); continuing "
                          f"[{self.read_errors} so far]", file=sys.stderr, flush=True)
                time.sleep(0.05)
                continue
            if not ok or frame is None:
                self.read_errors += 1
                if self.read_errors in (1, 10) or self.read_errors % 500 == 0:
                    print(f"[camera] read returned no frame; is the device still there? "
                          f"[{self.read_errors} so far]", file=sys.stderr, flush=True)
                time.sleep(0.05)
                continue

            # Stamped here, on the thread that actually took the frame. Anywhere later
            # would fold the handoff delay into the camera's own latency and hide it.
            t0 = time.perf_counter()
            cropped = self._apply_crop(frame)
            with self._new:
                if self._pending is not None:
                    # The loop never collected the previous one: it is stale now, and
                    # this counter is what makes "the camera is faster than inference"
                    # visible instead of merely felt.
                    self._dropped_since_recv += 1
                    self.stale_dropped += 1
                self._pending = (cropped, t0)
                self._seq += 1
                self.frames_read += 1
                self._last_frame = time.monotonic()
                self._new.notify()

    # --- the hot path ----------------------------------------------------------------

    def recv(self):
        with self._new:
            if self._pending is None:
                self._new.wait(READ_TIMEOUT_S)
            if self._pending is None:
                self.stalls += 1
                # A frame of None means HOLD the current state, exactly as a corrupt
                # datagram does on the udp source. It does NOT release the key: this is
                # the same stuck-key hole trigger.py documents - a Mac that is alive but
                # blind keeps feeding the far end's watchdog. Ctrl-C is the way out.
                return Capture(None, time.perf_counter(), self._seq,
                               self.width, self.height,
                               note=f"[camera] no frame for {READ_TIMEOUT_S:g}s; "
                                    f"the trigger state is being held "
                                    f"[{self.stalls} stalls]")
            frame, t0 = self._pending
            self._pending = None
            dropped = self._dropped_since_recv
            self._dropped_since_recv = 0
            seq = self._seq

        return Capture(frame, t0, seq, self.width, self.height,
                       # Both unknown, and stated as unknown rather than guessed:
                       # sensor latency is unmeasurable from software, and there is no
                       # second clock to be wrong about.
                       upstream_ms=None, transit_ms=None,
                       dropped=dropped)

    @property
    def idle_s(self):
        if self._last_frame == 0.0:
            return 0.0
        return time.monotonic() - self._last_frame

    def status(self):
        return {"kind": self.name, "description": self.description,
                "device": self.device, "crop": list(self.crop) if self.crop else None,
                "size": [self.width, self.height], "fps_requested": self.fps,
                "frames_read": self.frames_read, "stale_dropped": self.stale_dropped,
                "read_errors": self.read_errors, "stalls": self.stalls,
                "idle_s": round(self.idle_s, 3)}

    def close(self):
        self._stop.set()
        if self._thread.is_alive():
            # The thread is blocked inside read(), which returns at the camera's own
            # frame period - so it notices _stop within about one frame. The timeout
            # only matters for a camera that has hung, and then we release anyway.
            # Joining BEFORE releasing is deliberate: releasing the device out from
            # under a read() in progress is undefined behaviour in OpenCV.
            self._thread.join(timeout=0.5)
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
