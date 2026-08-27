"""Checks keymap.py against the kernel's own headers and the USB HID spec.

HID_USAGE is ~100 entries of hand-transcribed numbers bridging two unrelated
numbering schemes. A single wrong digit means one key types the wrong character on
the PC, and nothing reports it - you find out months later by happening to press
that key. Structure is what makes it checkable: the HID keyboard page assigns
letters, digits and function keys in strict contiguous runs, so those can be
verified by construction rather than by re-reading the table.

    python3 -m tests.test_keymap        (from pi-agent/)

Reads /usr/include/linux/input-event-codes.h, so it needs Linux - run it on the Pi.
"""

import re
import sys

from piproxy.keymap import HID_USAGE, MODIFIER_BITS, TRIGGER_KEY_NAMES

HEADER = "/usr/include/linux/input-event-codes.h"


def kernel_key_codes():
    """KEY_* name -> code, straight from the kernel headers.

    The point is to compare against the authority rather than against a second copy
    of the same assumption: if this test hard-coded the evdev numbers too, it would
    only prove the two transcriptions match each other.
    """
    codes = {}
    with open(HEADER) as fh:
        for line in fh:
            m = re.match(r"#define\s+(KEY_\w+)\s+(\d+)\s*$", line)
            if m:
                codes.setdefault(m.group(1), int(m.group(2)))
    return codes


def run():
    try:
        names = kernel_key_codes()
    except FileNotFoundError:
        print(f"skip: {HEADER} not found (this test needs Linux)")
        return 0

    failures = []

    def check(label, key_name, expected):
        code = names.get(key_name)
        if code is None:
            failures.append(f"{label}: {key_name} absent from the kernel headers")
            return
        got = HID_USAGE.get(code)
        if got != expected:
            got_s = f"0x{got:02x}" if got is not None else "missing"
            failures.append(f"{label}: {key_name} (evdev {code}) -> {got_s}, "
                            f"expected 0x{expected:02x}")

    # Contiguous runs in HID Usage Page 0x07. These are the ranges where a
    # transcription slip is both easy to make and invisible in review.
    for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ"):
        check("letters", f"KEY_{ch}", 0x04 + i)
    for i, ch in enumerate("123456789"):
        check("digits", f"KEY_{ch}", 0x1E + i)
    check("digits", "KEY_0", 0x27)          # zero sits after nine, not before one
    for i in range(1, 13):
        check("function keys", f"KEY_F{i}", 0x39 + i)
    for i, key in enumerate(("KEY_KP1", "KEY_KP2", "KEY_KP3", "KEY_KP4", "KEY_KP5",
                             "KEY_KP6", "KEY_KP7", "KEY_KP8", "KEY_KP9")):
        check("keypad", key, 0x59 + i)
    check("keypad", "KEY_KP0", 0x62)

    # Two evdev codes mapping to one usage means one of them types the wrong thing.
    by_usage = {}
    for code, usage in HID_USAGE.items():
        by_usage.setdefault(usage, []).append(code)
    reverse = {v: k for k, v in names.items()}
    for usage, codes in by_usage.items():
        if len(codes) > 1:
            which = ", ".join(reverse.get(c, str(c)) for c in codes)
            failures.append(f"duplicate: usage 0x{usage:02x} is produced by {which}")

    # A code the kernel does not define is dead weight at best, and at worst masks
    # the real code for that key.
    known = set(names.values())
    for code in list(HID_USAGE) + list(MODIFIER_BITS):
        if code not in known:
            failures.append(f"evdev code {code} is not defined in the kernel headers")

    # Modifiers live in byte 0 as bits, never in a key slot. A key present in both
    # maps would be emitted twice, in two different encodings.
    overlap = set(HID_USAGE) & set(MODIFIER_BITS)
    if overlap:
        failures.append(f"in both HID_USAGE and MODIFIER_BITS: "
                        f"{[reverse.get(c, c) for c in overlap]}")
    if sorted(MODIFIER_BITS.values()) != [1, 2, 4, 8, 16, 32, 64, 128]:
        failures.append("MODIFIER_BITS must be the eight distinct bits of one byte")

    # Every trigger key must be reachable, and inside the one-byte usage range.
    for name, usage in TRIGGER_KEY_NAMES.items():
        if not 0 < usage <= 0xFF:
            failures.append(f"trigger key {name!r} has out-of-range usage {usage}")

    print(f"checked {len(HID_USAGE)} keys + {len(MODIFIER_BITS)} modifiers "
          f"+ {len(TRIGGER_KEY_NAMES)} trigger names against {HEADER}")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("letters, digits, F1-F12, keypad, uniqueness, kernel names: all consistent")
    return 0


if __name__ == "__main__":
    sys.exit(run())
