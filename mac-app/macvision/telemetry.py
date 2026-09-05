"""The telemetry tap: what the frame loop hands to whoever is watching, and how.

docs/DASHBOARD.md, contract 1. This is the ONE output macvision gains for the dashboard,
and the two rules that file states are enforced here rather than merely described:

  - Off by default, and then not one instruction runs for it. The publisher is only
    constructed when --telemetry / $MACVISION_TELEMETRY names a socket, and the loop
    holds None otherwise.
  - Nothing here may block or copy ahead of the trigger byte. frame() is called AFTER
    action.update() and after mac_ms is sampled, and it does one thing: when a client
    is connected, copy the pixels once and drop them in a "latest" slot for the
    publisher thread. Newest wins - the same rule both sources apply on the way in. A
    subscriber that cannot keep up is handed a fresher frame, never a backlog, and it
    costs the loop nothing beyond that single copy. No lock of this module's is taken
    on that path; the one Event.set() takes the Event's own internal lock for a
    bounded few microseconds, and even that lands after the byte.

The listener is IPv4 only (AF_INET). tcp://[::1]:50510 parses but cannot bind, and
main() then says "telemetry disabled" and runs without it: the dashboard lives on this
Mac and talks to 127.0.0.1, which is the default.

The wire format is a stream of self-delimiting messages:

    0   4  magic b"MVT1"
    4   4  H, header length, uint32 little-endian
    8   4  P, payload length, uint32 little-endian
    12  H  header: one JSON object, UTF-8. Always carries "v" and "type".
    12+H P  payload: raw bytes (the pixels for a frame), or empty

encode_message() and MessageReader are the two halves of that framing, and they live
here - not in the dashboard - so that there is exactly one implementation of the layout
for the publisher, the dashboard's subscriber and tools/telemetry_tap.py to share.
Stdlib only: this module is listed among the PURE modules in tests/test_imports.py.
"""

import json
import os
import select
import socket
import struct
import sys
import threading
import time
from collections import deque
from urllib.parse import urlparse

MAGIC = b"MVT1"
VERSION = 1
FRAME_HEADER = "<4sII"                       # magic, H, P
FRAME_HEADER_SIZE = struct.calcsize(FRAME_HEADER)   # 12

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 50510

# A header is a small JSON object and a payload is one ROI's pixels. Anything past
# these is not a message from this protocol but a stream that has lost sync, and the
# reader refuses it instead of allocating whatever a corrupt length asks for.
MAX_HEADER = 1 << 20          # 1MB
MAX_PAYLOAD = 64 << 20        # 64MB

# The channel order a subscriber may assume for c channels. Anything else goes out as
# "c<n>": declared as unknown rather than mislabelled as one of these.
FRAME_FORMATS = {1: "gray8", 3: "bgr8", 4: "bgra8"}


def parse_telemetry(target):
    """--telemetry / $MACVISION_TELEMETRY -> a plain dict. Pure, and it never raises.

    Same contract as sources.parse_source and trigger.parse_target, for the same
    reason: user input must not become a traceback out of the one function whose job
    is to make sense of it.

        unset / "" / none      -> {"kind": "none"}
        tcp://[host][:port]    -> {"kind": "tcp", "host", "port"}
        anything else          -> {"kind": "unknown", "reason"}
    """
    out = {"kind": "none", "host": None, "port": None, "raw": target, "reason": None}
    text = (target or "").strip()
    if not text or text.casefold() == "none":
        return out
    if "://" not in text:
        out["kind"] = "unknown"
        out["reason"] = "expected tcp://[host][:port] or none (no scheme in this value)"
        return out
    scheme = text.casefold().split("://", 1)[0]
    if scheme != "tcp":
        out["kind"] = "unknown"
        out["reason"] = f"unknown scheme {scheme!r} (expected tcp)"
        return out
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError as exc:
        out["kind"] = "unknown"
        out["reason"] = str(exc)
        return out
    out["kind"] = "tcp"
    out["host"] = parsed.hostname or DEFAULT_HOST
    out["port"] = port if port is not None else DEFAULT_PORT
    return out


def encode_prefix(header, payload_len=0):
    """Everything before the payload: magic + lengths + header JSON, as bytes.

    `header` is a dict; "v" is stamped if absent. The header is serialised compactly
    (no spaces) - it goes out once per frame. Split from the payload on purpose: the
    publisher sends the two as separate buffers, so the pixels are never concatenated
    into a second copy (see TelemetryPublisher._broadcast).
    """
    if "v" not in header:
        header = dict(header, v=VERSION)
    body = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack(FRAME_HEADER, MAGIC, len(body), payload_len) + body


def encode_message(header, payload=b""):
    """One framed message, whole: encode_prefix() + payload, ready for sendall().

    What a test or a fake publisher wants. The publisher itself does not call this for
    frames: the concatenation is a copy of the pixels it has no reason to make.
    """
    return encode_prefix(header, len(payload)) + payload


class MessageReader:
    """The incremental half: feed() bytes as they arrive, get complete messages back.

    A stream, so a recv() may end anywhere - mid-magic, mid-header, mid-pixels. This
    buffers until a whole message is present and yields (header, payload) pairs, with
    the header decoded and the payload as bytes.

    Losing sync is a hard error, on purpose. The magic is checked at every message
    boundary, and a mismatch raises ValueError: a subscriber that sees one must close
    and reconnect. There is no scan for the next magic, because the pixels can contain
    those four bytes and a scan would resync onto garbage with no error anywhere.
    """

    def __init__(self):
        self._buf = bytearray()
        self.messages = 0
        self.bytes = 0

    def feed(self, data):
        """Append bytes; return a list of (header, payload) for every completed message."""
        self._buf += data
        self.bytes += len(data)
        out = []
        while True:
            if len(self._buf) < FRAME_HEADER_SIZE:
                break
            magic, h, p = struct.unpack_from(FRAME_HEADER, self._buf, 0)
            if magic != MAGIC:
                raise ValueError(f"lost sync: expected {MAGIC!r}, found {bytes(magic)!r}")
            if h > MAX_HEADER or p > MAX_PAYLOAD:
                raise ValueError(f"lost sync: implausible lengths header={h} payload={p}")
            total = FRAME_HEADER_SIZE + h + p
            if len(self._buf) < total:
                break
            body = bytes(self._buf[FRAME_HEADER_SIZE:FRAME_HEADER_SIZE + h])
            payload = bytes(self._buf[FRAME_HEADER_SIZE + h:total])
            del self._buf[:total]
            try:
                header = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError(f"lost sync: header is not JSON ({exc})")
            if not isinstance(header, dict):
                raise ValueError("lost sync: header is not a JSON object")
            self.messages += 1
            out.append((header, payload))
        return out

    @property
    def pending(self):
        """Bytes buffered but not yet a whole message. For status lines and tests."""
        return len(self._buf)


def json_safe(value):
    """A copy of `value` that json.dumps will accept, or the nearest thing to one.

    The hello's status dict is assembled from every block's own status(), whose contents
    this module does not own and cannot vet: a tuple, a set, a numpy scalar or a path
    object in one of them must not turn into a TypeError on the publisher thread and a
    subscriber that never gets its hello. Tuples and sets become lists, dict keys become
    strings, numpy scalars unwrap through .item(), and anything else becomes its str().
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    if hasattr(value, "item"):
        # A numpy scalar (or a 0-d array). Duck-typed on purpose: numpy is not imported
        # here and must not be.
        try:
            return json_safe(value.item())
        except Exception:
            pass
    return str(value)


class TelemetryPublisher:
    """Contract 1's sender: one listener, any number of subscribers, one thread.

    Two paths, kept apart on purpose:

      frame() / stats()    called on the FRAME LOOP's thread. No socket, no JSON, no
                           lock of this module's. frame() reads one tuple to learn
                           whether anyone is listening; when someone is, it copies the
                           pixels once and drops (header, payload) into the "latest"
                           slot. stats() appends to a short queue. Both then set an
                           Event and return - and Event.set() does take the Event's
                           own internal lock, for a bounded few microseconds that
                           nothing here contends for, after the trigger byte.
      the "telemetry" thread    accepts connections, sends each its hello, drains the
                           stats queue, takes whatever is in the slot and sendall()s
                           it. It is the only thread that ever touches a socket or
                           encodes JSON.

    The slot is a single attribute and the client list is a tuple replaced whole, so
    the hot path needs no lock of its own: an attribute store and a tuple read are
    each one operation under the GIL, the same argument Trigger.update makes for
    `_active` and Trigger.status for `dropped_writes`. "Newest wins" falls out of the
    slot being one deep - a slow subscriber is handed a fresher frame, never a
    backlog, and the loop pays the same single copy whether the subscriber is fast,
    slow or dead.

    Nothing raises out of frame() or stats(), ever. Whatever goes wrong is counted in
    `errors`, printed a few times the way Trigger._send does, and the loop carries on.

    Counters, all plain ints read lock-free by status():
      frames_offered    frame() calls that stored a frame
      frames_sent       frames the thread took out of the slot (and wrote to whoever
                        was connected at that moment)
      frames_coalesced  offered - sent: overwritten before the thread got to them
      connections       accepted, ever;  clients: connected now
      errors            exceptions swallowed, on either thread
    """

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, describe=None, roi=(0, 0),
                 argv=None):
        """Bind and listen. Raises OSError if the port is busy; main() treats that as a
        warning, not a failure, exactly as it treats the debug window.

        `describe` is called on the publisher thread, once per connection, to fill the
        hello's "status". It must be cheap and lock-free, because it runs concurrently
        with the frame loop: every block's status() qualifies - they read counters and
        strings, and Trigger.status in particular takes no lock for exactly this reason.
        port=0 asks the kernel for a free one; .port is the port actually bound.
        """
        self._describe = describe
        self._roi = list(roi)
        self._argv = list(argv) if argv else []

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # SO_REUSEADDR, and here it is right. sources/udp.py refuses the flag, with a
        # comment explaining why, and that argument is a UDP argument: on UDP the flag
        # lets a second receiver bind the same port and split the stream. On TCP a
        # second LISTENER still fails its bind, loudly - tests/test_telemetry.py asserts
        # it - and all the flag buys is the right to re-bind while the previous run's
        # connections sit in TIME_WAIT, which is precisely "restart macvision from the
        # dashboard" and would otherwise fail for a minute or two for no reason.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind((host, port))
            listener.listen(4)
        except OSError:
            listener.close()
            raise
        listener.setblocking(False)
        self.host, self.port = listener.getsockname()[:2]
        self._listener = listener

        self._clients = ()          # a tuple, replaced whole, never mutated in place
        self._stats = deque(maxlen=32)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="telemetry")

        self.frames_offered = 0
        self.frames_sent = 0
        self.connections = 0
        self.errors = 0

    # ---- the frame loop's side -------------------------------------------------------

    def frame(self, cap, boxes, hit, cx, cy, n, e2e_ms, queue_ms, mac_ms, decide_ms,
              upstream_label):
        """THE hot-path method. Called by the loop after the trigger byte and after the
        mac_ms sample, so nothing it does is measured by anything.

        With nobody connected this is one tuple test. With a subscriber it is one
        tobytes(), one small dict, one attribute store and one Event.set() - and then
        it is the publisher thread's problem.
        """
        if not self._clients:
            return
        try:
            frame = cap.frame
            payload = frame.tobytes()
            shape = getattr(frame, "shape", ())
            c = int(shape[2]) if len(shape) == 3 else 1
            if len(shape) >= 2:
                # The pixels' own geometry, not cap.width/height. For the udp source
                # those two are the SENDER's datagram header - the crop it meant to
                # send - while the payload is whatever the JPEG decoded to, and a
                # subscriber reshapes the payload by w*h*c: if the two disagree the
                # frame is unreadable. The array is the truth; the header follows it.
                h, w = int(shape[0]), int(shape[1])
            else:
                h, w = int(cap.height), int(cap.width)
            upstream_ms = cap.upstream_ms
            header = {
                "type": "frame", "t": time.time(),
                "seq": int(cap.seq), "n": n,
                "w": w, "h": h, "c": c,
                "fmt": FRAME_FORMATS.get(c, f"c{c}"),
                # Declared, not assumed. Every source today hands over uint8, and a
                # subscriber given anything else should at least be told so.
                "dtype": str(getattr(frame, "dtype", "uint8")),
                "hit": bool(hit), "cx": int(cx), "cy": int(cy),
                # The very list the rule tested, as plain floats - no class, no score,
                # by design: those would cost a second device sync per frame on torch.
                "boxes": [[float(v) for v in box[:4]] for box in boxes],
                "timing": {
                    "e2e_ms": float(e2e_ms),
                    "decide_ms": float(decide_ms),
                    "mac_ms": float(mac_ms),
                    "upstream_ms": None if upstream_ms is None else float(upstream_ms),
                    "queue_ms": None if queue_ms is None else float(queue_ms),
                    "upstream_label": upstream_label,
                    # stats.overlay_text's rule, restated so a subscriber can draw the
                    # same mark for the same reason.
                    "e2e_mark": "~" if upstream_ms is not None else ">",
                },
            }
            # Counted BEFORE the store so frames_sent can never lead frames_offered,
            # which would make the coalesced count go negative for a tick.
            self.frames_offered += 1
            self._latest = (header, payload)    # one store; the thread pops it
            self._wake.set()
        except Exception as exc:
            # No exception from here may ever reach the frame loop.
            self.errors += 1
            self._complain("frame()", exc)

    def stats(self, n, stats_status, stale_dropped, dropped_writes, summary):
        """Queue one "stats" message. Queued, never coalesced: losing one of these is
        losing information rather than staleness. Called at the [stats] cadence, so
        the deque's bound is never reached unless the thread has died."""
        try:
            self._stats.append({
                "type": "stats", "t": time.time(), "n": n,
                "stats": stats_status,
                "stale_dropped": stale_dropped, "dropped_writes": dropped_writes,
                "summary": summary,
            })
            self._wake.set()
        except Exception as exc:
            self.errors += 1
            self._complain("stats()", exc)

    # ---- lifecycle -------------------------------------------------------------------

    def start(self):
        """Start the publisher thread. Idempotent, and a no-op after stop()."""
        if self._thread.is_alive() or self._stop.is_set():
            return
        self._thread.start()

    def stop(self):
        """Shut every socket down, then stop and join the thread. Idempotent.

        Sockets FIRST, the flag second, the join last, and the order is the point. The
        thread may be inside sendall() to a subscriber that stopped reading, where it
        would otherwise sit for the socket's full 1s timeout; shutdown() ends that send
        at once. main() calls this from its finally block AHEAD of trigger.stop(), so
        every millisecond spent here is one more in which the keepalive re-asserts the
        last state - which may be a held key. The join is bounded for the same reason:
        teardown must not wait on a browser.
        """
        if self._stop.is_set():
            return
        clients, self._clients = self._clients, ()
        for conn in clients:
            self._close(conn)
        self._close(self._listener)
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout=0.5)
        # A connection the thread accepted between the sweep above and the listener's
        # close would otherwise outlive this call, with a client that never sees EOF.
        clients, self._clients = self._clients, ()
        for conn in clients:
            self._close(conn)

    @property
    def alive(self):
        return self._thread.is_alive()

    @property
    def description(self):
        return f"tcp://{self.host}:{self.port}"

    def status(self):
        # Takes NO lock, like Trigger.status: every field is a plain int or a tuple
        # length, so polling never contends with the frame loop.
        offered, sent = self.frames_offered, self.frames_sent
        return {"host": self.host, "port": self.port,
                "clients": len(self._clients), "connections": self.connections,
                "frames_offered": offered, "frames_sent": sent,
                "frames_coalesced": offered - sent,
                "errors": self.errors, "alive": self.alive}

    # ---- the publisher thread --------------------------------------------------------

    def _loop(self):
        # A timed wait on the wake Event, never time.sleep(): frame() and stats() wake
        # it the instant there is work, and the timeout is only for noticing new
        # connections and departed ones while the loop is quiet. clear() comes BEFORE
        # the work, so a set() that lands mid-send is seen on the next pass rather
        # than lost.
        while not self._stop.is_set():
            self._wake.wait(0.25)
            self._wake.clear()
            try:
                self._accept()
                self._reap()
                self._drain_stats()
                self._send_latest()
            except Exception as exc:
                # Never let the thread die: a dead publisher would leave frame()
                # copying into a slot nobody empties, for every remaining frame.
                self.errors += 1
                self._complain("publisher thread", exc)

    def _accept(self):
        while True:
            try:
                conn, _ = self._listener.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return          # the listener is closed: stop() is under way
            self.connections += 1
            # A timeout rather than blocking forever: a subscriber that stops reading
            # must cost the thread at most one second, after which it is dropped and
            # the others keep getting frames.
            conn.settimeout(1.0)
            # TCP_NODELAY: a frame goes out as two buffers, the small prefix and then
            # the pixels (see _broadcast), and Nagle would hold the prefix back until
            # the pixels arrive or an ACK does. A frame message must not wait on either.
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            data = self._encode(self._hello())
            if data is None:
                self._close(conn)
                continue
            # Hello first, on this connection only, before it joins the broadcast set -
            # so no frame can ever precede it.
            try:
                conn.sendall(data)
            except OSError:
                self._close(conn)
                continue
            self._clients = self._clients + (conn,)

    def _hello(self):
        status = {}
        if self._describe is not None:
            try:
                status = json_safe(self._describe())
            except Exception as exc:
                # The hello still goes out: a subscriber with no hello has nothing to
                # show and no way to tell "no status" from "no connection".
                self.errors += 1
                self._complain("describe()", exc)
                status = {"error": str(exc)}
        return {"type": "hello", "t": time.time(), "pid": os.getpid(),
                "roi": list(self._roi), "argv": list(self._argv), "status": status}

    def _reap(self):
        # A subscriber that went away is otherwise only noticed by the next send, and
        # with a quiet source there may be no next send - meanwhile frame() would keep
        # paying its copy for a client that no longer exists. Readable-with-nothing is
        # EOF; anything a subscriber does send is ignored, because the stream is one
        # way and a command channel is exactly what docs/DASHBOARD.md rules out.
        clients = self._clients
        if not clients:
            return
        try:
            readable, _, _ = select.select(clients, [], [], 0)
        except (OSError, ValueError):
            return
        for conn in readable:
            try:
                data = conn.recv(4096)
            except OSError:
                data = b""
            if not data:
                self._drop(conn)

    def _drain_stats(self):
        while True:
            try:
                header = self._stats.popleft()    # atomic; the loop appends
            except IndexError:
                return
            if not self._clients:
                continue        # nobody to tell; the next hello carries fresh status
            prefix = self._encode(header)
            if prefix is not None:
                self._broadcast(prefix)

    def _send_latest(self):
        # An atomic take-and-clear. `x = self._latest; self._latest = None` is two
        # operations, and a frame() landing between them would be dropped on the floor
        # - and it would be the newest frame, the one that matters when it turns out to
        # be the last before a pause. dict.pop is one operation under the GIL. Nothing
        # ever READS the attribute - frame() stores it, this pops it - so there is no
        # class-level fallback, and none is needed.
        latest = self.__dict__.pop("_latest", None)
        if latest is None:
            return
        self.frames_sent += 1
        if not self._clients:
            return
        header, payload = latest
        prefix = self._encode(header, len(payload))
        if prefix is not None:
            self._broadcast(prefix, payload)

    def _encode(self, header, payload_len=0):
        try:
            return encode_prefix(header, payload_len)
        except Exception as exc:
            self.errors += 1
            self._complain("encode", exc)
            return None

    def _broadcast(self, prefix, payload=b""):
        # Two sends rather than one buffer: prefix + payload would be a second copy of
        # the pixels (~270KB per frame) made on this thread, under the GIL the frame
        # loop needs. Any failure on EITHER send - the 1s timeout included - drops the
        # client, because a subscriber holding the prefix and half the pixels has a
        # stream that is out of sync, and the only correct move for it is to reconnect.
        for conn in self._clients:       # iterates a snapshot; _drop replaces the tuple
            try:
                conn.sendall(prefix)
                if payload:
                    conn.sendall(payload)
            except OSError:
                # A subscriber that stopped reading is a dead one as far as this
                # thread is concerned.
                self._drop(conn)

    def _drop(self, conn):
        self._clients = tuple(c for c in self._clients if c is not conn)
        self._close(conn)

    @staticmethod
    def _close(sock):
        # shutdown() first, and it is the half that matters when ANOTHER thread is
        # inside sendall() on this socket: on Linux close() alone does not wake a
        # poll() already in progress, shutdown() does, and the send then fails at
        # once with EPIPE/EBADF instead of running out its 1s timeout. ENOTCONN from
        # a socket that was never connected (the listener) or is already gone is the
        # expected noise, and so is a second close.
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _complain(self, where, exc):
        # Rate-limited the way Trigger._send is: the first, the tenth, then every
        # 500th. Unbounded printing from a per-frame path is its own kind of stall.
        if self.errors in (1, 10) or self.errors % 500 == 0:
            print(f"[telemetry] {where} raised ({exc!r}); continuing "
                  f"[{self.errors} so far]", file=sys.stderr, flush=True)
