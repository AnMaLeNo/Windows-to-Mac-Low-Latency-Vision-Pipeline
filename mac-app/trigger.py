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
"""

import glob
import os
import socket
import threading
import time
from typing import Optional
from urllib.parse import urlparse

# The bridge chip on a classic ESP32 devkit (CP2102/CH340) still runs a real UART
# between itself and the MCU, so the byte costs 1/BAUD * 10 bits on that hop: 87us at
# 115200. Raising this to 921600 would save ~78us of a pipeline measured in tens of
# milliseconds, at the cost of CH340 clones that get flaky at high rates.
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


def find_port() -> Optional[str]:
    for pattern in PORT_GLOBS:
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[0]
    return None


# --- transports -------------------------------------------------------------------


class SerialTransport:
    """USB serial to the ESP32, which mirrors the state onto a GPIO."""

    def __init__(self, port: str, baud: int = BAUD):
        import serial

        # write_timeout=0 makes writes non-blocking. If the ESP32 ever stops draining
        # its input the kernel's tx buffer fills up, and a blocking write would stall
        # the detection loop itself - a serial hiccup must never become vision latency.
        self.ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
        self.description = f"serial {port} @ {baud}"

    def send(self, active: bool) -> bool:
        import serial

        try:
            self.ser.write(b"\x01" if active else b"\x00")
            return True
        except serial.SerialTimeoutException:
            return False

    def close(self) -> None:
        self.ser.close()


class UdpTransport:
    """One datagram per update to the Pi agent.

    Fire-and-forget is the right fit, not a weakness: the state is re-sent every
    20ms, so there is never anything worth retransmitting - a newer truth is always
    already on its way. A dropped datagram costs at most one keepalive interval.
    """

    def __init__(self, host: str, port: int = PI_TRIGGER_PORT):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.description = f"udp {host}:{port}"

    def send(self, active: bool) -> bool:
        try:
            self.sock.sendto(b"\x01" if active else b"\x00", self.addr)
            return True
        except OSError:
            # No route, interface down, buffer full. Same reasoning as a dropped
            # serial write: the keepalive resends within 20ms, and the Pi's watchdog
            # releases the key if the stream really has stopped.
            return False

    def close(self) -> None:
        self.sock.close()


# --- the trigger itself -------------------------------------------------------------


class Trigger:
    """Holds the trigger state and pushes it over a transport.

    Writes happen on two paths: `update()` writes immediately on the caller's thread
    (this is the latency-critical path - it runs right after inference, before any
    drawing), and a daemon thread rewrites the same state every KEEPALIVE_S so the
    link stays alive while the screen is static.
    """

    def __init__(self, transport):
        self.transport = transport
        self.port = getattr(transport, "description", "?")
        self.active = False
        self.dropped_writes = 0
        self._lock = threading.Lock()
        self._send(False)
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def _send(self, active: bool) -> None:
        with self._lock:
            if not self.transport.send(active):
                self.dropped_writes += 1

    def update(self, active: bool) -> None:
        self.active = active
        self._send(active)

    def _keepalive_loop(self) -> None:
        while True:
            time.sleep(KEEPALIVE_S)
            self._send(self.active)

    def close(self) -> None:
        self._send(False)
        self.transport.close()


class NullTrigger:
    """Stand-in used when no link is configured, so the vision pipeline runs alone."""

    port = None
    dropped_writes = 0

    def __init__(self):
        self.active = False

    def update(self, active: bool) -> None:
        self.active = active

    def close(self) -> None:
        pass


def open_trigger(target: Optional[str] = None):
    """Open the link described by TRIGGER_TARGET, or fall back to a no-op."""
    target = target or os.environ.get("TRIGGER_TARGET", "auto")

    if target in ("none", "null", ""):
        print("[trigger] disabled (TRIGGER_TARGET=none)")
        return NullTrigger()

    if target.startswith("udp://"):
        parsed = urlparse(target)
        if not parsed.hostname:
            print(f"[trigger] malformed {target!r}, expected udp://host[:port]"
                  " - running without a trigger link")
            return NullTrigger()
        transport = UdpTransport(parsed.hostname, parsed.port or PI_TRIGGER_PORT)
        # No connection to fail here: UDP to an unreachable host looks exactly like
        # UDP to a host that is simply not listening yet. Check the Pi's HTTP
        # /status endpoint to confirm packets are actually arriving.
        trigger = Trigger(transport)
        print(f"[trigger] sending to {transport.description}")
        return trigger

    if target.startswith("serial://") or target == "auto":
        port = target[len("serial://"):] if target.startswith("serial://") else find_port()
        if not port:
            print("[trigger] no serial device found - running without a trigger link")
            return NullTrigger()
        try:
            transport = SerialTransport(port)
        except Exception as exc:
            print(f"[trigger] could not open {port}: {exc} - running without a trigger link")
            return NullTrigger()
        trigger = Trigger(transport)
        print(f"[trigger] connected on {transport.description}")
        return trigger

    print(f"[trigger] unrecognised TRIGGER_TARGET={target!r} "
          "(expected udp://host:port, serial:///dev/..., auto, or none)")
    return NullTrigger()
