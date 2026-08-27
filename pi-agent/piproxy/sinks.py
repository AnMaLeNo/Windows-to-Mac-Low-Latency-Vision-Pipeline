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
    """Sends reports over a UART to the Pro Micro.

    Framing matters here in a way it did not for the old one-byte trigger link: a
    dropped byte would desynchronise every following report, so each one is wrapped
    in a start byte and a checksum and the receiver resynchronises on the next
    header it recognises.
    """

    name = "serial"
    START = 0xAB

    def __init__(self, port: str, baud: int = 115200):
        import serial  # imported lazily so the log sink needs no pyserial

        # write_timeout=0 makes writes non-blocking. If the Pro Micro ever stops
        # draining, a blocking write would stall the thread that owns keyboard
        # input - a serial hiccup must never become typing latency.
        self.ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
        self.port = port
        self.baud = baud
        self.dropped = 0
        self._lock = threading.Lock()

    def send(self, report: bytes) -> None:
        checksum = 0
        for b in report:
            checksum ^= b
        frame = bytes([self.START]) + report + bytes([checksum])
        import serial

        with self._lock:
            try:
                self.ser.write(frame)
            except serial.SerialTimeoutException:
                # Safe to drop: the Mac's keepalive means the current state is resent
                # within 20ms, and the Pro Micro's own watchdog releases everything if
                # the stream really has stopped.
                self.dropped += 1

    def close(self) -> None:
        try:
            self.send(bytes(8))
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
        self._thread = threading.Thread(target=self._loop, daemon=True, name="emitter")

    def start(self) -> None:
        self._thread.start()

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
            self.sink.send(report)

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
