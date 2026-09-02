"""Find the iPhone's camera index, and see exactly what the pipeline would crop from it.

OpenCV's AVFoundation backend addresses cameras by index only - it cannot report a
device's name, and the indices are not stable: the built-in FaceTime camera is usually
0, but where a Continuity Camera lands depends on what else is attached and when it
connected. So this probes every index in turn and writes a preview of each, letting you
identify the iPhone by looking at it rather than by guessing a number.

Each preview is drawn with the 300x300 native centre crop marked, because that rectangle -
not the full frame - is what YOLO would actually see. The crop is saved separately too, at
1:1, so you can judge the real field of view before committing to a capture resolution.

    python tools/list_cameras.py             # probe indices 0..5 at the camera's default mode
    python tools/list_cameras.py 8           # probe 0..7
    python tools/list_cameras.py 6 1280x720  # also request a mode, and report what was granted

Two failure modes this is meant to expose, both of which look like a working camera:

  * No macOS camera permission. The prompt goes to whichever app owns this process
    (Terminal, iTerm, VS Code), not to Python. Deny it - or never see the prompt - and
    frames arrive all-black instead of failing, so a black preview is called out by name.
  * Continuity Camera not offered at all. `system_profiler` below lists what macOS itself
    sees; if the iPhone is missing there, no amount of index probing will find it.
"""

import subprocess
import sys
import time

import cv2
import numpy as np

CROP_W, CROP_H = 300, 300  # must match ROI_W/ROI_H in receiver.py

OUT_DIR = "tools/camera_previews"

# Continuity Camera's first frames are routinely black or half-decoded while the link
# comes up, and grabbing one of those would misreport a perfectly good camera as broken.
WARMUP_FRAMES = 10
FPS_FRAMES = 30      # how many frames to time for the measured (not advertised) rate
BLACK_LEVEL = 8      # a frame whose brightest pixel is under this is black, not dark


def system_cameras() -> int:
    """What macOS itself sees. Names, which OpenCV cannot give us. Returns how many.

    This runs before any probing so the two answers can be compared: macOS listing a
    camera that no index will open is a permission problem, whereas macOS listing
    nothing means the device is simply not there.
    """
    print("=" * 72)
    print("Cameras macOS reports (system_profiler):")
    print("=" * 72)
    try:
        out = subprocess.run(
            ["system_profiler", "SPCameraDataType"],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except Exception as exc:
        print(f"  could not run system_profiler: {exc}")
        print()
        return -1

    print(out if out else "  (none reported)")
    print()
    # Every camera is listed as an indented "Name:" line under the "Camera:" heading,
    # each followed by its indented Model ID / Unique ID properties. Counting Model ID
    # lines counts devices without having to parse the nesting.
    return out.count("Model ID:")


def probe(index: int, want: "tuple[int, int] | None") -> bool:
    """Open one index, describe it, and write previews. True if it produced a frame."""
    cap = cv2.VideoCapture(index, cv2.CAP_AVFOUNDATION)
    if not cap.isOpened():
        print(f"[{index}] not opened")
        return False

    if want:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, want[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, want[1])

    t_open = time.perf_counter()
    frame = None
    for _ in range(WARMUP_FRAMES):
        ok, f = cap.read()
        if ok and f is not None:
            frame = f
    if frame is None:
        print(f"[{index}] opened but produced no frame")
        cap.release()
        return False
    first_frame_ms = (time.perf_counter() - t_open) * 1000

    # The rate the driver claims, versus the rate we actually get. They disagree often
    # enough - especially over wireless Continuity - that only the measured one is worth
    # designing around.
    advertised_fps = cap.get(cv2.CAP_PROP_FPS)
    t0 = time.perf_counter()
    read = 0
    for _ in range(FPS_FRAMES):
        ok, f = cap.read()
        if ok and f is not None:
            frame, read = f, read + 1
    measured_fps = read / (time.perf_counter() - t0) if read else 0.0

    h, w = frame.shape[:2]
    granted = ""
    if want and (w, h) != want:
        # AVFoundation snaps to the nearest format it supports rather than refusing, so a
        # request is a hint. Say what came back, since it decides the crop's field of view.
        granted = f"  (requested {want[0]}x{want[1]})"

    print(f"[{index}] {w}x{h}{granted}  "
          f"fps: {measured_fps:.1f} measured / {advertised_fps:.0f} advertised  "
          f"first frame in {first_frame_ms:.0f}ms")

    if int(frame.max()) < BLACK_LEVEL:
        print(f"     ^ ALL BLACK. Almost always the macOS camera permission: it is granted")
        print(f"       to the app running this process, not to Python. Check System")
        print(f"       Settings > Privacy & Security > Camera for your terminal or editor.")

    # The centre crop, taken exactly as the pipeline would take it: native pixels, no
    # resize. What fraction of the scene this covers is entirely a function of the
    # capture resolution above - that is the only lever, since the crop size is fixed.
    x0, y0 = (w - CROP_W) // 2, (h - CROP_H) // 2
    if x0 < 0 or y0 < 0:
        print(f"     ^ frame is smaller than the {CROP_W}x{CROP_H} crop - skipping crop preview")
        cap.release()
        return True
    crop = frame[y0:y0 + CROP_H, x0:x0 + CROP_W]

    marked = frame.copy()
    cv2.rectangle(marked, (x0, y0), (x0 + CROP_W, y0 + CROP_H), (0, 255, 0), 2)
    cv2.imwrite(f"{OUT_DIR}/cam{index}_full.jpg", marked)
    cv2.imwrite(f"{OUT_DIR}/cam{index}_crop.jpg", crop)
    print(f"     -> {OUT_DIR}/cam{index}_full.jpg (crop marked), "
          f"cam{index}_crop.jpg ({CROP_W}x{CROP_H}, "
          f"{100 * CROP_H / h:.0f}% of frame height)")

    cap.release()
    return True


def main() -> None:
    max_index = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    want = None
    if len(sys.argv) > 2:
        want = tuple(int(v) for v in sys.argv[2].lower().split("x"))

    import os
    os.makedirs(OUT_DIR, exist_ok=True)

    n_system = system_cameras()

    print("=" * 72)
    print(f"Probing OpenCV/AVFoundation indices 0..{max_index - 1}")
    print("=" * 72)
    found = [i for i in range(max_index) if probe(i, want)]

    print()
    if found:
        print(f"Working indices: {found}. Open the previews in {OUT_DIR}/ to see which "
              f"one is the iPhone.")
    elif n_system > 0:
        # The diagnostic case worth spelling out: macOS has cameras, OpenCV can reach
        # none of them. That is the TCC permission, and the reason it is confusing is
        # that the permission does not belong to Python - it belongs to whatever app is
        # responsible for this process, so where you launched from decides who is asked.
        print(f"macOS lists {n_system} camera(s), but no index opened. That is the camera")
        print("permission, not a missing device. Look for 'not authorized to capture")
        print("video' above.")
        print()
        print("macOS grants camera access to the app responsible for this process, never")
        print("to python itself - so run this from Terminal.app or iTerm directly and")
        print("answer the prompt. Launched from an editor's integrated terminal, the")
        print("prompt is attributed to the editor, and for some hosts never appears at")
        print("all. Then check System Settings > Privacy & Security > Camera.")
    else:
        print("macOS itself reports no cameras, so there is nothing for OpenCV to open.")


if __name__ == "__main__":
    main()
