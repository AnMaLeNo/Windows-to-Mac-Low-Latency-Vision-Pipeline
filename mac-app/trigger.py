"""Mac -> ESP32 trigger link.

One byte of state per update: 0x01 = "a person covers the ROI's centre pixel", 0x00 = not.
The ESP32 mirrors that byte onto a GPIO, which the Pro Micro turns into a held key on the
Windows PC. See docs/TRIGGER.md for wiring and the full rationale.
"""

import glob
import threading
import time
from typing import Optional

import serial

# The bridge chip on a classic ESP32 devkit (CP2102/CH340) still runs a real UART between
# itself and the MCU, so the byte costs 1/BAUD * 10 bits on that hop: 87us at 115200. Raising
# this to 921600 would save ~78us of a pipeline measured in tens of milliseconds, at the cost
# of CH340 clones that get flaky at high rates. Not a trade worth making.
BAUD = 115200

# Resend the current state this often even when nothing changes. This is not just a
# keepalive for the ESP32 watchdog - it is required for correctness: DXGI Desktop
# Duplication only produces a frame when the screen actually changes, so a static screen
# means the receive loop blocks and no frame-driven update happens. Without an independent
# timer the key would release itself whenever the PC's screen stopped moving.
KEEPALIVE_S = 0.020

# macOS exposes two device nodes per serial port. Always use the "cu." (call-unit) one:
# opening "tty.*" blocks until DCD is asserted, which a USB-serial bridge never does, so
# the open would hang forever.
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


class Trigger:
    """Holds the trigger state and pushes it to the ESP32.

    Writes happen on two paths: `update()` writes immediately on the caller's thread (this
    is the latency-critical path - it runs right after inference, before any drawing), and a
    daemon thread rewrites the same state every KEEPALIVE_S so the link stays alive while
    the screen is static.
    """

    def __init__(self, port: str, baud: int = BAUD):
        # write_timeout=0 makes writes non-blocking. If the ESP32 ever stops draining its
        # input the kernel's tx buffer fills up, and a blocking write would stall the
        # detection loop itself - a serial hiccup must never become vision latency.
        self.ser = serial.Serial(port, baud, timeout=0, write_timeout=0)
        self.port = port
        self.active = False
        self.dropped_writes = 0
        self._lock = threading.Lock()
        self._send(False)
        threading.Thread(target=self._keepalive_loop, daemon=True).start()

    def _send(self, active: bool) -> None:
        with self._lock:
            try:
                self.ser.write(b"\x01" if active else b"\x00")
            except serial.SerialTimeoutException:
                # Buffer full. Dropping this byte is safe: the keepalive thread resends the
                # current state within KEEPALIVE_S, and the ESP32's watchdog fails the GPIO
                # low if the stream really has died.
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
        self.ser.close()


class NullTrigger:
    """Stand-in used when no ESP32 is plugged in, so the vision pipeline still runs alone."""

    port = None
    dropped_writes = 0

    def __init__(self):
        self.active = False

    def update(self, active: bool) -> None:
        self.active = active

    def close(self) -> None:
        pass


def open_trigger(port: Optional[str] = None):
    """Open the link, or fall back to a no-op so the receiver runs without the hardware."""
    port = port or find_port()
    if port is None:
        print("[trigger] no serial device found - running without the ESP32 link")
        return NullTrigger()
    try:
        trigger = Trigger(port)
    except serial.SerialException as exc:
        print(f"[trigger] could not open {port}: {exc} - running without the ESP32 link")
        return NullTrigger()
    print(f"[trigger] connected on {port} at {BAUD} baud")
    return trigger
