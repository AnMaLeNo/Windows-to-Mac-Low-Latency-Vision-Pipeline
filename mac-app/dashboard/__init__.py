"""dashboard - a web page, on the Mac, for the Mac. See docs/DASHBOARD.md.

It starts and stops macvision as a subprocess, reads the MVT1 telemetry stream back
from it, and serves one page that shows the debug image, the latency numbers and the
trigger state as they happen. It is a debug and comfort tool: the pipeline is complete
without it, and nothing here can slow the pipeline down, because all of it runs in
this process, on the far side of a socket that cannot block.

Nothing is imported here on purpose, for the same reason macvision/__init__.py is
empty: every module in this package must import with nothing installed. The only
third-party code the dashboard ever touches is opencv, reached lazily from
frames.py as an optional accelerator, and the whole thing runs on the stdlib alone.

    python3 -m dashboard            (from mac-app/)
"""

__version__ = "0.1.0"
