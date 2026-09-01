"""Checks the one-byte link: how TRIGGER_TARGET is read, and what leaves on the wire.

Two bugs this defends against. TRIGGER_TARGET is the only public configuration surface,
and a silently ignored or misreported value is a working vision pipeline attached to
nothing - the failure commit 3e8a1a4 exists to refuse. And a keepalive that re-asserts
after the release leaves a key held down by a process that has already exited.

    python3 -m tests.test_trigger      (from mac-app/)

Zero third-party imports: no test here constructs a SerialTransport, so pyserial is
never reached.
"""

import io
import os
import sys
import threading
import time
from contextlib import redirect_stdout, redirect_stderr

from macvision.trigger import (PI_TRIGGER_PORT, PORT_GLOBS, NullTransport, Transport,
                               Trigger, find_port, list_ports, open_trigger,
                               parse_target)

# Sampled here, before any test constructs anything: importing the module must not pull
# in pyserial. Further down, open_trigger() deliberately DOES exercise the serial branch,
# which imports it - so the check has to happen now or not at all.
AT_IMPORT = set(sys.modules)


class Recording(Transport):
    """Records every byte, and can be told to fail or to raise."""

    name = "rec"
    description = "recording"

    def __init__(self, returns=True, raises=False):
        self.sent = []
        self.dropped = 0
        self.closed = False
        self.returns = returns
        self.raises = raises

    def send(self, active):
        if self.raises:
            raise RuntimeError("transport is angry")
        self.sent.append(1 if active else 0)
        return self.returns

    def close(self):
        self.closed = True


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    # --- parse_target: never raises, for anything ----------------------------------
    cases = [
        ("udp://raspberrypi.local", "udp", "raspberrypi.local", PI_TRIGGER_PORT, None),
        ("udp://h:9", "udp", "h", 9, None),
        ("udp://h:99999", "unknown", None, None, None),   # used to be a traceback
        ("udp://h:abc", "unknown", None, None, None),     # so did this
        ("udp://", "unknown", None, None, None),
        ("serial:///dev/cu.usbserial-0001", "serial", None, None,
         "/dev/cu.usbserial-0001"),
        ("serial://", "serial", None, None, ""),
        ("", "none", None, None, None),
        ("none", "none", None, None, None),
        ("null", "none", None, None, None),
        (" NONE ", "none", None, None, None),
        ("Auto", "auto", None, None, None),
        ("auto ", "auto", None, None, None),
        ("ftp://x", "unknown", None, None, None),
    ]
    for raw, kind, host, port, path in cases:
        try:
            spec = parse_target(raw)
        except Exception as exc:
            failures.append(f"parse_target({raw!r}) raised {exc!r}")
            continue
        check(f"parse_target({raw!r}).kind", spec["kind"], kind)
        if host is not None:
            check(f"parse_target({raw!r}).host", spec["host"], host)
        if port is not None:
            check(f"parse_target({raw!r}).port", spec["port"], port)
        if path is not None:
            check(f"parse_target({raw!r}).path", spec["path"], path)

    # A bare serial:// must NOT be mistaken for auto - the banner's text would lie.
    check("bare serial:// is not auto", parse_target("serial://")["kind"], "serial")

    # --- PORT_GLOBS ordering, with an injected globber (no Mac required) ------------
    fake = {
        "/dev/cu.usbserial-*": ["/dev/cu.usbserial-0001"],
        "/dev/cu.wchusbserial*": ["/dev/cu.wchusbserial1420"],
        "/dev/cu.SLAB_USBtoUART*": [],
        "/dev/cu.usbmodem*": ["/dev/cu.usbmodem14201"],
    }
    globber = lambda pattern: fake.get(pattern, [])
    check("CP2102 wins over CH340", find_port(globber=globber), "/dev/cu.usbserial-0001")
    check("every candidate is listed, in PORT_GLOBS order",
          list_ports(globber=globber),
          ["/dev/cu.usbserial-0001", "/dev/cu.wchusbserial1420", "/dev/cu.usbmodem14201"])
    only_last = lambda pattern: (["/dev/cu.usbmodem1"]
                                 if pattern == PORT_GLOBS[-1] else [])
    check("a later pattern wins only when the earlier ones are empty",
          find_port(globber=only_last), "/dev/cu.usbmodem1")
    check("nothing found", find_port(globber=lambda p: []), None)

    # --- Trigger ---------------------------------------------------------------------
    t = Recording()
    trig = Trigger(t, keepalive_s=10)
    trig.start()
    check("start() sends exactly one released state first", t.sent, [0])
    trig.start()
    check("start() is idempotent", t.sent, [0])

    n = len(t.sent)
    trig.update(True)
    check("update() writes synchronously on the caller's thread", len(t.sent), n + 1)
    check("and it wrote the new state", t.sent[-1], 1)
    trig.stop()

    # The cached state must be set BEFORE the send, so a keepalive firing in that
    # window re-sends the new state rather than the stale one.
    class ReadsState(Transport):
        name = "rs"
        description = "rs"

        def __init__(self):
            self.seen = []
            self.dropped = 0
            self.trig = None

        def send(self, active):
            self.seen.append(self.trig._active)
            return True

    rs = ReadsState()
    trig = Trigger(rs, keepalive_s=10)
    rs.trig = trig
    trig.start()
    trig.update(True)
    check("the cached state is set before the send", rs.seen[-1], True)
    trig.stop()

    # A transport that returns False is counted, not raised.
    t = Recording(returns=False)
    trig = Trigger(t, keepalive_s=10)
    trig.start()
    trig.update(True)
    check("a refused write is counted", trig.dropped_writes, 2)  # start + update
    trig.stop()

    # A transport that RAISES must never let it out of update(), and must not kill the
    # keepalive thread. This is the c13dfac lesson, asserted.
    t = Recording(raises=True)
    trig = Trigger(t, keepalive_s=0.005)
    err = io.StringIO()
    with redirect_stderr(err):
        trig.start()
        try:
            trig.update(True)
        except Exception as exc:
            failures.append(f"a raising transport escaped update(): {exc!r}")
        time.sleep(0.06)
        check("a raising transport does not kill the keepalive", trig.alive, True)
        if trig.dropped_writes < 3:
            failures.append("the keepalive stopped writing after the first exception")
        trig.stop()

    # The keepalive actually resends.
    t = Recording()
    trig = Trigger(t, keepalive_s=0.01)
    trig.start()
    trig.update(True)
    time.sleep(0.06)
    if t.sent.count(1) < 2:
        failures.append(f"the keepalive did not resend the held state: {t.sent}")
    trig.stop()

    # stop() while firing: the release must provably be the last byte on the wire.
    # Run it repeatedly with a fast keepalive - the old close() lost this race in 20
    # runs out of 20.
    lost = 0
    for _ in range(20):
        t = Recording()
        trig = Trigger(t, keepalive_s=0.005)
        trig.start()
        trig.update(True)
        time.sleep(0.02)
        trig.stop()
        snapshot = list(t.sent)
        time.sleep(0.03)          # any surviving keepalive would write here
        if t.sent[-1] != 0 or t.sent != snapshot:
            lost += 1
    check("stop() leaves the release as the last byte, every time", lost, 0)
    check("stop() closed the transport", t.closed, True)

    trig.close()   # alias, and safe to call twice
    check("close() is an alias for stop()", Trigger.close, Trigger.stop)

    # status() must not deadlock while a send is in flight, because it takes no lock.
    class Slow(Transport):
        name = "slow"
        description = "slow"

        def __init__(self):
            self.dropped = 0
            self.entered = threading.Event()

        def send(self, active):
            self.entered.set()
            time.sleep(0.15)
            return True

    slow = Slow()
    trig = Trigger(slow, keepalive_s=10)
    threading.Thread(target=lambda: trig.update(True), daemon=True).start()
    slow.entered.wait(1.0)
    done = threading.Event()
    threading.Thread(target=lambda: (trig.status(), done.set()), daemon=True).start()
    check("status() does not block behind an in-flight send", done.wait(0.5), True)

    st = Trigger(NullTransport()).status()
    for key in ("kind", "description", "active", "dropped_writes", "connected", "alive"):
        if key not in st:
            failures.append(f"status() is missing {key!r}")

    # --- open_trigger: never raises, always returns a started, usable object --------
    empty_globs = lambda pattern: []

    import macvision.trigger as tr
    real_glob = tr.glob.glob
    tr.glob.glob = empty_globs
    saved_env = os.environ.get("TRIGGER_TARGET")
    try:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            trig = open_trigger("auto")
        banner = out.getvalue()
        if "=" * 72 not in banner:
            failures.append("the auto banner lost its 72-column rule")
        if "NO TRIGGER LINK" not in banner:
            failures.append("the auto banner lost its headline")
        if "python3 -m macvision" not in banner:
            failures.append("the auto banner still names the old entry point")
        trig.update(True)
        trig.stop()

        # An explicit target that cannot be opened must NOT print the auto banner,
        # whose text hard-codes "TRIGGER_TARGET is 'auto'".
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            trig = open_trigger("serial:///dev/definitely-not-here")
        if "NO TRIGGER LINK" in out.getvalue():
            failures.append("an explicit serial:// target printed the auto banner")
        trig.stop()

        # A bare serial:// gets its own message, not the auto banner.
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            open_trigger("serial://").stop()
        if "NO TRIGGER LINK" in out.getvalue():
            failures.append("a bare serial:// printed the auto banner")

        # An empty value must say so, not claim the user typed "none".
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            open_trigger("").stop()
        if "empty" not in out.getvalue():
            failures.append(f"an empty TRIGGER_TARGET is misreported: {out.getvalue()!r}")

        # A malformed port must not be a traceback.
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                trig = open_trigger("udp://h:99999")
                trig.update(True)
                trig.stop()
        except Exception as exc:
            failures.append(f"open_trigger('udp://h:99999') raised {exc!r}")

        # The environment is read at CALL time, and an explicit argument wins.
        os.environ["TRIGGER_TARGET"] = "none"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            a = open_trigger()
            check("env honoured on the first call", a.status()["kind"], "none")
            a.stop()
            os.environ["TRIGGER_TARGET"] = "udp://127.0.0.1:48010"
            b = open_trigger()
            check("env re-read on the next call", b.status()["kind"], "udp")
            b.stop()
            c = open_trigger("none")
            check("an explicit argument wins over the env", c.status()["kind"], "none")
            c.stop()

        # Every branch returns a fully usable object.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            for target in ("none", "auto", "", "ftp://x", "udp://127.0.0.1:48010",
                           "serial://", "udp://h:abc"):
                trig = open_trigger(target)
                for attr in ("update", "start", "stop", "close", "status"):
                    if not callable(getattr(trig, attr, None)):
                        failures.append(f"open_trigger({target!r}) has no {attr}()")
                if not hasattr(trig, "dropped_writes"):
                    failures.append(f"open_trigger({target!r}) has no dropped_writes")
                trig.update(True)
                trig.update(False)
                trig.stop()
    finally:
        tr.glob.glob = real_glob
        if saved_env is None:
            os.environ.pop("TRIGGER_TARGET", None)
        else:
            os.environ["TRIGGER_TARGET"] = saved_env

    # --- regressions the adversarial review caught ----------------------------------
    # open_trigger(None) has always meant "use the environment" - the old signature was
    # open_trigger(target=None). The _UNSET sentinel must not turn None into a value.
    saved = os.environ.get("TRIGGER_TARGET")
    os.environ["TRIGGER_TARGET"] = "none"
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            t = open_trigger(None)
        check("open_trigger(None) still consults the environment",
              t.status()["kind"], "none")
        t.stop()
    except Exception as exc:
        failures.append(f"open_trigger(None) raised {exc!r}")
    finally:
        if saved is None:
            os.environ.pop("TRIGGER_TARGET", None)
        else:
            os.environ["TRIGGER_TARGET"] = saved
    try:
        check("parse_target(None) does not raise", parse_target(None)["kind"], "none")
    except Exception as exc:
        failures.append(f"parse_target(None) raised {exc!r}")

    # The unrecognised-value message must keep the keyword people grep for AND the list
    # of what is accepted - both were lost in the refactor.
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        open_trigger("ftp://nope").stop()
    msg = out.getvalue() + err.getvalue()
    for token in ("unrecognised", "TRIGGER_TARGET=", "udp://host:port",
                  "serial:///dev/", "auto", "none"):
        if token not in msg:
            failures.append(f"the unrecognised-target message lost {token!r}: {msg!r}")

    # An explicit "none" must print the value the user actually typed, without quotes.
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        open_trigger("none").stop()
    if "TRIGGER_TARGET=none" not in out.getvalue():
        failures.append(f"the disabled message no longer echoes the typed value: "
                        f"{out.getvalue()!r}")

    # send() must never block: a dead serial link reconnects on its own thread, never
    # on the caller's. Verified structurally - _open must not be reachable from send.
    import inspect
    send_src = inspect.getsource(tr.SerialTransport.send)
    if "_open()" in send_src:
        failures.append("SerialTransport.send() opens the port inline; a device open "
                        "would stall the frame loop inside Trigger._lock")

    for mod in ("cv2", "numpy", "torch", "ultralytics", "serial"):
        if mod in AT_IMPORT:
            failures.append(f"importing macvision.trigger pulled in {mod}")
    # These must stay absent even after every branch above has run.
    for mod in ("cv2", "numpy", "torch", "ultralytics"):
        if mod in sys.modules:
            failures.append(f"exercising macvision.trigger pulled in {mod}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("target parsing, port preference, synchronous writes, error containment, "
          "the release-last race, and every open_trigger branch: all correct")
    return 0


if __name__ == "__main__":
    sys.exit(run())
