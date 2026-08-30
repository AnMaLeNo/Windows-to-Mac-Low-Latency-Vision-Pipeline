// Pro Micro (ATmega32U4) as the Windows PC's only keyboard.
//
// Plugged into the PC's USB, where it enumerates as a plain HID keyboard - under a
// USB identity of our own choosing rather than the Arduino one, see usb_identity.h.
// It types nothing of its own: it receives complete 8-byte HID reports from the Raspberry Pi
// over a UART and forwards them to the host verbatim. The Pi has already merged the
// real keyboard with the vision trigger, so from the PC's side there is one keyboard
// and no way to tell the two sources apart.
//
//   Pi TX ──> D0/RX (Serial1)      the report stream
//   Pi GND ──> GND                 required, two boards on two different supplies
//
// *** NEVER wire this board's TX back to the Pi. *** Pi GPIO is 3.3V and NOT 5V
// tolerant; this board's 5V TX would damage the SoC. The link only runs one way.
//
// This supersedes pro-micro-hid.ino, which read a single GPIO and pressed one fixed
// key. That sketch is still correct for the ESP32 GPIO link; this one is for the
// full-keyboard proxy. See pi-agent/README.md.

#include "usb_identity.h"  // what Windows sees: VID/PID/REV, MI_00 and the two interfaces

static const uint32_t BAUD = 115200;  // must match esp32-proxy and --baud in piproxy

// 115200 because the slowest link in the chain sets the rate, and that is the CH340
// between the Pi and the ESP32 - clones of it get unreliable well before this AVR
// does. A 10-byte frame costs 868us at this rate, on a budget of a few milliseconds,
// so the ceiling is not worth chasing.
//
// If you ever do raise it: this chip is *exact* at 1000000 (the U2X divisor is 1) and
// 8.5% off at 921600, which is outside what a UART tolerates. The faster-looking rate
// is the broken one. All three ends must change together.

static const uint8_t  START_BYTE  = 0xAB;
static const uint32_t WATCHDOG_MS = 250;

// One frame carries exactly one keyboard report, so there is one length, and it is
// the USB side that defines it - a boot keyboard report is 8 bytes by definition.
static const uint8_t REPORT_LEN = KEYBOARD_REPORT_LEN;

static uint8_t  current[REPORT_LEN];   // what the host currently believes
static uint8_t  rx[REPORT_LEN];        // frame being assembled
static uint8_t  rx_len  = 0;
static bool     in_frame = false;
static uint32_t last_frame_ms = 0;
static bool     idle = true;           // true when everything is released

static void emit(const uint8_t *report) {
    memcpy(current, report, REPORT_LEN);
    usb_keyboard_send(current);
    idle = true;
    for (uint8_t i = 0; i < REPORT_LEN; i++) {
        if (current[i] != 0) { idle = false; break; }
    }
}

static void release_all() {
    uint8_t zero[REPORT_LEN] = {0};
    emit(zero);
}

// Runs before USBDevice.attach(), which is the last moment at which anything can be
// added to the USB device - after that the host is free to start asking who we are.
// setup() runs after attach and is too late here, because the order in which the two
// interfaces are plugged is what decides their numbers, and the keyboard has to end
// up as interface 0. See usb_identity.h.
void initVariant() {
    usb_identity_begin();  // keyboard -> interface 0 / MI_00, consumer -> 1 / MI_01
}

void setup() {
    Serial1.begin(BAUD);
    release_all();          // start with nothing held, before the Pi has said anything
    last_frame_ms = millis();
}

void loop() {
    while (Serial1.available() > 0) {
        uint8_t b = Serial1.read();

        if (!in_frame) {
            // Resynchronisation point. After a dropped byte the stream is misaligned,
            // and the only way back is to wait for something that looks like a header.
            if (b == START_BYTE) { in_frame = true; rx_len = 0; }
            continue;
        }

        if (rx_len < REPORT_LEN) {
            rx[rx_len++] = b;
            continue;
        }

        // Final byte of the frame is the XOR checksum. A corrupted frame is dropped
        // rather than guessed at: the Pi resends the current state every 20ms, so the
        // cost of dropping one is bounded, while acting on a bad report could stick a
        // key down that nobody asked for.
        uint8_t checksum = 0;
        for (uint8_t i = 0; i < REPORT_LEN; i++) checksum ^= rx[i];
        in_frame = false;
        if (checksum != b) continue;

        last_frame_ms = millis();
        // Only touch the USB stack when something actually changed. At a 20ms
        // keepalive most frames are identical to the last one, and an unchanged
        // report costs the host an interrupt transfer for no information.
        if (memcmp(rx, current, REPORT_LEN) != 0) emit(rx);
    }

    // Nothing heard for a while: the Pi crashed, was unplugged, or the cable came
    // loose. Fail released, so no key can survive the thing that was holding it.
    // At a 20ms keepalive, 250ms is ~12 missed frames - far outside normal jitter.
    if (!idle && (millis() - last_frame_ms) > WATCHDOG_MS) {
        release_all();
    }
}
