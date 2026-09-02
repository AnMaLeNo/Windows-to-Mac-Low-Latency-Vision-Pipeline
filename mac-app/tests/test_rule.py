"""Checks the trigger rule's edge behaviour.

The rule is the whole product, and until macvision existed it could not be imported on
a machine without torch - so its edges had never been checked once. Everything asserted
here is behaviour docs/TRIGGER.md describes, which means a future "tightening" has to
argue with this file rather than quietly change what the key does.

    python3 -m tests.test_rule      (from mac-app/)

Zero third-party imports: this runs anywhere.
"""

import sys

from macvision.rule import center_is_covered, roi_center

CX = CY = 150


def run():
    failures = []

    def check(label, got, expected):
        if got != expected:
            failures.append(f"{label}: got {got!r}, expected {expected!r}")

    check("roi_center(300, 300)", roi_center(300, 300), (150, 150))
    check("roi_center(301, 301) floors", roi_center(301, 301), (150, 150))
    check("roi_center(1, 1)", roi_center(1, 1), (0, 0))

    check("no boxes (list)", center_is_covered([], CX, CY), False)
    check("no boxes (tuple)", center_is_covered((), CX, CY), False)

    check("box strictly containing the point",
          center_is_covered([(100, 100, 200, 200)], CX, CY), True)

    # All four comparisons are inclusive: a box whose edge lands exactly on the pixel
    # counts as covering it. docs/TRIGGER.md calls that the rule, so it is asserted
    # rather than left to a reader of the source.
    check("left edge on the pixel",
          center_is_covered([(150, 100, 200, 200)], CX, CY), True)
    check("right edge on the pixel",
          center_is_covered([(100, 100, 150, 200)], CX, CY), True)
    check("top edge on the pixel",
          center_is_covered([(100, 150, 200, 200)], CX, CY), True)
    check("bottom edge on the pixel",
          center_is_covered([(100, 100, 200, 150)], CX, CY), True)
    check("corner exactly on the pixel",
          center_is_covered([(150, 150, 200, 200)], CX, CY), True)

    check("one pixel right of the box",
          center_is_covered([(100, 100, 149, 200)], CX, CY), False)
    check("one pixel left of the box",
          center_is_covered([(151, 100, 200, 200)], CX, CY), False)
    check("one pixel below the box",
          center_is_covered([(100, 100, 200, 149)], CX, CY), False)
    check("one pixel above the box",
          center_is_covered([(100, 151, 200, 200)], CX, CY), False)

    many_miss = [(0, 0, 10, 10), (200, 200, 250, 250), (0, 200, 10, 250)]
    check("three boxes, none covering", center_is_covered(many_miss, CX, CY), False)
    check("three boxes, only the last covering",
          center_is_covered(many_miss[:2] + [(100, 100, 200, 200)], CX, CY), True)

    # .tolist() produces a list of 4-element lists; tuples and floats must behave the
    # same, because what arrives here depends on the detector backend.
    check("list of lists", center_is_covered([[100, 100, 200, 200]], CX, CY), True)
    check("floats", center_is_covered([(100.5, 100.5, 200.5, 200.5)], CX, CY), True)
    check("float box just short",
          center_is_covered([(100.0, 100.0, 149.9, 200.0)], CX, CY), False)

    # No normalisation happens, and that is documented rather than fixed: a degenerate
    # box is a detector bug, and silently swapping the corners would hide it.
    check("degenerate box (x1 > x2) is not normalised",
          center_is_covered([(200, 100, 100, 200)], CX, CY), False)

    for mod in ("cv2", "numpy", "torch", "ultralytics"):
        if mod in sys.modules:
            failures.append(f"importing macvision.rule pulled in {mod}")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("centre arithmetic, inclusive edges, misses, multiple boxes, coordinate "
          "types, degenerate boxes: all as documented")
    return 0


if __name__ == "__main__":
    sys.exit(run())
