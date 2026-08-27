"""The 8-byte USB HID boot keyboard report, and the merge of the two key sources.

Report layout (HID boot protocol, the format every BIOS and OS understands):

    byte 0   modifier bitmask (ctrl/shift/alt/gui, left and right)
    byte 1   reserved, always 0
    byte 2-7 up to six simultaneously-held key usages, 0 = empty slot

Two independent sources write into it: the physical keyboard read from evdev, and
the vision trigger pushed in over the network. They are merged rather than
interleaved, because both are *level* signals - "this key is currently held" - not
events. A key is down in the emitted report if either source holds it.
"""

import threading
from typing import Iterable, Optional, Set

RELEASE_ALL = bytes(8)  # every key and modifier up; what a watchdog sends

# The boot protocol has exactly six key slots. A real keyboard signals overflow by
# filling all six with 0x01 (ErrorRollOver); we never do that, because it would also
# drop the trigger key, and the trigger is the one key in this system that must
# never be lost.
MAX_KEYS = 6


class KeyState:
    """Merges physical keys and trigger keys into one report.

    Thread-safe: the evdev reader thread and the network receiver thread both mutate
    this, and the emitter reads it. All three go through one lock. The critical
    sections are a handful of set operations on at most a few dozen elements, so
    contention is measured in microseconds and never shows up as input latency.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._physical: Set[int] = set()   # HID usages held on the real keyboard
        self._modifiers = 0                # bitmask from the real keyboard
        self._trigger: Set[int] = set()    # HID usages held by the vision trigger
        # Insertion order matters when more than six keys are down: without it,
        # iterating a set would evict an arbitrary key on every report, making the
        # emitted stream flicker between different subsets of the same held keys.
        self._order: list[int] = []

    # --- physical keyboard side -------------------------------------------------

    def press_physical(self, usage: int) -> None:
        with self._lock:
            if usage not in self._physical:
                self._physical.add(usage)
                self._order.append(usage)

    def release_physical(self, usage: int) -> None:
        with self._lock:
            self._physical.discard(usage)
            if usage not in self._trigger and usage in self._order:
                self._order.remove(usage)

    def set_modifiers(self, mask: int) -> None:
        with self._lock:
            self._modifiers = mask & 0xFF

    def set_modifier_bit(self, bit: int, down: bool) -> None:
        """Set or clear one modifier bit atomically.

        The read-modify-write has to happen under a single lock acquisition. Doing it
        as snapshot-then-set would let a second modifier event land in between and be
        overwritten - and a lost Shift release is a keyboard that types in capitals
        until you press Shift again.
        """
        with self._lock:
            if down:
                self._modifiers |= bit
            else:
                self._modifiers &= ~bit

    def clear_physical(self) -> None:
        """Drop every physical key. Used when the keyboard disconnects, so its keys
        cannot stay held on the PC by a device that is no longer even present."""
        with self._lock:
            self._physical.clear()
            self._modifiers = 0
            self._order = [u for u in self._order if u in self._trigger]

    # --- trigger side -----------------------------------------------------------

    def set_trigger(self, usages: Iterable[int]) -> None:
        """Replace the trigger's held keys. Idempotent by design: the Mac sends the
        current state on every frame and on a keepalive, never deltas, so applying
        the same state twice must be a no-op (see docs/TRIGGER.md)."""
        new = set(usages)
        with self._lock:
            if new == self._trigger:
                return
            for usage in new - self._trigger:
                if usage not in self._physical:
                    self._order.append(usage)
            for usage in self._trigger - new:
                if usage not in self._physical and usage in self._order:
                    self._order.remove(usage)
            self._trigger = new

    # --- emission ---------------------------------------------------------------

    def build(self) -> bytes:
        """Serialise the merged state into an 8-byte boot keyboard report."""
        with self._lock:
            modifiers = self._modifiers
            held = self._physical | self._trigger
            # Walk _order for a stable slot assignment, then let the trigger jump the
            # queue if we are at capacity: a person crossing the ROI must still register
            # even while six keys are already held down.
            ordered = [u for u in self._order if u in held]
            trigger = self._trigger
        keys = ordered[:MAX_KEYS]
        missing = [u for u in ordered if u in trigger and u not in keys]
        for i, usage in enumerate(missing[:MAX_KEYS]):
            keys[MAX_KEYS - 1 - i] = usage
        return bytes([modifiers, 0] + keys + [0] * (MAX_KEYS - len(keys)))

    def snapshot(self) -> dict:
        """Human-readable state, for the API's /status endpoint."""
        with self._lock:
            return {
                "modifiers": self._modifiers,
                "physical_keys": sorted(self._physical),
                "trigger_keys": sorted(self._trigger),
            }


def describe(report: bytes) -> str:
    """Render a report as hex, for logs. Short enough to sit on one line at 50Hz."""
    return " ".join(f"{b:02x}" for b in report)


def is_release_all(report: Optional[bytes]) -> bool:
    return report == RELEASE_ALL
