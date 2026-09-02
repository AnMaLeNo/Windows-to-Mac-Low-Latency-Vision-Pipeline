"""Contract 3: the routes, the JSON API and the one SSE stream. Stdlib http.server.

The shape is pi-agent/piproxy/api.py's: a handler class built by a factory that closes
over the context, _reply/_body helpers, a threading server on its own thread. Four
things here are deliberate and easy to lose in a tidy-up:

  - Cache-Control: no-store on everything, static files included. The page is plain
    ES modules with no build step and no service worker, precisely so that a saved
    file is the file the browser runs next reload. A cache in between is the one
    component that would serve stale modules during development.
  - /events has no Content-Length and says Connection: close. It is an HTTP/1.1
    response of unbounded length, and the handler's own loop owns the socket until the
    browser goes away or the bus is closed. Every other response carries a
    Content-Length, because protocol_version is HTTP/1.1 and keep-alive without one
    would hang the next request.
  - Static paths are resolved with realpath and must stay under static_dir. The server
    binds to loopback and has no secrets, but "GET /static/../server.py" reading its
    own source is still the wrong answer.
  - Every route runs under _guarded(): a bug in one answers 500 with the exception's
    repr. The base class would log a traceback to the terminal and drop the socket,
    which the page sees as a failed fetch with no reason - and the reason is the one
    thing a debug tool owes its user.
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from .runner import RunnerBusy

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 50511
HEARTBEAT_S = 5.0
# A form's values are a few hundred bytes; a body anywhere near this is not a form.
MAX_BODY = 1 << 20
# How long one sendall() to a browser may stall before the stream is given up on.
EVENTS_WRITE_TIMEOUT_S = 10.0

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
}

ROUTES = ["GET /", "GET /static/<path>", "GET /manifest.webmanifest", "GET /icons/<name>",
          "GET /api/args", "GET /api/status", "GET /api/log?n=200", "GET /events",
          "POST /api/preview", "POST /api/start", "POST /api/stop", "POST /api/oneshot"]


def format_sse(event, data):
    """One SSE record. json.dumps escapes every newline, so the data is one line - the
    record framing depends on that, and on the event name being ours."""
    body = json.dumps(data, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode("utf-8")


class DashboardContext:
    """What the handler is allowed to see and touch."""

    def __init__(self, runner, subscriber, encoder, bus, started_at=None):
        self.runner = runner
        self.subscriber = subscriber
        self.encoder = encoder
        self.bus = bus
        self.started_at = started_at if started_at is not None else time.time()

    def status(self):
        return {
            "process": self.runner.status(),
            "telemetry": self.subscriber.status(),
            "encoder": self.encoder.status(),
            "hello": self.bus.last.get("hello"),
            "clients": self.bus.client_count(),
            "uptime_s": time.time() - self.started_at,
        }


def make_handler(ctx, static_dir):
    root = os.path.realpath(static_dir)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "dashboard/1"

        # Every request logged to stderr would bury the child's own output, which is
        # the thing the terminal is for.
        def log_message(self, fmt, *args):
            pass

        # --- helpers -----------------------------------------------------------------
        def send_response(self, code, message=None):
            # Remembered so that a route raising after its headers went out is not
            # answered a second time (see _guarded).
            self._responded = True
            super().send_response(code, message)

        def _guarded(self, route):
            """Run a route; a bug in it becomes a 500 that says why. Once a response
            has started there is nothing sensible left to send, so the exception goes
            on to the base class, which logs it and closes the connection."""
            self._responded = False
            try:
                route()
            except Exception as exc:
                if self._responded:
                    raise
                # Whatever the route did with the body is unknown; a keep-alive peer
                # must not reuse this connection.
                self.close_connection = True
                self._reply(500, {"error": repr(exc)})

        def _reply(self, code, payload):
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _body(self):
            """The request body as a JSON object, or None once a 400 or 413 has been
            sent. Exactly Content-Length bytes are read, never more and never "until
            EOF": with keep-alive, an unread body is parsed as the next request, and a
            read that waits for EOF waits for the browser to give up."""
            raw = self.headers.get("Content-Length")
            try:
                length = int(raw) if raw is not None else 0
            except ValueError:
                self.close_connection = True
                self._reply(400, {"error": f"Content-Length {raw!r} is not an integer"})
                return None
            if length < 0:
                self.close_connection = True
                self._reply(400, {"error": "Content-Length must not be negative"})
                return None
            if length > MAX_BODY:
                # Refused unread, so the connection cannot carry another request.
                self.close_connection = True
                self._reply(413, {"error": f"body larger than {MAX_BODY} bytes"})
                return None
            data = self.rfile.read(length) if length else b""
            if len(data) != length:
                # The peer went away mid-body. Not "{}": a truncated /api/start must
                # not become a launch with the defaults.
                self.close_connection = True
                self._reply(400, {"error": f"body truncated ({len(data)} of {length} bytes)"})
                return None
            if not data:
                return {}
            try:
                parsed = json.loads(data)         # UnicodeDecodeError is a ValueError
            except ValueError:
                parsed = None
            if not isinstance(parsed, dict):
                self._reply(400, {"error": "body must be a JSON object"})
                return None
            return parsed

        def _file(self, rel):
            """A file under static_dir, or a JSON 404/403. rel is URL-decoded."""
            if "\0" in rel:
                return self._reply(404, {"error": "not found"})
            path = os.path.realpath(os.path.join(root, rel))
            if path != root and not path.startswith(root + os.sep):
                return self._reply(403, {"error": "forbidden"})
            if not os.path.isfile(path):
                return self._reply(404, {"error": "not found", "path": rel})
            try:
                with open(path, "rb") as fh:
                    body = fh.read()
            except OSError as exc:
                return self._reply(500, {"error": str(exc)})
            mime = MIME.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _not_found(self):
            return self._reply(404, {"error": "not found", "routes": ROUTES})

        # --- GET ---------------------------------------------------------------------
        def do_GET(self):
            self._guarded(self._get)

        def do_HEAD(self):
            # A GET whose body is withheld: _reply and _file look at self.command, so
            # the status and the Content-Length are the ones the GET would carry.
            self._guarded(self._get)

        def _get(self):
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)

            if path == "/":
                return self._file("index.html")
            if path.startswith("/static/"):
                return self._file(unquote(path[len("/static/"):]))
            if path == "/manifest.webmanifest":
                return self._file("manifest.webmanifest")
            if path.startswith("/icons/"):
                return self._file(os.path.join("icons", unquote(path[len("/icons/"):])))

            if path == "/api/args":
                try:
                    return self._reply(200, ctx.runner.describe())
                except RuntimeError as exc:
                    return self._reply(500, {"error": str(exc)})
            if path == "/api/status":
                return self._reply(200, ctx.status())
            if path == "/api/log":
                try:
                    n = int(query.get("n", ["200"])[0])
                except ValueError:
                    return self._reply(400, {"error": "n must be an integer"})
                return self._reply(200, {"lines": ctx.runner.log_tail(n)})
            if path == "/events":
                if self.command == "HEAD":
                    return self._reply(405, {"error": "the stream has no HEAD"})
                return self._events()
            return self._not_found()

        # --- POST --------------------------------------------------------------------
        def do_POST(self):
            self._guarded(self._post)

        def _post(self):
            path = urlsplit(self.path).path
            body = self._body()
            if body is None:
                return                  # _body() has already answered

            if path == "/api/preview":
                values = body.get("values", {})
                try:
                    return self._reply(200, ctx.runner.preview(values))
                except ValueError as exc:
                    return self._reply(400, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._reply(500, {"error": str(exc)})

            if path == "/api/start":
                values = body.get("values", {})
                try:
                    return self._reply(200, ctx.runner.start(values))
                except RunnerBusy as exc:
                    return self._reply(409, {"error": str(exc),
                                             "process": ctx.runner.status()})
                except ValueError as exc:
                    return self._reply(400, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._reply(500, {"error": str(exc)})

            if path == "/api/stop":
                return self._reply(200, {"exit_code": ctx.runner.stop()})

            if path == "/api/oneshot":
                flag = body.get("flag")
                if not isinstance(flag, str) or not flag:
                    return self._reply(400, {"error": "expected {\"flag\": \"--list-x\"}"})
                try:
                    return self._reply(200, ctx.runner.oneshot(flag))
                except RunnerBusy as exc:
                    return self._reply(409, {"error": str(exc),
                                             "process": ctx.runner.status()})
                except ValueError as exc:
                    return self._reply(400, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._reply(500, {"error": str(exc)})

            return self._not_found()

        # --- the stream --------------------------------------------------------------
        def _events(self):
            # Subscribed BEFORE the headers go out: by the time a client sees the
            # response start, nothing published afterwards can be missed.
            client = ctx.bus.subscribe()
            # A peer that is open but no longer reading (a lid closed on a tab) would
            # otherwise park this thread in sendall() for good, with its client still
            # registered as wanting frames. A timed-out write ends the stream, and an
            # EventSource reconnects on its own.
            self.connection.settimeout(EVENTS_WRITE_TIMEOUT_S)
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                self.wfile.write(b"retry: 1000\n\n")
                self.wfile.flush()
                while not client.closed:
                    batch = client.next(timeout=HEARTBEAT_S)
                    if not batch:
                        if client.closed:
                            break
                        batch = [("heartbeat", {"t": time.time()})]
                    for event, data in batch:
                        self.wfile.write(format_sse(event, data))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                ctx.bus.unsubscribe(client)
                self.close_connection = True

    return Handler


class DashboardServer:
    def __init__(self, ctx, static_dir, host=DEFAULT_HOST, port=DEFAULT_PORT):
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def handle_error(self, request, client_address):
                # A browser resets keep-alive connections as a matter of course - a
                # reload, a closed tab, an EventSource giving up. The default prints
                # a forty-line traceback into the terminal for each one.
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, BrokenPipeError,
                                    ConnectionAbortedError)):
                    return
                print(f"[dashboard] request from {client_address[0]}:{client_address[1]} "
                      f"failed: {exc!r}", file=sys.stderr, flush=True)

        self.ctx = ctx
        self.server = Server((host, port), make_handler(ctx, static_dir))
        self.host, self.port = self.server.server_address[:2]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                        name="dashboard-http")

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"

    def start(self):
        self._thread.start()

    def stop(self):
        # The bus first: that is what ends the /events loops, which serve_forever's
        # shutdown() does not wait for and server_close() cannot reach.
        self.ctx.bus.close()
        try:
            self.server.shutdown()
        except Exception:
            pass
        self.server.server_close()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)
