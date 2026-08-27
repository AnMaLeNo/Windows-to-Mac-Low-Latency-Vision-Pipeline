"""Reads the real keyboard off the Logitech receiver and feeds it into KeyState.

Two things make this more than a read loop:

EVIOCGRAB. Without it the Pi *also* processes every keystroke - it would be typing
into its own consoles and any local session while forwarding to the PC. Grabbing
gives this process exclusive delivery, so keys go to the Windows PC and nowhere else.

Hot-plug. The receiver can be unplugged, the keyboard sleeps, USB re-enumerates.
Every one of those looks like a read error on a file descriptor, and the safe
response is always the same: drop every physical key first (so nothing stays held
on the PC by a keyboard that is no longer there), then go back to looking for it.
"""

import errno
import glob
import os
import select
import sys
import threading
import time
from typing import List, Optional

from .keymap import HID_USAGE, MODIFIER_BITS

# evdev event types and key values we care about.
EV_KEY = 0x01
VALUE_UP, VALUE_DOWN, VALUE_REPEAT = 0, 1, 2

# Substrings that identify a forwardable keyboard. A Logitech Unifying/Bolt receiver
# is demultiplexed by the kernel's hid-logitech-dj driver into one input node per
# paired device, and those nodes carry the paired device's own name.
DEFAULT_NAME_HINTS = ("keyboard", "keybd", "logitech", "usb receiver", "wireless device")

# Devices that must never be grabbed even if they match: taking the Pi's own power
# button away is how you lose the ability to shut the machine down cleanly, and the
# HDMI nodes register as keyboards but only ever emit CEC events.
NAME_BLOCKLIST = ("pwr_button", "vc4-hdmi", "power button")

RESCAN_INTERVAL_S = 1.0


def _read_name(event_path: str) -> str:
    """Read a device's name from sysfs rather than opening it. Opening every
    /dev/input/event* just to identify it would briefly disturb devices we have no
    business touching."""
    base = os.path.basename(event_path)
    try:
        with open(f"/sys/class/input/{base}/device/name") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def list_keyboards(name_hints=DEFAULT_NAME_HINTS) -> List[dict]:
    """Every input device that looks like a keyboard we could forward."""
    found = []
    for path in sorted(glob.glob("/dev/input/event*")):
        name = _read_name(path)
        low = name.lower()
        if any(b in low for b in NAME_BLOCKLIST):
            continue
        if not any(h in low for h in name_hints):
            continue
        found.append({"path": path, "name": name})
    return found


class KeyboardReader:
    """Grabs one or more keyboards and mirrors their key state into KeyState."""

    def __init__(self, state, emitter, device_paths: Optional[List[str]] = None,
                 name_hints=DEFAULT_NAME_HINTS, grab: bool = True):
        from evdev import InputDevice  # lazy: the log-only path needs no evdev

        self._InputDevice = InputDevice
        self.state = state
        self.emitter = emitter
        self.explicit_paths = device_paths
        self.name_hints = name_hints
        self.grab = grab
        self.devices: dict = {}          # path -> InputDevice
        self.unmapped_codes: set = set()  # keys with no HID equivalent, reported once
        self.events_seen = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="keyboard")

    # --- device lifecycle -------------------------------------------------------

    def _candidate_paths(self) -> List[str]:
        if self.explicit_paths:
            return [p for p in self.explicit_paths if os.path.exists(p)]
        return [d["path"] for d in list_keyboards(self.name_hints)]

    def _open(self, path: str) -> None:
        try:
            dev = self._InputDevice(path)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.EACCES):
                print(f"[keyboard] cannot open {path}: {exc}", file=sys.stderr, flush=True)
            return
        if self.grab:
            try:
                dev.grab()
            except OSError as exc:
                # EBUSY means something else already holds it exclusively. Forwarding
                # without the grab would make the Pi type locally too, so refuse the
                # device rather than half-work.
                print(f"[keyboard] could not grab {path} ({dev.name}): {exc} - skipping",
                      file=sys.stderr, flush=True)
                dev.close()
                return
        self.devices[path] = dev
        print(f"[keyboard] forwarding {path!r} ({dev.name})"
              f"{' [grabbed]' if self.grab else ' [SHARED - Pi types too]'}", flush=True)

    def _drop(self, path: str, reason: str) -> None:
        dev = self.devices.pop(path, None)
        if dev is None:
            return
        print(f"[keyboard] lost {path} ({reason})", file=sys.stderr, flush=True)
        try:
            if self.grab:
                dev.ungrab()
            dev.close()
        except OSError:
            pass
        # Whatever was held on that keyboard is not held any more - the keyboard is
        # gone. Releasing here is what stops a key surviving its own device.
        if not self.devices:
            self.state.clear_physical()
            self.emitter.nudge()

    # --- event handling ---------------------------------------------------------

    def _handle(self, code: int, value: int) -> None:
        # value 2 is the kernel's software auto-repeat. A USB keyboard never sends
        # repeats - it reports the key as continuously held and the *host* decides
        # the repeat rate. Forwarding these would double up with Windows' own repeat.
        if value == VALUE_REPEAT:
            return

        bit = MODIFIER_BITS.get(code)
        if bit is not None:
            snap = self.state.snapshot()["modifiers"]
            self.state.set_modifiers(snap | bit if value == VALUE_DOWN else snap & ~bit)
            self.emitter.nudge()
            return

        usage = HID_USAGE.get(code)
        if usage is None:
            if code not in self.unmapped_codes:
                self.unmapped_codes.add(code)
                print(f"[keyboard] evdev code {code} has no HID mapping; ignored "
                      f"(add it to keymap.py if you need it)", file=sys.stderr, flush=True)
            return

        if value == VALUE_DOWN:
            self.state.press_physical(usage)
        else:
            self.state.release_physical(usage)
        self.emitter.nudge()

    # --- main loop --------------------------------------------------------------

    def _loop(self) -> None:
        last_scan = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_scan >= RESCAN_INTERVAL_S:
                last_scan = now
                for path in self._candidate_paths():
                    if path not in self.devices:
                        self._open(path)

            if not self.devices:
                time.sleep(0.05)
                continue

            fd_map = {dev.fd: path for path, dev in self.devices.items()}
            try:
                ready, _, _ = select.select(list(fd_map), [], [], 0.2)
            except (OSError, ValueError):
                # A device vanished between building the map and selecting on it.
                for path in list(self.devices):
                    if not os.path.exists(path):
                        self._drop(path, "unplugged")
                continue

            for fd in ready:
                path = fd_map[fd]
                dev = self.devices.get(path)
                if dev is None:
                    continue
                try:
                    for event in dev.read():
                        if event.type == EV_KEY:
                            self.events_seen += 1
                            self._handle(event.code, event.value)
                except BlockingIOError:
                    continue
                except OSError as exc:
                    self._drop(path, f"read error: {exc}")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        for path in list(self.devices):
            dev = self.devices.pop(path)
            try:
                if self.grab:
                    dev.ungrab()
                dev.close()
            except OSError:
                pass

    def status(self) -> dict:
        return {
            "attached": [{"path": p, "name": d.name} for p, d in self.devices.items()],
            "events_seen": self.events_seen,
            "grabbed": self.grab,
            "unmapped_codes": sorted(self.unmapped_codes),
        }
