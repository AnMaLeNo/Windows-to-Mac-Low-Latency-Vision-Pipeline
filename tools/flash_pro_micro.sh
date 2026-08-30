#!/usr/bin/env bash
# Flash firmware/pro-micro-proxy onto the Pro Micro, from the Mac, through the Pi.
#
# The Mac has no AVR toolchain and no way to reach the board; the Pi has both. So the
# sketch is copied over, built there, and uploaded from there.
#
# The board no longer exposes a USB serial port - that is -DCDC_DISABLED in
# platformio.ini, the flag that puts the keyboard on interface 0 so Windows calls it
# MI_00 - so the 1200-baud touch that normally reboots it into its bootloader has
# nowhere to land. The bootloader has to be entered by hand, and it only stays up for
# about 8 seconds.
#
# That window is the whole reason this script exists. Everything slow happens before
# the board is touched: the copy and the compile need no hardware at all. Only then
# does it start watching for the bootloader's serial port, so the upload fires the
# instant the port appears and the 8 seconds are spent on avrdude alone.
#
#     tools/flash_pro_micro.sh
#     ...wait for "short RST to GND now"...
#     short RST to GND twice, quickly
#
# Ctrl-C any time before that prompt and nothing has been touched: the board keeps
# running whatever it is running.
#
#     PI=otherhost tools/flash_pro_micro.sh     # if the Pi is not `raspberrypi`
#     WAIT_S=300 tools/flash_pro_micro.sh       # longer than the default 120s to reset

set -euo pipefail

PI=${PI:-raspberrypi}
REMOTE=${REMOTE:-build-promicro}
WAIT_S=${WAIT_S:-120}

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../firmware/pro-micro-proxy" && pwd)"

# Read the identity out of the source rather than hardcoding 1234:1234, so the check at
# the end still means something after someone edits usb_identity.h.
vid=$(sed -n 's/.*USB_ID_VENDOR *= *0x\([0-9a-fA-F]\{4\}\).*/\1/p' "$SRC/usb_identity.h")
pid=$(sed -n 's/.*USB_ID_PRODUCT *= *0x\([0-9a-fA-F]\{4\}\).*/\1/p' "$SRC/usb_identity.h")
[ -n "$vid" ] && [ -n "$pid" ] || { echo "could not read VID/PID from usb_identity.h" >&2; exit 1; }

echo "==> copying $SRC to $PI:~/$REMOTE"
ssh "$PI" "mkdir -p ~/$REMOTE"
# --exclude .pio also protects it from --delete, so the build cache survives and a
# rebuild after a one-line edit stays under two seconds.
rsync -a --delete --exclude .pio "$SRC/" "$PI:$REMOTE/"

ssh "$PI" bash -s -- "$REMOTE" "$WAIT_S" "$vid" "$pid" <<'REMOTE'
set -euo pipefail
dir=$1; wait_s=$2; vid=$3; pid=$4
pio=$HOME/.pio-venv/bin/platformio

cd "$HOME/$dir"
echo "==> building"
"$pio" run

# Whatever ACM ports exist right now are somebody else's. The bootloader is the one
# that shows up *after* this line, which is also what keeps the script from uploading
# into an unrelated device that happened to be plugged in.
before=$(ls /dev/ttyACM* 2>/dev/null || true)

new_port() {
    local p
    for p in /dev/ttyACM*; do
        [ -e "$p" ] || continue
        printf '%s\n' "$before" | grep -qxF "$p" && continue
        printf '%s\n' "$p"
        return 0
    done
    return 1
}

echo
echo "==> short RST to GND now - twice, quickly.  (Ctrl-C to abort, nothing flashed yet)"
port=""
for _ in $(seq 1 $((wait_s * 10))); do
    if port=$(new_port); then break; fi
    sleep 0.1
done
[ -n "$port" ] || { echo "no bootloader appeared within ${wait_s}s - aborted, board untouched" >&2; exit 1; }

echo "==> bootloader on $port"

# avrdude is called directly rather than through `pio run -t upload`, and that is not a
# shortcut - it is the difference between this working and not.
#
# PlatformIO's upload target re-runs the build, then does its own 1200-baud open/close
# on whatever --upload-port it was given. That dance exists to knock a *running sketch*
# into its bootloader; here the bootloader is already up, and doing it costs several
# seconds of the ~8 second window and resets the very thing being talked to. avrdude
# then reached the chip, read its device code, and hung mid-conversation when the
# window closed underneath it - twice, reproducibly. Going straight to avrdude spends
# the window on the upload itself.
#
# The flags are the ones PlatformIO derives from board = micro; -D is required, since
# without it avrdude erases the chip first and takes the bootloader with it.
avrdude=$HOME/.platformio/packages/tool-avrdude/bin/avrdude
hex=$HOME/$dir/.pio/build/micro/firmware.hex

# Still bounded: avr109 answers a closed window by spinning at 100% CPU forever rather
# than failing, and nothing else times it out. A real upload takes about 4 seconds.
if ! timeout --signal=INT --kill-after=5 60 "$avrdude" \
        -p atmega32u4 \
        -C "$HOME/.platformio/packages/tool-avrdude/avrdude.conf" \
        -c avr109 -b 57600 -D -P "$port" \
        -U "flash:w:${hex}:i"; then
    status=$?
    if [ "$status" -ge 124 ]; then
        echo "==> avrdude hung and was killed - the bootloader window closed early." >&2
    fi
    echo "    Nothing was written; the board still runs its previous sketch. Re-run and" >&2
    echo "    short RST to GND as soon as the prompt appears." >&2
    exit 1
fi

# The board reboots into the sketch on its own; give the host a moment to enumerate it
# before claiming anything about what it became.
sleep 3
echo
if lsusb | grep -i "${vid}:${pid}" ; then
    echo "==> back as ${vid}:${pid}"
else
    echo "==> WARNING: no ${vid}:${pid} on the bus. The upload reported success, so the" >&2
    echo "    sketch is on the chip; check the cable and the descriptor before assuming" >&2
    echo "    the flash failed." >&2
    exit 1
fi
REMOTE
