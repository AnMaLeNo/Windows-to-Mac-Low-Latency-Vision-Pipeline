"""Latency accounting, and why it does not trust the two machines' clocks.

transit = this machine's clock minus the Windows capture timestamp. It contains the
real network delay AND the offset between the two machines' clocks, which is not small:
stock Windows w32time targets ~1s accuracy, and this pair was measured 238ms apart.
Taking transit at face value would report latency that is almost entirely clock skew.

So we self-calibrate instead of trusting the clocks. Queueing delay varies frame to
frame and occasionally clears; a clock offset is constant. Therefore min(transit) over a
few hundred frames ~= clock_offset + best-case network hop, and subtracting it leaves
the *excess* delay above best case - exact, and immune to any offset. A rolling window
keeps this correct even as w32time slowly slews the Windows clock during a run.

What the bounded window costs, which the original code did not state: a backlog that
persists past `window` frames re-baselines against itself and reads as zero excess. That
is inherent to a rolling minimum, and it is the price of tracking the slew.

See docs/PROTOCOL.md, which defines the published vocabulary this module produces.
"""

import statistics
from collections import deque

STATS_WINDOW = 200   # frames kept for the rolling stats line
STATS_EVERY = 100    # print the stats line this often


class LatencyStats:
    """The rolling window, and the only place the four numbers are combined."""

    def __init__(self, window=STATS_WINDOW):
        self.window = window
        self._transit = deque(maxlen=window)
        self._e2e = deque(maxlen=window)
        self._decide = deque(maxlen=window)

    def observe(self, transit_ms, upstream_ms, mac_ms, decide_ms):
        """Records this frame and returns (queue_ms, e2e_ms).

        transit_ms and upstream_ms are None for a source that cannot measure them.
        A single-machine source (a camera) has no second clock to be wrong about, so
        there is nothing to calibrate and queue_ms is None; and it cannot see how long
        the photons took to become an array, so upstream_ms is None too. e2e is then a
        lower BOUND rather than an estimate, and the overlay says so with ">".
        """
        if transit_ms is None:
            # Nothing to calibrate. Returning 0.0 here instead would be a lie of a
            # familiar kind: a number that looks measured and is not.
            e2e_ms = (upstream_ms or 0.0) + mac_ms
            self._e2e.append(e2e_ms)
            self._decide.append(decide_ms)
            return None, e2e_ms

        # The append comes FIRST and min() second, and the two live in one method
        # precisely so a caller cannot invert them. Both consequences of that order look
        # arbitrary and are not: the deque can never be empty when min() runs (on the
        # first frame that is ValueError: min() arg is an empty sequence), and the
        # current sample is inside the window, so the best transit yet reports exactly
        # 0 excess rather than a negative delay.
        self._transit.append(transit_ms)

        # Excess network delay above the best case seen recently - this is where lag
        # buildup shows up, and it carries no clock-offset error.
        queue_ms = transit_ms - min(self._transit)

        # Everything we can account for honestly. Understates true glass-to-glass by
        # exactly the irreducible one-way hop (~1-2ms on this LAN, per ping), which is
        # far better than the ~238ms of clock skew a raw wall-clock delta would inject.
        # This must NEVER become transit_ms + mac_ms: that is the 238ms-of-fake-latency
        # bug commit c837bc9 exists to kill.
        e2e_ms = (upstream_ms or 0.0) + queue_ms + mac_ms

        self._e2e.append(e2e_ms)
        self._decide.append(decide_ms)
        return queue_ms, e2e_ms

    @property
    def n(self):
        return len(self._transit)

    def summary(self):
        """The [stats] line's body. Never raises on an empty run."""
        if not self._e2e:
            return "n=0"
        if not self._transit:
            # A single-machine source: no calibration to report, because there was
            # never a foreign clock in the measurement.
            return (f"n={len(self._e2e)}  "
                    f"e2e med={statistics.median(self._e2e):.1f} "
                    f"max={max(self._e2e):.1f}ms (lower bound)  |  "
                    f"decide med={statistics.median(self._decide):.1f}ms")
        return (f"n={len(self._transit)}  "
                f"e2e med={statistics.median(self._e2e):.1f} "
                f"max={max(self._e2e):.1f}ms  |  "
                f"clock offset+hop={min(self._transit):.1f}ms (calibrated out)  |  "
                f"decide med={statistics.median(self._decide):.1f}ms")

    def status(self):
        if not self._e2e:
            return {"n": 0, "window": self.window, "e2e_median_ms": None,
                    "e2e_max_ms": None, "decide_median_ms": None, "offset_ms": None}
        return {
            "n": len(self._e2e),
            "window": self.window,
            "e2e_median_ms": statistics.median(self._e2e),
            "e2e_max_ms": max(self._e2e),
            "decide_median_ms": statistics.median(self._decide),
            "offset_ms": min(self._transit) if self._transit else None,
        }


def overlay_text(e2e_ms, upstream_ms, queue_ms, mac_ms, seq, hit,
                 upstream_label="win"):
    """The per-frame debug overlay.

    It lives here rather than in display.py for two reasons: these four labels are
    latency vocabulary that docs/PROTOCOL.md defines by name, and keeping the wording in
    a zero-dependency module makes it testable with nothing installed.

    The tilde on e2e is deliberate and load-bearing. It marks a stated understatement of
    exactly one irreducible one-way hop (~1-2ms on this LAN), which is far better than
    the ~238ms of clock skew a raw wall-clock delta would inject.

    `>` replaces it when the source cannot measure its own upstream delay - a camera
    cannot know when the photons arrived - so the number is a lower bound by an unknown
    amount, not an estimate short by a known one. Unmeasured fields render as "-.-"
    rather than 0.0, because a zero here would read as "measured, and it was nothing".
    The label is source-supplied: docs/PROTOCOL.md defines "win" for the Windows agent.
    """
    mark = "~" if upstream_ms is not None else ">"
    up = f"{upstream_ms:.1f}" if upstream_ms is not None else "-.-"
    net = f"{queue_ms:.1f}" if queue_ms is not None else "-.-"
    return (f"e2e{mark}{e2e_ms:.0f}ms  {upstream_label}={up} net+={net} "
            f"mac={mac_ms:.1f}  seq={seq}  trig={'ON' if hit else 'off'}")
