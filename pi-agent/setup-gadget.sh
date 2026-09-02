#!/usr/bin/env bash
# Turn this Pi's USB-C port into a USB HID keyboard (/dev/hidg0), for `--sink hidg`.
#
# This is the ONLY script here that edits /boot/firmware/config.txt and needs a
# reboot, which is why it is separate from setup.sh. Read the warnings below before
# running it on a Pi that does anything else.
#
#   ./setup-gadget.sh --check     say what would change, change nothing
#   ./setup-gadget.sh             enable it (prompts before editing config.txt)
#   ./setup-gadget.sh --revert    put dr_mode back to host
#
# WHAT YOU ARE GIVING UP, on a Pi 5:
#
#   * The USB-C port stops being a host port and stops being a normal power input
#     path for your setup: it becomes the data link to the PC. The Pi 5 has exactly
#     one dual-role controller (dwc2) and it is that port. All four USB-A ports are
#     behind xhci-hcd host controllers, which have no device mode at all.
#   * You must therefore power the Pi another way - GPIO pins 2/4 (5V) and 6 (GND)
#     from a supply that can actually feed a Pi 5. A PC USB port cannot.
#   * Two supplies with no isolation will backfeed each other. The usual fix is a
#     USB-C -> USB-A adapter at the PC end, which drops the CC lines so PD never
#     negotiates.
#   * If the PC is off, the Pi is off.
#
# If this Pi runs anything you care about, use `--sink serial` instead: it needs
# none of the above. See README.md.

set -euo pipefail

CONFIG=/boot/firmware/config.txt
[ -f "$CONFIG" ] || CONFIG=/boot/config.txt
GADGET_NAME=piproxy
GADGET_DIR="/sys/kernel/config/usb_gadget/${GADGET_NAME}"
UDEV_RULE=/etc/udev/rules.d/99-piproxy-hidg.rules
INIT_SCRIPT=/usr/local/sbin/piproxy-gadget-up

MODE=enable
for arg in "$@"; do
    case "$arg" in
        --check)  MODE=check ;;
        --revert) MODE=revert ;;
        -h|--help) sed -n '2,30p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

step "Current state"
ok "config: $CONFIG"
CURRENT="$(grep -E '^\s*dtoverlay=dwc2' "$CONFIG" 2>/dev/null || echo '(no dwc2 overlay)')"
ok "dwc2 line: $CURRENT"
if [ -n "$(ls -A /sys/class/udc 2>/dev/null)" ]; then
    ok "UDC available: $(ls /sys/class/udc) - peripheral mode is ACTIVE"
else
    warn "no UDC - peripheral mode is not active (a reboot is needed after enabling)"
fi

if [ "$MODE" = check ]; then
    step "Check only; nothing changed."
    exit 0
fi

# --- config.txt --------------------------------------------------------------------

if [ "$MODE" = revert ]; then
    step "Reverting to host mode"
    sudo sed -i 's/^\(\s*dtoverlay=dwc2\).*/\1,dr_mode=host/' "$CONFIG"
    sudo rm -f "$INIT_SCRIPT" "$UDEV_RULE"
    ok "config.txt set back to dr_mode=host; reboot to apply"
    exit 0
fi

step "Enabling peripheral mode"
cat <<EOF

  This will set  dtoverlay=dwc2,dr_mode=peripheral  in $CONFIG
  and REQUIRES A REBOOT.

  On this machine that means: the USB-C port stops behaving as it does now, and
  the Pi will need power via the GPIO header. Anything else running on this Pi
  goes down for the reboot. Containers whose restart policy is 'no' will NOT
  come back on their own - check with:
      docker ps --format '{{.Names}}' | xargs -r docker inspect \\
        --format '{{.Name}} {{.HostConfig.RestartPolicy.Name}}'

EOF
read -r -p "  Type 'yes' to continue: " CONFIRM
[ "$CONFIRM" = yes ] || { echo "  aborted."; exit 1; }

sudo cp "$CONFIG" "${CONFIG}.piproxy.bak"
ok "backed up to ${CONFIG}.piproxy.bak"
if grep -qE '^\s*dtoverlay=dwc2' "$CONFIG"; then
    sudo sed -i 's/^\(\s*dtoverlay=dwc2\).*/\1,dr_mode=peripheral/' "$CONFIG"
else
    echo 'dtoverlay=dwc2,dr_mode=peripheral' | sudo tee -a "$CONFIG" >/dev/null
fi
ok "set dtoverlay=dwc2,dr_mode=peripheral"

# --- the gadget itself -------------------------------------------------------------
# configfs gadgets do not survive a reboot, so this has to be recreated at every boot.

step "Installing the boot-time gadget builder"
sudo tee "$INIT_SCRIPT" >/dev/null <<'GADGET'
#!/usr/bin/env bash
# Builds the USB HID keyboard gadget. configfs gadgets are not persistent, so this
# runs on every boot (see the systemd unit that calls it).
set -euo pipefail
G=/sys/kernel/config/usb_gadget/piproxy
modprobe libcomposite
[ -d "$G" ] && exit 0

mkdir -p "$G"
# A generic composite-device VID/PID from Linux's own gadget examples. Windows needs
# no driver for a boot keyboard, so the identity only affects how it is listed.
echo 0x1d6b > "$G/idVendor"
echo 0x0104 > "$G/idProduct"
echo 0x0100 > "$G/bcdDevice"
echo 0x0200 > "$G/bcdUSB"

mkdir -p "$G/strings/0x409"
echo "piproxy"           > "$G/strings/0x409/manufacturer"
echo "Keyboard Proxy"    > "$G/strings/0x409/product"
echo "0001"              > "$G/strings/0x409/serialnumber"

mkdir -p "$G/functions/hid.usb0"
echo 1 > "$G/functions/hid.usb0/protocol"      # 1 = keyboard
echo 1 > "$G/functions/hid.usb0/subclass"      # 1 = boot interface
echo 8 > "$G/functions/hid.usb0/report_length"
# The standard 8-byte boot keyboard report descriptor: 8 modifier bits, one padding
# byte, 5 LED bits + 3 padding, then 6 key-usage bytes.
printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' \
    > "$G/functions/hid.usb0/report_desc"

mkdir -p "$G/configs/c.1/strings/0x409"
echo "HID keyboard" > "$G/configs/c.1/strings/0x409/configuration"
echo 250            > "$G/configs/c.1/MaxPower"
ln -s "$G/functions/hid.usb0" "$G/configs/c.1/"

# Binding to the UDC is what makes the PC see a new device appear.
ls /sys/class/udc > "$G/UDC"
GADGET
sudo chmod +x "$INIT_SCRIPT"
ok "wrote $INIT_SCRIPT"

sudo tee /etc/systemd/system/piproxy-gadget.service >/dev/null <<EOF
[Unit]
Description=Create the piproxy USB HID gadget
After=sys-kernel-config.mount
Before=piproxy.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=${INIT_SCRIPT}

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable piproxy-gadget.service >/dev/null
ok "enabled piproxy-gadget.service"

# Without this the node is root-only and piproxy would have to run as root.
sudo tee "$UDEV_RULE" >/dev/null <<'EOF'
KERNEL=="hidg[0-9]*", MODE="0660", GROUP="input"
EOF
ok "wrote $UDEV_RULE (hidg nodes readable by group 'input')"

step "Reboot required"
cat <<EOF
  sudo reboot

  After it comes back:
    ls -l /dev/hidg0
    sudo sed -i 's|^PIPROXY_ARGS=.*|PIPROXY_ARGS="--sink hidg"|' /etc/default/piproxy
    sudo systemctl restart piproxy

  To undo:  ./setup-gadget.sh --revert && sudo reboot
EOF
