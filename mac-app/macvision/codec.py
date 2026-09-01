"""JPEG bytes -> a BGR frame.

Its own module rather than a class inside detector.py for one reason: "decode a JPEG"
should not require importing the module whose other class needs ultralytics and torch.
That split is also what lets a machine with opencv-python-headless run decode and
detection with no HighGUI at all, and it is the natural swap point for a turbojpeg
backend with the same two-member surface.
"""


class JpegDecoder:
    """cv2.imdecode behind a two-method surface. Never raises on bad input."""

    def __init__(self):
        import cv2      # lazy: the stdlib-only core never decodes anything
        import numpy    # lazy: same

        # Bound at construction rather than looked up per frame. This removes three
        # module-global lookups from the stretch ahead of the trigger write. Not worth
        # overselling - imdecode itself costs hundreds of microseconds - but it costs
        # nothing and it keeps the import machinery off the hot path entirely.
        self._imdecode = cv2.imdecode
        self._frombuffer = numpy.frombuffer
        self._uint8 = numpy.uint8
        self._flag = cv2.IMREAD_COLOR
        self.failures = 0

    def decode(self, buf):
        """-> a BGR ndarray, or None. None is exactly what cv2 reports; no exception."""
        frame = self._imdecode(self._frombuffer(buf, self._uint8), self._flag)
        if frame is None:
            self.failures += 1
        return frame

    def status(self):
        return {"backend": "cv2.imdecode", "failures": self.failures}
