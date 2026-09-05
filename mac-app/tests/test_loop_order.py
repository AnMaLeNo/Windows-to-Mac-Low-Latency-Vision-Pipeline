"""Makes the per-frame ordering executable instead of merely commented.

action.update() must fire before ANY drawing. That is the one place docs/TRIGGER.md
says this project spends care on latency, and until now it was protected only by a
comment - so a tidy-up that moved the trigger write below the render would have shipped.
It now fails a test.

The telemetry tap (docs/DASHBOARD.md) is held to the same standard: its one call sits
after the trigger byte and after both timing samples, before present(), and a delay
inside it must show up in neither decide_ms nor mac_ms. That, and the --describe-args
contract the dashboard builds its form from, are asserted below too.

The loop is driven for real, with fakes that append to one shared call log. Nothing
third-party is imported, so this runs on any machine.

    python3 -m tests.test_loop_order      (from mac-app/)
"""

import io
import json
import os
import sys
import time
from contextlib import redirect_stderr, redirect_stdout

from macvision.loop import run
from macvision.protocol import ROI_H, ROI_W
from macvision.sources import Capture
from macvision.stats import LatencyStats


def capture(seq, frame="frame", width=ROI_W, height=ROI_H, upstream_ms=1.0,
            transit_ms=5.0, dropped=0, note=None, stale=False):
    """One Capture, as a source would hand it over."""
    return Capture(frame, time.perf_counter(), seq, width, height,
                   upstream_ms=upstream_ms, transit_ms=transit_ms, dropped=dropped,
                   note=note, stale=stale)


class FakeSource:
    """Replays Captures, then ends the loop the way Ctrl-C does."""

    upstream_label = "win"

    def __init__(self, captures, log):
        self._captures = list(captures)
        self._log = log
        self.stale_dropped = 0

    def recv(self):
        if not self._captures:
            raise KeyboardInterrupt
        cap = self._captures.pop(0)
        cap.t0 = time.perf_counter()      # stamped at acquisition, as a real source does
        self.stale_dropped += cap.dropped
        return cap


class FakeDetector:
    def __init__(self, log, boxes_per_frame=None):
        self._log = log
        self._boxes = list(boxes_per_frame or [])
        self.n = 0

    def infer(self, frame):
        self.n += 1
        return f"result-for-{frame}"

    def boxes(self, result):
        if self._boxes:
            return self._boxes.pop(0)
        return []


class FakeAction:
    def __init__(self, log):
        self._log = log
        self.states = []
        self.dropped_writes = 0

    def update(self, active):
        self._log.append("trigger.update")
        self.states.append(active)


class FakeDisplay:
    def __init__(self, log, annotate_delay=0.0, present_delay=0.0, quit_after=None):
        self._log = log
        self.annotate_delay = annotate_delay
        self.present_delay = present_delay
        self.quit_after = quit_after
        self.presented = 0
        self.captions = []

    def annotate(self, result, hit, cx, cy):
        self._log.append("display.annotate")
        if self.annotate_delay:
            time.sleep(self.annotate_delay)
        return f"annotated-{result}"

    def present(self, annotated, overlay):
        self._log.append("display.present")
        self.presented += 1
        self.captions.append(overlay)
        if self.present_delay:
            time.sleep(self.present_delay)
        return self.quit_after is None or self.presented < self.quit_after


class SpyStats(LatencyStats):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.calls = []

    def observe(self, transit_ms, upstream_ms, mac_ms, decide_ms):
        self.calls.append({"transit": transit_ms, "upstream": upstream_ms,
                           "mac": mac_ms, "decide": decide_ms})
        return super().observe(transit_ms, upstream_ms, mac_ms, decide_ms)


class FakeTelemetry:
    """Records the tap's two calls. The optional delay stands in for a slow copy, so
    the test can prove the timings are sampled before it runs."""

    def __init__(self, log, frame_delay=0.0):
        self._log = log
        self.frame_delay = frame_delay
        self.frames = []
        self.stats_calls = []

    def frame(self, cap, boxes, hit, cx, cy, n, e2e_ms, queue_ms, mac_ms, decide_ms,
              upstream_label):
        self._log.append("telemetry.frame")
        self.frames.append({"seq": cap.seq, "boxes": boxes, "hit": hit, "cx": cx,
                            "cy": cy, "n": n, "e2e": e2e_ms, "queue": queue_ms,
                            "mac": mac_ms, "decide": decide_ms,
                            "label": upstream_label})
        if self.frame_delay:
            time.sleep(self.frame_delay)

    def stats(self, n, stats_status, stale_dropped, dropped_writes, summary):
        self._log.append("telemetry.stats")
        self.stats_calls.append({"n": n, "status": stats_status,
                                 "stale_dropped": stale_dropped,
                                 "dropped_writes": dropped_writes, "summary": summary})


def drive(captures, *, display=True, boxes=None, stats_every=100,
          annotate_delay=0.0, present_delay=0.0, quit_after=None, roi=(ROI_W, ROI_H),
          telemetry=False, telemetry_delay=0.0):
    log = []
    source = FakeSource(captures, log)
    detector = FakeDetector(log, boxes_per_frame=boxes)
    action = FakeAction(log)
    stats = SpyStats()
    disp = (FakeDisplay(log, annotate_delay, present_delay, quit_after)
            if display else None)
    tap = FakeTelemetry(log, telemetry_delay) if telemetry else None
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = run(source, detector, action, stats, disp,
                 roi[0], roi[1], stats_every=stats_every, telemetry=tap)
    return {"rc": rc, "log": log, "source": source, "trigger": action, "stats": stats,
            "display": disp, "telemetry": tap,
            "stdout": out.getvalue(), "stderr": err.getvalue()}


def run_tests():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    covering = [(0, 0, ROI_W, ROI_H)]     # covers the centre pixel
    missing = [(0, 0, 1, 1)]              # does not

    # --- the ordering, on every frame, in both trigger states ----------------------
    r = drive([capture(1), capture(2), capture(3)], boxes=[covering, missing, covering])
    check("clean quit", r["rc"], 0)
    check("the per-frame call order", r["log"],
          ["trigger.update", "display.annotate", "display.present"] * 3)
    check("the action saw each decision", r["trigger"].states, [True, False, True])

    # --- no frame holds the state; it does NOT release the key ----------------------
    # Every source signals "nothing usable this tick" the same way, whether that was a
    # corrupt datagram, a runt packet or a camera that stalled.
    r = drive([capture(1), capture(2, frame=None, note="[warn] nothing here"),
               capture(3)],
              boxes=[covering, covering])
    check("no action write on an empty capture", r["log"].count("trigger.update"), 2)
    check("and no display work either", r["log"].count("display.annotate"), 2)
    check("the held state is never contradicted", r["trigger"].states, [True, True])
    if "[warn] nothing here" not in r["stderr"]:
        failures.append("the source's note was not printed on the empty-frame path")

    # --- a STALE source releases; a merely empty tick holds -------------------------
    # These are opposite actions on the same frame=None, and which one is right depends
    # entirely on the source. DXGI produces no frames on a static screen, so silence on
    # the wire is normal and the state must be held. A camera that stops delivering is
    # dead, and holding would keep a key down on a Mac that can no longer see anything.
    r = drive([capture(1), capture(2, frame=None, stale=True), capture(3)],
              boxes=[covering, covering])
    check("a stale capture releases the trigger",
          r["trigger"].states, [True, False, True])
    check("and it is a real write, not a skipped frame",
          r["log"].count("trigger.update"), 3)
    check("but nothing is drawn for it", r["log"].count("display.annotate"), 2)

    # The same capture without the stale flag must NOT release.
    r = drive([capture(1), capture(2, frame=None), capture(3)],
              boxes=[covering, covering])
    check("a non-stale empty capture holds instead",
          r["trigger"].states, [True, True])

    # --- mac_ms is sampled before present(), decide_ms before annotate() -----------
    r = drive([capture(1)], boxes=[covering], present_delay=0.05)
    call = r["stats"].calls[0]
    if call["mac"] >= 50:
        failures.append(f"mac_ms includes present(): {call['mac']:.1f}ms")
    r = drive([capture(1)], boxes=[covering], annotate_delay=0.05)
    call = r["stats"].calls[0]
    if call["decide"] >= 50:
        failures.append(f"decide_ms includes annotate(): {call['decide']:.1f}ms")
    if call["mac"] < 50:
        failures.append("mac_ms did not include annotate(), but it must")

    # --- headless: the decision still happens, and mac collapses to decide ---------
    r = drive([capture(1), capture(2)], display=False, boxes=[covering, missing])
    check("the loop runs with no display", r["rc"], 0)
    check("the action still fires on every frame", r["trigger"].states, [True, False])
    check("no display calls at all",
          [c for c in r["log"] if c.startswith("display")], [])
    for call in r["stats"].calls:
        if call["mac"] != call["decide"]:
            failures.append("headless mac_ms should equal decide_ms: "
                            f"{call['mac']} != {call['decide']}")

    # --- the source's measurements are passed through untouched --------------------
    r = drive([capture(1, upstream_ms=7.5, transit_ms=241.0)], boxes=[covering])
    call = r["stats"].calls[0]
    check("upstream_ms reaches stats unchanged", call["upstream"], 7.5)
    check("transit_ms reaches stats unchanged", call["transit"], 241.0)
    # A single-machine source reports neither, and the loop must not invent them.
    r = drive([capture(1, upstream_ms=None, transit_ms=None)], boxes=[covering])
    call = r["stats"].calls[0]
    check("an unmeasurable upstream stays None", call["upstream"], None)
    check("an absent second clock stays None", call["transit"], None)

    # --- the drop count survives the module boundary --------------------------------
    r = drive([capture(1, dropped=4), capture(2, dropped=1)], boxes=[covering] * 2,
              stats_every=2)
    check("stale drops accumulate on the source", r["source"].stale_dropped, 5)
    if "stale dropped=5" not in r["stdout"]:
        failures.append(f"the stats line lost the drop count: {r['stdout']!r}")

    # --- the [stats] cadence counts PROCESSED frames, not everything the source saw --
    caps = [capture(1), capture(2, frame=None), capture(3), capture(4), capture(5)]
    r = drive(caps, boxes=[covering] * 4, stats_every=3)
    check("one stats line, at the third processed frame",
          r["stdout"].count("[stats]"), 1)

    # --- the one-shot geometry check ------------------------------------------------
    r = drive([capture(i, width=640, height=480) for i in (1, 2, 3)],
              boxes=[covering] * 3)
    check("a geometry mismatch is reported exactly once",
          r["stderr"].count("GEOMETRY MISMATCH"), 1)
    r = drive([capture(1), capture(2)], boxes=[covering] * 2)
    check("matching geometry says nothing", r["stderr"].count("GEOMETRY MISMATCH"), 0)

    # --- the note is printed AFTER the trigger write --------------------------------
    r = drive([capture(1, note="[gap] something")], boxes=[covering])
    if "[gap] something" not in r["stderr"]:
        failures.append("the source's note was never printed")
    check("and the write still came first", r["log"][0], "trigger.update")

    # --- 'q' ends the loop cleanly ---------------------------------------------------
    r = drive([capture(i) for i in range(1, 6)], boxes=[covering] * 5, quit_after=2)
    check("q stops the loop", r["display"].presented, 2)
    check("and returns cleanly", r["rc"], 0)

    # --- run() tears nothing down; main()'s finally owns that ------------------------
    r = drive([capture(1)], boxes=[covering])
    if hasattr(r["trigger"], "stopped"):
        failures.append("run() called stop() - main()'s finally block owns that")

    # --- the telemetry tap: after the trigger byte, before present() ----------------
    # docs/DASHBOARD.md's rule 2, made executable. The tap's one call is the only copy
    # of the pixels the loop ever makes, and it must sit past everything that is
    # timed, and past the trigger byte above all.
    r = drive([capture(1), capture(2)], boxes=[covering, missing], telemetry=True)
    check("with a display: trigger, annotate, telemetry, present", r["log"],
          ["trigger.update", "display.annotate", "telemetry.frame",
           "display.present"] * 2)
    tap = r["telemetry"]
    check("the tap sees each decision", [f["hit"] for f in tap.frames], [True, False])
    check("and the very boxes the rule tested", tap.frames[0]["boxes"], covering)
    check("and the processed-frame count", [f["n"] for f in tap.frames], [1, 2])
    check("and the source's seq", [f["seq"] for f in tap.frames], [1, 2])
    check("and the source's upstream label", tap.frames[0]["label"], "win")
    check("and the centre the rule used", (tap.frames[0]["cx"], tap.frames[0]["cy"]),
          (ROI_W // 2, ROI_H // 2))
    # The timings handed to the tap are the ones stats saw, not a re-sample.
    for f, call in zip(tap.frames, r["stats"].calls):
        check("the tap gets stats' decide_ms", f["decide"], call["decide"])
        check("the tap gets stats' mac_ms", f["mac"], call["mac"])

    r = drive([capture(1)], display=False, boxes=[covering], telemetry=True)
    check("headless: trigger, then telemetry", r["log"],
          ["trigger.update", "telemetry.frame"])

    # A delay inside the tap must land in NEITHER timing: both are sampled before it.
    for display_on in (True, False):
        r = drive([capture(1)], display=display_on, boxes=[covering], telemetry=True,
                  telemetry_delay=0.05)
        call = r["stats"].calls[0]
        where = "with a display" if display_on else "headless"
        if call["decide"] >= 50:
            failures.append(f"{where}: decide_ms includes telemetry.frame(): "
                            f"{call['decide']:.1f}ms")
        if call["mac"] >= 50:
            failures.append(f"{where}: mac_ms includes telemetry.frame(): "
                            f"{call['mac']:.1f}ms")

    # An empty capture - held or stale - reaches no tap: there is nothing to show.
    r = drive([capture(1), capture(2, frame=None), capture(3, frame=None, stale=True)],
              boxes=[covering], telemetry=True)
    check("an empty capture produces no telemetry call",
          r["log"].count("telemetry.frame"), 1)

    # telemetry.stats(): the [stats] cadence, after the print, with the printed line.
    r = drive([capture(i, dropped=(4 if i == 1 else 1 if i == 2 else 0))
               for i in range(1, 7)], boxes=[covering] * 6, stats_every=3,
              telemetry=True)
    tap = r["telemetry"]
    check("telemetry.stats at the stats cadence", r["log"].count("telemetry.stats"), 2)
    check("with the processed-frame count", [s["n"] for s in tap.stats_calls], [3, 6])
    # Frames 1 and 2 log four calls each; the third, with its stats line, five.
    check("at the third frame, after the tap's frame and before present()",
          r["log"][8:13],
          ["trigger.update", "display.annotate", "telemetry.frame", "telemetry.stats",
           "display.present"])
    printed = [line for line in r["stdout"].splitlines() if line.startswith("[stats]")]
    check("the summary IS the printed line, verbatim",
          [s["summary"] for s in tap.stats_calls], printed)
    check("the drop counts pass through",
          [(s["stale_dropped"], s["dropped_writes"]) for s in tap.stats_calls],
          [(5, 0), (5, 0)])
    # The last snapshot was taken on the last frame, so it is what stats holds now;
    # the first was taken three frames earlier and says so.
    check("and the stats dict is stats.status()", tap.stats_calls[1]["status"],
          r["stats"].status())
    check("stats.status() n at the first line", tap.stats_calls[0]["status"]["n"], 3)
    # The loop renders the line from the snapshot it publishes, so the medians are
    # computed once - and the wording must not change for it, on any of the three
    # shapes the line takes.
    cam = LatencyStats()
    cam.observe(None, None, 3.2, 3.0)
    for label, st in (("calibrated", r["stats"]), ("single-machine", cam),
                      ("empty", LatencyStats())):
        check(f"summary(status) renders the {label} line identically",
              st.summary(st.status()), st.summary())

    # With no tap - the default - the loop is exactly what it was.
    r = drive([capture(1), capture(2)], boxes=[covering] * 2, stats_every=2)
    check("telemetry=None: the old per-frame shape", r["log"],
          ["trigger.update", "display.annotate", "display.present"] * 2)
    check("telemetry=None: nothing named telemetry anywhere",
          [c for c in r["log"] if c.startswith("telemetry")], [])
    check("telemetry=None: the stats line still prints", r["stdout"].count("[stats]"), 1)

    # --- --describe-args: contract 2, what the dashboard builds its form from --------
    from macvision.__main__ import describe_args, main
    spec = describe_args()
    try:
        json.dumps(spec)
    except (TypeError, ValueError) as exc:
        failures.append(f"describe_args() is not JSON-serialisable: {exc}")
    check("describe_args v and prog", (spec.get("v"), spec.get("prog")), (1, "macvision"))
    groups = {g["title"]: {a["dest"]: a for a in g["args"]} for g in spec["groups"]}
    everything = {dest: a for g in groups.values() for dest, a in g.items()}
    keys = {"dest", "flag", "kind", "default", "choices", "help", "oneshot"}
    for dest, a in everything.items():
        check(f"describe_args {dest} has every key", set(a), keys)
    if "trigger" not in groups:
        failures.append(f"no 'trigger' group in describe_args: {sorted(groups)}")
    else:
        tt = groups["trigger"].get("trigger_target", {})
        check("trigger_target kind", tt.get("kind"), "str")
        check("trigger_target flag", tt.get("flag"), "--trigger-target")
        check("trigger_target oneshot", tt.get("oneshot"), False)
    if "options" not in groups:
        failures.append(f"argparse's default group was not normalised to 'options': "
                        f"{sorted(groups)}")
    else:
        lc = groups["options"].get("list_cameras", {})
        check("list_cameras kind", lc.get("kind"), "bool")
        check("list_cameras oneshot", lc.get("oneshot"), True)
        check("list_cameras default", lc.get("default"), False)
    cu = everything.get("compute_units", {})
    check("compute_units kind", cu.get("kind"), "choice")
    check("compute_units has its four choices", len(cu.get("choices") or []), 4)
    check("the int and float kinds", (everything.get("stats_every", {}).get("kind"),
                                      everything.get("keepalive_ms", {}).get("kind")),
          ("int", "float"))
    check("--telemetry is described, in its own group",
          groups.get("telemetry", {}).get("telemetry", {}).get("flag"), "--telemetry")
    check("the oneshot flags are exactly the probes",
          sorted(d for d, a in everything.items() if a["oneshot"]),
          ["list_cameras", "list_ports"])
    for hidden in ("help", "describe_args"):
        if hidden in everything:
            failures.append(f"describe_args lists {hidden}; it must not")
    # The default is the parser's default AT THE TIME OF THE CALL: the environment
    # shows through, which is what lets the dashboard's form start from $TRIGGER_TARGET.
    saved = os.environ.get("TRIGGER_TARGET")
    os.environ["TRIGGER_TARGET"] = "udp://pi.local:48010"
    try:
        tt = {a["dest"]: a for g in describe_args()["groups"] for a in g["args"]}
        check("the environment shows through the default",
              tt["trigger_target"]["default"], "udp://pi.local:48010")
    finally:
        if saved is None:
            del os.environ["TRIGGER_TARGET"]
        else:
            os.environ["TRIGGER_TARGET"] = saved
    # main(["--describe-args"]) prints it and returns 0, opening nothing.
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        rc = main(["--describe-args", "--trigger-target", "none"])
    check("--describe-args exits 0", rc, 0)
    try:
        check("and prints the same thing", json.loads(out.getvalue())["v"], 1)
    except ValueError:
        failures.append(f"--describe-args did not print JSON: {out.getvalue()[:80]!r}")
    # And BEFORE the argument guards: a default the environment supplied that
    # parse_args would refuse - $MACVISION_CLASSES=abc, $MACVISION_TELEMETRY=garbage -
    # must still describe and exit 0, because that is exactly the value the form has
    # to be able to show. Refusing it with exit 2 leaves the dashboard with no form.
    saved = {k: os.environ.get(k) for k in ("MACVISION_CLASSES", "MACVISION_TELEMETRY")}
    os.environ["MACVISION_CLASSES"] = "abc"
    os.environ["MACVISION_TELEMETRY"] = "garbage"
    try:
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = main(["--describe-args"])
        except SystemExit as exc:
            rc = exc.code
        check("--describe-args with a bad env default still exits 0", rc, 0)
        try:
            described = {a["dest"]: a for g in json.loads(out.getvalue())["groups"]
                         for a in g["args"]}
            check("and shows the bad values, for the form to correct",
                  (described["classes"]["default"], described["telemetry"]["default"]),
                  ("abc", "garbage"))
        except (ValueError, KeyError) as exc:
            failures.append(f"--describe-args with a bad env default printed no usable "
                            f"JSON ({exc!r}): {out.getvalue()[:80]!r}")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    # argparse creates its default group first, but it holds only the probes, and a
    # form opens on the first group it is given.
    check("argparse's default group is described last",
          [g["title"] for g in spec["groups"]][-1], "options")

    # --- argument guards, verified through the real parser ---------------------------
    # Each of these used to reach the frame loop and crash or spin there, after the
    # trigger link was already open. They are refused at parse time now.
    from macvision.__main__ import parse_args
    for argv, flag in ((["--stats-every", "0"], "--stats-every"),
                       (["--stats-window", "0"], "--stats-window"),
                       (["--keepalive-ms", "0"], "--keepalive-ms"),
                       (["--roi-w", "0"], "--roi-w"),
                       (["--udp-port", "99999"], "--udp-port")):
        try:
            with redirect_stderr(io.StringIO()):
                parse_args(argv)
        except SystemExit as exc:
            if exc.code != 2:
                failures.append(f"{flag} exited {exc.code}, expected argparse's 2")
        else:
            failures.append(f"{flag} 0 was accepted; it crashes or spins in the loop")
    # A valid combination must still parse.
    try:
        with redirect_stderr(io.StringIO()):
            args = parse_args(["--stats-every", "1", "--stats-window", "1",
                               "--keepalive-ms", "0.5"])
        check("a valid keepalive survives", args.keepalive_ms, 0.5)
    except SystemExit:
        failures.append("a valid argument combination was rejected")
    # --telemetry is refused at parse time, not downgraded to a warning at step 4b: a
    # typo that silently ran without telemetry would leave the dashboard waiting on a
    # socket nobody opened.
    for bad in ("garbage", "udp://127.0.0.1:50510", "tcp://a:bad"):
        try:
            with redirect_stderr(io.StringIO()):
                parse_args(["--telemetry", bad])
        except SystemExit as exc:
            if exc.code != 2:
                failures.append(f"--telemetry {bad!r} exited {exc.code}, expected 2")
        else:
            failures.append(f"--telemetry {bad!r} was accepted")
    try:
        with redirect_stderr(io.StringIO()):
            args = parse_args(["--telemetry", "tcp://"])
            check("--telemetry tcp:// is accepted as typed", args.telemetry, "tcp://")
            args = parse_args(["--telemetry", "none"])
            check("--telemetry none is accepted", args.telemetry, "none")
            args = parse_args([])
            check("--telemetry defaults to off", args.telemetry,
                  os.environ.get("MACVISION_TELEMETRY", ""))
            check("--describe-args is off unless given", args.describe_args, False)
            args = parse_args(["--describe-args"])
            check("--describe-args parses", args.describe_args, True)
    except SystemExit:
        failures.append("a valid --telemetry / --describe-args line was rejected")

    for mod in ("cv2", "numpy", "torch", "ultralytics"):
        if mod in sys.modules:
            failures.append(f"importing macvision.loop pulled in {mod}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("trigger-before-render on every frame, held state on an empty capture, "
          "mac/decide sampling points, headless, measurement pass-through, drop "
          "accounting, cadence, geometry check, telemetry placement and cost, "
          "--describe-args: all correct")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
