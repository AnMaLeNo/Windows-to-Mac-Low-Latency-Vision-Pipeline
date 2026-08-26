"""End-to-end self-test of the trigger hardware, run from the Windows PC.

Drives the ESP32's serial port exactly as the Mac would, then watches this machine's real
keyboard state to see the Pro Micro's keypress arrive. That closes the loop entirely in
software: serial -> ESP32 -> GPIO -> Pro Micro -> USB HID -> Windows, with no LED to squint
at and no Mac required.

It also times each leg, which is the only direct measurement of the trigger link's own
latency - `docs/TRIGGER.md` predicts 2-4ms for everything after the detection decision.

    python tools/trigger_selftest.py COM5
    python tools/trigger_selftest.py COM5 --key j --trials 20

Key state is read with GetAsyncKeyState, which reports the physical key regardless of which
window has focus. The characters themselves still land in the focused window, so put the
focus somewhere harmless before running this.
"""

import argparse
import ctypes
import statistics
import sys
import time

import serial

BAUD = 115200
KEEPALIVE_S = 0.020
POLL_S = 0.0002  # 200us - fine enough not to blur a millisecond-scale measurement

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short


def key_is_down(vk: int) -> bool:
    # Bit 15 is "currently down". Bit 0 ("pressed since last call") is deliberately ignored:
    # it is consumed by whoever reads it first, which makes it useless when something else
    # on the system is also polling.
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def wait_for(vk: int, want_down: bool, timeout_s: float):
    """Block until the key reaches `want_down`. Returns elapsed seconds, or None on timeout."""
    t0 = time.perf_counter()
    deadline = t0 + timeout_s
    while time.perf_counter() < deadline:
        if key_is_down(vk) == want_down:
            return time.perf_counter() - t0
        time.sleep(POLL_S)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", help="serial port of the ESP32, e.g. COM5")
    ap.add_argument("--key", default="k",
                    help="the key TRIGGER_KEY is set to in pro-micro-hid.ino (default: k)")
    ap.add_argument("--trials", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=1.0,
                    help="how long to wait for each edge before calling it a failure")
    args = ap.parse_args()

    vk = ord(args.key.upper())
    ser = serial.Serial(args.port, BAUD, timeout=0, write_timeout=1)
    time.sleep(0.3)

    press_ms, release_ms, failures = [], [], []
    try:
        # Establish a known-released baseline. If the key is already down here the wiring is
        # wrong (floating input, or the line stuck high) and every measurement below would be
        # meaningless, so say so instead of reporting a fake 0ms.
        ser.write(b"\x00")
        time.sleep(0.4)
        if key_is_down(vk):
            print(f"ABORT: '{args.key}' is already held down before any trigger was sent.")
            print("       Check the wiring - a floating D2 or a line stuck high does this.")
            return 2

        print(f"{args.port} -> ESP32 -> Pro Micro, watching for '{args.key}', "
              f"{args.trials} trials\n")
        for i in range(args.trials):
            # Assert, and time how long until Windows sees the key go down.
            ser.write(b"\x01")
            down = wait_for(vk, True, args.timeout)
            if down is None:
                failures.append(f"trial {i+1}: key never went down")
                ser.write(b"\x00")
                time.sleep(0.2)
                continue

            # Release, and time the other edge.
            ser.write(b"\x00")
            up = wait_for(vk, False, args.timeout)
            if up is None:
                failures.append(f"trial {i+1}: key never came back up")
                continue

            press_ms.append(down * 1000)
            release_ms.append(up * 1000)
            print(f"  trial {i+1:2d}: press {down*1000:6.2f}ms   release {up*1000:6.2f}ms")
            time.sleep(0.15)  # let the HID stack settle between trials

        # The watchdog is the safety property the whole design rests on, so test it for real:
        # assert the line, then stop talking while staying connected, and require the key to
        # come up on its own.
        print("\nwatchdog: asserting, then going silent...")
        deadline = time.time() + 0.5
        while time.time() < deadline:
            ser.write(b"\x01")
            time.sleep(KEEPALIVE_S)
        if not key_is_down(vk):
            failures.append("watchdog: key was not down before going silent")
        else:
            released = wait_for(vk, False, 2.0)
            if released is None:
                failures.append("watchdog: KEY STAYED DOWN - a crash would stick a key")
            else:
                print(f"  key self-released after {released*1000:.0f}ms "
                      f"(firmware WATCHDOG_MS is 250)")
    finally:
        ser.write(b"\x00")
        ser.close()

    print()
    if press_ms:
        print(f"press   n={len(press_ms):2d}  min {min(press_ms):5.2f}  "
              f"med {statistics.median(press_ms):5.2f}  max {max(press_ms):5.2f} ms")
        print(f"release n={len(release_ms):2d}  min {min(release_ms):5.2f}  "
              f"med {statistics.median(release_ms):5.2f}  max {max(release_ms):5.2f} ms")
    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  - " + f)
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
