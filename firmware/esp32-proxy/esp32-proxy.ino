// ESP32 as the USB-serial bridge between the Raspberry Pi and the Pro Micro.
//
// The Pi cannot reach the Pro Micro directly: the Pro Micro is a USB *device* on the
// Windows PC, and the Pi's USB-A ports are hosts. So the Pi speaks USB serial to this
// board, and this board re-emits the same bytes on a UART wired to the Pro Micro.
//
//   Pi ──USB (CH340)──> ESP32 ──GPIO4 (UART TX)──> Pro Micro D0/RX
//                                GND ───────────>  Pro Micro GND
//
// TX is remapped onto GPIO 4 on purpose: that is the pin the old esp32-link.ino drove
// as a plain output, so an existing GPIO4 wire does not have to move. The Pro Micro
// end does: it must land on D0/RX, not D2, because D2 is not a UART pin on the
// ATmega32U4 and no firmware can make it one.
//
// This supersedes esp32-link.ino, which mirrored one byte onto a GPIO. That was
// enough for a 1-bit trigger and cannot carry an 8-byte HID report.

static const uint32_t USB_BAUD  = 115200;  // to the Pi. Must match --baud in piproxy
static const uint32_t LINK_BAUD = 115200;  // to the Pro Micro. Must match its BAUD
static const int      TX_PIN    = 4;       // to Pro Micro D0/RX
static const int      RX_PIN    = -1;      // unused: the link is one-way by design
static const int      LED_PIN   = 2;       // onboard LED on most devkits; -1 to disable

// 115200 costs 868us for a 10-byte frame, on a budget of a few milliseconds - and it
// is the rate this project already trusts on a CH340. Faster is possible (the AVR at
// the far end is exact at 1000000, and badly off at 921600), but CH340 clones get
// unreliable at high rates, and 868us is not where the latency is. Raise all three
// ends together, or not at all.

static const uint8_t START_BYTE = 0xAB;
static const uint8_t FRAME_LEN  = 10;   // start + 8 report bytes + checksum

// Statistics, parsed alongside the pass-through rather than gating it - see loop().
static uint32_t frames_ok = 0, frames_bad = 0, bytes_seen = 0;
static uint8_t  frame[FRAME_LEN];
static uint8_t  frame_len = 0;
static bool     in_frame = false;
static uint32_t last_activity_ms = 0;

static void report_stats() {
    Serial.printf("[esp32-proxy] bytes=%lu frames_ok=%lu frames_bad=%lu "
                  "idle_ms=%lu tx_pin=%d baud=%lu\n",
                  bytes_seen, frames_ok, frames_bad,
                  millis() - last_activity_ms, TX_PIN, LINK_BAUD);
}

void setup() {
    Serial.begin(USB_BAUD);
    Serial1.begin(LINK_BAUD, SERIAL_8N1, RX_PIN, TX_PIN);
    if (LED_PIN >= 0) {
        // Configured as an output only after boot: GPIO2 is a strapping pin and must
        // stay free while the chip samples it at reset, which it has already done.
        pinMode(LED_PIN, OUTPUT);
        digitalWrite(LED_PIN, LOW);
    }
    last_activity_ms = millis();
}

void loop() {
    while (Serial.available() > 0) {
        uint8_t b = Serial.read();

        if (!in_frame && b != START_BYTE) {
            // Outside a frame, anything that is not a header is a command. Framing is
            // what makes this unambiguous: a payload byte can equal '?' without being
            // mistaken for one, because inside a frame we never look.
            if (b == '?') report_stats();
            continue;
        }

        // Forward immediately, byte by byte, before parsing. Buffering a whole frame
        // to validate it first would double this hop's latency for no benefit: the
        // Pro Micro verifies the checksum itself and drops what it cannot trust.
        Serial1.write(b);
        bytes_seen++;
        last_activity_ms = millis();

        if (!in_frame) {
            in_frame = true;
            frame_len = 0;
            continue;
        }
        frame[frame_len++] = b;
        if (frame_len < FRAME_LEN - 1) continue;

        uint8_t checksum = 0;
        for (uint8_t i = 0; i < FRAME_LEN - 2; i++) checksum ^= frame[i];
        if (checksum == frame[FRAME_LEN - 2]) frames_ok++; else frames_bad++;
        in_frame = false;
    }

    // The LED tracks whether the stream is alive, which is the only way to tell this
    // half of the link apart from a dead cable without instrumenting anything.
    if (LED_PIN >= 0) {
        digitalWrite(LED_PIN, (millis() - last_activity_ms) < 250 ? HIGH : LOW);
    }
}
