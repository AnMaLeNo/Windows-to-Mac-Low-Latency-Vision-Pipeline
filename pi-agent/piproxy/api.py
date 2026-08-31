"""How the Mac tells the Pi that a car is on the ROI's centre pixel.

Two doors, on purpose:

UDP (the hot path). One byte, 0x01 = active / 0x00 = idle - byte-for-byte the same
protocol the ESP32 serial link already spoke, so mac-app/trigger.py keeps its logic
and only swaps its transport. Fire-and-forget suits a stream that is re-sent 50x a
second: there is nothing to retransmit, because a newer state is always 20ms away.

HTTP (control and inspection). Same trigger reachable with curl, plus /status and
manual key presses for testing. Convenient, but it is a request/response round trip
on a fresh connection - fine at human rates, not for the 50Hz stream.
"""

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .keymap import resolve_trigger_key
from .report import describe

TRIGGER_PORT = 48010   # UDP, the hot path
HTTP_PORT = 48011      # TCP, control and status


class TriggerReceiver:
    """Blocking UDP reader. One byte in, trigger state out.

    Drains the whole socket backlog and keeps only the newest datagram before
    acting. If bytes have piled up they are stale history, and the freshest one is
    both the correct state and the fastest way to catch up - the same reasoning the
    ESP32 firmware used when draining its UART buffer.
    """

    def __init__(self, state, emitter, watchdog, trigger_usage: int,
                 host: str = "0.0.0.0", port: int = TRIGGER_PORT):
        self.state = state
        self.emitter = emitter
        self.watchdog = watchdog
        self.trigger_usage = trigger_usage
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Deliberately NO SO_REUSEADDR. On UDP it buys nothing - there is no TIME_WAIT
        # to work around - and it lets a second instance bind this port successfully,
        # after which the kernel hands each datagram to one of them and the trigger
        # silently goes to whichever process you were not looking at. Failing the bind
        # is the correct, loud behaviour.
        self.sock.bind((host, port))
        self.host, self.port = host, port
        self.packets = 0
        self.stale_dropped = 0
        self.last_peer = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="trigger-udp")

    def apply(self, active: bool) -> None:
        self.state.set_trigger([self.trigger_usage] if active else [])
        self.watchdog.feed()
        self.emitter.nudge()

    def _loop(self) -> None:
        self.sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, peer = self.sock.recvfrom(64)
            except socket.timeout:
                continue
            except OSError:
                break

            self.sock.setblocking(False)
            while True:
                try:
                    data, peer = self.sock.recvfrom(64)
                    self.stale_dropped += 1
                except (BlockingIOError, OSError):
                    break
            self.sock.settimeout(0.2)

            if not data:
                continue
            self.packets += 1
            self.last_peer = f"{peer[0]}:{peer[1]}"
            self.apply(data[-1] != 0)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def status(self) -> dict:
        return {
            "udp_port": self.port,
            "packets": self.packets,
            "stale_dropped": self.stale_dropped,
            "last_peer": self.last_peer,
        }


def make_http_handler(ctx):
    """Build the request handler bound to the running agent's objects."""

    class Handler(BaseHTTPRequestHandler):
        # BaseHTTPRequestHandler logs every request to stderr, which at any real
        # rate drowns out the messages that matter.
        def log_message(self, fmt, *args):
            pass

        def _reply(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, UnicodeDecodeError):
                return {}

        def do_GET(self):
            if self.path in ("/health", "/healthz"):
                return self._reply(200, {"ok": True})
            if self.path == "/status":
                return self._reply(200, ctx.status())
            if self.path == "/keyboards":
                from .keyboard import list_keyboards
                return self._reply(200, {"candidates": list_keyboards()})
            return self._reply(404, {"error": "not found",
                                     "routes": ["/health", "/status", "/keyboards",
                                                "POST /trigger", "POST /key"]})

        def do_POST(self):
            body = self._body()
            if self.path == "/trigger":
                if "active" not in body:
                    return self._reply(400, {"error": "expected {\"active\": true|false}"})
                ctx.trigger.apply(bool(body["active"]))
                return self._reply(200, {"active": bool(body["active"])})

            if self.path == "/key":
                name, action = body.get("key"), body.get("action", "press")
                if not name:
                    return self._reply(400, {"error": "expected {\"key\": \"k\", "
                                                      "\"action\": \"press\"|\"release\"}"})
                try:
                    usage = resolve_trigger_key(name)
                except ValueError as exc:
                    return self._reply(400, {"error": str(exc)})
                if action == "press":
                    ctx.state.press_physical(usage)
                elif action == "release":
                    ctx.state.release_physical(usage)
                else:
                    return self._reply(400, {"error": "action must be press or release"})
                ctx.emitter.nudge()
                return self._reply(200, {"key": name, "action": action, "usage": usage})

            return self._reply(404, {"error": "not found"})

    return Handler


class HttpApi:
    def __init__(self, ctx, host: str = "0.0.0.0", port: int = HTTP_PORT):
        self.server = ThreadingHTTPServer((host, port), make_http_handler(ctx))
        self.port = port
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True, name="http-api")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass


class AgentContext:
    """What the API is allowed to see and touch."""

    def __init__(self, state, emitter, watchdog, keyboard, trigger, sink):
        self.state = state
        self.emitter = emitter
        self.watchdog = watchdog
        self.keyboard = keyboard
        self.trigger = trigger
        self.sink = sink

    def status(self) -> dict:
        last = self.emitter.last_report
        return {
            "sink": {
                "kind": self.sink.name,
                # connected is the field to look at when reports stop arriving at
                # the PC while everything else here looks healthy.
                "connected": self.sink.healthy,
                "port": getattr(self.sink, "port", None),
                "dropped": getattr(self.sink, "dropped", 0),
                "reconnects": getattr(self.sink, "reconnects", 0),
                "last_error": getattr(self.sink, "last_error", None),
            },
            "emitter": {
                "alive": self.emitter.alive,
                "errors": self.emitter.errors,
            },
            "keyboard": self.keyboard.status() if self.keyboard else
                        {"attached": [], "note": "capture disabled"},
            "trigger": {
                **self.trigger.status(),
                "stale": self.watchdog.stale,
                "watchdog_fired": self.watchdog.fired,
            },
            "state": self.state.snapshot(),
            "last_report": describe(last) if last else None,
        }
