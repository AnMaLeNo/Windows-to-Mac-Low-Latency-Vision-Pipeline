"""The telemetry tap: the address parsing, the framing, and the publisher over loopback.

docs/DASHBOARD.md, contract 1. Two things this defends. The framing is shared by three
readers - the publisher, the dashboard's subscriber and tools/telemetry_tap.py - and a
byte out of place in the header desynchronises all of them at once, so the layout is
asserted byte by byte rather than merely round-tripped. And the hot path's promise -
nothing copied while nobody listens, one copy while someone does, nothing blocked and
nothing raised, ever - is the only reason the tap is allowed to exist, so it is tested
with a frame object that records whether it was touched.

    python3 -m tests.test_telemetry      (from mac-app/)

Stdlib only. The "frame" is a fake with .shape and .tobytes(), which is all the
publisher asks of one, so this runs on the Pi as well as on the Mac.
"""

import io
import json
import os
import socket
import struct
import sys
import threading
import time
from contextlib import redirect_stderr

from macvision.sources import Capture
from macvision.telemetry import (DEFAULT_HOST, DEFAULT_PORT, FRAME_HEADER_SIZE, MAGIC,
                                 MAX_HEADER, MAX_PAYLOAD, MessageReader,
                                 TelemetryPublisher, encode_message, json_safe,
                                 parse_telemetry)


class FakeFrame:
    """Looks enough like an ndarray for the publisher: .shape and .tobytes().

    Counts the tobytes() calls, because "no client, no copy" is the property that
    lets --telemetry be on at all.
    """

    def __init__(self, shape=(300, 300, 3), fill=b"\x7f"):
        self.shape = shape
        self.calls = 0
        n = 1
        for d in shape:
            n *= d
        self.data = fill * n

    def tobytes(self):
        self.calls += 1
        return self.data


class Client:
    """What any subscriber is: a plain socket and a MessageReader."""

    def __init__(self, host, port, rcvbuf=None):
        if rcvbuf is None:
            self.sock = socket.create_connection((host, port), timeout=2.0)
        else:
            # Set BEFORE connect: the receive window is negotiated then, and a buffer
            # this small is what makes a send to this client block on loopback.
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            self.sock.settimeout(2.0)
            self.sock.connect((host, port))
        self.sock.settimeout(0.2)
        self.reader = MessageReader()
        self.pending = []

    def take(self, pred=lambda header: True, timeout=2.0):
        """The first message whose header satisfies pred, consuming everything before
        it (a stream is in order, so what came earlier is what was superseded). None
        on timeout; the skipped messages land in .skipped."""
        self.skipped = []
        deadline = time.monotonic() + timeout
        while True:
            while self.pending:
                msg = self.pending.pop(0)
                if pred(msg[0]):
                    return msg
                self.skipped.append(msg)
            if time.monotonic() >= deadline:
                return None
            try:
                data = self.sock.recv(1 << 16)
            except socket.timeout:
                continue
            if not data:
                return None
            self.pending.extend(self.reader.feed(data))

    def eof(self, timeout=2.0):
        """True if the far end closed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = self.sock.recv(1 << 16)
            except socket.timeout:
                continue
            except OSError:
                return True
            if not data:
                return True
            self.pending.extend(self.reader.feed(data))
        return False

    def close(self):
        self.sock.close()


def wait_for(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def capture(frame, seq, upstream_ms=4.0, transit_ms=10.0):
    return Capture(frame, time.perf_counter(), seq, 300, 300,
                   upstream_ms=upstream_ms, transit_ms=transit_ms)


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    # --- parse_telemetry: the same contract as parse_source, and it never raises -----
    table = [
        (None, "none", None, None),
        ("", "none", None, None),
        ("   ", "none", None, None),
        ("none", "none", None, None),
        ("NONE", "none", None, None),
        ("tcp://", "tcp", DEFAULT_HOST, DEFAULT_PORT),
        ("tcp://0.0.0.0:6000", "tcp", "0.0.0.0", 6000),
        ("tcp://:6001", "tcp", DEFAULT_HOST, 6001),
        ("tcp://mac.local", "tcp", "mac.local", DEFAULT_PORT),
        ("TCP://127.0.0.1:50510", "tcp", "127.0.0.1", 50510),
        ("udp://x", "unknown", None, None),
        ("garbage", "unknown", None, None),
        ("tcp://a:bad", "unknown", None, None),
        ("tcp://a:99999", "unknown", None, None),
        ("tcp://[::1", "unknown", None, None),
    ]
    for text, kind, host, port in table:
        try:
            spec = parse_telemetry(text)
        except Exception as exc:
            failures.append(f"parse_telemetry({text!r}) raised {exc!r}; it must not")
            continue
        check(f"parse_telemetry({text!r}) kind", spec["kind"], kind)
        if kind == "tcp":
            check(f"parse_telemetry({text!r}) host", spec["host"], host)
            check(f"parse_telemetry({text!r}) port", spec["port"], port)
        if kind == "unknown" and not spec["reason"]:
            failures.append(f"parse_telemetry({text!r}) is unknown but gives no reason")
    check("DEFAULT_PORT is the one docs/DASHBOARD.md names", DEFAULT_PORT, 50510)

    # --- the framing, byte by byte ---------------------------------------------------
    header = {"type": "hello", "t": 1725270000.5, "note": "héllo 日本"}
    payload = b"\x00\x01MVT1\xff" * 100          # contains the magic, on purpose
    data = encode_message(header, payload)
    body = json.dumps(dict(header, v=1), separators=(",", ":")).encode("utf-8")
    check("magic at offset 0", data[0:4], b"MVT1")
    check("H at offset 4, uint32 little-endian", data[4:8],
          len(body).to_bytes(4, "little"))
    check("P at offset 8, uint32 little-endian", data[8:12],
          len(payload).to_bytes(4, "little"))
    check("H really is little-endian (a small length fills the LOW byte)",
          (data[4], data[5:8]), (len(body), b"\x00\x00\x00"))
    check("the header follows at 12, UTF-8 JSON, compact, with v stamped",
          json.loads(data[12:12 + len(body)].decode("utf-8")), dict(header, v=1))
    check("the payload follows the header", data[12 + len(body):], payload)
    check("FRAME_HEADER_SIZE", FRAME_HEADER_SIZE, 12)
    check("struct.unpack agrees with the hand-decoded layout",
          struct.unpack("<4sII", data[:12]), (MAGIC, len(body), len(payload)))
    check("an explicit v is kept, not overwritten",
          json.loads(encode_message({"type": "x", "v": 7})[12:].decode("utf-8"))["v"], 7)
    empty = encode_message({"type": "stats"})
    check("no payload means P=0 and nothing after the header",
          (empty[8:12], len(empty)), (b"\x00\x00\x00\x00", 12 + len(empty) - 12))

    stream = data * 3 + empty
    for size in (1, 2, 3, 7, 13, 64, 1000, len(stream)):
        reader = MessageReader()
        got = []
        for i in range(0, len(stream), size):
            got.extend(reader.feed(stream[i:i + size]))
        check(f"chunks of {size}: four messages", len(got), 4)
        if len(got) == 4:
            for k in range(3):
                check(f"chunks of {size}: header {k} survives", got[k][0],
                      dict(header, v=1))
                check(f"chunks of {size}: payload {k} survives", got[k][1], payload)
            check(f"chunks of {size}: the empty one", got[3],
                  ({"type": "stats", "v": 1}, b""))
        check(f"chunks of {size}: nothing left over", reader.pending, 0)
        check(f"chunks of {size}: counted", (reader.messages, reader.bytes),
              (4, len(stream)))

    # Losing sync is an error, never a scan.
    for label, bad in (("wrong magic", b"XXXX" + data[4:]),
                       ("implausible header length",
                        struct.pack("<4sII", MAGIC, MAX_HEADER + 1, 0)),
                       ("implausible payload length",
                        struct.pack("<4sII", MAGIC, 2, MAX_PAYLOAD + 1) + b"{}"),
                       ("header is not JSON", struct.pack("<4sII", MAGIC, 3, 0) + b"abc"),
                       ("header is not an object",
                        struct.pack("<4sII", MAGIC, 2, 0) + b"[]")):
        try:
            MessageReader().feed(bad)
        except ValueError:
            pass
        else:
            failures.append(f"{label}: MessageReader accepted it instead of raising")
    reader = MessageReader()
    reader.feed(data[:5])
    check("a partial header waits rather than raising", reader.pending, 5)

    # json_safe: the hello's status is other people's dicts.
    check("json_safe", json_safe({"a": (1, 2), "b": {1: {"c": frozenset([3])}},
                                  "d": None, "e": 1.5, "f": True}),
          {"a": [1, 2], "b": {"1": {"c": [3]}}, "d": None, "e": 1.5, "f": True})
    check("json_safe stringifies what it cannot place", json_safe(object()).__class__,
          str)

    # --- the publisher, over loopback -----------------------------------------------
    described = {"source": {"kind": "fake", "description": "fake://0"},
                 "detector": {"weights": "fake.pt", "roi": (300, 300)},
                 "trigger": {"kind": "none", "description": "no link"},
                 "display": None}
    pub = TelemetryPublisher(host="127.0.0.1", port=0, describe=lambda: described,
                             roi=(300, 300), argv=["--source", "camera://0"])
    err = io.StringIO()
    try:
        if pub.port == 0:
            failures.append("port=0 did not resolve to the port actually bound")
        check("description", pub.description, f"tcp://127.0.0.1:{pub.port}")
        check("not alive before start()", pub.status()["alive"], False)

        # A busy port raises; main() turns that into a warning, not this class.
        try:
            TelemetryPublisher(host=pub.host, port=pub.port)
        except OSError:
            pass
        else:
            failures.append("a second publisher bound the same port; that must raise")

        pub.start()
        pub.start()                        # idempotent
        check("alive after start()", pub.status()["alive"], True)
        check("the thread is named for the ps listing",
              any(t.name == "telemetry" for t in threading.enumerate()), True)

        # No client: the frame is never touched. This is the whole cost model.
        frame = FakeFrame()
        boxes = [(12.5, 40.0, 210.0, 190.0)]
        with redirect_stderr(err):
            pub.frame(capture(frame, 812), boxes, True, 150, 150, 800,
                      31.2, 0.3, 9.1, 6.9, "win")
        check("no client: tobytes() never called", frame.calls, 0)
        check("no client: nothing offered", pub.status()["frames_offered"], 0)

        # A client: hello first, with what describe() said.
        client = Client(pub.host, pub.port)
        hello = client.take()
        if hello is None:
            failures.append("no hello arrived on connect")
        else:
            h, p = hello
            check("hello first", h.get("type"), "hello")
            check("hello v", h.get("v"), 1)
            check("hello pid", h.get("pid"), os.getpid())
            check("hello roi", h.get("roi"), [300, 300])
            check("hello argv", h.get("argv"), ["--source", "camera://0"])
            check("hello status is describe(), verbatim (tuples as lists)",
                  h.get("status"), json_safe(described))
            check("hello has no payload", p, b"")
            if not isinstance(h.get("t"), float):
                failures.append(f"hello t is {h.get('t')!r}, expected time.time()")
        check("one client counted", wait_for(lambda: pub.status()["clients"] == 1), True)
        check("one connection so far", pub.status()["connections"], 1)

        # A frame: header per the doc, pixels as the payload, ONE copy.
        with redirect_stderr(err):
            pub.frame(capture(frame, 812), boxes, True, 150, 150, 800,
                      31.2, 0.3, 9.1, 6.9, "win")
        msg = client.take(lambda h: h.get("type") == "frame")
        if msg is None:
            failures.append("no frame arrived")
        else:
            h, p = msg
            expected = {"type": "frame", "v": 1, "seq": 812, "n": 800,
                        "w": 300, "h": 300, "c": 3, "fmt": "bgr8", "dtype": "uint8",
                        "hit": True, "cx": 150, "cy": 150,
                        "boxes": [[12.5, 40.0, 210.0, 190.0]],
                        "timing": {"e2e_ms": 31.2, "decide_ms": 6.9, "mac_ms": 9.1,
                                   "upstream_ms": 4.0, "queue_ms": 0.3,
                                   "upstream_label": "win", "e2e_mark": "~"}}
            got = dict(h)
            t = got.pop("t", None)
            check("frame header, field by field", got, expected)
            if not isinstance(t, float):
                failures.append(f"frame t is {t!r}, expected time.time()")
            check("frame payload is the pixels, w*h*c bytes", (len(p), p[:8]),
                  (270000, frame.data[:8]))
        check("with a client: exactly one copy", frame.calls, 1)

        # Grey pixels, and a source that cannot measure its upstream: null, not 0.
        gray = FakeFrame((300, 300))
        with redirect_stderr(err):
            pub.frame(capture(gray, 813, upstream_ms=None, transit_ms=None), [], False,
                      150, 150, 801, 12.0, None, 5.0, 5.0, "up")
        msg = client.take(lambda h: h.get("type") == "frame" and h.get("seq") == 813)
        if msg is None:
            failures.append("the grey frame never arrived")
        else:
            h, p = msg
            check("a 2-D frame is gray8 with c=1", (h["c"], h["fmt"]), (1, "gray8"))
            check("no boxes is an empty list", h["boxes"], [])
            check("hit is a bool", h["hit"], False)
            check("unmeasurable spans are null and the mark is >",
                  (h["timing"]["upstream_ms"], h["timing"]["queue_ms"],
                   h["timing"]["e2e_mark"], h["timing"]["upstream_label"]),
                  (None, None, ">", "up"))
            check("gray payload length", len(p), 90000)

        # The header's geometry is the PIXELS', not the capture's. A udp sender's
        # datagram header can claim one size while the JPEG decoded to another, and a
        # subscriber reshapes the payload by w*h*c - so the array is the authority.
        odd = FakeFrame((200, 320, 3))
        with redirect_stderr(err):
            pub.frame(capture(odd, 814), [], False, 150, 150, 802,   # cap says 300x300
                      1.0, 0.0, 1.0, 1.0, "win")
        msg = client.take(lambda h: h.get("type") == "frame" and h.get("seq") == 814)
        if msg is None:
            failures.append("the oddly shaped frame never arrived")
        else:
            h, p = msg
            check("w and h come from the frame's shape, not cap.width/height",
                  (h["w"], h["h"]), (320, 200))
            check("and w*h*c is the payload length", h["w"] * h["h"] * h["c"], len(p))

        # Four channels are bgra8, not "gray8 by elimination"; and the element type is
        # declared, so a frame that is not uint8 is at least announced as such.
        bgra = FakeFrame((4, 4, 4))
        bgra.dtype = "uint16"
        with redirect_stderr(err):
            pub.frame(capture(bgra, 815), [], False, 150, 150, 803,
                      1.0, 0.0, 1.0, 1.0, "win")
        msg = client.take(lambda h: h.get("type") == "frame" and h.get("seq") == 815)
        if msg is None:
            failures.append("the four-channel frame never arrived")
        else:
            h, p = msg
            check("four channels is bgra8", (h["c"], h["fmt"]), (4, "bgra8"))
            check("the frame's dtype is declared, as its str()", h["dtype"], "uint16")

        # A stats message, per the doc.
        status = {"n": 200, "window": 200, "e2e_median_ms": 30.1, "e2e_max_ms": 48.0,
                  "decide_median_ms": 6.8, "offset_ms": 238.4}
        with redirect_stderr(err):
            pub.stats(900, status, 3, 0, "[stats] n=200 ...")
        msg = client.take(lambda h: h.get("type") == "stats")
        if msg is None:
            failures.append("no stats message arrived")
        else:
            h, p = msg
            got = dict(h)
            got.pop("t", None)
            check("stats header", got,
                  {"type": "stats", "v": 1, "n": 900, "stats": status,
                   "stale_dropped": 3, "dropped_writes": 0,
                   "summary": "[stats] n=200 ..."})
            check("stats has no payload", p, b"")

        # Newest wins, and provably so. "The last frame arrived" and "sent <= offered"
        # are also true of a publisher that queues everything and happens to be quick,
        # so the thread's send step is held back while 200 frames are offered: the
        # slot must then hold ONLY the last of them, and once the step is released
        # exactly one frame may reach the client.
        before = pub.status()
        burst = FakeFrame()
        held = []
        pub._send_latest = lambda: held.append(1)   # the thread resolves it per pass
        pub._wake.set()
        # Once the stand-in has run, the thread is past any pass that resolved the
        # real method, so nothing offered below can slip out early.
        check("the stand-in send step runs", wait_for(lambda: bool(held)), True)
        with redirect_stderr(err):
            for i in range(200):
                pub.frame(capture(burst, 1000 + i), [], False, 150, 150, 1000 + i,
                          1.0, 0.0, 1.0, 1.0, "win")
        check("the burst copied once per call", burst.calls, 200)
        slot = getattr(pub, "_latest", None)
        check("the slot holds only the LAST frame offered",
              slot[0].get("seq") if slot else None, 1199)
        mid = pub.status()
        check("every call was offered",
              mid["frames_offered"] - before["frames_offered"], 200)
        check("nothing was sent while the step was held",
              mid["frames_sent"], before["frames_sent"])
        del pub._send_latest                        # the class's method shows again
        pub._wake.set()
        one = client.take(lambda h: h.get("type") == "frame")
        check("the one frame that reaches the client is the last one offered",
              one[0].get("seq") if one else None, 1199)
        check("nothing came ahead of it",
              [h["seq"] for h, _ in client.skipped if h.get("type") == "frame"], [])
        extra = client.take(lambda h: h.get("type") == "frame", timeout=0.3)
        check("and nothing after it", extra[0].get("seq") if extra else None, None)
        after = pub.status()
        check("frames_sent moved by exactly one",
              after["frames_sent"] - mid["frames_sent"], 1)
        check("frames_coalesced by the other 199",
              after["frames_coalesced"] - before["frames_coalesced"], 199)
        check("coalesced is offered minus sent", after["frames_coalesced"],
              after["frames_offered"] - after["frames_sent"])
        check("the held burst raised nothing", after["errors"], 0)

        # And live, with nothing patched: a burst outruns the thread, so fewer frames
        # are sent than offered, the last one is among them, and order is kept.
        before = pub.status()
        with redirect_stderr(err):
            for i in range(200):
                pub.frame(capture(burst, 1200 + i), [], False, 150, 150, 1200 + i,
                          1.0, 0.0, 1.0, 1.0, "win")
        last = client.take(lambda h: h.get("type") == "frame" and h.get("seq") == 1399,
                           timeout=4.0)
        if last is None:
            failures.append("the last frame offered (seq 1399) never arrived - "
                            "newest did not win")
        seqs = [h["seq"] for h, _ in client.skipped if h.get("type") == "frame"]
        if seqs != sorted(seqs):
            failures.append(f"frames arrived out of order: {seqs}")
        after = pub.status()
        offered = after["frames_offered"] - before["frames_offered"]
        sent = after["frames_sent"] - before["frames_sent"]
        check("every call was offered", offered, 200)
        if not 1 <= sent < offered:
            failures.append(f"frames_sent moved by {sent} for {offered} offers; a "
                            f"burst must coalesce, not queue")
        check("the live burst raised nothing", after["errors"], 0)

        # A client that leaves is dropped - even with no frame to fail a send on - and
        # the loop goes back to paying nothing.
        client.close()
        check("a departed client is dropped",
              wait_for(lambda: pub.status()["clients"] == 0), True)
        idle = FakeFrame()
        with redirect_stderr(err):
            pub.frame(capture(idle, 2000), [], False, 150, 150, 2000,
                      1.0, 0.0, 1.0, 1.0, "win")
        check("nobody listening again: no copy", idle.calls, 0)

        # And a new subscriber is served as the first was.
        client2 = Client(pub.host, pub.port)
        hello2 = client2.take()
        check("the second subscriber gets its hello",
              hello2[0].get("type") if hello2 else None, "hello")
        check("two connections ever", pub.status()["connections"], 2)
        wait_for(lambda: pub.status()["clients"] == 1)
        with redirect_stderr(err):
            pub.frame(capture(idle, 2001), boxes, True, 150, 150, 2001,
                      1.0, 0.0, 1.0, 1.0, "win")
        msg = client2.take(lambda h: h.get("type") == "frame")
        check("and its frames", msg[0].get("seq") if msg else None, 2001)

        # A frame with no tobytes(): counted, complained about once, never raised.
        errors_before = pub.status()["errors"]
        bad = capture("not an array", 2002)
        complaint = io.StringIO()
        with redirect_stderr(complaint):
            try:
                pub.frame(bad, [], False, 150, 150, 2002, 1.0, 0.0, 1.0, 1.0, "win")
            except Exception as exc:
                failures.append(f"frame() raised {exc!r} into the loop")
        check("the bad frame is counted", pub.status()["errors"], errors_before + 1)
        if "[telemetry]" not in complaint.getvalue():
            failures.append("the bad frame was not reported on stderr")
        with redirect_stderr(complaint):
            pub.frame(capture(idle, 2003), [], False, 150, 150, 2003,
                      1.0, 0.0, 1.0, 1.0, "win")
        msg = client2.take(lambda h: h.get("type") == "frame" and h.get("seq") == 2003)
        check("the publisher carries on after a bad frame",
              msg[0].get("seq") if msg else None, 2003)

        client2.close()
        wait_for(lambda: pub.status()["clients"] == 0)

        # stop() must not wait on a wedged subscriber. This one reads its hello and
        # then never reads again, so the publisher's send to it blocks in sendall()
        # under a 1s timeout once the loopback buffers fill - and main() calls stop()
        # from its finally block AHEAD of trigger.stop(), so every millisecond stop()
        # waits is one more in which the keepalive re-asserts a possibly held key. The
        # receive window is pinned small and the frame is 32MB, past what any loopback
        # can absorb, so the thread is provably inside that send when stop() runs.
        wedged = Client(pub.host, pub.port, rcvbuf=4096)
        hello_w = wedged.take()
        check("the wedged subscriber gets its hello",
              hello_w[0].get("type") if hello_w else None, "hello")
        wait_for(lambda: pub.status()["clients"] == 1)
        huge = FakeFrame((4096, 2048, 4))
        sent_before = pub.status()["frames_sent"]
        with redirect_stderr(err):
            pub.frame(capture(huge, 3000), [], False, 150, 150, 3000,
                      1.0, 0.0, 1.0, 1.0, "win")
        check("the thread took the huge frame",
              wait_for(lambda: pub.status()["frames_sent"] > sent_before), True)
        time.sleep(0.05)                                  # and is inside sendall() now

        # stop(): shuts the sockets, joins, releases the port - and fast. Twice is fine.
        t0 = time.perf_counter()
        pub.stop()
        took = time.perf_counter() - t0
        pub.stop()
        if took >= 0.5:
            failures.append(f"stop() took {took:.3f}s with a subscriber that stopped "
                            f"reading; it must not wait out that socket's timeout")
        check("stop() joins the thread", pub.status()["alive"], False)
        check("stop() closes the clients", wedged.eof(), True)
        check("stop() forgets the clients", pub.status()["clients"], 0)
        wedged.close()
        copies = idle.calls
        with redirect_stderr(err):
            pub.frame(capture(idle, 2004), [], False, 150, 150, 2004,
                      1.0, 0.0, 1.0, 1.0, "win")
        check("after stop(), frame() is inert", idle.calls, copies)
        pub.start()
        check("start() after stop() is a no-op", pub.status()["alive"], False)

        # The port is free again - the TIME_WAIT argument for SO_REUSEADDR, in a test -
        # and a describe() that raises still produces a hello.
        try:
            pub2 = TelemetryPublisher(host=pub.host, port=pub.port,
                                      describe=lambda: {}["missing"])
        except OSError as exc:
            failures.append(f"the port was not released by stop(): {exc}")
        else:
            try:
                pub2.start()
                client3 = Client(pub2.host, pub2.port)
                with redirect_stderr(io.StringIO()):
                    hello3 = client3.take()
                if hello3 is None:
                    failures.append("no hello when describe() raises")
                else:
                    check("a failing describe() still sends the hello, saying so",
                          (hello3[0].get("type"), "error" in hello3[0].get("status", {})),
                          ("hello", True))
                check("and the failure is counted", pub2.status()["errors"] >= 1, True)
                client3.close()
            finally:
                pub2.stop()
    finally:
        pub.stop()

    if err.getvalue():
        failures.append(f"unexpected stderr from the publisher: {err.getvalue()!r}")

    for mod in ("cv2", "numpy", "torch", "ultralytics"):
        if mod in sys.modules:
            failures.append(f"importing macvision.telemetry pulled in {mod}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("target parsing, byte layout, chunked reads, sync loss, hello-first, frame "
          "and stats headers, no-client no-copy, newest wins, client churn, error "
          "containment, stop against a wedged subscriber and port release: all correct")
    return 0


if __name__ == "__main__":
    sys.exit(run())
