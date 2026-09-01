"""The trigger rule: is a person on the ROI's centre pixel?

This is the whole product in fifteen lines. docs/TRIGGER.md calls center_is_covered()
"the rule", so the name is a documented contract.

It takes plain (x1, y1, x2, y2) coordinates rather than an Ultralytics Results object,
and that single change is the point of this module: the rule that decides whether a key
is pressed can now be imported, read and tested on any machine, with nothing installed.
Before this it could not be imported at all without torch, so its edge behaviour had
never once been checked.

If the zero-dependency constraint is ever dropped, fold this file into detector.py
rather than keeping it for symmetry - it earns a module of its own only because it must
import nothing.
"""


def roi_center(width, height):
    """(cx, cy) of an ROI. Integer floor division: 300x300 -> (150, 150)."""
    return width // 2, height // 2


def center_is_covered(boxes, cx, cy):
    """Is (cx, cy) inside any of `boxes`?

    `boxes` is any iterable of (x1, y1, x2, y2) in the frame's own pixels - a list of
    lists from Tensor.tolist(), tuples, ints or floats, all the same.

    Three preconditions that were invisible while this lived inside the receive loop,
    written down because docs/TRIGGER.md describes all three as part of "the rule":

      - This is a PERSON test only because detector.py passes classes=[0] into predict(),
        so non-person detections are discarded inside NMS and never exist. Hand this
        function boxes from an unfiltered model and it fires on a chair. Class filtering
        is upstream's job and must stay there.
      - All four comparisons are inclusive: a box whose edge lands exactly on the pixel
        counts as covering it.
      - There is no confidence floor beyond Ultralytics' 0.25 default and no minimum box
        area, so a marginal ghost box holds the key down.

    A plain loop, not a vectorised numpy expression, and it must stay one. Measured on a
    Raspberry Pi 5 (numpy 1.24.2), microseconds per call at 0/1/3/10/70 boxes:

        np.any((b[:,0]<=cx)&(cx<=b[:,2])&(b[:,1]<=cy)&(cy<=b[:,3]))
                       15.8   16.7   19.2   17.1   17.4
        this loop       0.10   0.17   0.34   1.14   1.86

    Four comparison ops, three ands and an .any() each carry microseconds of per-call
    overhead no matter how small the array is. With classes=[0] on a 300x300 ROI the
    count is single digits, so there is no crossover to worry about and no reason for a
    second implementation. The Mac is faster than the Pi; the shape of the result is not.

    One honest caveat about that table: it compares the two predicates over data each
    already holds, but the refactor also changed how the boxes get here - the old code
    read boxes.xyxy.cpu().numpy(), a zero-copy view, and detector.boxes_xyxy() now calls
    .tolist(), which allocates Python floats. That allocation is not in the numbers
    above. It is paid once per frame rather than once per box comparison, and at single
    digit box counts it is well under a microsecond, but the two changes were made
    together and only one of them was measured.
    """
    for x1, y1, x2, y2 in boxes:
        if x1 <= cx <= x2 and y1 <= cy <= y2:
            return True
    return False
