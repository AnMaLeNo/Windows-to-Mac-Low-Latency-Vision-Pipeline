"""Checks the trigger gate: the Mac asks, the arm key decides.

The gate is one boolean, which is exactly what makes it worth testing - a boolean
guarding a *held* key has failure modes a boolean guarding an event does not. A
disarm that forgets to release leaves K down on the PC forever; an arm that only
takes effect on the Mac's next frame is a car already gone by the time the key
presses; a toggle that also fires on key-up never changes anything at all.

    python3 -m tests.test_arm        (from pi-agent/)

Pure logic, no hardware and no evdev: the keyboard reader is fed synthetic events.
"""

import sys
import types

from piproxy.keymap import HID_USAGE, MODIFIER_BITS, resolve_trigger_key
from piproxy.report import MAX_KEYS, KeyState

K = resolve_trigger_key("k")
A = resolve_trigger_key("a")

# evdev codes, from the kernel's input-event-codes.h. Hard-coded rather than read
# from the header because this test must run on the Mac too; test_keymap.py is where
# the table is checked against the kernel.
CODE_A, CODE_K, CODE_S, CODE_LEFTSHIFT = 30, 37, 31, 42
VALUE_UP, VALUE_DOWN, VALUE_REPEAT = 0, 1, 2


class FakeEmitter:
    """Counts nudges. A gate change that does not nudge is a gate change the PC does
    not see until the next keepalive."""

    def __init__(self):
        self.nudges = 0

    def nudge(self):
        self.nudges += 1


def make_reader(state, emitter):
    """A KeyboardReader with no evdev behind it.

    Its constructor imports evdev lazily, so a stub module is enough to build one on
    a machine that has no /dev/input at all - and _handle, the part under test, never
    touches a device.
    """
    sys.modules.setdefault("evdev", types.SimpleNamespace(InputDevice=object))
    from piproxy.keyboard import KeyboardReader
    return KeyboardReader(state, emitter, arm_usage=A, arm_key="a")


def keys_in(report):
    return [b for b in report[2:] if b]


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    # --- the gate itself --------------------------------------------------------

    st = KeyState()
    check("starts disarmed", st.armed, False)

    st.set_trigger([K])
    check("disarmed: nothing emitted", keys_in(st.build()), [])
    check("disarmed: request is remembered", st.snapshot()["trigger_requested"], [K])

    # Arming with a request already on file must press the key now. The Mac is not
    # going to resend anything - it has been sending the same state for a while.
    check("arm returns new state", st.toggle_armed(), True)
    check("armed: key appears with no new frame", keys_in(st.build()), [K])

    check("disarm returns new state", st.toggle_armed(), False)
    check("disarmed: key released", keys_in(st.build()), [])
    check("disarmed: request survives", st.snapshot()["trigger_requested"], [K])

    st.set_armed(True)
    check("set_armed(True) presses", keys_in(st.build()), [K])
    st.set_armed(True)
    check("set_armed is idempotent", keys_in(st.build()), [K])
    st.set_trigger([])
    check("armed: the Mac can still let go", keys_in(st.build()), [])

    # --- the gate never touches the physical keyboard ---------------------------

    st = KeyState(armed=False)
    st.press_physical(K)
    check("physical K passes while disarmed", keys_in(st.build()), [K])
    st.set_trigger([K])
    st.set_armed(True)
    st.set_armed(False)
    check("disarm leaves the physical K alone", keys_in(st.build()), [K])
    st.release_physical(K)
    check("released physical K, still disarmed", keys_in(st.build()), [])

    # Slot ordering is bookkeeping the gate has to keep straight: _order is mutated
    # on both arming and disarming, so a leak there shows up as a key that cannot be
    # released, or as duplicate slots in the report.
    st = KeyState(armed=True)
    for _ in range(50):
        st.set_trigger([K])
        st.set_armed(False)
        st.set_armed(True)
        st.set_trigger([])
    st.set_trigger([K])
    check("no slot leak after 50 cycles", keys_in(st.build()), [K])
    st.set_armed(False)
    check("still releasable after 50 cycles", keys_in(st.build()), [])

    # Six physical keys plus the trigger: the trigger jumping the queue must keep
    # working through the gate, since that is the one key allowed to displace another.
    st = KeyState(armed=True)
    for code in (0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23):
        st.press_physical(code)
    st.set_trigger([K])
    report = st.build()
    check("report is 8 bytes", len(report), 8)
    check("trigger displaces a key when full", K in keys_in(report), True)
    check("still exactly six slots", len(keys_in(report)), MAX_KEYS)
    st.set_armed(False)
    check("disarmed: the displaced key comes back", K in keys_in(st.build()), False)
    check("disarmed: six physical keys again", len(keys_in(st.build())), MAX_KEYS)

    # --- the arm key, as the keyboard reader sees it ----------------------------

    st, em = KeyState(), FakeEmitter()
    reader = make_reader(st, em)

    reader._handle(CODE_A, VALUE_DOWN)
    check("A down arms", st.armed, True)
    check("A is forwarded to the PC too", keys_in(st.build()), [A])
    check("A down nudged the emitter", em.nudges, 1)

    reader._handle(CODE_A, VALUE_UP)
    check("A up does not disarm", st.armed, True)
    check("A up releases the letter", keys_in(st.build()), [])

    reader._handle(CODE_A, VALUE_DOWN)
    check("second A press disarms", st.armed, False)
    reader._handle(CODE_A, VALUE_REPEAT)
    reader._handle(CODE_A, VALUE_REPEAT)
    check("holding A does not re-toggle", st.armed, False)
    reader._handle(CODE_A, VALUE_UP)
    check("two presses, two toggles", reader.arm_toggles, 2)

    # A disarm has to reach the wire in the report carrying its own keypress, not at
    # the next keepalive 20ms later.
    st, em = KeyState(armed=True), FakeEmitter()
    reader = make_reader(st, em)
    st.set_trigger([K])
    check("armed and firing", keys_in(st.build()), [K])
    before = em.nudges
    reader._handle(CODE_A, VALUE_DOWN)
    check("trigger dropped in the same report", keys_in(st.build()), [A])
    check("the disarm nudged the emitter", em.nudges > before, True)

    # Other keys must not go near the gate.
    st, em = KeyState(), FakeEmitter()
    reader = make_reader(st, em)
    reader._handle(CODE_S, VALUE_DOWN)
    reader._handle(CODE_K, VALUE_DOWN)
    reader._handle(CODE_LEFTSHIFT, VALUE_DOWN)
    check("no other key arms", st.armed, False)
    check("other keys still forwarded", sorted(keys_in(st.build())),
          sorted([HID_USAGE[CODE_S], HID_USAGE[CODE_K]]))
    check("modifiers still forwarded", st.build()[0], MODIFIER_BITS[CODE_LEFTSHIFT])

    # The arm key is named from the same table as the trigger key, so a name that
    # resolves for one must resolve for the other.
    for name in ("a", "f13", "space"):
        try:
            resolve_trigger_key(name)
        except ValueError as exc:
            failures.append(f"arm key {name!r} does not resolve: {exc}")

    print("checked the gate: request/emit split, arm and disarm timing, slot "
          "ordering, and the arm key through the keyboard reader")
    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("all consistent")
    return 0


if __name__ == "__main__":
    sys.exit(run())
