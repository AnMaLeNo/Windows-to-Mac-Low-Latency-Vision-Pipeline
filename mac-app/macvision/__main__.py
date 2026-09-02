"""Entry point: python3 -m macvision [options]   (run from mac-app/)

Wires the pieces together and then gets out of the way. The construction ORDER below is
the important text in this file, because every step's cost hides behind the next one and
nothing in the code makes that visible:

  1. the action link, first. On a classic ESP32 devkit the bridge chip's DTR line is
     wired to the auto-reset circuit, so merely opening the port reboots the MCU and
     costs ~1s of boot. Opening it here hides that entirely behind the MPS warmup below.
     (docs/TRIGGER.md records that this particular board's IO0 half of that circuit is
     broken, so it does NOT visibly reset - and deasserting DTR/RTS before the open, as
     SerialTransport now does, may remove the cost altogether. The disagreement is
     recorded rather than resolved: the ordering is still right, because the port open is
     the one step whose cost we do not control and the warmup is the expensive step that
     wants to be second.)
  2. the source. It is opened HERE, not last, because it is the only thing that knows
     the frame geometry - a camera reports its crop immediately, and the detector's
     warmup has to be shaped from it. On macOS this is also where the Camera permission
     prompt appears, which wants to be early rather than after a silent minute of model
     loading.
  3. the detector. The MPS/Metal warmup lands here, shaped by step 2.
  4. the debug window. A failure here is not fatal.
  5. source.flush(), LAST, immediately before the loop. Everything that arrived during
     the multi-second warmup is discarded, so the first frame processed is genuinely
     fresh.

Step 5 is what makes step 2 safe, and it replaces the older rule of binding the socket
last. Binding last worked only because a socket is the one source you can acquire in
microseconds; a camera cannot be, so the freshness guarantee had to move from the order
of construction to an explicit flush. It is also strictly more robust: it covers the
model load, the window creation and anything else added between.

See mac-app/README.md.
"""

import argparse
import os
import signal
import sys

from .loop import run
from .protocol import ROI_H, ROI_W, UDP_PORT
from .sources import build_source, parse_source
from .stats import STATS_EVERY, STATS_WINDOW, LatencyStats
from .trigger import BAUD, list_ports, open_trigger


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="macvision",
        description="Takes a frame stream - from the Windows agent over UDP, or from a "
                    "camera on this Mac - detects a car on the centre pixel of the "
                    "region, and pushes one byte of state to whatever holds the key "
                    "down on the PC.",
    )

    trg = p.add_argument_group("trigger")
    trg.add_argument(
        "--trigger-target",
        # Computed HERE, inside parse_args, so the environment is read at run time.
        # default="auto" would be wrong in a way nothing would show you: the flag would
        # then always be present, open_trigger() would never consult the environment, and
        # TRIGGER_TARGET would break silently - exactly the class of failure commit
        # 3e8a1a4 exists to refuse.
        default=os.environ.get("TRIGGER_TARGET", "auto"),
        help="where a detection goes: udp://host[:48010], serial:///dev/cu.*, auto, or "
             "none. Defaults to $TRIGGER_TARGET, then auto. The flag is an extra door - "
             "the env var is what the READMEs document")
    trg.add_argument("--baud", type=int, default=BAUD,
                     help="serial baud (default 115200; must match BAUD in "
                          "firmware/esp32-link and the Pro Micro firmware)")
    trg.add_argument("--keepalive-ms", type=float, default=20.0,
                     help="re-send the current state this often (default 20). The far "
                          "end releases the key after 250ms of silence, so this is ~12 "
                          "missed sends of margin; raising it silently weakens every "
                          "downstream watchdog")

    vis = p.add_argument_group("vision")
    vis.add_argument("--weights", default=os.environ.get("MACVISION_WEIGHTS",
                                                         "yolov8n.pt"),
                     help="model weights, resolved against the working directory "
                          "(default yolov8n.pt). A .pt runs through torch on --device; "
                          "a .mlpackage runs through CoreML on the Neural Engine, which "
                          "is both faster and far steadier - see "
                          "macvision/detector_coreml.py")
    vis.add_argument("--device", default="mps",
                     help="torch device for both the warmup and inference (default "
                          "mps). Ignored for a .mlpackage, which uses --compute-units")
    vis.add_argument("--compute-units", default="ALL",
                     choices=("ALL", "CPU_AND_NE", "CPU_AND_GPU", "CPU_ONLY"),
                     help="which hardware CoreML may use for a .mlpackage (default "
                          "ALL, which picks the Neural Engine). The others exist to "
                          "make placement measurable: on this M5, yolov8n at 640 runs "
                          "in 1.8ms on the ANE, 5.6ms on the GPU and 9.9ms on the CPU")
    vis.add_argument("--roi-w", type=int, default=ROI_W,
                     help=f"ROI width the sender is capturing (default {ROI_W})")
    vis.add_argument("--roi-h", type=int, default=ROI_H,
                     help=f"ROI height the sender is capturing (default {ROI_H})")

    src = p.add_argument_group("source")
    src.add_argument(
        "--source",
        # Read at run time, same reasoning as --trigger-target: a literal default would
        # make the flag always present and silently shadow the environment.
        default=os.environ.get("FRAME_SOURCE", ""),
        help="where frames come from. udp://[host][:port] for the Windows agent, or "
             "camera://<index>[?crop=x,y,w,h&size=WxH&fps=N] for a camera on this Mac. "
             "Defaults to $FRAME_SOURCE, then udp")
    src.add_argument("--udp-port", type=int, default=UDP_PORT,
                     help=f"where the Windows agent sends (default {UDP_PORT}); "
                          f"overridden by a port inside --source")
    src.add_argument("--bind", default="0.0.0.0",
                     help="interface to bind; overridden by a host inside --source")

    dsp = p.add_argument_group("display")
    dsp.add_argument("--no-display", action="store_true",
                     help="run with no debug window. This is the flag that lets the "
                          "whole pipeline run on a machine with no opencv GUI, the way "
                          "--sink log lets piproxy run with no hardware. There is then "
                          "no 'q' - Ctrl-C is the way out - and `mac` in the stats line "
                          "becomes the decision time")
    dsp.add_argument("--window", default="debug", help="window title (default debug)")

    tim = p.add_argument_group("timing")
    tim.add_argument("--stats-every", type=int, default=STATS_EVERY,
                     help=f"print the stats line this often (default {STATS_EVERY})")
    tim.add_argument("--stats-window", type=int, default=STATS_WINDOW,
                     help=f"frames kept for the rolling stats (default {STATS_WINDOW})")

    p.add_argument("--list-ports", action="store_true",
                   help="list candidate trigger serial devices and exit")
    p.add_argument("--list-cameras", action="store_true",
                   help="probe camera indices and exit. Slow, and it blinks each "
                        "camera's activity light - there is no way to enumerate "
                        "AVFoundation devices through opencv without opening them")
    args = p.parse_args(argv)

    # Validated here rather than trusted, because each of these reaches a place with no
    # guard of its own and turns into a crash or a spin mid-run - after the trigger link
    # is open, which is the worst moment to discover a typo:
    #   --stats-every 0   -> ZeroDivisionError on the first frame (frames % 0)
    #   --stats-window 0  -> deque(maxlen=0), then min() of an empty sequence
    #   --keepalive-ms 0  -> Event.wait(0) returns instantly, so the keepalive becomes a
    #                        ~500,000/s spin holding the lock the frame loop needs
    for flag, value, low in (("--stats-every", args.stats_every, 1),
                             ("--stats-window", args.stats_window, 1),
                             ("--roi-w", args.roi_w, 1),
                             ("--roi-h", args.roi_h, 1),
                             ("--baud", args.baud, 1)):
        if value < low:
            p.error(f"{flag} must be at least {low} (got {value})")
    if args.keepalive_ms <= 0:
        p.error(f"--keepalive-ms must be greater than 0 (got {args.keepalive_ms}). "
                f"It is a period, not a rate: 0 would spin instead of pausing.")
    if not 0 <= args.udp_port <= 65535:
        p.error(f"--udp-port must be between 0 and 65535 (got {args.udp_port})")
    return args


def _raise_interrupt(*_):
    # SIGTERM must RAISE, and SIGINT is deliberately left at its default (which already
    # raises KeyboardInterrupt). A handler that merely sets a flag returns normally, and
    # PEP 475 then RESTARTS the blocking recvfrom - so the loop would not notice the
    # signal until the next datagram, which on a dead sender never arrives. Measured on
    # this machine: a flag-setting handler let a 5s recvfrom run its full 5.00s with the
    # flag already set; a raising handler unblocked it in 0.50s. This is the path that
    # decides whether Ctrl-C releases a held key.
    raise KeyboardInterrupt


def main(argv=None):
    args = parse_args(argv)

    if args.list_ports:
        found = list_ports()
        if not found:
            # stderr, not stdout: a listing that found nothing must print nothing to
            # stdout, so callers can treat "any output" as "found something".
            print("No candidate serial device found on this Mac.\n"
                  "If the keyboard proxy runs on the Raspberry Pi, there is nothing to "
                  "find here - use TRIGGER_TARGET=udp://raspberrypi.local:48010.",
                  file=sys.stderr)
            return 1
        for path in found:
            print(path)
        return 0

    if args.list_cameras:
        try:
            from .sources.camera import list_cameras
            found = list_cameras()
        except ImportError as exc:
            print(f"error: opencv-python is not installed ({exc}). "
                  "Run pip install -r requirements.txt.", file=sys.stderr)
            return 2
        if not found:
            # stderr, so a caller can treat "any stdout output" as "found something".
            print("No camera answered on indices 0-7.\n"
                  "On macOS, check System Settings -> Privacy & Security -> Camera and "
                  "grant this terminal access.", file=sys.stderr)
            return 1
        for cam in found:
            print(f"camera://{cam['index']}\t{cam['size'][0]}x{cam['size'][1]}")
        return 0

    # 1. The action link, first. Never raises; degrades to a no-op link on every branch.
    trigger = open_trigger(args.trigger_target, baud=args.baud,
                           keepalive_s=args.keepalive_ms / 1000.0)

    # 2. The source. Opened here, not last, because it is the only thing that knows the
    #    frame geometry - and step 5's flush() is what keeps that safe.
    try:
        spec = parse_source(args.source)
    except Exception as exc:
        # parse_source is documented as never raising, and its tests assert it. Belt and
        # braces at the boundary: a traceback here would skip the teardown below and
        # leave the trigger link open.
        print(f"error: --source {args.source!r} could not be parsed ({exc})",
              file=sys.stderr)
        trigger.stop()
        return 2
    if spec["kind"] == "unknown":
        print(f"error: --source {args.source!r}: {spec['reason']}\n"
              f"Expected udp://[host][:port] or camera://<index>[?crop=x,y,w,h].",
              file=sys.stderr)
        trigger.stop()
        return 2
    try:
        source = build_source(
            spec["kind"],
            host=spec["host"] or args.bind,
            port=spec["port"] if spec["port"] is not None else args.udp_port,
            device=spec["device"] if spec["device"] is not None else 0,
            crop=spec["crop"], size=spec["size"], fps=spec["fps"])
        source.open()
    except ImportError as exc:
        print(f"error: opencv-python is not installed ({exc}). "
              "Run pip install -r requirements.txt.", file=sys.stderr)
        trigger.stop()
        return 2
    except OSError as exc:
        # For udp this is almost always a second receiver already holding the port. Say
        # so, rather than making the user decode an errno - two instances splitting the
        # same frame stream is exactly the failure UdpSource refuses SO_REUSEADDR to
        # prevent. For a camera it is a bad index or a missing permission, and the
        # source's own message says which.
        print(f"error: could not open the {spec['kind']} source: {exc}", file=sys.stderr)
        if spec["kind"] == "udp":
            print(f"Another macvision receiver is probably already running. Check with:"
                  f"\n    lsof -nP -iUDP:{args.udp_port}\n"
                  f"Stop it first, or pass a different --udp-port.", file=sys.stderr)
        trigger.stop()
        return 2
    except Exception as exc:
        print(f"error: could not open the {spec['kind']} source: {exc}", file=sys.stderr)
        trigger.stop()
        return 2

    # The source's own geometry wins when it knows it. A camera reports its crop at
    # open(); a socket cannot know the ROI until the first datagram, so it reports 0 and
    # the configured value stands, to be checked against the first frame by the loop.
    roi_w = source.width or args.roi_w
    roi_h = source.height or args.roi_h

    # 3. The detector, shaped from the geometry resolved above.
    #
    # The backend is chosen by the weights' EXTENSION rather than by a flag of its own.
    # A .mlpackage is a CoreML export and can only run through CoreML; a .pt can only
    # run through torch. A --backend flag would therefore add a way to state something
    # the filename already settles, and a way to state it wrongly.
    coreml = args.weights.endswith((".mlpackage", ".mlmodel"))
    try:
        if coreml:
            from .detector_coreml import CoreMLDetector
            detector = CoreMLDetector(roi_w, roi_h, weights=args.weights,
                                      compute_units=args.compute_units)
        else:
            from .detector import Detector
            detector = Detector(roi_w, roi_h, weights=args.weights, device=args.device)
    except ImportError as exc:
        missing = "coremltools" if coreml else "ultralytics"
        print(f"error: {missing} is not installed ({exc}). "
              "Run pip install -r requirements.txt.", file=sys.stderr)
        source.close()
        trigger.stop()
        return 2
    except Exception as exc:
        where = f"compute units {args.compute_units}" if coreml \
            else f"device {args.device}"
        print(f"error: could not load {args.weights} on {where}: {exc}",
              file=sys.stderr)
        source.close()
        trigger.stop()
        return 2

    # 4. The window. A display failure is not fatal.
    display = None
    if not args.no_display:
        try:
            from .display import DebugWindow
            display = DebugWindow(args.window)
        except Exception as exc:
            print(f"warning: debug window disabled ({exc})", file=sys.stderr)

    stats = LatencyStats(window=args.stats_window)

    print(f"[macvision] source {source.description}, roi {roi_w}x{roi_h}, "
          f"trigger {trigger.status()['description']}"
          + ("" if display is not None else ", display off"), flush=True)

    signal.signal(signal.SIGTERM, _raise_interrupt)

    # 5. Discard everything that piled up while the model was warming, so the first
    #    frame the loop sees is fresh rather than however old the warmup was.
    discarded = source.flush()
    if discarded:
        print(f"[macvision] discarded {discarded} frame(s) buffered during startup",
              flush=True)
    # Kept verbatim. This is the honest "the pipeline is ready" signal that people wait
    # for, not a startup banner.
    print(f"Listening on UDP {source.status().get('udp_port')}..." if spec["kind"] == "udp"
          else f"Reading from {source.description}...", flush=True)

    # Sampled inside the try, BEFORE the teardown: trigger.stop() joins the keepalive
    # thread, so asking after the finally block always says "not alive" and every clean
    # shutdown would report a dead thread and exit 1.
    keepalive_died = False
    try:
        rc = run(source, detector, trigger, stats, display, roi_w, roi_h,
                 stats_every=args.stats_every)
        keepalive_died = (not trigger.alive
                          and trigger.status()["kind"] != "none")
    finally:
        # Reverse construction order, in a finally block so it runs on every exit path.
        # trigger.stop() is LAST because it is what releases the key.
        #
        # A crash or a SIGKILL still skips all of this, which is exactly what the far
        # end's watchdog is there for: the ESP32 fails its GPIO low ~250ms after the byte
        # stream stops (firmware/esp32-link), and piproxy's TriggerWatchdog does the same
        # on the Pi. No key can stay stuck down.
        if display is not None:
            display.close()
        source.close()
        trigger.stop()

    if keepalive_died:
        # It died DURING the run, which means the state stopped being re-sent while the
        # pipeline carried on - the far end's watchdog will have released the key and
        # nothing here would otherwise say why.
        print("[macvision] the trigger keepalive thread died during the run; the key "
              "may have been released under you", file=sys.stderr, flush=True)
        return 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
