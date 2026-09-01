"""Compatibility shim. The receiver is now the macvision package.

    python3 -m macvision          (from mac-app/)

This file stays because its name is in the READMEs, in docs/TRIGGER.md, and - the one
that actually matters - in the runtime banner that commit 3e8a1a4 exists to print. It
also keeps the working directory at mac-app/, which is what makes .gitignore's
mac-app/*.pt keep covering the weights ultralytics downloads on first run.

See mac-app/README.md.
"""

import os
import sys

# CPython already does this for a script run by path; the explicit line makes the shim
# work if it is ever invoked another way.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from macvision.__main__ import main  # noqa: E402

if __name__ == "__main__":
    print("[macvision] note: receiver.py is now a shim; run `python3 -m macvision` "
          "from mac-app/.", file=sys.stderr)
    sys.exit(main())
