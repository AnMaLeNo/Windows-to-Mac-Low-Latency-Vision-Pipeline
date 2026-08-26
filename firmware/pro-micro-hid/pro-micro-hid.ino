// Pro Micro (ATmega32U4) side of the trigger link. Plugged into the Windows PC's USB,
// where it enumerates as a plain USB HID keyboard.
//
// SIGNAL_PIN high  -> hold TRIGGER_KEY down
// SIGNAL_PIN low   -> release it
//
// That is the whole behaviour: the key is held for exactly as long as the ESP32 asserts the
// line, so Windows' own key-repeat handles what "held" means to the target application.
//
// WIRING WARNING: the ATmega32U4 has no internal pull-down, so SIGNAL_PIN needs an external
// 10k resistor to GND. Without it the pin floats whenever the ESP32 is unpowered or
// unplugged, and a floating input on a device that types for a living will press keys on
// its own. See docs/TRIGGER.md.

#include <Keyboard.h>

static const int  SIGNAL_PIN  = 2;
static const char TRIGGER_KEY = 'k';  // change this one line; KEY_* constants also work

static bool pressed = false;

void setup() {
    pinMode(SIGNAL_PIN, INPUT);  // external 10k pull-down to GND is required, see above
    Keyboard.begin();
}

void loop() {
    bool want = (digitalRead(SIGNAL_PIN) == HIGH);
    if (want == pressed) {
        return;  // no edge: touch the USB stack only when the state actually changes
    }
    pressed = want;
    if (pressed) {
        Keyboard.press(TRIGGER_KEY);
    } else {
        Keyboard.release(TRIGGER_KEY);
    }
}
