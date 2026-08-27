"""Entry point: python3 -m piproxy [options]

Wires the pieces together and then gets out of the way. Startup order matters -
the sink and emitter come up before anything can produce a key event, so no press
can ever arrive with nowhere to go.
"""

import argparse
import signal
import sys
import threading

from .api import HTTP_PORT, TRIGGER_PORT, AgentContext, HttpApi, TriggerReceiver
from .keymap import resolve_trigger_key
from .report import KeyState
from .sinks import Emitter, TriggerWatchdog, build_sink


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="piproxy",
        description="Keyboard proxy: forwards a real keyboard to a PC and injects a "
                    "key when the Mac's vision pipeline says a person is on the ROI.",
    )
    out = p.add_argument_group("output")
    out.add_argument("--sink", choices=("log", "serial", "hidg"), default="log",
                     help="where reports go. log = print only, no hardware needed "
                          "(default); serial = UART to the Pro Micro; hidg = this "
                          "machine is the USB gadget")
    out.add_argument("--serial-port", help="e.g. /dev/ttyUSB0 (with --sink serial)")
    out.add_argument("--baud", type=int, default=1_000_000,
                     help="serial baud (default 1000000: an exact divisor on a 16MHz "
                          "ATmega32U4, unlike 921600)")
    out.add_argument("--hid-device", default="/dev/hidg0",
                     help="gadget node (with --sink hidg)")
    out.add_argument("--echo-repeats", action="store_true",
                     help="log every keepalive too, not just changes")

    kb = p.add_argument_group("keyboard capture")
    kb.add_argument("--device", action="append", dest="devices", metavar="PATH",
                    help="explicit /dev/input/eventN to forward; repeatable. "
                         "Default: auto-detect by name")
    kb.add_argument("--no-keyboard", action="store_true",
                    help="do not capture a keyboard; trigger only")
    kb.add_argument("--no-grab", action="store_true",
                    help="do NOT take the keyboard exclusively. The Pi will then "
                         "also receive everything you type. Testing only")
    kb.add_argument("--list", action="store_true",
                    help="list candidate keyboards and exit")

    trg = p.add_argument_group("trigger")
    trg.add_argument("--trigger-key", default="k",
                     help="key held while a person covers the ROI centre (default k). "
                          "f13-f15 exist on no real keyboard, so they can never "
                          "collide with something you actually press")
    trg.add_argument("--udp-port", type=int, default=TRIGGER_PORT)
    trg.add_argument("--http-port", type=int, default=HTTP_PORT)
    trg.add_argument("--no-http", action="store_true", help="disable the HTTP API")

    tim = p.add_argument_group("timing")
    tim.add_argument("--keepalive-ms", type=float, default=20.0,
                     help="re-send the current report this often (default 20)")
    tim.add_argument("--watchdog-ms", type=float, default=250.0,
                     help="release trigger keys after this much silence from the Mac "
                          "(default 250, ~12 missed keepalives)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list:
        from .keyboard import list_keyboards
        found = list_keyboards()
        if not found:
            # stderr, not stdout: a listing that found nothing must print nothing to
            # stdout, so callers can treat "any output" as "found something".
            print("No keyboard-like input device found.\n"
                  "Plug the Logitech receiver into a USB-A port and try again.",
                  file=sys.stderr)
            return 1
        for d in found:
            print(f"{d['path']}\t{d['name']}")
        return 0

    try:
        trigger_usage = resolve_trigger_key(args.trigger_key)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        sink = build_sink(
            args.sink,
            port=args.serial_port,
            baud=args.baud,
            device=args.hid_device,
            echo_repeats=args.echo_repeats,
        )
    except Exception as exc:
        print(f"error: could not open the {args.sink} sink: {exc}", file=sys.stderr)
        return 2

    state = KeyState()
    emitter = Emitter(state, sink, keepalive_s=args.keepalive_ms / 1000.0)
    watchdog = TriggerWatchdog(state, emitter, timeout_s=args.watchdog_ms / 1000.0)

    keyboard = None
    if not args.no_keyboard:
        try:
            from .keyboard import KeyboardReader
            keyboard = KeyboardReader(state, emitter, device_paths=args.devices,
                                      grab=not args.no_grab)
        except ImportError:
            print("error: python3-evdev is not installed. Run pi-agent/setup.sh, "
                  "or pass --no-keyboard to run trigger-only.", file=sys.stderr)
            return 2

    trigger = TriggerReceiver(state, emitter, watchdog, trigger_usage,
                              port=args.udp_port)
    ctx = AgentContext(state, emitter, watchdog, keyboard, trigger, sink)

    # Output first: nothing may generate a keypress before there is somewhere to put it.
    emitter.start()
    watchdog.start()
    trigger.start()
    if keyboard:
        keyboard.start()

    http = None
    if not args.no_http:
        try:
            http = HttpApi(ctx, port=args.http_port)
            http.start()
        except OSError as exc:
            print(f"warning: HTTP API disabled ({exc})", file=sys.stderr)

    print(f"[piproxy] sink={sink.name} trigger_key={args.trigger_key!r} "
          f"(usage 0x{trigger_usage:02x})", flush=True)
    print(f"[piproxy] trigger UDP on :{args.udp_port}"
          + (f", HTTP API on :{args.http_port}" if http else ""), flush=True)
    if keyboard and args.no_grab:
        print("[piproxy] WARNING: --no-grab, this Pi will also type what you type",
              file=sys.stderr, flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()

    print("\n[piproxy] shutting down; releasing all keys", flush=True)
    if http:
        http.stop()
    trigger.stop()
    if keyboard:
        keyboard.stop()
    watchdog.stop()
    # Last, and it sends an all-zero report: every key comes up before we exit. A
    # crash skips this, which is what the downstream watchdog is for.
    emitter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
