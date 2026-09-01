"""The Windows agent's link: a UDP datagram per frame, newest wins.

Block until at least one datagram is available, then drain whatever piled up in the
kernel socket buffer while the previous frame was being decoded and inferred - and keep
only the newest one. Without this, a plain recvfrom() loop processes every frame in
arrival order: if inference is slower than the arrival rate, nothing is ever lost, but
everything falls further and further behind, so the debug window visibly lags more the
longer motion continues. Dropping stale frames trades "see every frame" for "always see
the most recent one", which is what a latency-critical detector wants (commit 8ff3ae1).

The header parse and the JPEG decode both live here, and that is the point of the source
abstraction: a camera has neither, so the frame loop must not know they exist.
"""

import socket
import sys
import time

from ..protocol import MAX_DATAGRAM, UDP_PORT, payload, unpack_header
from . import Capture, Source


class SequenceTracker:
    """Turns sequence numbers into one diagnostic line, or None.

    A separate class so the accounting can be tested by feeding it integers, with no
    socket in sight.
    """

    def __init__(self):
        # None, never 0, so the first frame cannot report a gap for phantom frames
        # before it.
        self.last_seq = None
        self.gaps = 0
        self.lost_in_transit = 0
        self.restarts = 0

    def observe(self, seq, dropped):
        """-> the line to print, or None. Updates last_seq on every path."""
        if self.last_seq is None:
            self.last_seq = seq
            return None

        if seq <= self.last_seq:
            # windows-agent/src/main.cpp starts `sequence` at 0 on every launch, so
            # restarting the agent while this runs used to print "missing -5001 ...
            # -5001 lost in transit" - the arithmetic below assumes monotonic growth.
            prev = self.last_seq
            self.restarts += 1
            self.last_seq = seq
            return f"[seq] sender restarted (seq went {prev} -> {seq})"

        line = None
        if seq != self.last_seq + 1:
            missing = seq - self.last_seq - 1
            lost = missing - dropped
            self.gaps += 1
            self.lost_in_transit += lost
            # The two-way split is the entire diagnostic value of this line and cannot
            # be reconstructed anywhere else: "dropped here" means inference is slower
            # than capture, "lost in transit" means the link is losing packets.
            # Opposite causes, opposite fixes.
            line = (f"[gap] {missing} missing (seq {self.last_seq + 1}..{seq - 1}): "
                    f"{dropped} dropped here for staleness, {lost} lost in transit")

        # Assigned on EVERY path, before the caller does anything else, so a frame that
        # later fails to decode cannot make the next one report a phantom 1-frame gap.
        self.last_seq = seq
        return line

    def status(self):
        return {"gaps": self.gaps, "lost_in_transit": self.lost_in_transit,
                "restarts": self.restarts, "last_seq": self.last_seq}


class UdpSource(Source):
    name = "udp"
    # docs/PROTOCOL.md defines this label: Windows-side capture -> encode -> send.
    upstream_label = "win"

    def __init__(self, host="0.0.0.0", port=None, decoder=None):
        # `decoder` is a plug point, not a nicety. Anything with .decode(bytes) -> frame
        # works, which is what lets this class be tested for real on a machine with no
        # opencv, and what makes a turbojpeg backend a one-line swap. Left None, open()
        # builds the cv2 one - and only then, so importing this module needs nothing.
        self._decoder = decoder
        self.host = host
        self.requested_port = UDP_PORT if port is None else port
        self.port = self.requested_port
        self.description = f"udp {host}:{self.requested_port}"
        self._sock = None
        self.tracker = SequenceTracker()
        self.packets = 0
        self.stale_dropped = 0
        self.malformed = 0
        self.decode_failures = 0
        self.last_peer = None
        self._last_datagram = 0.0

    def open(self):
        if self._decoder is None:
            from ..codec import JpegDecoder  # lazy: only the real path needs opencv
            self._decoder = JpegDecoder()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Deliberately NO SO_REUSEADDR, and this comment is here because the omission is
        # otherwise indistinguishable from an oversight and someone will helpfully add
        # the flag. On UDP it buys nothing - there is no TIME_WAIT to work around - and
        # it lets a second instance bind this port successfully, after which the kernel
        # hands each datagram to one of them and frames silently go to whichever process
        # you were not looking at. Failing the bind is the correct, loud behaviour.
        # (pi-agent/piproxy/api.py refuses the same flag for the same reason.)
        sock.bind((self.host, self.requested_port))
        self._sock = sock
        # Read back rather than echoed, so port=0 yields a real ephemeral socket - which
        # is how the tests exercise this class for real with no dependencies.
        self.host, self.port = sock.getsockname()
        self.description = f"udp {self.host}:{self.port}"

    def flush(self):
        """Throw away everything that arrived during the model warmup.

        Without this the first frame the loop sees is however old the MPS warmup was -
        several seconds - and the pipeline starts by reporting a huge queueing delay
        that is entirely its own startup.
        """
        if self._sock is None:
            return 0
        discarded = 0
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    self._sock.recvfrom(MAX_DATAGRAM)
                    discarded += 1
                except BlockingIOError:
                    break
        finally:
            self._sock.setblocking(True)
        return discarded

    def recv(self):
        # The blocking read comes FIRST and the drain is a mop-up, never a poll loop: a
        # poll would burn a core and add scheduling jitter to every frame.
        #
        # And NO settimeout() anywhere on this socket. settimeout makes CPython wrap
        # every recv in a poll() syscall, doubling syscalls on the hot path, and it
        # changes the drain's exception type from BlockingIOError to socket.timeout.
        # This runs on the main thread and needs no stop flag: it exits on "q", on
        # KeyboardInterrupt from Ctrl-C, or on the SIGTERM handler raising.
        data, peer = self._sock.recvfrom(MAX_DATAGRAM)
        dropped = 0
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    data, peer = self._sock.recvfrom(MAX_DATAGRAM)
                    dropped += 1
                except BlockingIOError:
                    break
        finally:
            # The try/finally is not decoration. Without it the pairing holds only
            # because nothing in between can raise except BlockingIOError - and a socket
            # left non-blocking makes the next "blocking" recvfrom raise BlockingIOError
            # immediately: an uncaught crash, or a full-speed busy spin if someone
            # catches it.
            self._sock.setblocking(True)

        # Both clocks, back to back, after the drain has settled on the surviving
        # datagram and before any parsing. Taking them here binds both measurements to
        # the datagram actually processed rather than to the first, discarded one, and
        # keeps the parse and decode cost inside mac_ms. They are two different clocks
        # measuring two different spans and may NEVER be merged: perf_counter is
        # monotonic and has no epoch, so it cannot be compared to the Windows stamp,
        # while time.time is comparable but can step backwards mid-run and would
        # corrupt every duration derived from it. See docs/PROTOCOL.md.
        t0 = time.perf_counter()
        recv_wallclock_us = time.time() * 1_000_000

        self.packets += 1
        self.stale_dropped += dropped
        self.last_peer = f"{peer[0]}:{peer[1]}"
        self._last_datagram = time.monotonic()

        header = unpack_header(data)
        if header is None:
            self.malformed += 1
            if self.malformed in (1, 10) or self.malformed % 500 == 0:
                print(f"[udp] {len(data)}-byte datagram is shorter than the 24-byte "
                      f"header; not ours? [{self.malformed} so far]",
                      file=sys.stderr, flush=True)
            return Capture(None, t0, self.tracker.last_seq, 0, 0, dropped=dropped)

        (seq, capture_wallclock_us, capture_to_send_us,
         width, height, jpeg_size) = header

        # Before the decode, because last_seq is assigned inside observe() on every
        # path: a frame that then fails to decode must not make the next one report a
        # phantom gap.
        note = self.tracker.observe(seq, dropped)

        jpeg_bytes = payload(data, jpeg_size)
        frame = self._decoder.decode(jpeg_bytes)
        if frame is None:
            self.decode_failures += 1
            # Claimed vs present costs one len() and is the difference between "the
            # JPEG is corrupt" and "that was not our packet".
            warn = (f"[warn] seq={seq}: failed to decode JPEG payload "
                    f"({jpeg_size} claimed, {len(jpeg_bytes)} present), skipping")
            note = f"{note}\n{warn}" if note else warn

        return Capture(
            frame, t0, seq, width, height,
            upstream_ms=capture_to_send_us / 1000,
            transit_ms=(recv_wallclock_us - capture_wallclock_us) / 1000,
            dropped=dropped, note=note)

    @property
    def idle_s(self):
        """Seconds since the last datagram, or 0.0 if none has arrived yet.

        Exposed rather than acted upon: see the stuck-key paragraph in trigger.py. "No
        datagrams" cannot be distinguished from "the screen is static" by arrival alone,
        so nothing here may release the key on its own.
        """
        if self._last_datagram == 0.0:
            return 0.0
        return time.monotonic() - self._last_datagram

    def status(self):
        st = {"kind": self.name, "description": self.description,
              "udp_port": self.port, "packets": self.packets,
              "stale_dropped": self.stale_dropped, "malformed": self.malformed,
              "decode_failures": self.decode_failures, "last_peer": self.last_peer,
              "idle_s": round(self.idle_s, 3)}
        st.update(self.tracker.status())
        return st

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
