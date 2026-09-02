"""The debug window. The only module that touches cv2's HighGUI, and the only one that
may be absent entirely.

On macOS, Cocoa requires HighGUI windows to be created and pumped from the MAIN thread.
So "run the debug display on its own thread" - the one loose-coupling move that looks
obviously right here - either crashes or silently never draws. This class may own the
drawing; the pump stays on the main thread.

It knows nothing about the win / net+ / mac / e2e~ vocabulary: present() takes any
caption string, which is what makes it a reusable annotated-preview window for any
per-frame loop, and keeps the doc-defined wording in stats.py where it can be tested
with nothing installed.
"""

WINDOW_TITLE = "debug"
HIT_COLOR = (0, 0, 255)        # BGR red - the trigger is firing
IDLE_COLOR = (255, 255, 255)   # white


class DebugWindow:
    def __init__(self, title=WINDOW_TITLE):
        import cv2  # lazy: --no-display and every test path imports this with no opencv

        self._cv2 = cv2
        self.title = title
        # Created ONCE, here, so window creation never lands inside a frame and never
        # sits between the socket bind and the first recv.
        cv2.namedWindow(title, cv2.WINDOW_NORMAL)

    def annotate(self, result, hit, cx, cy):
        """Draw the boxes and the crosshair. Ends exactly where mac_ms is sampled.

        Two methods rather than one show() because mac_ms is taken BETWEEN them - after
        plot() and drawMarker, before putText - which is docs/PROTOCOL.md's published
        definition of `mac`. Merging them would silently redefine the headline number by
        several milliseconds with no code looking wrong.
        """
        cv2 = self._cv2
        annotated = result.plot()
        # Crosshair on the pixel the rule actually tests, coloured by the decision, so the
        # debug window shows what the far end is doing without probing the wire. It must
        # be the SAME `hit` the trigger used, never a recomputed one.
        cv2.drawMarker(annotated, (cx, cy), HIT_COLOR if hit else IDLE_COLOR,
                       cv2.MARKER_CROSS, 12, 1)
        return annotated

    def present(self, annotated, overlay):
        """Caption, show, pump. -> False when the user pressed q."""
        cv2 = self._cv2
        cv2.putText(annotated, overlay, (5, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.4,
                    (0, 255, 0), 1)
        cv2.imshow(self.title, annotated)
        # EXACTLY ONE waitKey in this file and in the package. It is simultaneously the
        # GUI event pump, the >=1ms floor on every iteration, and the only "q" path. A
        # second one would cost another millisecond per frame.
        return (cv2.waitKey(1) & 0xFF) != ord("q")

    def close(self):
        try:
            self._cv2.destroyWindow(self.title)
        except Exception:
            pass

    def status(self):
        return {"window": self.title}
