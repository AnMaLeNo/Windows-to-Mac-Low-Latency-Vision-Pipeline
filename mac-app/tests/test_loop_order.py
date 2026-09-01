"""Makes the per-frame ordering executable instead of merely commented.

action.update() must fire before ANY drawing. That is the one place docs/TRIGGER.md
says this project spends care on latency, and until now it was protected only by a
comment - so a tidy-up that moved the trigger write below the render would have shipped.
It now fails a test.

The loop is driven for real, with fakes that append to one shared call log. Nothing
third-party is imported, so this runs on any machine.

    python3 -m tests.test_loop_order      (from mac-app/)
"""

import io
import sys
import time
from contextlib import redirect_stderr, redirect_stdout

from macvision.loop import run
from macvision.protocol import ROI_H, ROI_W
from macvision.sources import Capture
from macvision.stats import LatencyStats


def capture(seq, frame="frame", width=ROI_W, height=ROI_H, upstream_ms=1.0,
            transit_ms=5.0, dropped=0, note=None):
    """One Capture, as a source would hand it over."""
    return Capture(frame, time.perf_counter(), seq, width, height,
                   upstream_ms=upstream_ms, transit_ms=transit_ms, dropped=dropped,
                   note=note)


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


def drive(captures, *, display=True, boxes=None, stats_every=100,
          annotate_delay=0.0, present_delay=0.0, quit_after=None, roi=(ROI_W, ROI_H)):
    log = []
    source = FakeSource(captures, log)
    detector = FakeDetector(log, boxes_per_frame=boxes)
    action = FakeAction(log)
    stats = SpyStats()
    disp = (FakeDisplay(log, annotate_delay, present_delay, quit_after)
            if display else None)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = run(source, detector, action, stats, disp,
                 roi[0], roi[1], stats_every=stats_every)
    return {"rc": rc, "log": log, "source": source, "trigger": action, "stats": stats,
            "display": disp, "stdout": out.getvalue(), "stderr": err.getvalue()}


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
          "accounting, cadence, geometry check: all correct")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
