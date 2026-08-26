// ESP32 side of the trigger link. Plugged into the MacBook's USB.
//
// Reads one state byte per update from the Mac (0x01 = person on the ROI's centre pixel,
// 0x00 = not) and mirrors it onto TRIGGER_PIN, which runs to the Pro Micro. Active high.
//
// See docs/TRIGGER.md for wiring and the reasoning behind the numbers here.

static const int      TRIGGER_PIN  = 4;       // safe GPIO: not a strapping pin, not on the flash bus
static const int      LED_PIN      = 2;       // onboard LED on most ESP32 devkits; -1 to disable
static const uint32_t BAUD         = 115200;  // must match BAUD in mac-app/trigger.py
static const uint32_t WATCHDOG_MS  = 250;

static bool     active        = false;
static uint32_t last_byte_ms  = 0;

static void set_output(bool on) {
    active = on;
    digitalWrite(TRIGGER_PIN, on ? HIGH : LOW);
    // The LED mirrors the trigger line. It is the only way to verify this half of the link
    // with nothing else wired up - drive the serial port and watch the board.
    if (LED_PIN >= 0) {
        digitalWrite(LED_PIN, on ? HIGH : LOW);
    }
}

void setup() {
    pinMode(TRIGGER_PIN, OUTPUT);
    if (LED_PIN >= 0) {
        // Configured as an output only after boot: GPIO2 is a strapping pin and must stay
        // free while the chip samples it at reset, which it has already done by now.
        pinMode(LED_PIN, OUTPUT);
    }
    set_output(false);  // start released, before the Mac has said anything
    Serial.begin(BAUD);
    last_byte_ms = millis();
}

void loop() {
    // Drain the whole input buffer and keep only the newest byte. The Mac sends a state
    // byte per frame plus a 20ms keepalive, so if bytes ever pile up they are stale
    // history - acting on the last one is both correct and the fastest way to catch up.
    int last = -1;
    while (Serial.available() > 0) {
        last = Serial.read();
    }

    if (last >= 0) {
        last_byte_ms = millis();
        bool want = (last != 0);
        if (want != active) {
            set_output(want);
        }
        return;
    }

    // Nothing heard for a while: the Mac app crashed, quit, or got unplugged. Fail low so a
    // held key can never survive the thing that was holding it. The Mac's keepalive is 20ms,
    // so 250ms is ~12 missed sends - far outside normal jitter, and still fast enough that a
    // stuck key is never noticeable.
    if (active && (millis() - last_byte_ms) > WATCHDOG_MS) {
        set_output(false);
    }
}
