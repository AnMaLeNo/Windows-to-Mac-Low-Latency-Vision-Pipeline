"""Checks both frame sources against the one property they share.

Either source can outrun inference, and both must then answer "give me the NEWEST frame,
and tell me how many you threw away". Everything else about them differs - one parses a
wire header and decodes JPEG, the other drives a device on a thread - but that contract
is what lets the detector, the rule and the action never learn which is running.

Three bugs this defends against. A socket left non-blocking turns the next "blocking"
recvfrom into a busy spin or a crash. A drop count lost at a module boundary makes "my
Mac is too slow" indistinguishable from "the link is losing frames". And a camera read
straight from the loop hands back the OLDEST queued frame, so the debug window falls
further behind the longer it runs.

    python3 -m tests.test_sources      (from mac-app/)

Both sources take a plug point - a decoder for udp, a capture object for camera - so
this runs with no opencv at all.
"""

import socket
import struct
import sys
import time

from macvision.protocol import HEADER_FORMAT
from macvision.sources import Capture, Source, build_source, parse_source
from macvision.sources.camera import CameraSource
from macvision.sources.udp import SequenceTracker, UdpSource

AT_IMPORT = set(sys.modules)


class FakeDecoder:
    """Anything with .decode(bytes) drives UdpSource."""

    def __init__(self):
        self.failures = 0

    def decode(self, buf):
        if not buf.startswith(b"\xff\xd8"):
            self.failures += 1
            return None
        return f"frame<{len(buf)}>"


class FakeCam:
    """Anything with .read()/.release() drives CameraSource."""

    def __init__(self, w=640, h=480, fps=400.0, ok_frames=None):
        self.w, self.h, self.period = w, h, 1.0 / fps
        self.n = 0
        self.ok_frames = ok_frames
        self.released = False

    def read(self):
        time.sleep(self.period)
        self.n += 1
        if self.ok_frames is not None and self.n > self.ok_frames:
            return False, None
        return True, _Frame(self.h, self.w)

    def set(self, *a):
        return True

    def release(self):
        self.released = True


class _Frame:
    """A stand-in for an ndarray: only .shape and slicing are ever used."""

    def __init__(self, h, w, y=0, x=0):
        self.shape = (h, w, 3)
        self._origin = (y, x)

    def __getitem__(self, key):
        ys, xs = key
        return _Frame(ys.stop - ys.start, xs.stop - xs.start, ys.start, xs.start)


def datagram(seq, body=b"\xff\xd8jpeg", w=300, h=300, win_us=1500, age_us=5000):
    return struct.pack(HEADER_FORMAT, seq, int(time.time() * 1e6) - age_us, win_us,
                       w, h, len(body)) + body


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    # --- parse_source: never raises, and reason <-> unknown is one rule --------------
    cases = [
        ("", "udp"), ("udp", "udp"), ("udp://", "udp"), ("udp://0.0.0.0:50505", "udp"),
        ("udp://h:99999", "unknown"), ("udp://h:abc", "unknown"),
        ("camera://0", "camera"), ("camera://0?crop=1,2,3,4&size=640x480&fps=30", "camera"),
        ("camera:///dev/video0", "camera"), ("camera://", "unknown"),
        ("camera://0?crop=1,2,3", "unknown"), ("camera://0?size=abc", "unknown"),
        ("camera://0?fps=x", "unknown"), ("ftp://x", "unknown"), ("nonsense", "unknown"),
    ]
    for raw, kind in cases:
        try:
            spec = parse_source(raw)
        except Exception as exc:
            failures.append(f"parse_source({raw!r}) raised {exc!r}")
            continue
        check(f"parse_source({raw!r}).kind", spec["kind"], kind)
        if (spec["reason"] is not None) != (spec["kind"] == "unknown"):
            failures.append(f"parse_source({raw!r}): reason and kind disagree "
                            f"({spec['kind']}, {spec['reason']!r})")

    spec = parse_source("camera://0?crop=100,100,300,300&size=1280x720&fps=60")
    check("camera device", spec["device"], 0)
    check("camera crop", spec["crop"], (100, 100, 300, 300))
    check("camera size", spec["size"], (1280, 720))
    check("camera fps", spec["fps"], 60.0)
    # An absolute device path must keep its leading slash, or it opens nothing.
    check("device path keeps its slash",
          parse_source("camera:///dev/video0")["device"], "/dev/video0")
    check("udp host", parse_source("udp://1.2.3.4:9")["host"], "1.2.3.4")
    check("udp port", parse_source("udp://1.2.3.4:9")["port"], 9)

    try:
        build_source("nope")
    except ValueError:
        pass
    except Exception as exc:
        failures.append(f"build_source('nope') raised {exc!r}, expected ValueError")
    else:
        failures.append("build_source('nope') did not raise")

    # --- SequenceTracker: integers in, one diagnostic line out ----------------------
    t = SequenceTracker()
    check("the first frame reports no phantom gap", t.observe(4, 0), None)
    check("consecutive frames are silent", t.observe(5, 0), None)
    check("no gaps counted", t.gaps, 0)

    t = SequenceTracker()
    t.observe(4, 0)
    check("a gap of 3 with 1 dropped locally", t.observe(8, 1),
          "[gap] 3 missing (seq 5..7): 1 dropped here for staleness, 2 lost in transit")
    check("gaps counted", t.gaps, 1)
    check("lost_in_transit counted", t.lost_in_transit, 2)
    check("last_seq advanced past the gap", t.last_seq, 8)

    t = SequenceTracker()
    t.observe(10, 0)
    t.observe(11, 0)        # caller "fails to decode" and continues
    check("last_seq updates even when the caller drops the frame", t.last_seq, 11)
    check("so the next frame is silent", t.observe(12, 0), None)

    t = SequenceTracker()
    t.observe(5000, 0)
    line = t.observe(0, 0)
    check("a sender restart is reported as one", line,
          "[seq] sender restarted (seq went 5000 -> 0)")
    check("restarts counted", t.restarts, 1)
    if "missing -" in (line or ""):
        failures.append("a sender restart still reports a negative missing count")
    check("no phantom losses", t.lost_in_transit, 0)

    # --- UdpSource over a real loopback socket --------------------------------------
    dec = FakeDecoder()
    s = UdpSource(host="127.0.0.1", port=0, decoder=dec)
    s.open()
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if not s.port:
            failures.append("UdpSource did not read back an ephemeral port")
        check("a socket does not know its geometry until a frame arrives",
              (s.width, s.height), (0, 0))

        for i in range(4):
            tx.sendto(datagram(i), ("127.0.0.1", s.port))
        time.sleep(0.05)
        check("flush discards the startup backlog", s.flush(), 4)
        check("and leaves the socket blocking", s._sock.gettimeout(), None)

        for seq in (10, 11, 12):
            tx.sendto(datagram(seq), ("127.0.0.1", s.port))
        time.sleep(0.05)
        cap = s.recv()
        check("newest wins", cap.seq, 12)
        check("and reports how many it discarded", cap.dropped, 2)
        check("stale_dropped accumulated inside recv", s.stale_dropped, 2)
        check("the frame came from the injected decoder", cap.frame, "frame<6>")
        check("geometry from the wire", (cap.width, cap.height), (300, 300))
        check("upstream is measurable on this source", cap.upstream_ms, 1.5)
        if cap.transit_ms is None:
            failures.append("udp must report transit_ms; it is a two-machine source")
        if s._sock.gettimeout() is not None:
            failures.append("the socket was left non-blocking after a drain")

        tx.sendto(datagram(20), ("127.0.0.1", s.port))
        time.sleep(0.05)
        cap = s.recv()
        check("a gap is reported through the note", cap.note,
              "[gap] 7 missing (seq 13..19): 0 dropped here for staleness, "
              "7 lost in transit")

        tx.sendto(b"runt", ("127.0.0.1", s.port))
        time.sleep(0.05)
        cap = s.recv()
        check("a runt yields no frame", cap.frame, None)
        check("and is counted", s.malformed, 1)

        tx.sendto(datagram(21, body=b"not-a-jpeg"), ("127.0.0.1", s.port))
        time.sleep(0.05)
        cap = s.recv()
        check("a bad payload yields no frame", cap.frame, None)
        check("and is counted", s.decode_failures, 1)
        if "10 claimed, 10 present" not in (cap.note or ""):
            failures.append(f"the decode warning lost claimed-vs-present: {cap.note!r}")

        # An error inside the drain must still restore blocking. A real socket refuses
        # attribute assignment, so the whole object is wrapped.
        class ExplodingDrain:
            def __init__(self, sock):
                self._s, self.calls = sock, 0

            def recvfrom(self, *a, **kw):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("boom inside the drain")
                return self._s.recvfrom(*a, **kw)

            def __getattr__(self, name):
                return getattr(self._s, name)

        real_sock = s._sock
        s._sock = ExplodingDrain(real_sock)
        tx.sendto(datagram(30), ("127.0.0.1", s.port))
        try:
            s.recv()
        except RuntimeError:
            pass
        else:
            failures.append("the injected drain error did not propagate")
        s._sock = real_sock
        if s._sock.gettimeout() is not None:
            failures.append("an error inside the drain left the socket non-blocking")

        st = s.status()
        for key in ("kind", "udp_port", "packets", "stale_dropped", "malformed",
                    "decode_failures", "gaps", "lost_in_transit", "restarts", "idle_s"):
            if key not in st:
                failures.append(f"UdpSource.status() is missing {key!r}")
    finally:
        tx.close()
        s.close()

    # --- CameraSource ----------------------------------------------------------------
    cam = FakeCam()
    c = CameraSource(device=0, crop=(100, 100, 300, 300), capture=cam)
    c.open()
    try:
        check("a camera knows its geometry at open()", (c.width, c.height), (300, 300))
        cap = c.recv()
        check("the frame is cropped", cap.frame.shape, (300, 300, 3))
        check("a camera cannot measure its upstream delay", cap.upstream_ms, None)
        check("and has no second clock to calibrate", cap.transit_ms, None)

        # The reader thread must keep only the newest while the loop is busy.
        before = c.stale_dropped
        time.sleep(0.2)
        cap = c.recv()
        if cap.dropped < 5:
            failures.append(f"the camera reader kept a backlog: dropped={cap.dropped} "
                            f"after a 200ms stall")
        if c.stale_dropped <= before:
            failures.append("stale_dropped did not accumulate")

        time.sleep(0.1)
        if c.flush() < 1:
            failures.append("flush() discarded nothing after a stall")
        check("flush leaves nothing pending", c.flush(), 0)
    finally:
        c.close()
    check("close() released the device", cam.released, True)

    # A crop that does not fit must be refused, not silently clamped: numpy would hand
    # the detector a frame of the wrong shape and move the pixel the rule tests.
    try:
        CameraSource(device=0, crop=(500, 400, 300, 300), capture=FakeCam()).open()
    except ValueError:
        pass
    except Exception as exc:
        failures.append(f"an out-of-bounds crop raised {exc!r}, expected ValueError")
    else:
        failures.append("an out-of-bounds crop was accepted")

    # A device that never yields a frame must fail at open(), not silently later.
    try:
        CameraSource(device=9, capture=FakeCam(ok_frames=0)).open()
    except OSError:
        pass
    except Exception as exc:
        failures.append(f"a dead camera raised {exc!r}, expected OSError")
    else:
        failures.append("a dead camera opened cleanly")

    # A camera that dies mid-run must HOLD the state, never release the key.
    import macvision.sources.camera as cm
    saved = cm.READ_TIMEOUT_S
    cm.READ_TIMEOUT_S = 0.2
    try:
        dying = FakeCam(ok_frames=2)
        c = CameraSource(device=0, capture=dying)
        c.open()
        c.recv()
        cap = c.recv()
        check("a dead camera yields no frame", cap.frame, None)
        if "held" not in (cap.note or ""):
            failures.append(f"the stall note does not say the state is held: {cap.note!r}")
        c.close()
    finally:
        cm.READ_TIMEOUT_S = saved

    # --- the shared contract ---------------------------------------------------------
    for cls in (UdpSource, CameraSource):
        for attr in ("open", "flush", "recv", "close", "status"):
            if not callable(getattr(cls, attr, None)):
                failures.append(f"{cls.__name__} does not implement {attr}()")
        for attr in ("name", "upstream_label", "stale_dropped", "width", "height"):
            if not hasattr(cls, attr):
                failures.append(f"{cls.__name__} has no {attr}")
    check("udp keeps the documented overlay label", UdpSource.upstream_label, "win")
    check("the camera has its own", CameraSource.upstream_label, "cam")

    cap = Capture(None, 0.0, 1, 2, 3)
    for attr in Capture.__slots__:
        if not hasattr(cap, attr):
            failures.append(f"Capture has no {attr}")

    for mod in ("cv2", "numpy", "torch", "ultralytics"):
        if mod in AT_IMPORT:
            failures.append(f"importing the sources pulled in {mod}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("source parsing, newest-wins on both sources, drop accounting, gap wording, "
          "sender restart, cropping, dead devices, the shared contract: all correct")
    return 0


if __name__ == "__main__":
    sys.exit(run())
