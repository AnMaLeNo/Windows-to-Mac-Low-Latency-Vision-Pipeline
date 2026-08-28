#!/usr/bin/env bash
# Set up the keyboard proxy on a Raspberry Pi. Safe to re-run: every step checks
# before it changes anything, so this is also the way to upgrade an existing install.
#
#   ./setup.sh              install dependencies + the systemd service (not started)
#   ./setup.sh --start      ... and enable + start it
#   ./setup.sh --check      report what is and is not in place, change nothing
#
# Deliberately does NOT touch /boot/firmware/config.txt. Switching the USB-C port to
# peripheral mode needs a reboot and takes the port away from whatever else uses it;
# on a Pi that is also a server that is a decision for a human. See setup-gadget.sh.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="piproxy"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULTS_PATH="/etc/default/${SERVICE_NAME}"
RUN_USER="${SUDO_USER:-$USER}"

CHECK_ONLY=0
DO_START=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=1 ;;
        --start) DO_START=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# --- 1. the machine ----------------------------------------------------------------

step "Machine"
MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)"
ok "$MODEL"
ok "kernel $(uname -r)"
if [ -f /lib/modules/"$(uname -r)"/kernel/drivers/usb/gadget/libcomposite.ko.xz ] \
   || [ -f /lib/modules/"$(uname -r)"/kernel/drivers/usb/gadget/libcomposite.ko ]; then
    ok "libcomposite present (USB gadget output is available if you want it)"
else
    warn "libcomposite not found - the 'hidg' sink will not work on this kernel"
fi

# --- 2. packages -------------------------------------------------------------------
# Bookworm marks the system Python externally-managed (PEP 668), so pip install would
# refuse. Both dependencies are packaged by Debian, which sidesteps the whole question
# and keeps the systemd service running against a Python that apt maintains.

step "Dependencies"
MISSING=()
for pkg in python3-evdev python3-serial; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        ok "$pkg"
    else
        bad "$pkg is missing"
        MISSING+=("$pkg")
    fi
done

if [ ${#MISSING[@]} -gt 0 ]; then
    if [ "$CHECK_ONLY" = 1 ]; then
        warn "would install: ${MISSING[*]}"
    else
        echo "  installing: ${MISSING[*]}"
        sudo apt-get update -qq
        sudo apt-get install -y -qq "${MISSING[@]}"
        ok "installed"
    fi
fi

# --- 3. permissions ----------------------------------------------------------------
# 'input' is what lets us open /dev/input/event* and call EVIOCGRAB without root;
# 'dialout' is for the serial sink. Running the proxy as a normal user instead of
# root is worth these two lines.

step "Permissions for $RUN_USER"
for grp in input dialout; do
    if id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx "$grp"; then
        ok "in group $grp"
    elif [ "$CHECK_ONLY" = 1 ]; then
        warn "not in group $grp (would add)"
    else
        sudo usermod -aG "$grp" "$RUN_USER"
        warn "added to $grp - log out and back in for it to take effect"
    fi
done

# --- 4. a stable name for the serial bridge ------------------------------------------
# Linux numbers tty devices in attachment order, so unplugging the bridge and plugging
# it back in renames it - ttyUSB0 becomes ttyUSB1 - and a config pinned to the old path
# points at nothing. The symlink is keyed on the USB identity instead, which does not
# change. (The sink also falls back to searching, but a config that reads correctly is
# better than one that only works because something recovers from it.)

step "Stable device name"
UDEV_LINK_RULE=/etc/udev/rules.d/99-piproxy-serial.rules
UDEV_LINK_CONTENT='# USB-serial bridges used by piproxy -> /dev/piproxy-link
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523", SYMLINK+="piproxy-link"
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="piproxy-link"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", SYMLINK+="piproxy-link"'

if [ "$CHECK_ONLY" = 1 ]; then
    if [ -f "$UDEV_LINK_RULE" ]; then ok "$UDEV_LINK_RULE exists"; else warn "would create $UDEV_LINK_RULE"; fi
elif [ -f "$UDEV_LINK_RULE" ] && [ "$(cat "$UDEV_LINK_RULE")" = "$UDEV_LINK_CONTENT" ]; then
    ok "$UDEV_LINK_RULE already up to date"
else
    echo "$UDEV_LINK_CONTENT" | sudo tee "$UDEV_LINK_RULE" >/dev/null
    sudo udevadm control --reload-rules 2>/dev/null || true
    sudo udevadm trigger --subsystem-match=tty 2>/dev/null || true
    ok "wrote $UDEV_LINK_RULE"
fi
if [ -e /dev/piproxy-link ]; then
    ok "/dev/piproxy-link -> $(readlink -f /dev/piproxy-link)"
else
    warn "/dev/piproxy-link not present (plug the ESP32 in, or replug it once)"
fi

# --- 5. configuration --------------------------------------------------------------

step "Configuration"
if [ -f "$DEFAULTS_PATH" ]; then
    ok "$DEFAULTS_PATH exists (left untouched)"
elif [ "$CHECK_ONLY" = 1 ]; then
    warn "would create $DEFAULTS_PATH"
else
    sudo tee "$DEFAULTS_PATH" >/dev/null <<'EOF'
# Options for the piproxy systemd service. Restart after editing:
#   sudo systemctl restart piproxy

# Where reports go:
#   log     print only - no hardware needed, use this to bring the system up
#   serial  UART to a Pro Micro that is the USB HID keyboard on the PC
#           add: --serial-port /dev/piproxy-link
#   hidg    this Pi is itself the USB keyboard (needs USB-C in peripheral mode,
#           see setup-gadget.sh) - add: --hid-device /dev/hidg0
PIPROXY_ARGS="--sink log"

# Key held while a person covers the ROI centre. f13-f15 exist on no real
# keyboard, so they can never collide with a key you actually press.
#PIPROXY_ARGS="--sink serial --serial-port /dev/piproxy-link --trigger-key f13"
EOF
    ok "created $DEFAULTS_PATH"
fi

# --- 6. systemd service ------------------------------------------------------------

step "systemd service"
UNIT_CONTENT="[Unit]
Description=Keyboard proxy (real keyboard + vision trigger -> one HID keyboard)
Documentation=file://${REPO_DIR}/README.md
After=network.target

[Service]
Type=simple
User=${RUN_USER}
WorkingDirectory=${REPO_DIR}
EnvironmentFile=-${DEFAULTS_PATH}
ExecStart=/usr/bin/python3 -m piproxy \$PIPROXY_ARGS
# The whole point of the watchdog is that no key survives the process holding it.
# Restarting fast closes the window where the PC has a key down and nothing is
# left to release it.
Restart=always
RestartSec=1
# Reaching /dev/input and /dev/hidg0 is the entire job; everything else is not.
PrivateTmp=yes
ProtectSystem=full
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
"

if [ "$CHECK_ONLY" = 1 ]; then
    if [ -f "$UNIT_PATH" ]; then ok "$UNIT_PATH exists"; else warn "would create $UNIT_PATH"; fi
elif [ -f "$UNIT_PATH" ] && [ "$(cat "$UNIT_PATH")" = "$UNIT_CONTENT" ]; then
    ok "$UNIT_PATH already up to date"
else
    echo "$UNIT_CONTENT" | sudo tee "$UNIT_PATH" >/dev/null
    sudo systemctl daemon-reload
    ok "wrote $UNIT_PATH"
fi

# --- 7. keyboards ------------------------------------------------------------------

step "Keyboards visible right now"
if python3 -c 'import evdev' 2>/dev/null; then
    FOUND="$(cd "$REPO_DIR" && python3 -m piproxy --list 2>/dev/null || true)"
    if [ -n "$FOUND" ]; then
        echo "$FOUND" | while read -r line; do ok "$line"; done
    else
        warn "none found - plug the Logitech receiver into a USB-A port"
    fi
else
    warn "python3-evdev not importable yet; re-run after the install above"
fi

# --- 8. done -----------------------------------------------------------------------

if [ "$CHECK_ONLY" = 1 ]; then
    step "Check complete (nothing was changed)"
    exit 0
fi

if [ "$DO_START" = 1 ]; then
    step "Starting"
    sudo systemctl enable --now "$SERVICE_NAME"
    sleep 1
    sudo systemctl --no-pager --lines=10 status "$SERVICE_NAME" || true
else
    step "Done"
    cat <<EOF
  Edit  $DEFAULTS_PATH  to choose the output sink, then:

    sudo systemctl enable --now $SERVICE_NAME
    curl -s localhost:48011/status | python3 -m json.tool

  Or run it in the foreground to watch it work:

    cd $REPO_DIR && python3 -m piproxy --sink log
EOF
fi
