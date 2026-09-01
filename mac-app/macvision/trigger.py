"""Mac -> trigger link.

One state per update: "a person covers the ROI's centre pixel", or not. Two
transports carry it, and the rest of this file is deliberately identical for both,
because the protocol is the part that makes the link correct:

    serial  -> ESP32 -> GPIO -> Pro Micro       the original wired path
    udp     -> Raspberry Pi keyboard proxy      the Pi merges it with the real
                                                keyboard, so the PC sees one keyboard

Choose with the TRIGGER_TARGET environment variable:

    TRIGGER_TARGET=udp://raspberrypi.local:48010
    TRIGGER_TARGET=serial:///dev/cu.usbserial-0001
    TRIGGER_TARGET=none                  run the vision pipeline with no trigger
    (unset)                              auto: first serial port found, else none

See docs/TRIGGER.md for wiring and pi-agent/README.md for the Pi side.

Why this side keeps a direct write, where the Pi merges both write paths onto one
thread. pi-agent/piproxy/sinks.py's Emitter waits on _wake.wait(keepalive_s) and sends
from a single thread, which is right there: its producer is an evdev reader and nothing
in that process is latency-ranked. Here the producer IS the latency-critical loop, so
update() writes synchronously on the caller's thread and the keepalive is a second,
independent writer. Merging them for symmetry would put the trigger byte behind a
scheduler wake and a GIL handoff, undoing the ordering docs/TRIGGER.md documents. This
paragraph exists so the asymmetry is not "fixed" later.

The one stuck-key path no watchdog in this system covers. If the Windows agent dies
while the last decision was hit=True, the frame loop blocks in recvfrom forever while
this keepalive keeps asserting 0x01 - so the far end's 250ms watchdog is continuously
fed and never fires. The key is held indefinitely by a Mac that is alive but blind, with
the debug window frozen on a red crosshair. Releasing on idle does NOT fix it: DXGI
produces no frames on a static screen, which is the whole reason KEEPALIVE_S exists, and
"no datagrams" cannot be told apart from "nothing is moving" by arrival alone.
Every source exposes idle_s for exactly this; Ctrl-C and SIGTERM both reach stop().
"""

import glob
import os
import socket
import sys
import threading
import time
from urllib.parse import urlparse

# The bridge chip on a classic ESP32 devkit (CP2102/CH340) still runs a real UART
# between itself and the MCU, so the byte costs 1/BAUD * 10 bits on that hop: 87us at
# 115200. Raising this to 921600 would save ~78us of a pipeline measured in tens of
# milliseconds, at the cost of CH340 clones that get flaky at high rates.
# firmware/esp32-link/esp32-link.ino points at this symbol by name - do not rename it.
BAUD = 115200

# Default UDP port of the Pi agent (piproxy). Must match TRIGGER_PORT in
# pi-agent/piproxy/api.py.
PI_TRIGGER_PORT = 48010

# Resend the current state this often even when nothing changes. This is not just a
# keepalive for the receiver's watchdog - it is required for correctness: DXGI Desktop
# Duplication only produces a frame when the screen actually changes, so a static
# screen means the receive loop blocks and no frame-driven update happens. Without an
# independent timer the key would release itself whenever the PC's screen stopped
# moving.
KEEPALIVE_S = 0.020

# macOS exposes two device nodes per serial port. Always use the "cu." (call-unit)
# one: opening "tty.*" blocks until DCD is asserted, which a USB-serial bridge never
# does, so the open would hang forever.
PORT_GLOBS = (
    "/dev/cu.usbserial-*",     # CP2102 / FTDI
    "/dev/cu.wchusbserial*",   # CH340
    "/dev/cu.SLAB_USBtoUART*", # older Silabs driver
    "/dev/cu.usbmodem*",       # ESP32-S2/S3 native USB CDC
)

_UNSET = object()  # so an explicit open_trigger("") differs from an absent argument


def list_ports(globs=PORT_GLOBS, globber=glob.glob):
    """Every candidate device, in PORT_GLOBS order.

    `globber` is injectable so the ordering can be asserted from a machine that is not
    a Mac. The order is a preference, not an accident: the adapter most likely to be an
    ESP32 devkit comes first.
    """
    found = []
    for pattern in globs:
        found.extend(sorted(globber(pattern)))
    return found


def find_port(globs=PORT_GLOBS, globber=glob.glob):
    """The first candidate device, or None."""
    for pattern in globs:
        matches = sorted(globber(pattern))
        if matches:
            return matches[0]
    return None


# --- transports -------------------------------------------------------------------


class Transport:
    """Interface.

    send() must NEVER block and must NEVER raise. It is called with a lock held, on the
    thread that is also running inference, so a blocking write would turn a serial
    hiccup into vision latency and an exception would end the frame loop. Nothing
    enforces this, so it is written down: every implementation below is non-blocking by
    construction (write_timeout=0, setblocking(False)) and swallows its own errors.

    send() returns a bool - True if the byte went out. That is the mac-side convention
    dropped_writes is built on; it predates piproxy's void Sink.send() and stays.
    """

    name = "none"
    description = "none"

    def send(self, active):
        raise NotImplementedError

    def close(self):
        pass

    @property
    def healthy(self):
        return True

    def status(self):
        return {"kind": self.name, "description": self.description,
                "connected": self.healthy, "dropped": getattr(self, "dropped", 0),
                "buffer_full": getattr(self, "buffer_full", 0)}


class NullTransport(Transport):
    """Stand-in used when no link is configured, so the vision pipeline runs alone."""

    name = "none"
    description = "none"

    def __init__(self):
        self.dropped = 0

    def send(self, active):
        return True


class SerialTransport(Transport):
    """USB serial to the ESP32, which mirrors the state onto a GPIO."""

    name = "serial"
    RETRY_S = 1.0

    def __init__(self, port, baud=BAUD):
        import serial  # lazy: the udp and none paths need no pyserial

        self.port = port
        self.baud = baud
        self.description = f"serial {port} @ {baud}"
        self.ser = None
        self._fd = None
        self.dropped = 0
        self.buffer_full = 0
        self._buffer_full = False
        self.reconnects = 0
        self.last_error = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reconnect_thread = None
        if not self._open():
            raise OSError(f"{port}: {self.last_error}")

    def _open(self):
        import serial

        try:
            # DTR and RTS must be deasserted BEFORE the port opens. On an ESP32 devkit
            # those lines drive the auto-reset circuit - RTS goes to EN - so opening the
            # port the ordinary way asserts RTS and holds the chip in reset for as long
            # as we have it open. The board then looks dead, and the symptom is a silent
            # link rather than an error. Constructing the Serial without a port and
            # opening it after is the only way pyserial lets us set these first.
            ser = serial.Serial()
            ser.port = self.port
            ser.baudrate = self.baud
            ser.timeout = 0
            # write_timeout=0 does NOT do what it looks like it does, and this comment
            # used to claim it did. On the POSIX backend pyserial reads it as
            # "non-blocking", and serialposix.Serial.write() then loops on os.write
            # swallowing EAGAIN with no exit: a full tx buffer makes write() spin
            # forever instead of raising SerialTimeoutException. Reproduced on a pty
            # whose master was never drained - 20000 bytes, then a hang at
            # serialposix.py:621 that only SIGKILL ended.
            #
            # It is still set, because it is what puts the fd in O_NONBLOCK, which is
            # what makes the os.write() in send() below return EAGAIN instead of
            # blocking. The non-blocking guarantee comes from that direct write, not
            # from this line.
            ser.write_timeout = 0
            ser.dtr = False
            ser.rts = False
            ser.open()
            ser.dtr = False
            ser.rts = False
        except Exception as exc:
            self.last_error = str(exc)
            return False
        fd = getattr(ser, "fd", None)
        if fd is None:
            # Only the POSIX backend exposes a raw fd, and it is the only one this
            # project runs on. Refuse rather than silently falling back to the write
            # that can spin.
            try:
                ser.close()
            except Exception:
                pass
            self.last_error = ("this pyserial backend exposes no raw fd; "
                               "macvision needs the POSIX one")
            return False
        self.ser = ser
        self._fd = fd
        self.last_error = None
        return True

    def _drop_connection(self, exc):
        self.last_error = str(exc)
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self._fd = None

    def _reconnect_loop(self):
        """Reopens the port on a thread of its own, and nowhere else.

        This exists because of where send() is called from. pi-agent/piproxy/sinks.py
        reconnects inline, which is safe there: its send() runs on a dedicated emitter
        thread and nothing in that process is latency-ranked. Here send() is called by
        Trigger.update(), on the frame loop's own thread and inside the lock the
        keepalive also takes - so an inline serial.Serial.open(), which is a real device
        open with tcsetattr and ioctl, would stall inference by its full duration every
        RETRY_S for as long as the cable stayed out. That is precisely the "a serial
        hiccup must never become vision latency" rule the Transport docstring states,
        broken by the code meant to honour it.
        """
        while not self._stop.is_set():
            if self._stop.wait(self.RETRY_S):
                return
            if self.ser is not None:
                continue
            if self._open():
                if self._stop.is_set():
                    # close() ran while we were inside the open. Whoever closed us has
                    # already looked at self.ser and moved on, so this handle would be
                    # held by a dying process and the next run would fail with
                    # "Resource busy". Hand it back immediately.
                    with self._lock:
                        try:
                            self.ser.close()
                        except Exception:
                            pass
                        self.ser = None
                        self._fd = None
                    return
                self.reconnects += 1
                print(f"[trigger:serial] reconnected on {self.port}",
                      file=sys.stderr, flush=True)
                return

    def _start_reconnecting(self):
        if self._stop.is_set():
            return
        thread = self._reconnect_thread
        if thread is not None and thread.is_alive():
            return
        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True, name="trigger-serial-reconnect")
        self._reconnect_thread.start()

    def send(self, active):
        with self._lock:
            if self.ser is None:
                # No handle. Count it and return immediately - the reconnect thread is
                # what gets it back. Never block here.
                self._start_reconnecting()
                return self._fail(None)
            try:
                # os.write on the raw fd, NOT self.ser.write(). pyserial's POSIX write
                # busy-loops forever on a full buffer (see the write_timeout comment in
                # _open) - and it would do it while holding this lock AND Trigger._lock,
                # which the frame loop takes on its own thread. That is the exact thing
                # the Transport docstring promises can never happen: inference stops,
                # the keepalive can no longer re-assert the state so the far end's
                # watchdog drops the key anyway, and stop() cannot write its release
                # byte. The fd is already O_NONBLOCK, so this raises BlockingIOError
                # instead, which is a drop we count and resend 20ms later.
                os.write(self._fd, b"\x01" if active else b"\x00")
                if self._buffer_full:
                    self._buffer_full = False
                    print(f"[trigger:serial] {self.port} tx buffer drained; writing "
                          f"again [{self.buffer_full} dropped]",
                          file=sys.stderr, flush=True)
                return True
            except BlockingIOError:
                # The tx buffer is full. Safe to drop: the state is level-triggered and
                # goes out again on the next keepalive. Not a reason to tear down a port
                # that is merely busy, and not a reason to say anything more than once -
                # this fires on every write for as long as it lasts, so logging per
                # event buries the machine in stderr (536,000 lines in a 3s repro).
                # Report the two transitions instead, and keep the count in status().
                self.dropped += 1
                self.buffer_full += 1
                if not self._buffer_full:
                    self._buffer_full = True
                    print(f"[trigger:serial] {self.port} tx buffer full; dropping "
                          f"writes until it drains", file=sys.stderr, flush=True)
                return False
            except Exception as exc:
                # Every failure lands here, not just timeouts. A timeout means a full
                # buffer and is safe to drop - the state is resent within 20ms. An
                # OSError means the device is gone, and the only useful response is to
                # let go of it and start looking again. Catching only
                # SerialTimeoutException, as this did, let an unplugged ESP32 raise
                # SerialException straight out of the frame loop: the vision pipeline
                # died because a USB cable moved. pi-agent/piproxy/sinks.py learned this
                # in commit c13dfac; the lesson never crossed to the Mac until now.
                if not getattr(self.ser, "is_open", False) or isinstance(exc, OSError):
                    self._drop_connection(exc)
                    self._start_reconnecting()
                return self._fail(exc)

    def _fail(self, exc):
        self.dropped += 1
        # The counter alone is not enough. Without a message a permanently dead link is
        # invisible, which is the exact failure commit 3e8a1a4 refuses.
        if exc is not None and (self.dropped in (1, 10) or self.dropped % 500 == 0):
            print(f"[trigger:serial] write failed ({exc}); continuing "
                  f"[{self.dropped} so far]", file=sys.stderr, flush=True)
        return False

    @property
    def healthy(self):
        return self.ser is not None

    def close(self):
        self._stop.set()
        if self._reconnect_thread is not None:
            # RETRY_S long, so a thread parked inside a real device open can outlive
            # this join. The re-check below is what stops it leaving a handle behind.
            self._reconnect_thread.join(timeout=self.RETRY_S + 0.5)
        with self._lock:
            if self.ser is not None:
                try:
                    # Discard whatever is still queued before writing the release.
                    # Trigger.stop() already sent it, but if the buffer was full that
                    # write was dropped - and the stale backlog still ends in whatever
                    # the state was, so a far end reading it later would see the key
                    # held by a process that has exited. Flushing makes the release
                    # provably the last byte queued even on a wedged link.
                    self.ser.reset_output_buffer()
                except Exception:
                    pass
                try:
                    os.write(self._fd, b"\x00")
                except Exception:
                    pass
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
                self._fd = None


class UdpTransport(Transport):
    """One datagram per update to the Pi agent.

    Fire-and-forget is the right fit, not a weakness: the state is re-sent every
    20ms, so there is never anything worth retransmitting - a newer truth is always
    already on its way. A dropped datagram costs at most one keepalive interval.
    """

    name = "udp"

    def __init__(self, host, port=PI_TRIGGER_PORT):
        self.host = host
        self.dropped = 0
        # Resolve ONCE, here. self.addr used to hold the hostname, so every datagram -
        # one per frame plus 50 a second from the keepalive, all inside the lock that
        # update() contends for - performed a name lookup. The documented target is
        # raspberrypi.local, i.e. an mDNS round trip through mDNSResponder on macOS, on
        # the one call this project spends care on. The trade is explicit: the address
        # is pinned for the run, so a DHCP change mid-session needs a restart, and
        # `description` carries both names so the pinning is visible.
        try:
            info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
            self.addr = info[0][4]
            shown = self.addr[0]
        except OSError as exc:
            # Not fatal: the name may resolve later, and a trigger that refuses to exist
            # is worse than one that says so and keeps trying.
            self.addr = (host, port)
            shown = "unresolved"
            print(f"[trigger:udp] could not resolve {host} ({exc}); sending by name",
                  file=sys.stderr, flush=True)
        self.description = (f"udp {host}:{port}" if shown == host
                            else f"udp {host} ({shown}):{port}")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        # The socket stays UNCONNECTED. connect() + send() is marginally faster, but it
        # surfaces ICMP port-unreachable as ECONNREFUSED on a later send, which the
        # broad OSError catch below would swallow into a counter - turning a diagnosable
        # condition into a silent one.

    def send(self, active):
        try:
            self.sock.sendto(b"\x01" if active else b"\x00", self.addr)
            return True
        except OSError:
            # No route, interface down, buffer full. Same reasoning as a dropped
            # serial write: the keepalive resends within 20ms, and the Pi's watchdog
            # releases the key if the stream really has stopped.
            self.dropped += 1
            return False

    def close(self):
        self.sock.close()


def build_transport(kind, **kwargs):
    """The pure factory. Unlike open_trigger, this one RAISES.

    The asymmetry with pi-agent/piproxy's build_sink is deliberate: there a bad sink is
    fatal because output is the whole point of the process, while here a missing link
    must never stop the vision pipeline (mac-app/README.md). open_trigger is the layer
    that turns every failure here into a NullTransport.
    """
    if kind == "none":
        return NullTransport()
    if kind == "serial":
        port = kwargs.get("port")
        if not port:
            raise ValueError("the serial transport needs a port "
                             "(e.g. /dev/cu.usbserial-0001)")
        return SerialTransport(port, baud=kwargs.get("baud", BAUD))
    if kind == "udp":
        host = kwargs.get("host")
        if not host:
            raise ValueError("the udp transport needs a host "
                             "(e.g. raspberrypi.local)")
        return UdpTransport(host, port=kwargs.get("port", PI_TRIGGER_PORT))
    raise ValueError(f"unknown transport {kind!r} (expected: none, serial, udp)")


def parse_target(target):
    """TRIGGER_TARGET -> a plain dict. Pure: no sockets, no globbing, no printing.

    Never raises, for any input at all. urlparse defers port validation to attribute
    access, so udp://h:99999 and udp://h:abc both raise ValueError on `parsed.port` -
    which used to escape as an uncaught traceback out of the one factory in this file
    whose whole contract is that it cannot fail.

    -> {"kind": "udp"|"serial"|"auto"|"none"|"unknown", host, port, path, raw, reason}
    """
    out = {"kind": "unknown", "host": None, "port": None, "path": None,
           "raw": target, "reason": None}

    if target is None:
        out["kind"] = "none"
        return out

    # Strip and casefold ONLY for the bare keywords and the scheme. A device path is
    # case-sensitive and a hostname must not be mangled.
    bare = target.strip().casefold()
    if bare in ("none", "null", ""):
        out["kind"] = "none"
        return out
    if bare == "auto":
        out["kind"] = "auto"
        return out

    if bare.startswith("udp://"):
        stripped = target.strip()
        try:
            parsed = urlparse(stripped)
            port = parsed.port
        except ValueError as exc:
            out["reason"] = str(exc)
            return out
        if not parsed.hostname:
            out["reason"] = "no host in the udp:// URL"
            return out
        out.update(kind="udp", host=parsed.hostname, port=port or PI_TRIGGER_PORT)
        return out

    if bare.startswith("serial://"):
        # The slice, NOT urlparse: it is what makes serial:///dev/cu.x yield
        # /dev/cu.x with its leading slash. urlparse gives hostname None and a path
        # that has already lost the distinction we need.
        out.update(kind="serial", path=target.strip()[len("serial://"):])
        return out

    out["reason"] = "unrecognised scheme"
    return out


# --- the trigger itself -------------------------------------------------------------


class Trigger:
    """Holds the trigger state and pushes it over a transport.

    Writes happen on two paths: update() writes immediately on the caller's thread
    (this is the latency-critical path - it runs right after inference, before any
    drawing), and a daemon thread rewrites the same state every keepalive_s so the
    link stays alive while the screen is static.
    """

    def __init__(self, transport, keepalive_s=KEEPALIVE_S):
        self.transport = transport
        self.keepalive_s = keepalive_s
        self.dropped_writes = 0
        self._active = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="trigger-keepalive")

    def start(self):
        """Send the released state, then start the keepalive. Idempotent."""
        if self._thread.is_alive() or self._stop.is_set():
            return
        # This first 0x00 is load-bearing twice: it puts the far end in a known state so
        # a previous run's key cannot stay held, and it is the first byte the Pi ever
        # sees, which is what arms piproxy's TriggerWatchdog - that stays disarmed while
        # last_seen == 0.0.
        self._send(False)
        if isinstance(self.transport, NullTransport):
            return  # nothing to keep alive
        self._thread.start()

    def update(self, active):
        """The latency-critical path. Runs on the caller's thread, right after inference."""
        # Assigned BEFORE the send and deliberately outside the lock: if the keepalive
        # fires in this window it re-sends the NEW state, never the old one. Reversed,
        # there is a window where the keepalive re-asserts the stale state after the
        # fresh one has gone out - a key that visibly flickers. Bool assignment is
        # atomic in CPython.
        self._active = active
        self._send(active)

    def _send(self, active):
        with self._lock:
            # The lock is held across transport.send(), which is safe only because every
            # transport write is non-blocking - see the Transport docstring.
            try:
                if not self.transport.send(active):
                    self.dropped_writes += 1
            except Exception as exc:
                # No exception from a transport may ever reach the frame loop or kill
                # the keepalive thread.
                self.dropped_writes += 1
                if self.dropped_writes in (1, 10) or self.dropped_writes % 500 == 0:
                    print(f"[trigger] transport raised ({exc}); continuing "
                          f"[{self.dropped_writes} so far]", file=sys.stderr, flush=True)

    def _loop(self):
        # A timed wait on the stop Event, never time.sleep(), so stop() is immediate
        # rather than up to one interval late. This is TriggerWatchdog's idiom from
        # pi-agent/piproxy/sinks.py; the Emitter's _wake.wait() idiom next to it is
        # deliberately NOT used here - see this module's docstring - so that the code
        # does not read as a half-finished port to a shape this file rejects.
        while not self._stop.wait(self.keepalive_s):
            self._send(self._active)

    @property
    def alive(self):
        return self._thread.is_alive()

    def stop(self):
        """Release the key and shut the link down, in an order that cannot lose a race.

        NOT the guaranteed release path, and it must not be advertised as one: a crash
        or a SIGKILL still skips it. The far end's 250ms watchdog remains the real
        guarantee - the ESP32's GPIO fail-low, or piproxy's TriggerWatchdog.
        """
        self._active = False          # first, so a late wake re-asserts the release
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)   # join BEFORE the release: nothing may
                                             # write after it
        self._send(False)             # provably the last byte on the wire
        self.transport.close()

    # Kept for muscle memory: receiver.py called close(), and external tools may too.
    close = stop

    def status(self):
        # Takes NO lock. dropped_writes is read lock-free precisely so polling never
        # contends with update().
        st = self.transport.status()
        st.update(active=self._active, dropped_writes=self.dropped_writes,
                  alive=self.alive)
        return st


def open_trigger(target=_UNSET, *, baud=BAUD, keepalive_s=KEEPALIVE_S):
    """Open the link described by TRIGGER_TARGET, or fall back to a no-op.

    Never raises, on any branch. Always returns a started Trigger.
    """
    if target is _UNSET or target is None:
        # None is accepted as well as the sentinel: the old signature was
        # open_trigger(target=None) with `target or os.environ...`, so passing None
        # explicitly has always meant "fall back to the environment". The sentinel
        # exists only to tell an explicit "" apart from an absent argument.
        # Read at CALL time, never at import time. And a sentinel rather than
        # `target or ...`, so an explicit open_trigger("") is distinguishable from an
        # absent argument.
        target = os.environ.get("TRIGGER_TARGET", "auto")

    spec = parse_target(target)
    kind = spec["kind"]

    if kind == "none":
        if not target.strip():
            # It used to say "TRIGGER_TARGET=none" here, reporting a value the user
            # never typed. Given commit 3e8a1a4 exists because a quiet notice about a
            # missing trigger scrolled past unnoticed, this was the last path that
            # failed quietly and lied about why.
            print("[trigger] disabled (TRIGGER_TARGET is empty)")
        else:
            print(f"[trigger] disabled (TRIGGER_TARGET={target.strip()})")
        return _start(NullTransport(), keepalive_s)

    if kind == "unknown":
        print(f"[trigger] unrecognised TRIGGER_TARGET={target!r} ({spec['reason']}; "
              f"expected udp://host:port, serial:///dev/..., auto, or none) "
              f"- running without a trigger link", file=sys.stderr)
        return _start(NullTransport(), keepalive_s)

    if kind == "udp":
        try:
            transport = build_transport("udp", host=spec["host"], port=spec["port"])
        except Exception as exc:
            print(f"[trigger] could not open {target!r}: {exc} "
                  "- running without a trigger link", file=sys.stderr)
            return _start(NullTransport(), keepalive_s)
        trigger = _start(transport, keepalive_s)
        # No connection to fail here: UDP to an unreachable host looks exactly like
        # UDP to a host that is simply not listening yet. Check the Pi's HTTP
        # /status endpoint to confirm packets are actually arriving.
        print(f"[trigger] sending to {transport.description}")
        return trigger

    if kind == "serial":
        return _open_serial(spec["path"], baud, keepalive_s)

    # kind == "auto"
    candidates = list_ports()
    if not candidates:
        _print_no_link_banner()
        return _start(NullTransport(), keepalive_s)
    trigger = _open_serial(candidates[0], baud, keepalive_s, candidates=candidates)
    return trigger


def _open_serial(path, baud, keepalive_s, candidates=None):
    if not path:
        # An explicit bare "serial://". It must NOT get the auto banner below, whose
        # text hard-codes "TRIGGER_TARGET is 'auto'" and would tell someone who typed
        # serial:// that they typed something else - wrong in the one message where
        # being precisely right matters most.
        print("[trigger] TRIGGER_TARGET=serial:// names no device "
              "(expected serial:///dev/cu.usbserial-0001) - running without a trigger "
              "link", file=sys.stderr)
        return _start(NullTransport(), keepalive_s)
    try:
        transport = build_transport("serial", port=path, baud=baud)
    except Exception as exc:
        print(f"[trigger] could not open {path}: {exc} - running without a trigger link",
              file=sys.stderr)
        return _start(NullTransport(), keepalive_s)
    trigger = _start(transport, keepalive_s)
    if candidates:
        # "auto" will happily stream 0x01/0x00 at 50Hz into ANY USB-serial device on
        # this Mac - a Pro Micro, a 3D printer, a debug probe. pi-agent grew a name
        # blocklist for the mirror-image hazard in commit 7adfbd7; the least this can do
        # is say how many other things it could have picked.
        print(f"[trigger] connected on {transport.description} "
              f"({len(candidates)} candidate{'s' if len(candidates) != 1 else ''} found)")
    else:
        print(f"[trigger] connected on {transport.description}")
    return trigger


def _start(transport, keepalive_s):
    """Construct AND start, so no caller can forget.

    A Trigger that was constructed but never started has no keepalive, which means the
    key releases itself the instant the PC's screen goes static - the exact failure
    KEEPALIVE_S exists to prevent, and it would look like a vision bug.
    """
    trigger = Trigger(transport, keepalive_s=keepalive_s)
    try:
        trigger.start()
    except Exception as exc:
        print(f"[trigger] could not start the keepalive ({exc}) "
              "- running without a trigger link", file=sys.stderr)
        fallback = Trigger(NullTransport(), keepalive_s=keepalive_s)
        fallback.start()   # the whole point of this function is that nobody forgets
        return fallback
    return trigger


def _print_no_link_banner():
    # Loud on purpose. "auto" looks for a serial port *on this Mac*, which is
    # correct only for the original ESP32-on-the-Mac wiring. Once the link
    # moved to the Pi there is nothing here to find, and the old one-line
    # notice scrolled past unnoticed while the vision pipeline ran perfectly
    # and pressed nothing - the hardest kind of failure to trace, because
    # every part you would think to check is working.
    print("\n" + "=" * 72)
    print("  NO TRIGGER LINK - detections will not press anything.")
    print()
    print("  TRIGGER_TARGET is 'auto', which looks for a USB serial device on")
    print("  this Mac, and there is none. If the keyboard proxy runs on the")
    print("  Raspberry Pi, point at it explicitly:")
    print()
    print("      TRIGGER_TARGET=udp://raspberrypi.local:48010 python3 -m macvision")
    print()
    print("  Set TRIGGER_TARGET=none to silence this and run vision only.")
    print("=" * 72 + "\n")
