"""Where the merged HID report goes.

The output is the one part of this system whose hardware is still open (see
pi-agent/README.md), so it sits behind a two-method interface. Everything upstream -
evdev capture, the merge, the network trigger, the watchdog - is written once and
does not care which sink is attached.

    log     nothing is wired up yet; print reports. Lets the whole pipeline be
            tested end to end with no hardware at all.
    serial  UART to a Pro Micro that is the USB HID keyboard on the Windows PC.
    hidg    this machine is itself the USB gadget (/dev/hidg0). Needs a Pi whose
            USB-C port is free for peripheral mode - not the case on this server.
"""

import os
import sys
import threading
import time
from typing import Optional

from .report import describe


class Sink:
    """Interface. `send` must be safe to call from several threads."""

    name = "none"

    def send(self, report: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    @property
    def healthy(self) -> bool:
        return True


class LogSink(Sink):
    """Prints reports instead of emitting them. Deduplicates by default: at a 20ms
    keepalive the same report repeats 50x/second, which would bury anything else in
    the log without saying anything new."""

    name = "log"

    def __init__(self, echo_repeats: bool = False):
        self.echo_repeats = echo_repeats
        self._last: Optional[bytes] = None
        self._lock = threading.Lock()
        self.sent = 0

    def send(self, report: bytes) -> None:
        with self._lock:
            self.sent += 1
            if report == self._last and not self.echo_repeats:
                return
            self._last = report
        print(f"[sink:log] {describe(report)}", flush=True)


class SerialSink(Sink):
    """Sends reports over a UART to the Pro Micro, and survives the cable moving.

    Framing matters here in a way it did not for the old one-byte trigger link: a
    dropped byte would desynchronise every following report, so each one is wrapped
    in a start byte and a checksum and the receiver resynchronises on the next
    header it recognises.

    Reconnection is not a nicety. Unplugging the USB bridge makes every write raise,
    and Linux hands the device a *different* name when it comes back - ttyUSB0
    becomes ttyUSB1 - so a sink pinned to one path stays broken even once the
    hardware is back. Meanwhile the keyboard is still grabbed, so the user has no
    keyboard at all and nothing says why.
    """

    name = "serial"
    START = 0xAB
    RETRY_S = 1.0

    def __init__(self, port: str, baud: int = 115200):
        import serial  # noqa: F401 - imported here so the log sink needs no pyserial

        self.port_spec = port
        self.baud = baud
        self.ser = None
        self.port = None
        self.dropped = 0
        self.reconnects = 0
        self.last_error = None
        self._lock = threading.Lock()
        self._next_retry = 0.0
        if not self._open():
            # Not fatal. At boot this service can easily start before USB has
            # enumerated, and a sink that refuses to exist until the cable is
            # perfect is worse than one that says so and keeps trying.
            print(f"[sink:serial] {self.port_spec} not available yet "
                  f"({self.last_error}); retrying every {self.RETRY_S:.0f}s",
                  file=sys.stderr, flush=True)

    def _resolve(self):
        """The configured path, or any device of the same kind if it has moved."""
        import glob as _glob

        if os.path.exists(self.port_spec):
            return self.port_spec
        matches = sorted(_glob.glob(self.port_spec))
        if matches:
            return matches[0]
        # The exact node is gone. Accept the same class of device under a new name,
        # which is what a replug produces, rather than requiring a config edit.
        for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
            found = sorted(_glob.glob(pattern))
            if found:
                return found[0]
        return None

    def _open(self) -> bool:
        import serial

        path = self._resolve()
        if path is None:
            self.last_error = "no serial device present"
            return False
        try:
            # DTR and RTS must be deasserted BEFORE the port opens. On an ESP32
            # devkit those lines drive the auto-reset circuit - RTS goes to EN - so
            # opening the port the ordinary way asserts RTS and holds the chip in
            # reset for as long as we have it open. The board then looks dead, and
            # the symptom is a silent link rather than an error. Constructing the
            # Serial without a port and opening it after is the only way pyserial
            # lets us set these first.
            ser = serial.Serial()
            ser.port = path
            ser.baudrate = self.baud
            ser.timeout = 0
            # write_timeout=0 makes writes non-blocking. If the bridge ever stops
            # draining, a blocking write would stall the thread that owns keyboard
            # input - a serial hiccup must never become typing latency.
            ser.write_timeout = 0
            ser.dtr = False
            ser.rts = False
            ser.open()
            ser.dtr = False
            ser.rts = False
        except Exception as exc:
            self.last_error = str(exc)
            return False
        self.ser = ser
        self.port = path
        self.last_error = None
        return True

    def _drop_connection(self, exc) -> None:
        self.last_error = str(exc)
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None
        self._next_retry = time.monotonic() + self.RETRY_S

    def send(self, report: bytes) -> None:
        checksum = 0
        for b in report:
            checksum ^= b
        frame = bytes([self.START]) + report + bytes([checksum])

        with self._lock:
            if self.ser is None:
                if time.monotonic() < self._next_retry:
                    self.dropped += 1
                    return
                self._next_retry = time.monotonic() + self.RETRY_S
                if not self._open():
                    self.dropped += 1
                    return
                self.reconnects += 1
                print(f"[sink:serial] reconnected on {self.port}",
                      file=sys.stderr, flush=True)

            try:
                self.ser.write(frame)
            except Exception as exc:
                # Every failure lands here, not just timeouts. A timeout means a full
                # buffer and is safe to drop - the state is resent within 20ms. An
                # OSError means the device is gone, and the only useful response is
                # to let go of it and start looking again. Distinguishing them buys
                # nothing: neither may propagate, because this runs on the thread
                # that is the sole path to the user's keyboard.
                self.dropped += 1
                if self.ser is not None and not getattr(self.ser, "is_open", False):
                    self._drop_connection(exc)
                elif isinstance(exc, OSError) or "Input/output" in str(exc):
                    self._drop_connection(exc)
                    print(f"[sink:serial] link lost ({exc}); will reconnect",
                          file=sys.stderr, flush=True)

    @property
    def healthy(self) -> bool:
        return self.ser is not None

    def close(self) -> None:
        try:
            self.send(bytes(8))
        except Exception:
            pass
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass


class HidGadgetSink(Sink):
    """Writes straight into a USB HID gadget node, making this machine the keyboard.

    Requires the USB-C port in peripheral mode (`dtoverlay=dwc2,dr_mode=peripheral`)
    and a configfs gadget exporting /dev/hidg0 - see setup-gadget.sh.
    """

    name = "hidg"

    def __init__(self, device: str = "/dev/hidg0"):
        # Unbuffered: a buffered write could sit in userspace while the key it
        # represents is meant to already be down on the PC.
        self.fd = open(device, "wb", buffering=0)
        self.device = device
        self.dropped = 0
        self._lock = threading.Lock()

    def send(self, report: bytes) -> None:
        with self._lock:
            try:
                self.fd.write(report)
            except BlockingIOError:
                self.dropped += 1
            except OSError as exc:
                # ESHUTDOWN/EAGAIN here means the host is not listening - the PC is
                # off, asleep, or the cable is out. Not fatal: it comes back when the
                # host does, and the next keepalive re-sends the current state.
                self.dropped += 1
                if self.dropped % 500 == 1:
                    print(f"[sink:hidg] write failed ({exc}); host not attached?",
                          file=sys.stderr, flush=True)

    def close(self) -> None:
        try:
            self.fd.write(bytes(8))
            self.fd.close()
        except Exception:
            pass


def build_sink(kind: str, **kwargs) -> Sink:
    if kind == "log":
        return LogSink(echo_repeats=kwargs.get("echo_repeats", False))
    if kind == "serial":
        port = kwargs.get("port")
        if not port:
            raise ValueError("the serial sink needs --serial-port (e.g. /dev/ttyUSB0)")
        return SerialSink(port, baud=kwargs.get("baud", 1_000_000))
    if kind == "hidg":
        return HidGadgetSink(kwargs.get("device", "/dev/hidg0"))
    raise ValueError(f"unknown sink {kind!r} (expected: log, serial, hidg)")


class Emitter:
    """Owns the only thread that talks to the sink.

    Sends on two paths, exactly mirroring the design the ESP32 link already used
    (docs/TRIGGER.md): immediately when the state changes, so a keypress leaves the
    moment it exists, and again every `keepalive_s` so the link stays idempotent and
    the downstream watchdog stays fed. Nothing here is edge-triggered, so a lost
    report corrects itself on the next tick instead of desynchronising the two ends
    forever.
    """

    def __init__(self, state, sink: Sink, keepalive_s: float = 0.020):
        self.state = state
        self.sink = sink
        self.keepalive_s = keepalive_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last: Optional[bytes] = None
        self.errors = 0
        self._thread = threading.Thread(target=self._loop, daemon=True, name="emitter")

    def start(self) -> None:
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def nudge(self) -> None:
        """Called after any state change to send without waiting for the keepalive."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Waking on either the event or the timeout is what merges the two paths
            # into one thread: a state change sends now, silence sends every 20ms.
            self._wake.wait(self.keepalive_s)
            self._wake.clear()
            report = self.state.build()
            self._last = report
            try:
                self.sink.send(report)
            except Exception as exc:
                # This thread is the only route from a keypress to the PC. If it
                # dies the process stays up, systemd still reports "active", and
                # the keyboard stays grabbed - so the user loses their keyboard
                # entirely, with nothing anywhere saying why. That happened once,
                # to an OSError from a replugged USB bridge. No sink failure may
                # ever end this loop again.
                self.errors += 1
                if self.errors in (1, 10) or self.errors % 500 == 0:
                    print(f"[emitter] sink raised ({exc}); continuing "
                          f"[{self.errors} so far]", file=sys.stderr, flush=True)

    def stop(self) -> None:
        """Release every key on the way out. A crash skips this, which is what the
        downstream watchdog exists for."""
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=1.0)
        self.sink.close()

    @property
    def last_report(self) -> Optional[bytes]:
        return self._last


class TriggerWatchdog:
    """Releases the trigger's keys when the Mac goes quiet.

    The Mac re-sends its state every 20ms, so silence past this timeout means the
    vision app crashed, was killed, or lost the network. Without this, its last
    "key down" would be held forever by a process that no longer exists. This is the
    same 250ms rule the ESP32 firmware enforced, moved up a layer.
    """

    def __init__(self, state, emitter: Emitter, timeout_s: float = 0.250):
        self.state = state
        self.emitter = emitter
        self.timeout_s = timeout_s
        self.last_seen = 0.0
        self.fired = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="watchdog")

    def start(self) -> None:
        self._thread.start()

    def feed(self) -> None:
        self.last_seen = time.monotonic()

    def _loop(self) -> None:
        while not self._stop.wait(self.timeout_s / 4):
            if self.last_seen == 0.0:
                continue  # the Mac has never connected; nothing to time out yet
            if time.monotonic() - self.last_seen > self.timeout_s:
                if self.state.snapshot()["trigger_keys"]:
                    self.fired += 1
                    print("[watchdog] no trigger update; releasing trigger keys",
                          file=sys.stderr, flush=True)
                    self.state.set_trigger([])
                    self.emitter.nudge()

    def stop(self) -> None:
        self._stop.set()

    @property
    def stale(self) -> bool:
        if self.last_seen == 0.0:
            return True
        return (time.monotonic() - self.last_seen) > self.timeout_s
