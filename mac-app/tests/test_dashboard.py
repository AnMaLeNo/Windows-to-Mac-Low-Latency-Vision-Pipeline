"""Checks the dashboard's server side: the argv builder, the bus, the reader, the
encoder, the runner and the HTTP routes - docs/DASHBOARD.md contracts 2 and 3, and
the reading end of contract 1.

Nothing here needs macvision to be runnable. The runner is pointed at a fake module
written into a temp dir that speaks contract 2 and behaves like a well-mannered child
(prints, waits, exits 0 on SIGTERM), and the subscriber is fed bytes produced by
macvision.telemetry.encode_message from a local socket. So this runs on any machine,
with nothing installed - which is also the claim it verifies about the dashboard.

    python3 -m tests.test_dashboard      (from mac-app/)
"""

import base64
import http.client
import io
import json
import os
import shlex
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import zlib
from contextlib import redirect_stderr

from dashboard.__main__ import orphan_warning
from dashboard.bus import Bus
from dashboard.frames import FrameEncoder, encode_image, encode_png
from dashboard.runner import Runner, RunnerBusy, argv_from_values, command_string
from dashboard.server import DashboardContext, DashboardServer, format_sse
from dashboard.subscriber import TelemetrySubscriber
from macvision.telemetry import encode_message

# Sampled before anything runs: importing the dashboard must not pull in the heavy
# modules. frames.py may import numpy later, from inside a function - that is allowed.
AT_IMPORT = set(sys.modules)

SPEC = {
    "v": 1, "prog": "fakevision", "description": "a stand-in for macvision",
    "groups": [
        {"title": "main", "args": [
            {"dest": "name", "flag": "--name", "kind": "str", "default": "x",
             "choices": None, "help": "a string", "oneshot": False},
            {"dest": "count", "flag": "--count", "kind": "int", "default": 1,
             "choices": None, "help": "an int", "oneshot": False},
            {"dest": "ratio", "flag": "--ratio", "kind": "float", "default": 0.5,
             "choices": None, "help": "a float", "oneshot": False},
            {"dest": "fast", "flag": "--fast", "kind": "bool", "default": False,
             "choices": None, "help": "a flag", "oneshot": False},
            {"dest": "mode", "flag": "--mode", "kind": "choice", "default": "a",
             "choices": ["a", "b"], "help": "a choice", "oneshot": False},
            {"dest": "telemetry", "flag": "--telemetry", "kind": "str", "default": "",
             "choices": None, "help": "where to publish", "oneshot": False},
            {"dest": "boom", "flag": "--boom", "kind": "bool", "default": False,
             "choices": None, "help": "exit 3 at once", "oneshot": False}]},
        {"title": "options", "args": [
            {"dest": "list_things", "flag": "--list-things", "kind": "bool",
             "default": False, "choices": None, "help": "print two lines and exit",
             "oneshot": True},
            {"dest": "list_bytes", "flag": "--list-bytes", "kind": "bool",
             "default": False, "choices": None, "help": "print a byte that is not UTF-8",
             "oneshot": True}]}]}

FAKE_MAIN = '''\
import json, os, signal, sys, time
SPEC = json.loads(%s)
argv = sys.argv[1:]
if "--describe-args" in argv:
    print(json.dumps(SPEC))
    sys.exit(0)
if "--list-things" in argv:
    print("thing one")
    print("thing two")
    sys.exit(0)
if "--list-bytes" in argv:
    sys.stdout.buffer.write(b"raw \\xff byte\\n")
    sys.stdout.flush()
    sys.exit(0)
if "--boom" in argv:
    sys.exit(3)
print("ready", flush=True)
print("telemetry=" + os.environ.get("MACVISION_TELEMETRY", "<unset>"), flush=True)
print("note", file=sys.stderr, flush=True)
signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
while True:
    time.sleep(0.05)
''' % repr(json.dumps(SPEC))


def wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def png_chunks(data):
    """[(type, payload, crc_ok)] after the signature."""
    out = []
    pos = 8
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])
        out.append((kind, payload, crc == (zlib.crc32(kind + payload) & 0xFFFFFFFF)))
        pos += 12 + length
    return out


class SseReader:
    """Parses `event:`/`data:` records off an http.client response."""

    def __init__(self, resp):
        self.resp = resp
        self.seen = []

    def until(self, pred, limit=300):
        record = {}
        while limit > 0:
            line = self.resp.readline()
            if not line:
                return None
            line = line.decode("utf-8").rstrip("\r\n")
            if line == "":
                if "event" in record:
                    event = (record["event"], json.loads(record.get("data", "null")))
                    self.seen.append(event)
                    limit -= 1
                    if pred(event):
                        return event
                record = {}
                continue
            key, _, value = line.partition(":")
            record[key] = value[1:] if value.startswith(" ") else value
        return None


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    def expect_value_error(label, fn, needle=None):
        try:
            fn()
        except ValueError as exc:
            if needle and needle not in str(exc):
                failures.append(f"{label}: ValueError does not name {needle!r}: {exc}")
        except Exception as exc:
            failures.append(f"{label}: raised {exc!r}, expected ValueError")
        else:
            failures.append(f"{label}: did not raise")

    def expect_busy(label, fn):
        try:
            fn()
        except RunnerBusy:
            pass
        except Exception as exc:
            failures.append(f"{label}: raised {exc!r}, expected RunnerBusy")
        else:
            failures.append(f"{label}: did not raise")

    # --- contract 2: argv_from_values ---------------------------------------------
    check("null and empty are omitted",
          argv_from_values(SPEC, {"name": None, "count": "", "mode": None}), [])
    check("bool true adds the bare flag", argv_from_values(SPEC, {"fast": True}), ["--fast"])
    check("bool false adds nothing", argv_from_values(SPEC, {"fast": False}), [])
    for truthy in ("true", "True", "TRUE", 1, "1"):
        check(f"bool accepts {truthy!r}", argv_from_values(SPEC, {"fast": truthy}), ["--fast"])
    for falsy in ("false", "False", 0, "0"):
        check(f"bool accepts {falsy!r}", argv_from_values(SPEC, {"fast": falsy}), [])
    expect_value_error("a bool that is neither", lambda: argv_from_values(SPEC, {"fast": "maybe"}),
                       "fast")
    check("int is coerced", argv_from_values(SPEC, {"count": "12"}), ["--count", "12"])
    check("int from a number", argv_from_values(SPEC, {"count": 7}), ["--count", "7"])
    expect_value_error("a bad int names the dest",
                       lambda: argv_from_values(SPEC, {"count": "twelve"}), "count")
    expect_value_error("a fractional int names the dest",
                       lambda: argv_from_values(SPEC, {"count": "1.5"}), "count")
    check("float is coerced", argv_from_values(SPEC, {"ratio": "0.25"}), ["--ratio", "0.25"])
    expect_value_error("a bad float names the dest",
                       lambda: argv_from_values(SPEC, {"ratio": "x"}), "ratio")
    check("choice passes when valid", argv_from_values(SPEC, {"mode": "b"}), ["--mode", "b"])
    expect_value_error("choice rejects the rest", lambda: argv_from_values(SPEC, {"mode": "c"}),
                       "mode")
    expect_value_error("an unknown dest is an error, not a drop",
                       lambda: argv_from_values(SPEC, {"nmae": "x"}), "nmae")
    check("oneshot dests are skipped even when set",
          argv_from_values(SPEC, {"list_things": True, "name": "n"}), ["--name", "n"])
    check("str passes through", argv_from_values(SPEC, {"name": "hello world"}),
          ["--name", "hello world"])
    check("argv follows the description's order, not the values'",
          argv_from_values(SPEC, {"mode": "a", "fast": "1", "name": "n"}),
          ["--name", "n", "--fast", "--mode", "a"])
    expect_value_error("values must be an object", lambda: argv_from_values(SPEC, ["--x"]))
    check("command_string quotes for a shell",
          command_string("/usr/bin/python3", "macvision", ["--name", "a b"]),
          "/usr/bin/python3 -m macvision --name 'a b'")

    # --- the bus ---------------------------------------------------------------------
    bus = Bus()
    check("no client, no frames wanted", bus.wants_frames(), False)
    client = bus.subscribe()
    check("one client wants frames", bus.wants_frames(), True)
    check("client_count", bus.client_count(), 1)
    for i in range(10):
        bus.publish("frame", {"seq": i})
    bus.publish("log", {"line": "a"})
    bus.publish("stats", {"n": 1})
    bus.publish("process", {"state": "idle"})
    batch = client.next(timeout=0.5)
    check("ordered events first, then exactly one frame - the newest", batch,
          [("log", {"line": "a"}), ("stats", {"n": 1}), ("process", {"state": "idle"}),
           ("frame", {"seq": 9})])
    check("and the slots are cleared", client.next(timeout=0.05), [])
    bus.unsubscribe(client)
    check("unsubscribed: nobody wants frames", bus.wants_frames(), False)

    b2 = Bus()
    b2.publish("log", {"line": "l1"})
    b2.publish("process", {"state": "idle"})
    b2.publish("hello", {"pid": 1})
    b2.publish("frame", {"seq": 1})
    b2.publish("log", {"line": "l2"})
    b2.publish("telemetry", {"connected": False})
    expected = [("hello", {"pid": 1}), ("process", {"state": "idle"}),
                ("telemetry", {"connected": False}),
                ("log", {"line": "l1"}), ("log", {"line": "l2"})]
    check("replay: hello, process, telemetry, (stats), then the log ring; no frame",
          b2.replay(), expected)
    c2 = b2.subscribe()
    check("subscribe() preloads the replay", c2.next(timeout=0.5), expected)
    b2.publish("stats", {"n": 9})
    check("stats is remembered", "stats" in b2.last, True)
    b2.forget("stats")
    check("forget() takes it out of the replay", [e for e, _ in b2.replay()],
          ["hello", "process", "telemetry", "log", "log"])
    b2.forget("stats")                       # already gone: not an error
    b2.forget("frame")                       # never remembered: not an error either

    # A client nobody reads must never slow publish() down, and must count its loss.
    slow = b2.subscribe(replay=False)
    t0 = time.monotonic()
    for i in range(3000):
        b2.publish("log", {"line": str(i)})
    took = time.monotonic() - t0
    if took > 1.0:
        failures.append(f"3000 publishes to a stalled client took {took:.2f}s")
    check("the stalled client's overflow is counted", slow.dropped, 3000 - 512)
    check("the log ring keeps the last 500", len(b2.log), 500)
    t0 = time.monotonic()
    b2.close()
    check("close() wakes a waiting client at once", slow.next(timeout=5.0) != [], True)
    if time.monotonic() - t0 > 1.0:
        failures.append("close() did not wake the client promptly")
    check("a closed client says so", slow.closed, True)

    # --- format_sse ------------------------------------------------------------------
    rec = format_sse("log", {"line": "one\ntwo"})
    check("format_sse: one event line, one data line, one blank", rec.count(b"\n"), 3)
    check("format_sse escapes the newline", rec.startswith(b'event: log\ndata: {"line":"one\\ntwo"}\n\n'),
          True)

    # --- PNG, by hand ----------------------------------------------------------------
    bgr = bytes([1, 2, 3, 4, 5, 6])          # two pixels, B G R each
    mime, png = encode_image(bgr, 2, 1, 3, "bgr8", backend="png")
    check("png mime", mime, "image/png")
    check("png signature", png[:8], b"\x89PNG\r\n\x1a\n")
    chunks = png_chunks(png)
    check("chunk order", [k for k, _, _ in chunks], [b"IHDR", b"IDAT", b"IEND"])
    check("every crc is right", all(ok for _, _, ok in chunks), True)
    ihdr = chunks[0][1]
    check("IHDR: width, height, depth, colour type", struct.unpack(">IIBB", ihdr[:10]),
          (2, 1, 8, 2))
    raw = zlib.decompress(chunks[1][1])
    check("IDAT: h rows of filter byte + RGB pixels, channels swapped",
          raw, b"\x00" + bytes([3, 2, 1, 6, 5, 4]))
    check("IDAT length is h*(1+3w)", len(raw), 1 * (1 + 3 * 2))
    mime, gray = encode_image(bytes([9, 8, 7, 6]), 2, 2, 1, "gray8", backend="png")
    gchunks = png_chunks(gray)
    check("gray: colour type 0", struct.unpack(">IIBB", gchunks[0][1][:10]), (2, 2, 8, 0))
    check("gray: rows are untouched", zlib.decompress(gchunks[1][1]),
          b"\x00" + bytes([9, 8]) + b"\x00" + bytes([7, 6]))
    _, rgb = encode_image(bytes([3, 2, 1, 6, 5, 4]), 2, 1, 3, "rgb8", backend="png")
    check("rgb8 is not swapped", rgb, png)
    check("encode_png is what encode_image used", encode_png(bytes([3, 2, 1, 6, 5, 4]), 2, 1, 3),
          png)
    expect_value_error("a wrong payload length", lambda: encode_image(b"\0" * 5, 2, 1, 3, "bgr8",
                                                                     backend="png"))
    expect_value_error("an unknown backend", lambda: encode_image(bgr, 2, 1, 3, "bgr8",
                                                                  backend="gif"))
    try:
        import cv2  # noqa: F401  (only to know what backend=None must pick here)
        has_cv2 = True
    except ImportError:
        has_cv2 = False
    mime, _ = encode_image(bgr, 2, 1, 3, "bgr8")
    check("backend=None picks jpeg with opencv, png without",
          mime, "image/jpeg" if has_cv2 else "image/png")
    url = "data:%s;base64,%s" % ("image/png", base64.b64encode(png).decode("ascii"))
    head, _, b64 = url.partition(",")
    check("data URL round trip", (head, base64.b64decode(b64)), ("data:image/png;base64", png))

    # --- the encoder: rate-limited, coalescing, only when watched --------------------
    fbus = Bus()
    enc = FrameEncoder(fbus, fps=100, backend="png")
    hdr = {"type": "frame", "w": 2, "h": 1, "c": 3, "fmt": "bgr8"}
    enc.offer(dict(hdr, seq=1), bgr)
    enc.tick()
    check("no browser: not encoded, and the slot is held rather than skipped",
          (enc.offered, enc.encoded, enc.skipped), (1, 0, 0))
    watcher = fbus.subscribe()
    enc.tick()
    check("the held frame goes out on the first tick after a subscribe", enc.encoded, 1)
    batch = watcher.next(timeout=0.5)
    check("and it is that frame", [(e, d.get("seq")) for e, d in batch], [("frame", 1)])
    for seq in (2, 3, 4):
        enc.offer(dict(hdr, seq=seq), bgr)
    enc.tick()
    check("three offered between ticks: one encoded, two skipped",
          (enc.encoded, enc.skipped), (2, 2))
    batch = watcher.next(timeout=0.5)
    check("one frame event", [e for e, _ in batch], ["frame"])
    frame = batch[0][1]
    check("it is the newest", frame.get("seq"), 4)
    check("header fields travel with the image", frame.get("w"), 2)
    check("image is a png data URL", frame.get("image", "").startswith("data:image/png;base64,"),
          True)
    check("and it decodes to the png", base64.b64decode(frame["image"].split(",", 1)[1]), png)
    enc.tick()
    check("nothing new: nothing sent", enc.encoded, 2)
    enc.offer({"w": 5, "h": 5, "c": 3}, b"xx")
    with redirect_stderr(io.StringIO()):
        enc.tick()
    check("a bad frame is counted, not raised", enc.errors, 1)
    enc.start()
    enc.offer(dict(hdr, seq=5), bgr)
    check("the ticker thread encodes on its own", wait_for(lambda: enc.encoded == 3), True)
    enc.stop()
    check("stop() joins the ticker", enc.alive, False)
    st = enc.status()
    for key in ("backend", "fps", "offered", "encoded", "skipped", "errors"):
        if key not in st:
            failures.append(f"encoder status() is missing {key!r}")
    fbus.close()

    # --- the subscriber: connect, read, drop, reconnect ------------------------------
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    srv.settimeout(8.0)
    tport = srv.getsockname()[1]
    hold = threading.Event()
    server_errors = []

    def serve():
        try:
            # 1. a clean session: hello, three frames, a stats, then goodbye
            conn, _ = srv.accept()
            conn.sendall(encode_message({"type": "hello", "t": 1.0, "pid": 1})
                         + b"".join(encode_message({"type": "frame", "t": 1.0, "seq": i,
                                                    "w": 2, "h": 1, "c": 3, "fmt": "bgr8"},
                                                   bgr) for i in range(3))
                         + encode_message({"type": "stats", "t": 1.0, "n": 3}))
            conn.close()
            # 2. garbage where the magic should be: the reader must drop and retry
            conn, _ = srv.accept()
            conn.sendall(b"XXXX" + bytes(20))
            conn.settimeout(5.0)
            try:
                conn.recv(16)           # the subscriber closing is our cue
            except OSError:
                pass
            conn.close()
            # 3. a session that stays up until the test is done with it
            conn, _ = srv.accept()
            conn.sendall(encode_message({"type": "hello", "t": 2.0, "pid": 2}))
            hold.wait(8.0)
            conn.close()
        except Exception as exc:
            server_errors.append(repr(exc))

    threading.Thread(target=serve, daemon=True, name="fake-publisher").start()

    got = []
    states = []

    def on_message(header, payload):
        got.append((header["type"], payload))
        if header["type"] == "stats":
            raise RuntimeError("a bug in the listener")

    sub = TelemetrySubscriber("127.0.0.1", tport, on_message, on_state=states.append,
                              retry_s=0.1)
    check("the default retry is half a second",
          TelemetrySubscriber("127.0.0.1", 1, on_message).retry_s, 0.5)
    err = io.StringIO()
    with redirect_stderr(err):
        sub.start()
        ok = wait_for(lambda: len(states) >= 5 and sub.status()["messages"] >= 6, timeout=8.0)
        srv.close()
    if not ok:
        failures.append(f"the subscriber did not reconnect twice: states={states}, "
                        f"status={sub.status()}, publisher errors={server_errors}")
    check("connected, dropped, reconnected, dropped on bad magic, reconnected",
          states[:5], [True, False, True, False, True])
    check("every message reached the listener, in order",
          [t for t, _ in got], ["hello", "frame", "frame", "frame", "stats", "hello"])
    check("payloads arrive intact", got[1][1], bgr)
    st = sub.status()
    check("status: connected", st["connected"], True)
    check("status: messages", st["messages"], 6)
    check("status: frames", st["frames"], 3)
    check("status: reconnects", st["reconnects"], 2)
    check("status: bytes were counted", st["bytes"] > 0, True)
    check("status: last_message_at is stamped", st["last_message_at"] is not None, True)
    check("a raising listener is counted, not fatal", sub.callback_errors, 1)
    check("and the thread is still alive", sub.alive, True)
    if "on_message raised" not in err.getvalue():
        failures.append("the listener's exception was not reported on stderr")
    if "lost sync" not in (st["last_error"] or ""):
        failures.append(f"the bad magic was not the recorded reason: {st['last_error']!r}")
    hold.set()
    with redirect_stderr(io.StringIO()):
        sub.stop()
    check("stop() joins the reader", sub.alive, False)
    check("stop() reports the disconnect", states[-1], False)
    for key in ("connected", "messages", "frames", "bytes", "reconnects", "last_message_at"):
        if key not in st:
            failures.append(f"subscriber status() is missing {key!r}")

    # A peer that accepts and closes at once (a port held by something that is not
    # macvision) must be retried at the refusal pace, not as fast as the loop can go:
    # every attempt is two telemetry events to every browser. Started here and judged
    # after the runner section below, so it runs alongside the subprocess spawns.
    slam = socket.socket()
    slam.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    slam.bind(("127.0.0.1", 0))
    slam.listen(16)
    slam.settimeout(0.05)
    slam_done = threading.Event()

    def slammer():
        while not slam_done.is_set():
            try:
                conn, _ = slam.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            conn.close()

    threading.Thread(target=slammer, daemon=True, name="accept-and-close").start()
    slam_sub = TelemetrySubscriber("127.0.0.1", slam.getsockname()[1], lambda h, p: None,
                                   retry_s=0.1)
    slam_sub.start()
    slam_t0 = time.monotonic()

    # --- the orphan warning ------------------------------------------------------------
    url = "tcp://127.0.0.1:50510"
    own = {"state": "running", "pid": 42}
    check("the runner's own child is no orphan", orphan_warning({"pid": 42}, own, url), None)
    msg = orphan_warning({"pid": 43}, own, url) or ""
    check("another pid is named, with the url and the kill",
          ("pid 43" in msg, "kill 43" in msg, url in msg), (True, True, True))
    msg = orphan_warning({"pid": 42}, {"state": "idle", "pid": None}, url) or ""
    check("a hello while nothing was started is an orphan too", "kill 42" in msg, True)
    msg = orphan_warning({"pid": 42}, {"state": "exited", "pid": 42}, url) or ""
    check("and so is a hello from a pid the runner already saw exit", "kill 42" in msg, True)
    msg = orphan_warning({}, {"state": "idle", "pid": None}, url)
    check("a hello without a pid still warns, without a kill to suggest",
          (msg is not None, "kill" in (msg or "")), (True, False))

    # --- the runner, against a fake module -------------------------------------------
    tmp = tempfile.mkdtemp(prefix="dashboard-test-")
    saved_env = os.environ.pop("MACVISION_TELEMETRY", None)
    try:
        pkg = os.path.join(tmp, "fakevision")
        os.makedirs(pkg)
        with open(os.path.join(pkg, "__init__.py"), "w") as fh:
            fh.write("")
        with open(os.path.join(pkg, "__main__.py"), "w") as fh:
            fh.write(FAKE_MAIN)

        events = []
        runner = Runner(sys.executable, tmp, module="fakevision",
                        telemetry_url="tcp://127.0.0.1:59999",
                        on_event=lambda e, d: events.append((e, d)))
        spec = runner.describe()
        check("describe() returns the child's JSON", spec.get("prog"), "fakevision")
        check("describe() is cached", runner.describe() is spec, True)
        check("oneshot_flags()", runner.oneshot_flags(), ["--list-things", "--list-bytes"])
        pv = runner.preview({"name": "n", "count": "3", "fast": True})
        check("preview argv", pv["argv"], ["--name", "n", "--count", "3", "--fast"])
        check("preview command", pv["command"],
              shlex.join([sys.executable, "-m", "fakevision", "--name", "n", "--count",
                          "3", "--fast"]))
        check("idle status", runner.status()["state"], "idle")
        check("stop() with nothing running", runner.stop(), None)

        st = runner.start({"name": "x"})
        check("start() -> running", st["state"], "running")
        check("with a pid", isinstance(st["pid"], int) and st["pid"] > 0, True)
        check("and the argv", st["argv"], ["--name", "x"])
        check("since is the start time", isinstance(st["since"], float), True)
        expect_busy("a second start", lambda: runner.start({}))
        # A probe while the child runs would open every camera under it.
        expect_busy("a probe while the child runs", lambda: runner.oneshot("--list-things"))
        check("RunnerBusy is a RuntimeError", issubclass(RunnerBusy, RuntimeError), True)

        def lines(stream):
            return [d["line"] for e, d in events if e == "log" and d["stream"] == stream]

        ok = wait_for(lambda: len(lines("stdout")) >= 2 and len(lines("stderr")) >= 1)
        if not ok:
            failures.append(f"log events did not arrive: {events}")
        check("stdout lines are events", lines("stdout"),
              ["ready", "telemetry=tcp://127.0.0.1:59999"])
        check("stderr lines are events", lines("stderr"), ["note"])
        check("MACVISION_TELEMETRY reached the child",
              "telemetry=tcp://127.0.0.1:59999" in lines("stdout"), True)
        procs = [d for e, d in events if e == "process"]
        check("a process event right after spawn", procs[0]["state"] if procs else None,
              "running")
        for d in [d for e, d in events if e == "log"]:
            if not isinstance(d.get("t"), float):
                failures.append(f"log event without a timestamp: {d}")
                break

        code = runner.stop()
        check("stop() -> the child's exit code", code, 0)
        ok = wait_for(lambda: any(e == "process" and d["state"] == "exited" for e, d in events))
        if not ok:
            failures.append("no 'exited' process event after stop()")
        else:
            exited = [d for e, d in events if e == "process" and d["state"] == "exited"][-1]
            check("exited event: exit_code", exited["exit_code"], 0)
            check("exited event: pid kept", exited["pid"], st["pid"])
            check("exited event: since is the exit time", exited["since"] >= st["since"], True)
            s1, s2 = runner.status()["since"], runner.status()["since"]
            check("since is stamped once, not taken fresh per call", s1, s2)
            check("and it is the time the exited event carried", s1, exited["since"])
        check("status after stop", runner.status()["state"], "exited")
        check("stop() again returns the same code", runner.stop(), 0)
        check("log_tail(2) is two lines", len(runner.log_tail(2)), 2)
        check("log_tail(10) is everything so far",
              sorted(d["line"] for d in runner.log_tail(10)),
              sorted(["ready", "telemetry=tcp://127.0.0.1:59999", "note"]))

        # --boom: exits 3 on its own; the waiter reports it.
        events.clear()
        runner.start({"boom": True})
        ok = wait_for(lambda: any(e == "process" and d["state"] == "exited" for e, d in events))
        if not ok:
            failures.append("--boom: no exited event")
        else:
            exited = [d for e, d in events if e == "process" and d["state"] == "exited"][-1]
            check("--boom: exit_code 3", exited["exit_code"], 3)
        check("stop() on a finished child returns its code", runner.stop(), 3)

        # The form's own --telemetry wins over the environment.
        events.clear()
        runner.start({"telemetry": "none"})
        if not wait_for(lambda: len(lines("stdout")) >= 2):
            failures.append(f"--telemetry none: the child's stdout did not arrive: {events}")
        check("an explicit telemetry value leaves the env alone",
              "telemetry=<unset>" in lines("stdout"), True)
        runner.stop()

        # Probes.
        res = runner.oneshot("--list-things")
        check("oneshot: exit code", res["exit_code"], 0)
        check("oneshot: stdout", res["stdout"], "thing one\nthing two\n")
        check("oneshot: flag echoed", res["flag"], "--list-things")
        res = runner.oneshot("--list-bytes")
        check("oneshot: a byte that is not UTF-8 is replaced, not raised",
              (res["exit_code"], res["stdout"]), (0, "raw \ufffd byte\n"))
        expect_value_error("oneshot refuses a normal flag", lambda: runner.oneshot("--name"))
        expect_value_error("oneshot refuses a bool that is not a probe",
                           lambda: runner.oneshot("--boom"))
        expect_value_error("oneshot refuses nonsense", lambda: runner.oneshot("--nope"))

        # A runner that cannot describe says why.
        bad = Runner(sys.executable, tmp, module="no_such_module")
        try:
            bad.describe()
        except RuntimeError as exc:
            if "no_such_module" not in str(exc):
                failures.append(f"describe() error does not say what failed: {exc}")
        else:
            failures.append("describe() of a missing module did not raise")

        # The accept-and-close peer from above has been running through all of that.
        time.sleep(max(0.0, 0.3 - (time.monotonic() - slam_t0)))
        slam_sub.stop()
        slam_elapsed = time.monotonic() - slam_t0
        slam_done.set()
        slam.close()
        st = slam_sub.status()
        check("accept-and-close: the reader kept trying", st["reconnects"] >= 2, True)
        # retry_s=0.1 allows ten a second; the flood this guards against was ten
        # thousand. Generous, because the machine may be slow, not because it matters.
        if st["reconnects"] > slam_elapsed * 10 * 1.5 + 2:
            failures.append(f"accept-and-close: {st['reconnects']} reconnects in "
                            f"{slam_elapsed:.2f}s with retry_s=0.1 - sessions that "
                            f"deliver nothing are not paced")
        check("accept-and-close: nothing was invented", st["messages"], 0)

        # --- the server ------------------------------------------------------------
        static = os.path.join(tmp, "static")
        os.makedirs(os.path.join(static, "icons"))
        files = {"index.html": "<title>dash</title>", "app.js": "export default 1;",
                 "manifest.webmanifest": "{}", os.path.join("icons", "i.svg"): "<svg/>"}
        for name, text in files.items():
            with open(os.path.join(static, name), "w") as fh:
                fh.write(text)
        with open(os.path.join(tmp, "secret.txt"), "w") as fh:
            fh.write("not served")

        sbus = Bus()
        srunner = Runner(sys.executable, tmp, module="fakevision",
                         telemetry_url="tcp://127.0.0.1:59999", on_event=sbus.publish)
        nobody = free_port()
        ssub = TelemetrySubscriber("127.0.0.1", nobody, lambda h, p: None,
                                   on_state=lambda c: sbus.publish("telemetry", ssub.status()),
                                   retry_s=0.2)
        senc = FrameEncoder(sbus, fps=30, backend="png")
        ctx = DashboardContext(srunner, ssub, senc, sbus)
        server = DashboardServer(ctx, static, "127.0.0.1", 0)
        check("port 0 becomes a real port", server.port > 0, True)
        server.start()
        ssub.start()
        senc.start()
        sbus.publish("process", srunner.status())
        sbus.publish("telemetry", ssub.status())

        def req(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
            data = None if body is None else json.dumps(body).encode("utf-8")
            headers = {"Content-Type": "application/json"} if data else {}
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            conn.close()
            ctype = resp.getheader("Content-Type", "")
            parsed = json.loads(raw) if ctype.startswith("application/json") and raw else raw
            return resp.status, dict(resp.getheaders()), parsed

        def raw_request(data, half_close=False):
            """Send bytes exactly as given and parse one response: (status, headers,
            body). For the requests http.client refuses to make."""
            s = socket.create_connection(("127.0.0.1", server.port), timeout=5)
            s.sendall(data)
            if half_close:
                s.shutdown(socket.SHUT_WR)          # EOF, the way a dying peer sends it
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            status = int(lines[0].split()[1]) if lines and lines[0] else None
            hdrs = {k.strip().lower(): v.strip()
                    for k, _, v in (l.partition(b":") for l in lines[1:])}
            length = int(hdrs.get(b"content-length", b"0"))
            while len(rest) < length:
                chunk = s.recv(4096)
                if not chunk:
                    break
                rest += chunk
            s.close()
            return status, hdrs, rest

        code, hdrs, body = req("GET", "/")
        check("GET / -> index.html", (code, body), (200, b"<title>dash</title>"))
        check("GET / mime", hdrs.get("Content-Type"), "text/html; charset=utf-8")
        check("GET / is never cached", hdrs.get("Cache-Control"), "no-store")
        check("GET / has a length", hdrs.get("Content-Length"), str(len(body)))
        code, hdrs, body = req("GET", "/static/app.js")
        check("GET /static/app.js", (code, hdrs.get("Content-Type")),
              (200, "text/javascript; charset=utf-8"))
        code, hdrs, _ = req("GET", "/manifest.webmanifest")
        check("GET /manifest.webmanifest", (code, hdrs.get("Content-Type")),
              (200, "application/manifest+json"))
        code, hdrs, _ = req("GET", "/icons/i.svg")
        check("GET /icons/i.svg", (code, hdrs.get("Content-Type")), (200, "image/svg+xml"))
        code, hdrs, body = req("GET", "/static/../server.py")
        check("path traversal is refused", code in (403, 404), True)
        if isinstance(body, bytes) and b"BaseHTTPRequestHandler" in body:
            failures.append("GET /static/../server.py served the server's own source")
        for path in ("/static/../secret.txt", "/static/%2e%2e/secret.txt",
                     "/static/..%2fsecret.txt", "/static/%2e%2e%2fsecret.txt",
                     "/icons/../../secret.txt"):
            code, _, body = req("GET", path)
            check(f"traversal {path} is refused", code in (403, 404), True)
            if body == b"not served":
                failures.append(f"GET {path} served a file outside static/")
        code, hdrs, body = req("HEAD", "/")
        check("HEAD / -> the GET's status and length, no body",
              (code, hdrs.get("Content-Length"), body), (200, str(len(files["index.html"])), b""))
        code, hdrs, body = req("HEAD", "/api/status")
        check("HEAD /api/status -> 200 with a length and no body",
              (code, hdrs.get("Content-Length") is not None, body), (200, True, b""))
        code, _, _ = req("HEAD", "/events")
        check("HEAD /events -> 405, not a stream", code, 405)
        code, _, body = req("GET", "/static/missing.js")
        check("a missing file is a JSON 404", (code, isinstance(body, dict)), (404, True))
        code, hdrs, body = req("GET", "/nope")
        check("unknown route -> 404 with the routes", (code, "routes" in body), (404, True))
        check("JSON replies say no-store too", hdrs.get("Cache-Control"), "no-store")

        code, _, body = req("GET", "/api/args")
        check("GET /api/args -> contract 2, verbatim", (code, body), (200, SPEC))
        code, _, body = req("GET", "/api/status")
        check("GET /api/status", (code, body["process"]["state"]), (200, "idle"))
        ctx.status = lambda: 1 / 0
        try:
            code, _, body = req("GET", "/api/status")
        finally:
            del ctx.status
        check("a route that raises -> 500 JSON naming the exception",
              (code, "ZeroDivisionError" in (body.get("error", "") if isinstance(body, dict)
                                              else "")), (500, True))
        code, _, body = req("GET", "/api/status")
        check("and the server is still serving", (code, body["process"]["state"]), (200, "idle"))
        for key in ("process", "telemetry", "hello", "encoder", "clients", "uptime_s"):
            if key not in body:
                failures.append(f"/api/status is missing {key!r}")
        check("status: telemetry not connected", body["telemetry"]["connected"], False)
        check("status: no hello yet", body["hello"], None)

        code, _, body = req("POST", "/api/preview", {"values": {"count": 2, "fast": "1"}})
        check("POST /api/preview", (code, body.get("argv")), (200, ["--count", "2", "--fast"]))
        check("preview carries the command", "-m fakevision" in body.get("command", ""), True)
        code, _, body = req("POST", "/api/preview", {"values": {"count": "x"}})
        check("preview: bad value -> 400", code, 400)
        code, _, body = req("POST", "/api/preview", {"values": {"typo": 1}})
        check("preview: unknown dest -> 400 naming it", (code, "typo" in body.get("error", "")),
              (400, True))

        # The stream, opened before the launch so both the replay and the live event
        # can be seen.
        sconn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
        sconn.request("GET", "/events")
        resp = sconn.getresponse()
        check("GET /events -> 200 text/event-stream",
              (resp.status, resp.getheader("Content-Type")), (200, "text/event-stream"))
        check("/events has no Content-Length", resp.getheader("Content-Length"), None)
        check("/events says Connection: close", resp.getheader("Connection"), "close")
        first = resp.readline()
        check("the stream opens with a retry hint", first, b"retry: 1000\n")
        sse = SseReader(resp)
        ev = sse.until(lambda e: e[0] == "process")
        check("the replay carries the idle process", ev and ev[1]["state"], "idle")
        ev = sse.until(lambda e: e[0] == "telemetry")
        check("and the telemetry state", ev and ev[1]["connected"], False)

        code, _, body = req("POST", "/api/start", {"values": {"name": "srv"}})
        check("POST /api/start -> 200 with a pid", (code, isinstance(body.get("pid"), int)),
              (200, True))
        code, _, body = req("POST", "/api/start", {"values": {}})
        check("a second start -> 409", code, 409)
        code, _, body = req("POST", "/api/start", {"values": {"count": "no"}})
        check("a bad value -> 400 (even while running)", code, 400)
        ev = sse.until(lambda e: e[0] == "process" and e[1]["state"] == "running")
        check("the live process event says running", ev is not None, True)
        ev = sse.until(lambda e: e[0] == "log" and e[1]["line"] == "ready")
        check("the child's stdout streams as log events", ev is not None, True)

        sbus.publish("frame", {"seq": 7, "image": "data:image/png;base64,AA=="})
        ev = sse.until(lambda e: e[0] == "frame")
        check("a published frame reaches the stream", ev and ev[1].get("seq"), 7)

        wait_for(lambda: len(srunner.log_tail(10)) >= 3)
        code, _, body = req("GET", "/api/log?n=2")
        check("GET /api/log?n=2", (code, len(body.get("lines", []))), (200, 2))
        code, _, _ = req("GET", "/api/log?n=x")
        check("GET /api/log with a bad n -> 400", code, 400)

        code, _, body = req("POST", "/api/oneshot", {"flag": "--list-things"})
        check("a probe while the child runs -> 409 with the process",
              (code, body.get("process", {}).get("state")), (409, "running"))

        code, _, body = req("POST", "/api/stop")
        check("POST /api/stop -> the exit code", (code, body.get("exit_code")), (200, 0))
        ev = sse.until(lambda e: e[0] == "process" and e[1]["state"] == "exited")
        check("the exit is announced on the stream", ev and ev[1]["exit_code"], 0)
        code, _, body = req("GET", "/api/status")
        check("status after stop", body["process"]["state"], "exited")

        code, _, body = req("POST", "/api/oneshot", {"flag": "--list-things"})
        check("POST /api/oneshot", (code, body.get("stdout")), (200, "thing one\nthing two\n"))
        code, _, _ = req("POST", "/api/oneshot", {"flag": "--name"})
        check("oneshot of a normal flag -> 400", code, 400)
        code, _, _ = req("POST", "/api/oneshot", {})
        check("oneshot without a flag -> 400", code, 400)

        # A page that connects now sees the run's output first: the log ring is
        # replayed, after the state events and before anything live.
        lconn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=15)
        lconn.request("GET", "/events")
        lresp = lconn.getresponse()
        lresp.readline()                      # the retry hint
        late = SseReader(lresp)
        ev = late.until(lambda e: e[0] == "log" and e[1]["line"] == "ready")
        check("the log ring is replayed to a late connection", ev is not None, True)
        check("after the state events", [e for e, _ in late.seen][:2], ["process", "telemetry"])
        check("and before anything live", [e for e, _ in late.seen if e == "heartbeat"], [])

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        conn.request("POST", "/api/preview", body=b"not json",
                     headers={"Content-Type": "application/json"})
        resp2 = conn.getresponse()
        resp2.read()
        check("a body that is not JSON -> 400", resp2.status, 400)
        conn.close()
        code, _, _ = raw_request(b"POST /api/preview HTTP/1.1\r\nHost: x\r\n"
                                 b"Content-Length: abc\r\n\r\n")
        check("a Content-Length that is not an integer -> 400", code, 400)
        code, _, _ = raw_request(b"POST /api/preview HTTP/1.1\r\nHost: x\r\n"
                                 b"Content-Length: -5\r\n\r\n")
        check("a negative Content-Length -> 400, not a read that never returns", code, 400)
        code, _, _ = raw_request(b"POST /api/preview HTTP/1.1\r\nHost: x\r\n"
                                 b"Content-Length: 2000000\r\n\r\n")
        check("a body over 1 MB -> 413", code, 413)
        code, _, _ = raw_request(b"POST /api/start HTTP/1.1\r\nHost: x\r\n"
                                 b"Content-Length: 5\r\n\r\n", half_close=True)
        check("a truncated body -> 400, not a start with the defaults", code, 400)
        check("and nothing was launched", srunner.status()["state"], "exited")

        # A browser resets keep-alive connections all day long. That must not put a
        # traceback in the terminal: read one response, then slam the door with a RST.
        raw = socket.create_connection(("127.0.0.1", server.port), timeout=5)
        raw.sendall(b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n")
        head = b""
        while b"\r\n\r\n" not in head:
            head += raw.recv(4096)
        length = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")][0]
                     .split(b":")[1])
        body = head.split(b"\r\n\r\n", 1)[1]
        while len(body) < length:
            body += raw.recv(4096)
        err = io.StringIO()
        with redirect_stderr(err):
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            raw.close()
            time.sleep(0.3)
        if "Traceback" in err.getvalue():
            failures.append("a reset keep-alive connection printed a traceback")

        check("both streams are counted as clients", sbus.client_count(), 2)
        server.stop()
        check("stopping the server ends the stream", resp.readline(), b"")
        lresp.read()             # the rest of the replay was still buffered on this one
        check("and the late stream", lresp.readline(), b"")
        sconn.close()
        lconn.close()
        check("and the clients are gone", sbus.client_count(), 0)
        ssub.stop()
        senc.stop()
    finally:
        if saved_env is not None:
            os.environ["MACVISION_TELEMETRY"] = saved_env
        shutil.rmtree(tmp, ignore_errors=True)

    for mod in ("cv2", "numpy", "torch", "ultralytics"):
        if mod in AT_IMPORT:
            failures.append(f"importing the dashboard pulled in {mod}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("argv builder, bus coalescing and replay, PNG by hand, the rate limiter, the "
          "reconnecting reader and its pacing, the orphan warning, the runner against a "
          "fake child, and every route: all correct")
    return 0


if __name__ == "__main__":
    sys.exit(run())
