"""piproxy - keyboard proxy running on the Raspberry Pi.

The real keyboard is plugged into the Pi (via its Logitech receiver, on a USB-A
host port). The Pi forwards every keystroke to the Windows PC and, on a signal from
the Mac's vision pipeline, injects one extra key into the same stream. The PC
therefore sees exactly one keyboard.

See pi-agent/README.md for wiring and setup.
"""

__version__ = "0.1.0"
