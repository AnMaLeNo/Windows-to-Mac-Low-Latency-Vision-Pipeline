import numpy as np
from ultralytics import YOLO

WEIGHTS_PATH = "yolov8n.pt"  # swap this one line for your own trained model later

# Class indices to keep. 0 is "person" in COCO - filtering here (rather than after the
# fact) means non-person detections are discarded inside NMS, so they never reach
# result.plot() either.
CLASSES = [0]


class Detector:
    def __init__(self, roi_w: int, roi_h: int):
        self.model = YOLO(WEIGHTS_PATH)
        # Warm up before the receive loop starts: Ultralytics' AutoBackend already runs an
        # internal dummy forward pass on first call for non-CPU devices, which absorbs the
        # MPS/Metal kernel-compile cost. Running it here means that cost lands at startup
        # instead of stalling the first real detection.
        dummy = np.zeros((roi_h, roi_w, 3), dtype=np.uint8)
        self.model.predict(dummy, device="mps", verbose=False)

    def infer(self, frame_bgr: np.ndarray):
        return self.model.predict(frame_bgr, device="mps", classes=CLASSES, verbose=False)[0]
