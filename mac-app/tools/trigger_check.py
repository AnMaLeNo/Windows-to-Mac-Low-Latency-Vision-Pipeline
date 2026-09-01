"""Verify the Mac -> Pi trigger leg without the Windows PC, the camera or MPS.

    this Mac --UDP :48010--> piproxy on the Pi --> merged HID --> the Windows PC
    \________________________/
       the only hop under test

That hop carries no acknowledgement of any kind, so a trigger that never arrives looks
exactly like a vision pipeline that never fires - and the two have nothing in common.
This drives the real macvision.trigger module at the real 50Hz cadence and reads the
Pi's own packet counter over HTTP, so the answer comes from the far end rather than
from this side's hopes.

    sudo systemctl status piproxy       # on the Pi: it must be running
    TRIGGER_TARGET=udp://raspberrypi.local:48010 python3 -m tools.trigger_check

Needs no opencv, no ultralytics and no Windows agent.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from macvision.trigger import PI_TRIGGER_PORT, KEEPALIVE_S, open_trigger, parse_target

HTTP_PORT = 48011   # piproxy's control port; see pi-agent/piproxy/api.py
HOLD_S = 1.0        # long enough for ~50 keepalives at 20ms


def fetch_status(host, port, timeout=2.0):
    url = f"http://{host}:{port}/status"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def drive(trigger, active, seconds):
    """Hold one state for `seconds`, at the real cadence.

    Not a single datagram. Real traffic is a state re-sent every 20ms, not an edge, and
    a one-shot packet would be released by the Pi's 250ms watchdog part-way through -
    so the result would say more about this script's timing than about the link.
    """
    trigger.update(active)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(KEEPALIVE_S)
        trigger.update(active)


def main():
    p = argparse.ArgumentParser(prog="trigger_check", description=__doc__.splitlines()[0])
    p.add_argument("--target", default=os.environ.get("TRIGGER_TARGET"),
                   help="udp://host[:48010] (default: $TRIGGER_TARGET)")
    p.add_argument("--http-port", type=int, default=HTTP_PORT)
    p.add_argument("--hold", type=float, default=HOLD_S)
    args = p.parse_args()

    if not args.target:
        print("error: no target. Set TRIGGER_TARGET or pass --target "
              "udp://raspberrypi.local:48010", file=sys.stderr)
        return 2

    spec = parse_target(args.target)
    if spec["kind"] != "udp":
        print(f"error: this tool checks the UDP leg to the Pi; {args.target!r} is "
              f"{spec['kind']}. Pass --target udp://raspberrypi.local:48010",
              file=sys.stderr)
        return 2
    host, port = spec["host"], spec["port"] or PI_TRIGGER_PORT

    print(f"--- target {host}:{port}, HTTP :{args.http_port} ---")
    try:
        before = fetch_status(host, args.http_port)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"\nFAIL: cannot read http://{host}:{args.http_port}/status ({exc})")
        print("  1. is piproxy running?   ssh into the Pi and: systemctl status piproxy")
        print(f"  2. is the Pi reachable?  ping {host}")
        print("  3. is the HTTP API on?   piproxy must not be started with --no-http")
        return 1

    start_packets = before["trigger"]["packets"]
    print(f"    piproxy has seen {start_packets} trigger packets so far")

    trigger = open_trigger(args.target)
    try:
        print("--- trigger ON ---")
        drive(trigger, True, args.hold)
        mid = fetch_status(host, args.http_port)
        print(f"    keys held now: {mid['state']['trigger_keys']}")
        print("--- trigger OFF ---")
        drive(trigger, False, args.hold)
        after = fetch_status(host, args.http_port)
        print(f"    keys held now: {after['state']['trigger_keys']}")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"\nFAIL: the Pi stopped answering mid-test ({exc})")
        return 1
    finally:
        trigger.stop()

    received = after["trigger"]["packets"] - start_packets
    sent = trigger.status()["dropped_writes"]
    print(f"\n    piproxy received {received} packets during the test")
    print(f"    this Mac counted {sent} dropped writes")

    if received == 0:
        print("\nFAIL: not one packet arrived.")
        print(f"  1. is TRIGGER_TARGET pointing at the right host and port 48010? "
              f"(it says {host}:{port})")
        print("  2. is another process already bound to :48010 on the Pi?")
        print("  3. is outbound UDP blocked by the Mac's firewall? "
              "System Settings -> Network -> Firewall")
        return 1
    if not mid["state"]["trigger_keys"]:
        print("\nFAIL: packets arrived, but the Pi held no key while the trigger was on.")
        print("  1. check piproxy's --trigger-key argument")
        print("  2. read the Pi's log: journalctl -u piproxy -n 50")
        return 1
    if after["state"]["trigger_keys"]:
        print("\nFAIL: the key was still held after the trigger went off.")
        return 1

    print(f"\nPASS: {received} packets arrived, the key went down on ON and came back "
          f"up on OFF.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
