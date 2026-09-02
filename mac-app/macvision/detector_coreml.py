"""The CoreML adapter - the same three names as detector.py, on the Neural Engine.

Why this module exists at all. The PyTorch/MPS path is not GPU-bound, it is
overhead-bound: measured on this M5 Air with a 300x300 crop, the raw network forward is
3.5ms at 320 and 5.4ms at 640, while predict() around it costs 9-20ms. CoreML on the ANE
runs the SAME weights at the SAME input size, with NMS folded into the model, and the
whole per-frame path costs 2.1ms at 640 or 0.9ms at 320.

The median is not the interesting part. The tail is:

    PyTorch/MPS  @640    med  9.18ms   p90 16.56ms
    CoreML/ANE   @640    med  2.09ms   p90  5.46ms   (whole path, this module)

This is a trigger pipeline on a FANLESS machine. Sustained MPS load throttles, and the
p90 is what decides whether a key is held a frame too long; the ANE is both faster and
an order of magnitude steadier. That steadiness is the reason to prefer this path, more
than the median is.

What is NOT claimed here: better detections. The weights are identical and the boxes
were checked against the PyTorch path on the same image - same count, same classes, same
confidences, worst corner disagreement 0.10px on a 300x300 crop. This module buys
latency, not accuracy. Spending the saved milliseconds on a bigger model is the separate
decision, and yolov8m at 640 costs 5.75ms on the ANE - still under today's median.

Interface parity with detector.Detector is deliberate and load-bearing: loop.py calls
infer() then boxes(), display.py calls result.plot(). Nothing outside this file knows
which backend is running, which is why __main__.py can pick one from the weights
extension alone.

Third-party imports stay inside the constructor and the methods, like every other
adapter here - tests/test_imports.py asserts that importing this module pulls in
neither coremltools nor cv2 nor numpy.
"""

from .protocol import ROI_H, ROI_W

# Same class filter as detector.py, and for the same reason - see CLASSES there. Applied
# AFTER the model's NMS rather than inside it, which is only equivalent because the
# exported pipeline sets perClassSuppression=True (verified on the spec). If a future
# export turns that off, a car could be suppressed by an overlapping non-car box and
# this filter would silently hide it.
CLASSES = [2]

# Ultralytics' own defaults, restated because they now have to be passed explicitly:
# the CoreML pipeline takes them as model inputs rather than reading them from a config.
CONF = 0.25
IOU = 0.45

PAD_VALUE = 114  # letterbox grey, matching Ultralytics


class CoreMLDetector:
    def __init__(self, roi_w=ROI_W, roi_h=ROI_H, weights="yolov8n_640.mlpackage",
                 classes=CLASSES, conf=CONF, iou=IOU, compute_units="ALL"):
        import coremltools as ct
        import cv2
        import numpy as np

        self._ct, self._cv2, self._np = ct, cv2, np
        self.weights = weights
        self.classes = set(classes)
        self.conf = conf
        self.iou = iou
        self.roi = (roi_w, roi_h)
        self.compute_units = compute_units

        units = getattr(ct.ComputeUnit, compute_units)
        self.model = ct.models.MLModel(weights, compute_units=units)

        # The input size is READ from the model, never assumed. A .mlpackage is exported
        # at one fixed resolution; guessing 640 and loading a 320 export would not raise
        # - CoreML would refuse the image, or worse, a future export with a flexible
        # shape would silently accept the wrong one and every box would be misplaced.
        spec = self.model.get_spec()
        image_input = spec.description.input[0]
        self.size = (image_input.type.imageType.width,
                     image_input.type.imageType.height)
        self._input_name = image_input.name

        # Letterbox geometry, computed ONCE. roi_w/roi_h never change during a run - the
        # loop treats a geometry change as a fatal mismatch - so recomputing this per
        # frame would be pure waste on the hot path.
        sw, sh = self.size
        self._scale = min(sw / roi_w, sh / roi_h)
        self._nw, self._nh = round(roi_w * self._scale), round(roi_h * self._scale)
        self._px, self._py = (sw - self._nw) // 2, (sh - self._nh) // 2
        # A square ROI into a square model input needs no padding at all, and skipping
        # the canvas entirely is worth real time at this scale.
        self._square = (self._px == 0 and self._py == 0
                        and (self._nw, self._nh) == (sw, sh))

        # Preallocated and filled with the pad grey ONCE. The padded margins are the
        # same every frame because the geometry is fixed, so only the image region is
        # rewritten below - np.full() per frame would allocate 1.2MB on the hot path.
        self._canvas = None if self._square else \
            np.full((sh, sw, 3), PAD_VALUE, dtype=np.uint8)

        # Warm up for the same reason detector.py does: the first predict() loads the
        # model onto the Neural Engine, which takes ~0.4s. That cost belongs at startup,
        # not on the first real frame.
        from PIL import Image
        self.model.predict({self._input_name: Image.fromarray(
            np.zeros((sh, sw, 3), dtype=np.uint8)),
            "iouThreshold": iou, "confidenceThreshold": conf})

    # --- the hot path ---------------------------------------------------------------

    def _to_model_input(self, frame_bgr):
        """BGR ndarray in ROI pixels -> the RGB PIL image the model wants.

        cv2.cvtColor rather than frame[:, :, ::-1]: the numpy slice is a negative-stride
        view, and PIL has to walk it the slow way. Measured on a 640 frame, the slice
        path cost several times what cvtColor does, and it was most of the gap between
        this module's first draft (5.12ms) and what it costs now.
        """
        cv2 = self._cv2
        from PIL import Image

        if self._square:
            resized = cv2.resize(frame_bgr, self.size,
                                 interpolation=cv2.INTER_LINEAR)
            return Image.fromarray(cv2.cvtColor(resized, cv2.COLOR_BGR2RGB))

        canvas = self._canvas
        cv2.resize(frame_bgr, (self._nw, self._nh), interpolation=cv2.INTER_LINEAR,
                   dst=canvas[self._py:self._py + self._nh,
                              self._px:self._px + self._nw])
        return Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))

    def infer(self, frame_bgr):
        out = self.model.predict({self._input_name: self._to_model_input(frame_bgr),
                                  "iouThreshold": self.iou,
                                  "confidenceThreshold": self.conf})
        return CoreMLResult(self, frame_bgr, out)

    def boxes(self, result):
        """Thin delegate, mirroring Detector.boxes so loop.py needs no branch."""
        return result.boxes_xyxy()

    def status(self):
        return {"weights": self.weights, "device": f"coreml:{self.compute_units}",
                "classes": sorted(self.classes), "roi": list(self.roi),
                "input": list(self.size)}


class CoreMLResult:
    """What infer() hands back. Exists to satisfy two callers and nothing more.

    loop.py wants boxes; display.py wants plot() to return an annotated BGR image. An
    Ultralytics Results object provides both, so this provides both - that parity is
    what keeps display.py free of any knowledge of which backend produced the frame.

    The boxes are decoded LAZILY, on first request. Not premature cleverness: with
    --no-display the overlay is never drawn, and a detection that covers no box is the
    common case, so the decode should not happen at all when nothing asks.
    """

    def __init__(self, detector, frame_bgr, raw):
        self._d = detector
        self._frame = frame_bgr
        self._raw = raw
        self._boxes = None

    def boxes_xyxy(self):
        """-> [[x1, y1, x2, y2], ...] in the ROI's own pixels, cars only.

        Vectorised, unlike rule.center_is_covered's deliberate plain loop: this runs
        once per frame over every surviving detection, where numpy's per-call overhead
        is amortised, rather than once per box comparison where it is not.
        """
        if self._boxes is not None:
            return self._boxes
        np = self._d._np
        coords = np.asarray(self._raw["coordinates"])   # (N,4) normalised cx,cy,w,h
        confs = np.asarray(self._raw["confidence"])     # (N,80)
        if coords.shape[0] == 0:
            self._boxes = []                            # the common case
            return self._boxes

        keep = np.isin(confs.argmax(1), list(self._d.classes))
        coords = coords[keep]
        if coords.shape[0] == 0:
            self._boxes = []
            return self._boxes

        sw, sh = self._d.size
        cx = coords[:, 0] * sw
        cy = coords[:, 1] * sh
        hw = coords[:, 2] * sw / 2.0
        hh = coords[:, 3] * sh / 2.0
        # Undo the letterbox: drop the padding offset, then the scale. Getting this
        # backwards is the silent-coordinate-bug this module was checked against the
        # PyTorch path to rule out.
        s, px, py = self._d._scale, self._d._px, self._d._py
        xyxy = np.stack([(cx - hw - px) / s, (cy - hh - py) / s,
                         (cx + hw - px) / s, (cy + hh - py) / s], axis=1)
        self._boxes = xyxy.tolist()
        return self._boxes

    def scores(self):
        return self._kept()[1]

    def _kept(self):
        """-> (class indices, scores) for the detections that survived the filter."""
        np = self._d._np
        confs = np.asarray(self._raw["confidence"])
        if confs.shape[0] == 0:
            return [], []
        cls = confs.argmax(1)
        keep = np.isin(cls, list(self._d.classes))
        return cls[keep].tolist(), confs[keep].max(1).tolist()

    def plot(self):
        """An annotated BGR copy, the way Ultralytics' Results.plot() returns one.

        A COPY, deliberately: the frame is a view into the source's buffer (the camera
        source hands over a numpy slice, and nothing on the path ahead of the trigger
        write may copy the pixels). Drawing into it would corrupt the next frame's data.
        This is past the trigger write, so the copy is affordable here and only here.
        """
        cv2 = self._d._cv2
        annotated = self._frame.copy()
        cls, scores = self._kept()
        # The label carries the CLASS INDEX, not a hardcoded "car". With the default
        # classes=[2] every box is a car and the two would agree - but this class takes
        # `classes` as a parameter, and a label that reads "car" whatever was asked for
        # is a debug window that lies precisely when someone is widening the filter to
        # find out what else the model sees. COCO names would need ultralytics, which
        # this module must not import; the index is the honest thing available.
        for (x1, y1, x2, y2), c, score in zip(self.boxes_xyxy(), cls, scores):
            p1, p2 = (int(x1), int(y1)), (int(x2), int(y2))
            cv2.rectangle(annotated, p1, p2, (0, 255, 0), 2)
            cv2.putText(annotated, f"{c} {score:.2f}", (p1[0], max(11, p1[1] - 4)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
        return annotated
