"""Bench-test the trigger link without the vision pipeline.

Speaks the same one-byte protocol as mac-app/trigger.py, so it exercises the real firmware
path: serial -> ESP32 -> GPIO -> (LED, and the Pro Micro if wired). Runs from either machine;
on Windows the port is a COMn name, on the Mac a /dev/cu.* path.

    python tools/trigger_bench.py COM5 blink     # 1s on / 1s off, forever - watch the LED
    python tools/trigger_bench.py COM5 on        # assert and hold until Ctrl-C
    python tools/trigger_bench.py COM5 watchdog  # assert, then go silent - LED must self-clear

`watchdog` is the interesting one: it holds the line high, then stops sending entirely while
staying connected. If the firmware is correct the LED drops on its own about 250ms later.
That failure path is the whole reason a key can never stay stuck down, and it is invisible
in normal operation - this is the only way to see it work.
"""

import sys
import time

import serial

BAUD = 115200
KEEPALIVE_S = 0.020  # same cadence as mac-app/trigger.py


def send(ser, active):
    ser.write(b"\x01" if active else b"\x00")


def hold(ser, active, seconds):
    """Assert a state for a while, keepalive included - i.e. what the Mac actually does."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        send(ser, active)
        time.sleep(KEEPALIVE_S)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    port = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "blink"

    ser = serial.Serial(port, BAUD, timeout=0, write_timeout=1)
    # A CH340/CP2102 board resets when the port opens, because DTR is wired to the ESP32's
    # auto-reset circuit. Give the firmware time to boot or the first bytes hit a chip that
    # is still in its bootloader and are simply lost.
    print(f"{port} open at {BAUD}, waiting for the board to boot...")
    time.sleep(1.5)

    try:
        if mode == "on":
            print("holding ACTIVE (Ctrl-C to release)")
            hold(ser, True, 1e9)
        elif mode == "off":
            print("holding IDLE (Ctrl-C to stop)")
            hold(ser, False, 1e9)
        elif mode == "watchdog":
            print("holding ACTIVE for 2s...")
            hold(ser, True, 2.0)
            print("now silent - LED should drop by itself within ~250ms. Ctrl-C to exit.")
            time.sleep(1e9)
        elif mode == "blink":
            print("blinking 1s on / 1s off (Ctrl-C to stop)")
            while True:
                print("  ON")
                hold(ser, True, 1.0)
                print("  off")
                hold(ser, False, 1.0)
        else:
            print(f"unknown mode {mode!r}")
            return 2
    except KeyboardInterrupt:
        pass
    finally:
        send(ser, False)  # never leave the bench holding a key down
        ser.close()
        print("\nreleased, port closed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
