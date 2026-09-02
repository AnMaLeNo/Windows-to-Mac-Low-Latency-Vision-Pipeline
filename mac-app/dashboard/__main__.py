"""python3 -m dashboard: wire the four pieces together and serve until Ctrl-C.

    cd mac-app && python3 -m dashboard          # http://127.0.0.1:50511

The wiring is the whole of this file. Telemetry messages are dispatched by type into
the bus (hello, stats) or the frame encoder (frame); the runner's events go straight
to the bus; the server reads the bus. Unknown message types are ignored, as contract 1
requires of every subscriber.

Two startup behaviours are deliberate. A failing --describe-args is reported, loudly,
and the server comes up anyway: the likely cause is a wrong --cwd or --python, and a
running page with a clear error beats a dead process. And on Ctrl-C or SIGTERM the
child is stopped before anything else, so no orphaned macvision keeps a camera open
after the terminal that launched it has gone.

A SIGKILL gets no such chance, and the orphan it leaves holds the camera and the
telemetry port: the next dashboard's own child reports "telemetry disabled" while the
page shows the orphan's frames. Nothing here can fix that - see orphan_warning() - so a
hello from a pid this dashboard did not start is named on stderr, once, with the kill.
"""

import argparse
import os
import signal
import sys
import threading
import time
import webbrowser

from macvision.telemetry import parse_telemetry

from .bus import Bus
from .frames import DEFAULT_FPS, FrameEncoder
from .runner import DEFAULT_TELEMETRY, Runner
from .server import DEFAULT_HOST, DEFAULT_PORT, DashboardContext, DashboardServer
from .subscriber import TelemetrySubscriber

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="dashboard",
        description="A web page that starts, stops and watches macvision. "
                    "See docs/DASHBOARD.md.")
    p.add_argument("--bind", default=DEFAULT_HOST,
                   help=f"interface for the HTTP server (default {DEFAULT_HOST}; "
                        "loopback only, on purpose)")
    p.add_argument("--http-port", type=int, default=DEFAULT_PORT,
                   help=f"HTTP port (default {DEFAULT_PORT})")
    p.add_argument("--telemetry", default=DEFAULT_TELEMETRY,
                   help="where macvision publishes telemetry; passed to the child as "
                        f"MACVISION_TELEMETRY (default {DEFAULT_TELEMETRY})")
    p.add_argument("--python", default=sys.executable,
                   help="interpreter that runs macvision (default: this one)")
    p.add_argument("--cwd", default=os.path.dirname(HERE),
                   help="directory to launch macvision from (default: mac-app/)")
    p.add_argument("--fps", type=float, default=DEFAULT_FPS,
                   help=f"most frames per second sent to the browser (default {DEFAULT_FPS})")
    p.add_argument("--open", action="store_true",
                   help="open the page in the default browser once serving")
    args = p.parse_args(argv)

    spec = parse_telemetry(args.telemetry)
    if spec["kind"] != "tcp":
        reason = spec["reason"] or "the dashboard needs a socket to read from"
        p.error(f"--telemetry {args.telemetry!r}: expected tcp://[host][:port] ({reason})")
    if args.fps <= 0:
        p.error("--fps must be > 0")
    if not (0 < args.http_port < 65536):
        p.error("--http-port must be 1..65535")
    args.telemetry_spec = spec
    return args


def orphan_warning(header, process, url):
    """The one-line warning for a hello that did not come from the runner's own child,
    or None when it did. Pure, so the test covers it without a subprocess.

    Why a warning and not a fix: the dashboard cannot take a camera or a port from a
    process it does not own, macOS has nothing like prctl(PR_SET_PDEATHSIG) to make a
    child die with its SIGKILLed parent, and rule 2 forbids macvision spending a single
    instruction per frame checking whether its parent is still there. Naming the pid
    turns a page that silently shows the wrong process into one kill command."""
    pid = header.get("pid")
    if process.get("state") == "running" and process.get("pid") == pid:
        return None
    where = f"a macvision this dashboard did not start is publishing on {url}"
    if isinstance(pid, int):
        return f"[dashboard] WARNING: {where} (pid {pid}); stop it with kill {pid} if it is an orphan"
    return f"[dashboard] WARNING: {where} (pid unknown)"


def main(argv=None):
    args = parse_args(argv)
    spec = args.telemetry_spec

    bus = Bus()
    encoder = FrameEncoder(bus, fps=args.fps)
    runner = Runner(args.python, args.cwd, telemetry_url=args.telemetry,
                    on_event=bus.publish)
    warned = set()          # pids already named on stderr: once per orphan, not per hello

    def on_message(header, payload):
        kind = header.get("type")
        if kind == "frame":
            encoder.offer(header, payload)
        elif kind == "hello":
            # A hello opens a run. The stats remembered from the previous run belong
            # to that run, and a page connecting now must not get them replayed under
            # this hello as if they were current.
            bus.forget("stats")
            warning = orphan_warning(header, runner.status(), args.telemetry)
            if warning is not None and header.get("pid") not in warned:
                warned.add(header.get("pid"))
                print(warning, file=sys.stderr, flush=True)
            bus.publish("hello", header)
        elif kind == "stats":
            bus.publish("stats", header)
        # anything else: a newer macvision, and not our business

    subscriber = TelemetrySubscriber(
        spec["host"], spec["port"], on_message,
        on_state=lambda connected: bus.publish("telemetry", subscriber.status()))
    ctx = DashboardContext(runner, subscriber, encoder, bus, started_at=time.time())

    try:
        server = DashboardServer(ctx, STATIC_DIR, args.bind, args.http_port)
    except OSError as exc:
        print(f"[dashboard] cannot bind http://{args.bind}:{args.http_port}: {exc}\n"
              f"            is another dashboard running? try --http-port",
              file=sys.stderr, flush=True)
        return 2

    try:
        runner.describe()
    except RuntimeError as exc:
        print(f"[dashboard] WARNING: could not describe macvision's arguments:\n"
              f"            {exc}\n"
              f"            The launch form will be empty until this works. The usual "
              f"cause is a wrong --cwd ({args.cwd}) or --python ({args.python}).",
              file=sys.stderr, flush=True)

    encoder.start()
    subscriber.start()
    server.start()
    # A page that connects before anything changes still gets both state events.
    bus.publish("process", runner.status())
    bus.publish("telemetry", subscriber.status())

    print(f"Dashboard on http://{args.bind}:{server.port}  "
          f"(launches {args.python} -m macvision from {args.cwd}; "
          f"telemetry {args.telemetry})", flush=True)
    if args.open:
        webbrowser.open(f"http://{args.bind}:{server.port}")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda signum, frame: stop.set())
    try:
        # A timed wait in a loop, never a bare wait(): SIGTERM's handler only runs
        # between bytecodes, and Ctrl-C must land as KeyboardInterrupt here.
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        if runner.status()["state"] == "running":
            print("\n[dashboard] stopping macvision", flush=True)
            runner.stop()
        server.stop()
        subscriber.stop()
        encoder.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
