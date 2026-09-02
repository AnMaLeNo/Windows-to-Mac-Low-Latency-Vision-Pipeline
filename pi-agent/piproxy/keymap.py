"""evdev key code -> USB HID usage (Usage Page 0x07, "Keyboard/Keypad").

The two numbering schemes are unrelated: evdev codes come from Linux's
input-event-codes.h and follow the AT scancode order, HID usages come from the USB
HID Usage Tables. Every key we forward has to cross this table.

Why translate at all, rather than passing the dongle's raw HID reports straight
through? Because a Logitech Unifying/Bolt receiver is claimed by the kernel's
hid-logitech-dj driver, which demultiplexes the proprietary HID++ frames from the
receiver into per-device input nodes. The raw /dev/hidraw* nodes carry HID++, not
clean boot-keyboard reports, so reading them means reimplementing HID++. evdev is
the layer where the kernel has already done that work for us.
"""

# Modifiers are not slots in the report - they are bits in byte 0. Keeping them in
# their own map is what lets report.py treat them separately without special-casing
# ranges of usage codes.
MODIFIER_BITS = {
    29:  0x01,   # KEY_LEFTCTRL
    42:  0x02,   # KEY_LEFTSHIFT
    56:  0x04,   # KEY_LEFTALT
    125: 0x08,   # KEY_LEFTMETA
    97:  0x10,   # KEY_RIGHTCTRL
    54:  0x20,   # KEY_RIGHTSHIFT
    100: 0x40,   # KEY_RIGHTALT
    126: 0x80,   # KEY_RIGHTMETA
}

HID_USAGE = {
    # --- row 1: escape, digits, backspace, tab ---
    1: 0x29,    # KEY_ESC
    2: 0x1E, 3: 0x1F, 4: 0x20, 5: 0x21, 6: 0x22,      # 1 2 3 4 5
    7: 0x23, 8: 0x24, 9: 0x25, 10: 0x26, 11: 0x27,    # 6 7 8 9 0
    12: 0x2D,   # KEY_MINUS
    13: 0x2E,   # KEY_EQUAL
    14: 0x2A,   # KEY_BACKSPACE
    15: 0x2B,   # KEY_TAB

    # --- letters, in evdev (scancode) order, not alphabetical ---
    16: 0x14, 17: 0x1A, 18: 0x08, 19: 0x15, 20: 0x17,  # q w e r t
    21: 0x1C, 22: 0x18, 23: 0x0C, 24: 0x12, 25: 0x13,  # y u i o p
    26: 0x2F,   # KEY_LEFTBRACE
    27: 0x30,   # KEY_RIGHTBRACE
    28: 0x28,   # KEY_ENTER
    30: 0x04, 31: 0x16, 32: 0x07, 33: 0x09, 34: 0x0A,  # a s d f g
    35: 0x0B, 36: 0x0D, 37: 0x0E, 38: 0x0F,            # h j k l
    39: 0x33,   # KEY_SEMICOLON
    40: 0x34,   # KEY_APOSTROPHE
    41: 0x35,   # KEY_GRAVE
    43: 0x31,   # KEY_BACKSLASH
    44: 0x1D, 45: 0x1B, 46: 0x06, 47: 0x19, 48: 0x05,  # z x c v b
    49: 0x11, 50: 0x10,                                # n m
    51: 0x36,   # KEY_COMMA
    52: 0x37,   # KEY_DOT
    53: 0x38,   # KEY_SLASH
    57: 0x2C,   # KEY_SPACE
    58: 0x39,   # KEY_CAPSLOCK

    # --- function keys. F11/F12 sit at 87/88, far from F1..F10, because they were
    # added to the PC keyboard years after the original layout was frozen. ---
    59: 0x3A, 60: 0x3B, 61: 0x3C, 62: 0x3D, 63: 0x3E, 64: 0x3F,   # F1..F6
    65: 0x40, 66: 0x41, 67: 0x42, 68: 0x43,                       # F7..F10
    87: 0x44, 88: 0x45,                                           # F11 F12

    # --- keypad ---
    69: 0x53,   # KEY_NUMLOCK
    70: 0x47,   # KEY_SCROLLLOCK
    55: 0x55,   # KEY_KPASTERISK
    71: 0x5F, 72: 0x60, 73: 0x61,   # KP7 KP8 KP9
    74: 0x56,                       # KP_MINUS
    75: 0x5C, 76: 0x5D, 77: 0x5E,   # KP4 KP5 KP6
    78: 0x57,                       # KP_PLUS
    79: 0x59, 80: 0x5A, 81: 0x5B,   # KP1 KP2 KP3
    82: 0x62,                       # KP0
    83: 0x63,                       # KP_DOT
    96: 0x58,                       # KP_ENTER
    98: 0x54,                       # KP_SLASH

    # --- navigation cluster ---
    99:  0x46,  # KEY_SYSRQ (PrintScreen)
    102: 0x4A,  # KEY_HOME
    103: 0x52,  # KEY_UP
    104: 0x4B,  # KEY_PAGEUP
    105: 0x50,  # KEY_LEFT
    106: 0x4F,  # KEY_RIGHT
    107: 0x4D,  # KEY_END
    108: 0x51,  # KEY_DOWN
    109: 0x4E,  # KEY_PAGEDOWN
    110: 0x49,  # KEY_INSERT
    111: 0x4C,  # KEY_DELETE
    119: 0x48,  # KEY_PAUSE
    127: 0x65,  # KEY_COMPOSE (the "menu"/application key)

    # The extra key ISO layouts have that ANSI does not: the one left of Z on a
    # French/German board. Without it an AZERTY keyboard loses a character entirely.
    86:  0x64,  # KEY_102ND
}


# Names for the handful of keys the trigger can be configured to press. Kept small
# and explicit rather than reversing HID_USAGE: this is a UI surface (it appears in
# config files and in the API), so it should not silently grow when the table does.
TRIGGER_KEY_NAMES = {
    "a": 0x04, "b": 0x05, "c": 0x06, "d": 0x07, "e": 0x08, "f": 0x09,
    "g": 0x0A, "h": 0x0B, "i": 0x0C, "j": 0x0D, "k": 0x0E, "l": 0x0F,
    "m": 0x10, "n": 0x11, "o": 0x12, "p": 0x13, "q": 0x14, "r": 0x15,
    "s": 0x16, "t": 0x17, "u": 0x18, "v": 0x19, "w": 0x1A, "x": 0x1B,
    "y": 0x1C, "z": 0x1D,
    "1": 0x1E, "2": 0x1F, "3": 0x20, "4": 0x21, "5": 0x22,
    "6": 0x23, "7": 0x24, "8": 0x25, "9": 0x26, "0": 0x27,
    "enter": 0x28, "escape": 0x29, "space": 0x2C, "tab": 0x2B,
    "f1": 0x3A, "f2": 0x3B, "f3": 0x3C, "f4": 0x3D, "f5": 0x3E, "f6": 0x3F,
    "f7": 0x40, "f8": 0x41, "f9": 0x42, "f10": 0x43, "f11": 0x44, "f12": 0x45,
    # F13-F15 do not exist on any normal keyboard, which is exactly why they are
    # useful here: the trigger can never collide with a key you actually press.
    "f13": 0x68, "f14": 0x69, "f15": 0x6A,
    "up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4F,
}


def resolve_trigger_key(name: str) -> int:
    """Look up a trigger key by name, with an error that lists the alternatives."""
    usage = TRIGGER_KEY_NAMES.get(name.lower())
    if usage is None:
        raise ValueError(
            f"unknown trigger key {name!r}. Known keys: "
            + ", ".join(sorted(TRIGGER_KEY_NAMES))
        )
    return usage
