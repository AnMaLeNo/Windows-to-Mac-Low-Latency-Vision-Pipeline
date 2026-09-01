"""The frame loop. The ORDERING is the design, and this is where it is written down.

Every collaborator arrives as a parameter and is used through plain method calls, so
this module imports nothing third-party and can be driven end to end by fakes - which is
what tests/test_loop_order.py does to keep the one ordering that matters executable
rather than merely commented.

It does not know where frames come from. A source hands back a Capture; whether that
came off a wire from another machine or off a camera on this one changes nothing here,
which is the whole point of the split. Nor does it know what a JPEG is - decoding is
the udp source's private business, and the camera source has no decode step at all.

There is deliberately no context object, no Frame copy and no stage list:

  - A Capture holds a REFERENCE to the source's array. Nothing on the path ahead of the
    trigger write may copy the pixels.
  - A stage list runs in declaration order, and someone will eventually put render
    before trigger. The decision and the send must be reachable with no renderer in
    existence at all - which is exactly what run(display=None) gives.
"""

import sys
import time

from .rule import center_is_covered, roi_center
from .stats import STATS_EVERY, overlay_text


def run(source, detector, action, stats, display, roi_w, roi_h,
        stats_every=STATS_EVERY):
    """The frame loop. Returns 0 on a clean quit (q, Ctrl-C, SIGTERM).

    Tears NOTHING down - main() owns that, in a finally block, so the key is released on
    every exit path including the ones that never reach the end of this function.
    """
    cx, cy = roi_center(roi_w, roi_h)
    frames = 0
    geometry_checked = False

    try:
        while True:
            cap = source.recv()

            if cap.frame is None:
                # No usable image. HOLD the current state: a corrupt datagram, a camera
                # that stalled, or a runt packet is not evidence that the person left.
                # The loop must never be made total over action.update() - "call it
                # once per iteration, with hit=False when there is no frame" turns one
                # bad packet into a visible key release. The only thing entitled to
                # clear a state without new information is the far end's watchdog.
                if cap.note:
                    print(cap.note, file=sys.stderr, flush=True)
                continue

            if not geometry_checked:
                geometry_checked = True
                if (cap.width, cap.height) != (roi_w, roi_h):
                    # Checked against the configured value, which is what the detector's
                    # warmup was shaped from. Not a guarantee - it stays silent if both
                    # ends change together - but without it a geometry change is a
                    # silent, total failure: grow the region and the rule tests an
                    # off-centre pixel while the crosshair still draws at the configured
                    # centre, so the debug view agrees with itself and lies; shrink it
                    # past the centre and the tested point falls outside the frame, so
                    # the trigger can never fire again.
                    print("\n" + "=" * 72, file=sys.stderr)
                    print(f"  GEOMETRY MISMATCH - the source is sending "
                          f"{cap.width}x{cap.height}, this receiver is configured for "
                          f"{roi_w}x{roi_h}.", file=sys.stderr)
                    print("  The centre pixel the trigger rule tests is therefore the "
                          "wrong pixel.", file=sys.stderr)
                    print(f"  Pass --roi-w {cap.width} --roi-h {cap.height}, or fix the "
                          "source's crop.", file=sys.stderr)
                    print("=" * 72 + "\n", file=sys.stderr, flush=True)

            result = detector.infer(cap.frame)

            # Fire before drawing anything. result.plot() and imshow() together cost
            # several milliseconds, and none of that work is needed to decide whether to
            # press the key - so the trigger byte goes out the instant the decision
            # exists, not at end of frame. `hit` is computed once here and reused by the
            # crosshair and the overlay below.
            hit = center_is_covered(detector.boxes(result), cx, cy)
            action.update(hit)

            # Acquisition -> byte written. The number that actually matters for latency,
            # identical with and without a display, and the only one comparable between
            # a network source and a camera.
            decide_ms = (time.perf_counter() - cap.t0) * 1000

            if cap.note:
                # Held back until here on purpose: gaps happen precisely when the
                # pipeline is already behind, and stderr is unbuffered.
                print(cap.note, file=sys.stderr, flush=True)

            if display is not None:
                annotated = display.annotate(result, hit, cx, cy)
                # Sampled after plot() and drawMarker, before putText, the stats print
                # and imshow/waitKey - docs/PROTOCOL.md's published definition of `mac`.
                # It must also exist before the overlay text is composed, because it is
                # rendered into it and cannot then be measured.
                mac_ms = (time.perf_counter() - cap.t0) * 1000
            else:
                annotated = None
                mac_ms = decide_ms

            queue_ms, e2e_ms = stats.observe(cap.transit_ms, cap.upstream_ms,
                                             mac_ms, decide_ms)

            # Counts frames actually processed, not everything the source saw - the
            # `continue` above skips this line. The stats cadence is a number people
            # watch.
            frames += 1

            if frames % stats_every == 0:
                # The median and the flushing write sit past the trigger byte, which
                # is where they belong; a "measure, then act" reorganisation would move
                # a periodic spike to precisely the wrong place. Note that the O(window)
                # min() scan is NOT periodic - it runs on every frame inside
                # stats.observe() above, because the calibration needs it every time.
                # It is still past the trigger byte, which is the property that matters.
                print(f"[stats] {stats.summary()}  |  "
                      f"stale dropped={getattr(source, 'stale_dropped', 0)}  |  "
                      f"trigger writes dropped={action.dropped_writes}"
                      + ("  |  display=off" if display is None else ""), flush=True)

            if display is not None and not display.present(
                    annotated,
                    overlay_text(e2e_ms, cap.upstream_ms, queue_ms, mac_ms, cap.seq,
                                 hit, upstream_label=source.upstream_label)):
                break

    except KeyboardInterrupt:
        print("\n[macvision] interrupted; releasing the trigger",
              file=sys.stderr, flush=True)
    return 0
