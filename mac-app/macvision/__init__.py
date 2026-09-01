"""macvision - the Mac half of the low-latency vision pipeline.

The Windows PC captures a small fixed region of its screen, JPEG-encodes it and sends
it over UDP. This package decodes those frames, asks one question of each - does a
person cover the ROI's centre pixel? - and pushes one byte of state to whatever holds
the key down on the PC: an ESP32 over USB serial, or a Raspberry Pi keyboard proxy
over UDP.

Nothing is imported here on purpose. Keeping this file empty is what lets
`import macvision.rule` (or .protocol, .stats, .stream, .trigger, .loop) work on a
machine with no cv2, numpy, torch or ultralytics installed - one convenience
re-export would undo that for every one of them. tests/test_imports.py enforces it.

See mac-app/README.md for setup and TRIGGER_TARGET.
"""

__version__ = "0.1.0"
