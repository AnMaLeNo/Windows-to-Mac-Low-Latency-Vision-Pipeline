"""The Ultralytics adapter - the only module in this package that may import it.

Everything the rest of the pipeline knows about YOLO is behind these three names.
boxes_xyxy() is the one place that knows what an Ultralytics box looks like, which is
what lets rule.py import nothing at all.
"""

from .protocol import ROI_H, ROI_W

WEIGHTS_PATH = "yolov8n.pt"  # swap this one line for your own trained model later

# Class indices to keep. 2 is "car" in COCO - filtering here (rather than after the
# fact) means non-car detections are discarded inside NMS, so they never reach
# result.plot() either. Add 5 ("bus") and 7 ("truck") here if you want those too.
# It must stay a predict() argument and never become a post-filter: making the detector
# generic would cost more NMS work per frame, draw irrelevant boxes in the debug window,
# and - the real hazard - let the centre-pixel test match something that is not a car.
CLASSES = [2]

DEVICE = "mps"


class Detector:
    def __init__(self, roi_w=ROI_W, roi_h=ROI_H, weights=WEIGHTS_PATH, device=DEVICE,
                 classes=CLASSES):
        from ultralytics import YOLO  # lazy: importing this module must not need torch
        import numpy as np            # lazy: only the warmup dummy needs it

        self.weights = weights
        self.device = device
        self.classes = classes
        self.roi = (roi_w, roi_h)
        self.model = YOLO(weights)
        # Warm up before the receive loop starts: Ultralytics' AutoBackend already runs an
        # internal dummy forward pass on first call for non-CPU devices, which absorbs the
        # MPS/Metal kernel-compile cost. Running it here means that cost lands at startup
        # instead of stalling the first real detection.
        #
        # Lazy IMPORT inside a constructor is house style; lazy CONSTRUCTION on first use
        # would be a latency regression - it would move the model load plus the Metal
        # kernel compile onto the first real frame, stalling for seconds while the socket
        # backlog grows.
        #
        # What must match between this call and infer() is the input shape, the device
        # and verbose=False; a mismatch in any of the three moves the kernel compile onto
        # the first real frame, which is the entire reason the warmup exists. infer()
        # additionally passes classes=, and this does not: that is the long-standing
        # behaviour and there is no evidence it matters, because class filtering is a
        # post-NMS argument rather than a graph shape.
        dummy = np.zeros((roi_h, roi_w, 3), dtype=np.uint8)
        self.model.predict(dummy, device=device, verbose=False)

    def infer(self, frame_bgr):
        # A batch of one, on purpose: any batching "optimisation" adds latency by
        # construction here. verbose=False must survive on both calls - Ultralytics
        # otherwise prints a line per prediction, and stdout writes at 80fps are real
        # per-frame latency.
        return self.model.predict(frame_bgr, device=self.device, classes=self.classes,
                                  verbose=False)[0]

    def boxes(self, result):
        """Thin delegate, so loop.py can reach the extraction through this object.

        loop.py must not import this module at module scope - that would drag
        ultralytics and torch into the frame loop's import graph and break
        tests/test_loop_order.py on a machine with neither.
        """
        return boxes_xyxy(result)

    def status(self):
        return {"weights": self.weights, "device": self.device,
                "classes": self.classes, "roi": list(self.roi)}


def boxes_xyxy(result):
    """Ultralytics Results -> a plain list of [x1, y1, x2, y2].

    Module-level so it works on any Results without constructing a model. Call it ONCE
    per frame and reuse the list: calling it again to colour the crosshair or build the
    overlay would double the device-to-host sync per frame.
    """
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return []                      # the common case; the tensor is never touched
    return boxes.xyxy.cpu().tolist()   # exactly ONE device -> host sync
