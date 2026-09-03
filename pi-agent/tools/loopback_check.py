"""Verify the whole output chain without the target PC.

Plug the Pro Micro into the Pi instead of the Windows PC and its HID output comes
back to this machine as an ordinary keyboard. Every hop then becomes observable
from one place:

    piproxy --sink serial ─> ESP32 ─UART─> Pro Micro ─USB HID─> this Pi

That covers the two links that carry no acknowledgement of any kind - the UART and
the HID interrupt endpoint - which otherwise fail silently and look identical to a
firmware bug, a wrong pin, or a missing ground.

    sudo systemctl stop piproxy          # it holds the UDP port
    python3 -m tools.loopback_check      # from pi-agent/

Move the Pro Micro back to the PC afterwards.
"""

import glob
import os
import re
import select
import socket
import subprocess
import sys
import time

TRIGGER_PORT = 48010
SETTLE_S = 2.5      # the Pro Micro re-enumerates when its firmware restarts
HOLD_S = 0.6


def find_hid_device():
    """The event node the Pro Micro presents. Matched by name: its event number
    changes on every re-enumeration, so hard-coding one guarantees a stale path."""
    try:
        blob = open("/proc/bus/input/devices").read()
    except OSError:
        return None, None
    for block in blob.split("\n\n"):
        name = re.search(r'^N: Name="(.*)"', block, re.M)
        handlers = re.search(r"^H: Handlers=.*?(event\d+)", block, re.M)
        if name and handlers and re.search(r"arduino|micro|sparkfun", name.group(1), re.I):
            return f"/dev/input/{handlers.group(1)}", name.group(1)
    return None, None


def main():
    port = os.environ.get("SERIAL_PORT") or (
        sorted(glob.glob("/dev/ttyUSB*")) or [None])[0]
    if not port:
        print("No /dev/ttyUSB* found - is the ESP32 plugged into the Pi?")
        return 1

    proc = subprocess.Popen(
        # --start-armed because this test drives the trigger and nothing else: with
        # no keyboard attached there is no arm key to press, and the point here is
        # the wire, not the gate.
        [sys.executable, "-m", "piproxy", "--sink", "serial",
         "--serial-port", port, "--no-keyboard", "--no-http",
         "--trigger-key", "k", "--start-armed"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(1.0)
        if proc.poll() is not None:
            print("piproxy exited immediately:\n" + (proc.stdout.read() or ""))
            return 1
        print(f"piproxy running, serial sink on {port}")

        # The Pro Micro drops off the bus and comes back when its firmware restarts,
        # so look for it only after things have settled.
        time.sleep(SETTLE_S)
        dev_path, dev_name = find_hid_device()
        if not dev_path:
            print("The Pro Micro is not present as an input device.\n"
                  "Plug it into a USB-A port on THIS Pi (not the PC) for this test.")
            return 1
        print(f"reading HID output from {dev_path} ({dev_name})\n")

        from evdev import InputDevice, categorize, ecodes

        hid = InputDevice(dev_path)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        # Real traffic is a state resent every 20ms, not an edge. Reproducing that
        # here matters: a one-shot datagram would be released by the watchdog mid-test
        # and the result would say more about the timing of this script than the link.
        def drive(active, seconds):
            seen = []
            deadline = time.time() + seconds
            while time.time() < deadline:
                udp.sendto(bytes([1 if active else 0]), ("127.0.0.1", TRIGGER_PORT))
                if select.select([hid.fd], [], [], 0.02)[0]:
                    for event in hid.read():
                        if event.type == ecodes.EV_KEY:
                            seen.append((categorize(event).keycode, event.value))
            return seen

        drive(False, 0.4)                      # flush anything left over
        while select.select([hid.fd], [], [], 0)[0]:
            hid.read()

        print("--- trigger ON ---")
        pressed = drive(True, HOLD_S)
        for key, value in pressed:
            print(f"  {key:<12} {'down' if value == 1 else 'up' if value == 0 else 'repeat'}")
        if not pressed:
            print("  nothing arrived")

        print("--- trigger OFF ---")
        released = drive(False, HOLD_S)
        for key, value in released:
            print(f"  {key:<12} {'down' if value == 1 else 'up' if value == 0 else 'repeat'}")
        if not released:
            print("  nothing arrived")

        down = [k for k, v in pressed if v == 1]
        up = [k for k, v in released if v == 0]
        print()
        if down and up:
            print(f"PASS: the chain carries a key end to end "
                  f"(pressed {down[0]}, released {up[0]})")
            return 0
        if down and not up:
            print("FAIL: the key went down but never came back up - check the "
                  "Pro Micro's watchdog and that the trigger state is reaching it")
            return 1
        print("FAIL: nothing reached this machine.\n"
              "  Check, in order:\n"
              "   - GND shared between the ESP32 and the Pro Micro (silent if missing)\n"
              "   - ESP32 GPIO 4 wired to Pro Micro D0/RX, not D2\n"
              "   - both boards running the *-proxy firmware, not the old sketches\n"
              "   - send '?' to the ESP32 on the serial port: frames_ok should climb")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
