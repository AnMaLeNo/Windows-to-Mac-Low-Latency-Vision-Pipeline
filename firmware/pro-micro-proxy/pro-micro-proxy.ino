// Pro Micro (ATmega32U4) as the Windows PC's only keyboard.
//
// Plugged into the PC's USB, where it enumerates as a plain HID keyboard. It types
// nothing of its own: it receives complete 8-byte HID reports from the Raspberry Pi
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

#include <HID.h>

static const uint32_t BAUD = 1000000;  // must match --baud in piproxy

// Why 1000000 and not 921600, which looks faster: on a 16MHz ATmega32U4 the U2X
// divisor for 1Mbaud is exactly 1, while 921600 rounds to that same divisor and ends
// up 8.5% off - far outside the ~2% a UART tolerates. The "slower" rate is the only
// one of the two that actually works here.

static const uint8_t  START_BYTE  = 0xAB;
static const uint8_t  REPORT_LEN  = 8;
static const uint8_t  REPORT_ID   = 2;
static const uint32_t WATCHDOG_MS = 250;

// Standard boot-keyboard report layout, wrapped in a report ID so it can ride on the
// core's bundled HID.h - which means no lib_deps, unlike Keyboard.h under PlatformIO.
// Note the consequence: with a report ID this is not literally the USB boot protocol,
// so it will not work in a BIOS/UEFI setup screen. Windows uses report protocol and
// does not care.
static const uint8_t REPORT_DESCRIPTOR[] PROGMEM = {
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x06,        //   Usage (Keyboard)
    0xa1, 0x01,        //   Collection (Application)
    0x85, REPORT_ID,   //     Report ID
    0x05, 0x07,        //     Usage Page (Keyboard/Keypad)
    0x19, 0xe0,        //     Usage Minimum (LeftControl)
    0x29, 0xe7,        //     Usage Maximum (Right GUI)
    0x15, 0x00, 0x25, 0x01,
    0x75, 0x01, 0x95, 0x08,
    0x81, 0x02,        //     Input (Data,Var,Abs) - the 8 modifier bits
    0x95, 0x01, 0x75, 0x08,
    0x81, 0x03,        //     Input (Cnst) - the reserved byte
    0x95, 0x06, 0x75, 0x08,
    0x15, 0x00, 0x25, 0x65,
    0x05, 0x07, 0x19, 0x00, 0x29, 0x65,
    0x81, 0x00,        //     Input (Data,Ary) - the six key slots
    0xc0               //   End Collection
};

static uint8_t  current[REPORT_LEN];   // what the host currently believes
static uint8_t  rx[REPORT_LEN];        // frame being assembled
static uint8_t  rx_len  = 0;
static bool     in_frame = false;
static uint32_t last_frame_ms = 0;
static bool     idle = true;           // true when everything is released

static void emit(const uint8_t *report) {
    memcpy(current, report, REPORT_LEN);
    HID().SendReport(REPORT_ID, current, REPORT_LEN);
    idle = true;
    for (uint8_t i = 0; i < REPORT_LEN; i++) {
        if (current[i] != 0) { idle = false; break; }
    }
}

static void release_all() {
    uint8_t zero[REPORT_LEN] = {0};
    emit(zero);
}

void setup() {
    static HIDSubDescriptor node(REPORT_DESCRIPTOR, sizeof(REPORT_DESCRIPTOR));
    HID().AppendDescriptor(&node);
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
