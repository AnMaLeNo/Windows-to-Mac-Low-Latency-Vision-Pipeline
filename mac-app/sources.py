"""Where frames come from.

Two sources feed the same pipeline, one at a time, chosen with FRAME_SOURCE:

    FRAME_SOURCE=udp            the Windows agent over the Ethernet link (default)
    FRAME_SOURCE=camera://0     a camera on this Mac - index from tools/list_cameras.py

Everything downstream stays source-agnostic: detector.py and trigger.py never learn
which one is running, and the trigger rule is unchanged - the centre pixel of a
ROI_W x ROI_H frame, whether that frame was cropped on the Windows GPU or out of a
camera's sensor output.

What the two sources do *not* share is how silence should be read, and that is the
reason this is a module rather than an if-statement in receiver.py:

  * DXGI Desktop Duplication only emits a frame when the screen actually changes, so a
    silent UDP source means "nothing moved". The last decision still stands and the key
    must stay held - which is exactly what trigger.py's keepalive does on its own.
    UdpFrameSource.read() therefore blocks indefinitely, and that is correct.

  * A camera always produces frames. Silence there never means "the scene is static" -
    it means the iPhone locked, Continuity Camera dropped, or the cable went. Letting
    the keepalive hold the last decision would leave a key pressed until the process
    dies, so CameraFrameSource.read() gives up after STALE_AFTER_S and returns None,
    which receiver.py turns into a release.

Both sources hand back the newest frame available and discard anything older. For UDP
that is the socket drain; for the camera it is a grabber thread with a single slot.
Same reasoning either way: a latency-critical detector wants the most recent frame, not
every frame.
"""

import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple

import cv2
import numpy as np

from protocol import HEADER_SIZE, UDP_PORT, unpack_header

STATS_WINDOW = 200  # frames kept for rolling stats, in both sources and the receiver

# What to ask the camera for. None means "whatever mode it opens in", which on the
# iPhone over Continuity Camera is 1920x1080.
#
# Because the crop is native pixels with no resize, this constant is the *only* thing
# that decides how much of the scene those pixels cover. Measured on this hardware, a
# 300x300 crop is 28% of frame height at 1080p, 42% at 720p, 62% at 480p. Lower the
# capture resolution to see more of the world at the same 1:1 sharpness.
#
# AVFoundation snaps to the nearest format it supports rather than refusing, so this is
# a request, not a guarantee - the resolution actually granted is printed at startup.
CAPTURE_SIZE: Optional[Tuple[int, int]] = None

# A camera that has gone this long without delivering is dead, not idle. Comfortably
# longer than a frame interval at any rate worth running (the iPhone measures ~24fps,
# i.e. 42ms), so ordinary jitter never trips it - and well under the 250ms watchdog at
# the far end, so this Mac decides to release the key before the Pi has to decide for it.
STALE_AFTER_S = 0.150

# Continuity Camera's first frames are routinely black or half-decoded while the link
# comes up. Reading past them at startup means a healthy camera is never misreported.
WARMUP_FRAMES = 10
BLACK_LEVEL = 8  # a frame whose brightest pixel is under this is black, not dark

# Consecutive failed reads before the grabber gives up on the handle and reopens it.
# A handful of failures is normal when Continuity Camera renegotiates; a steady stream
# of them means the device is gone and the handle will never recover on its own.
REOPEN_AFTER_FAILURES = 30
REOPEN_BACKOFF_S = 0.5


@dataclass
class Frame:
    """One frame, plus everything needed to account for its latency honestly.

    `upstream_ms` is delay the source can actually prove, never an estimate: for UDP it
    is the Windows-side capture cost plus measured queueing, and for the camera it is
    how long the frame sat in the grabber's slot. Neither includes what happens before
    the source can see a frame at all - the one-way network hop, or the sensor-to-USB
    path on the iPhone - because neither is observable from this machine.
    """

    image: np.ndarray
    recv_t0: float      # perf_counter when this loop took delivery; receiver measures its own work from here
    upstream_ms: float  # provable delay before that moment
    overlay: str        # source-specific breakdown for the debug window
    label: str          # frame identity, for matching the window against the logs


class UdpFrameSource:
    """The Windows agent's stream. Unchanged behaviour, lifted out of receiver.py."""

    name = "udp"

    def __init__(self, roi_w: int, roi_h: int):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", UDP_PORT))
        self.last_seq = None
        self.stale_dropped_total = 0

        # transit = this machine's clock minus the Windows capture timestamp. It contains
        # the real network delay AND the offset between the two machines' clocks, which is
        # not small: stock Windows w32time targets ~1s accuracy, and this pair was measured
        # 238ms apart. Taking transit at face value would report latency that is almost
        # entirely clock skew.
        #
        # So we self-calibrate instead of trusting the clocks. Queueing delay varies frame
        # to frame and occasionally clears; a clock offset is constant. Therefore
        # min(transit) over a few hundred frames ~= clock_offset + best-case network hop,
        # and subtracting it leaves the *excess* delay above best case - exact, and immune
        # to any offset. A rolling window keeps this correct even as w32time slowly slews
        # the Windows clock during a run.
        self.transit_samples = deque(maxlen=STATS_WINDOW)

        print(f"[source] udp: listening on {UDP_PORT}, expecting {roi_w}x{roi_h} frames")

    def read(self) -> Optional[Frame]:
        # Loops only to skip undecodable payloads: every path out either returns a frame
        # or blocks. It never returns None, because silence here is meaningful - see the
        # module docstring.
        while True:
            # Block until at least one datagram is available, then drain any backlog that
            # piled up in the kernel socket buffer while we were busy decoding/inferring the
            # previous frame - keep only the newest one. Without this, a plain recvfrom() loop
            # processes every frame in arrival order: if inference is slower than the arrival
            # rate, nothing is ever lost, but everything falls further and further behind, so
            # the debug window visibly lags more the longer motion continues.
            data, _addr = self.sock.recvfrom(65535)
            dropped = 0
            self.sock.setblocking(False)
            while True:
                try:
                    data, _addr = self.sock.recvfrom(65535)
                    dropped += 1
                except BlockingIOError:
                    break
            self.sock.setblocking(True)

            recv_t0 = time.perf_counter()
            recv_wallclock_us = time.time() * 1_000_000

            seq, capture_wallclock_us, capture_to_send_us, _w, _h, jpeg_size = unpack_header(data)
            if self.last_seq is not None and seq != self.last_seq + 1:
                missing = seq - self.last_seq - 1
                lost_in_transit = missing - dropped
                print(f"[gap] {missing} missing (seq {self.last_seq + 1}..{seq - 1}): "
                      f"{dropped} dropped here for staleness, {lost_in_transit} lost in transit")
            self.last_seq = seq
            self.stale_dropped_total += dropped

            jpeg_bytes = data[HEADER_SIZE:HEADER_SIZE + jpeg_size]
            image = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                print(f"[warn] seq={seq}: failed to decode {jpeg_size}-byte JPEG payload, skipping")
                continue

            transit_ms = (recv_wallclock_us - capture_wallclock_us) / 1000
            self.transit_samples.append(transit_ms)
            win_ms = capture_to_send_us / 1000
            # Excess network delay above the best case seen recently - this is where lag
            # buildup shows up, and it carries no clock-offset error.
            queue_ms = transit_ms - min(self.transit_samples)

            return Frame(
                image=image,
                recv_t0=recv_t0,
                upstream_ms=win_ms + queue_ms,
                overlay=f"win={win_ms:.1f} net+={queue_ms:.1f}",
                label=f"seq={seq}",
            )

    def stats(self) -> str:
        return (f"n={len(self.transit_samples)}  "
                f"clock offset+hop={min(self.transit_samples):.1f}ms (calibrated out)  "
                f"stale dropped={self.stale_dropped_total}")

    def close(self) -> None:
        self.sock.close()


class CameraFrameSource:
    """A camera on this Mac - the iPhone over Continuity Camera, or the built-in one.

    Continuity Camera is the same AVFoundation device whether the iPhone is on USB or
    wireless, so nothing here distinguishes the two. Wireless costs latency, which shows
    up in the measured frame rate and in `wait=` on the overlay; it changes no code path.

    The grabber thread exists for the same reason the UDP source drains its socket:
    AVFoundation queues frames internally and cap.read() returns the oldest one, so a
    single-threaded loop slower than the camera falls permanently behind. (There is no
    shortcut via CAP_PROP_BUFFERSIZE - the AVFoundation backend ignores it.)
    """

    name = "camera"

    def __init__(self, index: int, roi_w: int, roi_h: int):
        self.index = index
        self.crop_w, self.crop_h = roi_w, roi_h
        self.grabbed = 0
        self.delivered = 0
        self.reopens = 0

        self._cond = threading.Condition()
        self._pending: Optional[Tuple[np.ndarray, float]] = None
        self._running = True
        self._warned_small = False

        cap = self._open()
        if cap is None:
            raise RuntimeError(f"camera index {index} would not open")

        image = None
        for _ in range(WARMUP_FRAMES):
            ok, f = cap.read()
            if ok and f is not None:
                image = f
        if image is None:
            cap.release()
            raise RuntimeError(f"camera index {index} opened but produced no frame")

        h, w = image.shape[:2]
        if w < self.crop_w or h < self.crop_h:
            cap.release()
            raise RuntimeError(f"camera index {index} gives {w}x{h}, smaller than the "
                               f"{self.crop_w}x{self.crop_h} crop")
        if int(image.max()) < BLACK_LEVEL:
            # Not fatal - a genuinely dark room looks the same - but this is nearly always
            # the macOS camera permission, which is granted to the app that owns this
            # process rather than to python, so it is worth naming before YOLO runs on
            # black frames for an hour.
            print(f"[source] camera {index}: WARNING - frames are all black. Usually the "
                  f"macOS camera permission (System Settings > Privacy & Security > Camera, "
                  f"granted to your terminal or editor, not to python).")

        asked = "" if CAPTURE_SIZE is None else f" (requested {CAPTURE_SIZE[0]}x{CAPTURE_SIZE[1]})"
        print(f"[source] camera {index}: {w}x{h}{asked}, "
              f"{self.crop_w}x{self.crop_h} native centre crop = {100 * self.crop_h / h:.0f}% "
              f"of frame height")

        threading.Thread(target=self._grab_loop, args=(cap,), daemon=True).start()

    def _open(self):
        cap = cv2.VideoCapture(self.index, cv2.CAP_AVFOUNDATION)
        if not cap.isOpened():
            return None
        if CAPTURE_SIZE is not None:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_SIZE[0])
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_SIZE[1])
        return cap

    def _center_crop(self, image: np.ndarray) -> Optional[np.ndarray]:
        h, w = image.shape[:2]
        x0, y0 = (w - self.crop_w) // 2, (h - self.crop_h) // 2
        if x0 < 0 or y0 < 0:
            if not self._warned_small:
                print(f"[source] camera {self.index}: frames are now {w}x{h}, smaller than "
                      f"the {self.crop_w}x{self.crop_h} crop - dropping them")
                self._warned_small = True
            return None
        # Native pixels, no resize: a 1:1 window on the sensor's output. Copied rather
        # than kept as a slice, because a view would pin the whole full-resolution frame
        # in memory for as long as the crop is alive.
        return image[y0:y0 + self.crop_h, x0:x0 + self.crop_w].copy()

    def _grab_loop(self, cap) -> None:
        failures = 0
        while self._running:
            ok, image = cap.read()
            if not ok or image is None:
                failures += 1
                if failures >= REOPEN_AFTER_FAILURES:
                    # The device is gone, not stumbling. Continuity Camera does this
                    # routinely - unlocking the iPhone or picking it up ends the session -
                    # so recovering has to be automatic, the way the serial bridge
                    # reconnects rather than ending the run.
                    print(f"[source] camera {self.index}: read failing, reopening")
                    cap.release()
                    cap, failures = None, 0
                    while self._running and cap is None:
                        time.sleep(REOPEN_BACKOFF_S)
                        cap = self._open()
                    if cap is not None:
                        self.reopens += 1
                        print(f"[source] camera {self.index}: reopened")
                continue
            failures = 0

            crop = self._center_crop(image)
            if crop is None:
                continue
            with self._cond:
                # Single slot, newest wins. Overwriting an undelivered frame is the point:
                # if inference is slower than the camera, the frames in between are stale
                # by the time we could use them.
                self._pending = (crop, time.perf_counter())
                self.grabbed += 1
                self._cond.notify()

        if cap is not None:
            cap.release()

    def read(self) -> Optional[Frame]:
        deadline = time.perf_counter() + STALE_AFTER_S
        with self._cond:
            while self._pending is None:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return None  # the camera has stopped: receiver.py releases the key
                self._cond.wait(remaining)
            image, t_grabbed = self._pending
            self._pending = None
            self.delivered += 1
            delivered = self.delivered

        now = time.perf_counter()
        # How long this frame sat waiting for the loop to come back for it. The camera's
        # own capture latency sits upstream of cap.read() and is not measurable here, so
        # it is excluded rather than guessed at.
        wait_ms = (now - t_grabbed) * 1000
        return Frame(
            image=image,
            recv_t0=now,
            upstream_ms=wait_ms,
            overlay=f"wait={wait_ms:.1f}",
            label=f"frame={delivered}",
        )

    def stats(self) -> str:
        skipped = self.grabbed - self.delivered
        return (f"grabbed={self.grabbed} used={self.delivered} "
                f"skipped={skipped} (newer frame won)  reopens={self.reopens}")

    def close(self) -> None:
        self._running = False


def open_source(roi_w: int, roi_h: int, target: Optional[str] = None):
    """Open the source described by FRAME_SOURCE. Fatal on failure, by design.

    Unlike a missing trigger link - which leaves a working vision pipeline attached to
    nothing, and so degrades to a warning - a source that will not open leaves nothing
    to run at all. There is no useful degraded mode to fall back to.
    """
    target = target or os.environ.get("FRAME_SOURCE", "udp")

    if target in ("udp", "udp://"):
        return UdpFrameSource(roi_w, roi_h)

    if target == "camera" or target.startswith("camera://"):
        raw = target[len("camera://"):] if "://" in target else ""
        if raw and not raw.isdigit():
            raise SystemExit(f"[source] malformed FRAME_SOURCE={target!r}, expected "
                             f"camera://<index> with a number - run "
                             f"tools/list_cameras.py to find it")
        index = int(raw) if raw else 0
        try:
            return CameraFrameSource(index, roi_w, roi_h)
        except RuntimeError as exc:
            # Loud, and specific about the two things that actually go wrong: the index
            # moved, or Continuity Camera is not being offered. Both look identical from
            # here - an index that will not open - and neither is guessable from the error.
            print("\n" + "=" * 72)
            print(f"  CAMERA {index} UNUSABLE - {exc}")
            print()
            print("  Camera indices are not stable. Plugging in the iPhone inserts it at")
            print("  index 0 and pushes the built-in camera to 1, and macOS lists them in")
            print("  a different order again, so the index cannot be derived - only found:")
            print()
            print("      python3 tools/list_cameras.py 6")
            print()
            print("  If that lists no iPhone at all, macOS is not offering Continuity")
            print("  Camera: it needs the phone locked, stationary, rear camera facing the")
            print("  scene, on the same Apple ID. Unlocking it ends the session.")
            print("=" * 72 + "\n")
            raise SystemExit(1)

    raise SystemExit(f"[source] unrecognised FRAME_SOURCE={target!r} "
                     f"(expected udp or camera://<index>)")
