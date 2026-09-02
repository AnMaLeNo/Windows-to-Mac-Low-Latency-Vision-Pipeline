"""Checks the clock-skew calibration that makes every published latency number honest.

Inverting the append/min order inside observe() either raises on the first frame or
publishes negative excess delay, and both look like a network problem rather than an
arithmetic one. Reporting transit directly instead of the sum re-introduces the ~238ms
of pure clock skew commit c837bc9 exists to remove.

    python3 -m tests.test_stats      (from mac-app/)

Zero third-party imports.
"""

import statistics
import sys

from macvision.stats import LatencyStats, overlay_text

OFFSET = 238.0   # the measured Windows/Mac clock offset on this pair


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    # The very first frame must not raise: this is the append-before-min ordering, and
    # this assertion exists precisely to catch its inversion (min() of an empty deque
    # raises ValueError).
    s = LatencyStats()
    try:
        queue_ms, e2e_ms = s.observe(OFFSET, 5.0, 3.0, 2.0)
    except Exception as exc:
        failures.append(f"the first observe() raised {exc!r}")
        queue_ms = e2e_ms = None
    check("first frame reports no excess", queue_ms, 0.0)
    check("first frame e2e = win + queue + mac", e2e_ms, 8.0)

    # A constant transit is pure clock offset and must calibrate out completely.
    s = LatencyStats()
    constant = [s.observe(OFFSET, 5.0, 3.0, 2.0)[0] for _ in range(50)]
    check("constant transit -> zero excess on every frame", set(constant), {0.0})

    # A flat baseline with one spike: the spike is exactly the excess, and the frame
    # after it returns to zero.
    s = LatencyStats()
    for _ in range(10):
        s.observe(OFFSET, 5.0, 3.0, 2.0)
    check("a +12ms spike reports 12ms of excess",
          s.observe(OFFSET + 12.0, 5.0, 3.0, 2.0)[0], 12.0)
    check("the next normal frame is back to zero",
          s.observe(OFFSET, 5.0, 3.0, 2.0)[0], 0.0)

    # Never negative, even as transit monotonically improves.
    s = LatencyStats()
    negatives = [s.observe(OFFSET - i, 5.0, 3.0, 2.0)[0] for i in range(30)]
    if any(q < 0 for q in negatives):
        failures.append(f"queue_ms went negative on an improving transit: "
                        f"{[q for q in negatives if q < 0][:3]}")

    # e2e must be win + queue + mac, and explicitly NOT transit + mac. With a 238ms
    # offset the two differ by exactly that, which is the whole point.
    s = LatencyStats()
    s.observe(OFFSET, 4.0, 6.0, 3.0)
    _, e2e = s.observe(OFFSET + 2.0, 4.0, 6.0, 3.0)
    check("e2e is the honest sum", e2e, 12.0)
    if abs(e2e - (OFFSET + 2.0 + 6.0)) < 1.0:
        failures.append("e2e looks like transit + mac - the clock skew is back")

    # The window is bounded so it follows a slewing clock instead of latching an
    # all-time minimum.
    s = LatencyStats(window=5)
    s.observe(100.0, 0.0, 0.0, 0.0)                 # a low sample...
    for _ in range(5):
        s.observe(200.0, 0.0, 0.0, 0.0)             # ...rolled out of the window
    check("a sample that left the window stops suppressing the excess",
          s.observe(200.0, 0.0, 0.0, 0.0)[0], 0.0)

    s = LatencyStats(window=200)
    for _ in range(200):
        s.observe(238.0, 0.0, 0.0, 0.0)
    check("baseline before the slew", s.status()["offset_ms"], 238.0)
    for _ in range(200):
        s.observe(250.0, 0.0, 0.0, 0.0)
    check("the calibration follows a slewing clock", s.status()["offset_ms"], 250.0)

    # status() and summary()
    s = LatencyStats()
    e2es = []
    for i in range(5):
        _, e = s.observe(OFFSET + i, 1.0, 2.0, 1.5)
        e2es.append(e)
    st = s.status()
    check("status n", st["n"], 5)
    check("status e2e median", st["e2e_median_ms"], statistics.median(e2es))
    check("status e2e max", st["e2e_max_ms"], max(e2es))
    check("status decide median", st["decide_median_ms"], 1.5)

    fresh = LatencyStats()
    try:
        check("summary() on an empty window", fresh.summary(), "n=0")
    except Exception as exc:
        failures.append(f"summary() raised on an empty window: {exc!r}")
    check("status() on an empty window", fresh.status()["n"], 0)

    # The published vocabulary. docs/PROTOCOL.md defines all four labels by name.
    text = overlay_text(12.34, 5.67, 0.0, 3.21, 99, True)
    for token in ("e2e~12ms", " win=5.7", " net+=0.0", " mac=3.2", "seq=99", "trig=ON"):
        if token not in text:
            failures.append(f"overlay text is missing {token!r}: {text!r}")
    if "trig=off" not in overlay_text(1, 1, 1, 1, 1, False):
        failures.append("overlay text does not say trig=off when the trigger is idle")

    # --- a single-machine source: nothing to calibrate, and no pretending -----------
    # A camera has no second clock to be wrong about, and cannot see how long the
    # photons took. Reporting 0.0 for either would be a lie of a familiar kind: a
    # number that looks measured and is not.
    c = LatencyStats()
    queue_ms, e2e_ms = c.observe(None, None, 3.2, 3.0)
    check("no transit -> no calibration", queue_ms, None)
    check("e2e is just what was measured", e2e_ms, 3.2)
    check("status reports no offset", c.status()["offset_ms"], None)
    check("but still counts frames", c.status()["n"], 1)
    if "lower bound" not in c.summary():
        failures.append(f"the summary does not mark e2e as a bound: {c.summary()!r}")
    if "calibrated out" in c.summary():
        failures.append("the summary claims a calibration that never happened")

    # An upstream that IS measurable still adds in, even with no transit.
    c2 = LatencyStats()
    check("upstream still counts without a transit",
          c2.observe(None, 4.0, 3.0, 2.0)[1], 7.0)

    # The overlay must say ">" (a lower bound) and "-.-" for what was never measured.
    cam = overlay_text(3.2, None, None, 3.2, 99, False, upstream_label="cam")
    for token in ("e2e>3ms", "cam=-.-", "net+=-.-", "mac=3.2", "trig=off"):
        if token not in cam:
            failures.append(f"the camera overlay is missing {token!r}: {cam!r}")
    if "e2e~" in cam:
        failures.append("the camera overlay claims a known understatement with ~")
    if "0.0" in cam:
        failures.append(f"an unmeasured field rendered as 0.0, which reads as "
                        f"'measured, and it was nothing': {cam!r}")

    for mod in ("cv2", "numpy", "torch", "ultralytics"):
        if mod in sys.modules:
            failures.append(f"importing macvision.stats pulled in {mod}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("first-frame ordering, constant-offset calibration, spikes, the rolling "
          "window, the honest sum, the single-machine path, and the overlay "
          "vocabulary: all correct")
    return 0


if __name__ == "__main__":
    sys.exit(run())
