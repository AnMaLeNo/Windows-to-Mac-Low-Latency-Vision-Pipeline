"""Enforces the rule the whole layout rests on: the core imports nothing third-party.

macvision.protocol, .rule, .stats, .sources (both of them), .trigger, .loop and
.telemetry must import with cv2, numpy, torch and ultralytics absent - that is what
lets the wire format, the latency arithmetic and the frame ordering be tested on any
machine, and it is why every other test file in this directory runs here at all.

Nothing but discipline enforces it. One convenience re-export in __init__.py, or one
stray module-level `import cv2`, breaks every other test with no code looking wrong. So
it is a test.

    python3 -m tests.test_imports      (from mac-app/)

This asserts what macvision PULLS IN, not what happens to be installed - it is just as
valid on a Mac with the full stack present.
"""

import importlib
import sys

HEAVY = ("cv2", "numpy", "torch", "ultralytics", "serial")

# Must import with nothing installed AND must pull in none of HEAVY.
PURE = ("macvision", "macvision.protocol", "macvision.rule", "macvision.stats",
        "macvision.sources", "macvision.sources.udp", "macvision.sources.camera",
        "macvision.trigger", "macvision.loop", "macvision.telemetry")

# The adapters. Importing the MODULE must also work with nothing installed, because
# every third-party import in them sits inside a constructor or a method - only
# CONSTRUCTING or OPENING them needs the dependency. The two sources count as pure
# above for the same reason: udp reaches opencv only inside open(), and camera only
# when it has to build its own VideoCapture.
ADAPTERS = ("macvision.codec", "macvision.detector", "macvision.detector_coreml",
            "macvision.display")


def run():
    failures = []

    already = sorted(m for m in HEAVY if m in sys.modules)
    if already:
        print(f"note: {', '.join(already)} already imported before this test began; "
              "the assertions below are about what macvision adds, not what exists")
    baseline = set(sys.modules)

    for name in PURE + ADAPTERS:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failures.append(f"import {name} failed: {exc!r}")

    for mod in HEAVY:
        if mod in sys.modules and mod not in baseline:
            failures.append(f"importing the macvision modules pulled in {mod!r} - "
                            f"a module-level third-party import has crept in")

    # __init__ must stay empty of re-exports: one `from .detector import Detector` for
    # convenience would drag ultralytics into every import above.
    import macvision
    extra = [n for n in vars(macvision)
             if not n.startswith("__") and n != "__version__"]
    # Submodules land in the package namespace once imported; that is not a re-export.
    extra = [n for n in extra
             if not isinstance(getattr(macvision, n), type(sys))]
    if extra:
        failures.append(f"macvision/__init__.py exposes {extra} - keep it empty of "
                        f"re-exports so the core stays importable with nothing installed")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"{len(PURE)} core modules + {len(ADAPTERS)} adapters import cleanly; "
          f"none of {', '.join(HEAVY)} was pulled in")
    return 0


if __name__ == "__main__":
    sys.exit(run())
