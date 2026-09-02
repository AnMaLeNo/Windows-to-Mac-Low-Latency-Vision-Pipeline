"""The reference subscriber for macvision's telemetry stream - no browser anywhere.

    python3 -m tools.telemetry_tap [tcp://host:port]        (from mac-app/)

Connects to a running `python3 -m macvision --telemetry tcp://127.0.0.1:50510` (the
default here matches), retrying every half second until Ctrl-C, and prints:

  - the hello, once per connection: pid, roi, argv, and the trigger's and the source's
    own descriptions;
  - one line a second: frames/s, KB/s, the last seq, the median decide and e2e over
    that second, and the trigger state as of the last frame;
  - every [stats] message's summary line, verbatim, as it arrives.

docs/DASHBOARD.md, contract 1. This is also how the "costs the loop nothing" claim gets
CHECKED rather than believed, and the check is worth doing after any change near the
frame loop:

    1. run macvision without --telemetry; note `decide med` in its [stats] line.
    2. run it again with --telemetry, with this tap connected and printing.
    3. compare. The two medians should agree to within the run-to-run noise. If the
       second is visibly higher, something is being done ahead of the trigger byte or
       inside the measured span, and loop.py's placement of telemetry.frame() - after
       action.update(), after the mac_ms sample - has been disturbed.

The subscriber is deliberately allowed to be slow: the publisher keeps only the newest
frame, so a tap that prints too much sees fewer frames, never a backlog and never a
stalled loop. Stdlib only, like everything else that reads this stream.
"""

import argparse
import socket
import statistics
import sys
import time

from macvision.telemetry import (DEFAULT_HOST, DEFAULT_PORT, MessageReader,
                                 parse_telemetry)

RETRY_S = 0.5


class Second:
    """What happened in the last ~second, for the one-line summary."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.t0 = time.monotonic()
        self.frames = 0
        self.bytes = 0
        self.decide = []
        self.e2e = []
        self.seq = None
        self.hit = None

    def line(self):
        elapsed = max(time.monotonic() - self.t0, 1e-6)
        med = (lambda xs: f"{statistics.median(xs):.1f}" if xs else "-.-")
        trig = "-" if self.hit is None else ("ON" if self.hit else "off")
        return (f"[tap] {self.frames / elapsed:5.1f} frames/s  "
                f"{self.bytes / elapsed / 1024:8.0f} KB/s  "
                f"seq={self.seq if self.seq is not None else '-'}  "
                f"decide med={med(self.decide)}ms  e2e med={med(self.e2e)}ms  "
                f"trig={trig}")


def show_hello(header):
    status = header.get("status") or {}

    def desc(block):
        st = status.get(block)
        if isinstance(st, dict):
            return st.get("description") or st.get("kind") or "?"
        return "none" if st is None else str(st)

    print(f"[tap] hello: pid {header.get('pid')}, roi {header.get('roi')}, "
          f"argv {header.get('argv')}")
    print(f"[tap]        trigger {desc('trigger')}  |  source {desc('source')}")


def serve(sock):
    """Read one connection until it ends. Raises ValueError on lost sync."""
    reader = MessageReader()
    second = Second()
    sock.settimeout(0.25)      # so the once-a-second line prints on a quiet stream
    while True:
        try:
            data = sock.recv(1 << 16)
        except socket.timeout:
            data = None
        if data == b"":
            return
        if data:
            for header, payload in reader.feed(data):
                kind = header.get("type")
                if kind == "hello":
                    show_hello(header)
                elif kind == "frame":
                    second.frames += 1
                    second.bytes += len(payload)
                    second.seq = header.get("seq")
                    second.hit = header.get("hit")
                    timing = header.get("timing") or {}
                    if timing.get("decide_ms") is not None:
                        second.decide.append(timing["decide_ms"])
                    if timing.get("e2e_ms") is not None:
                        second.e2e.append(timing["e2e_ms"])
                elif kind == "stats":
                    print(f"[tap] {header.get('summary')}")
                # Anything else is a type this tap predates. Ignored, as the contract
                # requires.
        if time.monotonic() - second.t0 >= 1.0:
            print(second.line(), flush=True)
            second.reset()


def main(argv=None):
    p = argparse.ArgumentParser(prog="telemetry_tap",
                                description=__doc__.splitlines()[0])
    p.add_argument("target", nargs="?", default=f"tcp://{DEFAULT_HOST}:{DEFAULT_PORT}",
                   help=f"the publisher's socket (default tcp://{DEFAULT_HOST}:"
                        f"{DEFAULT_PORT}, which is macvision --telemetry's default too)")
    args = p.parse_args(argv)
    spec = parse_telemetry(args.target)
    if spec["kind"] != "tcp":
        p.error(f"{args.target!r}: {spec['reason'] or 'expected tcp://host:port'}")
    host, port = spec["host"], spec["port"]

    waiting = False
    try:
        while True:
            try:
                sock = socket.create_connection((host, port), timeout=2.0)
            except OSError as exc:
                if not waiting:
                    print(f"[tap] waiting for tcp://{host}:{port} ({exc}); "
                          f"retrying every {RETRY_S}s", flush=True)
                    waiting = True
                time.sleep(RETRY_S)
                continue
            waiting = False
            print(f"[tap] connected to tcp://{host}:{port}", flush=True)
            try:
                serve(sock)
                print("[tap] the publisher closed the connection", flush=True)
            except ValueError as exc:
                # Sync is lost; the only correct move is to reconnect, and the reader
                # refuses to guess where the next message starts.
                print(f"[tap] {exc}; reconnecting", file=sys.stderr, flush=True)
            except OSError as exc:
                print(f"[tap] connection lost ({exc}); reconnecting", flush=True)
            finally:
                sock.close()
    except KeyboardInterrupt:
        print("\n[tap] stopped", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
