// The USB identity this board presents to the Windows host: everything the host can
// see about who we are, in one place. The sketch next door owns the UART framing and
// nothing else.
//
// Windows builds the hardware IDs it shows in Device Manager out of three fields of
// the USB *device* descriptor, plus the interface layout and the report descriptor:
//
//   HID\VID_0461&PID_0010&REV_0104&MI_00   idVendor, idProduct, bcdDevice, interface no.
//   HID\VID_0461&PID_0010&MI_00
//   HID\VID_0461&UP:0001_U:0006            top-level usage page/usage of the report
//   HID_DEVICE_SYSTEM_KEYBOARD             ...which is Generic Desktop / Keyboard
//   HID_DEVICE_UP:0001_U:0006
//   HID_DEVICE
//
// None of it is reachable through the normal Arduino API:
//
//   - VID and PID come from -DUSB_VID/-DUSB_PID, which the board definition owns, and
//     bcdDevice is the literal 0x100 written into USBCore.cpp. That descriptor is
//     declared PROGMEM, so it cannot be patched at run time either.
//   - MI_nn appears only when the host sees a *composite* device, and the number is
//     the interface index. A stock Micro spends interfaces 0 and 1 on CDC (the USB
//     serial port) and gives the keyboard 2, so it enumerates as MI_02.
//   - the interface's subclass and protocol bytes are hardcoded to NONE/NONE inside
//     HID_::getInterface(), in the core's bundled HID library.
//
// So the two interfaces are built here by hand instead, and the device descriptor is
// served through the one opening the core leaves: SendDescriptor() in USBCore.cpp asks
// every plugged PluggableUSB module first, and only falls back to its own descriptors
// if none of them answers.
//
// The keyboard still has to be interface 0 for Windows to call it MI_00, and that
// takes -DCDC_DISABLED in platformio.ini to get the CDC interfaces out of the way.
// Read the warning there: it also removes the auto-reset that uploads rely on.

#pragma once

#include <HID.h>  // for HIDDescriptor, D_HIDREPORT and the HID_* constants only:
                  // the HID_ singleton is never called, so it is never plugged, and
                  // the interfaces below are the only ones the host is offered.

// The three numbers Windows renders as VID_ / PID_ / REV_. REV_ is worth changing
// whenever the rest of the descriptor changes: Windows caches descriptors under
// HKLM\SYSTEM\CurrentControlSet\Control\usbflags\<VID><PID><REV>, and a new revision
// is a new cache key rather than a stale entry to fight.
static const uint16_t USB_ID_VENDOR   = 0x0461;
static const uint16_t USB_ID_PRODUCT  = 0x0010;
static const uint16_t USB_ID_REVISION = 0x0104;

// One boot-protocol keyboard report: 1 modifier byte, 1 reserved byte, 6 key slots.
static const uint8_t KEYBOARD_REPORT_LEN = 8;

// Device class 0x00 is what makes this a composite device: it tells the host the real
// class lives in the interface descriptors, and Windows answers by loading usbccgp,
// the generic parent driver that creates one MI_nn child per interface. (The core's
// own descriptor uses 0xEF/0x02/0x01 instead, the flavour that exists to hold the
// CDC's interface association descriptor together.)
//
// iManufacturer and iSerialNumber stay 0 - absent. The core would otherwise answer
// those with "Arduino LLC" and a serial number built from the plugged modules, which
// is not what a board claiming this VID should be saying. Only the product name is
// offered, and it is served below rather than by the core.
static const uint8_t PRODUCT_STRING_INDEX = 2;

static const DeviceDescriptor DEVICE_DESCRIPTOR PROGMEM =
    D_DEVICE(0x00, 0x00, 0x00,   // class / subclass / protocol: see the interfaces
             64,                 // endpoint 0 max packet size
             USB_ID_VENDOR, USB_ID_PRODUCT, USB_ID_REVISION,
             0, PRODUCT_STRING_INDEX, 0,  // iManufacturer, iProduct, iSerialNumber
             1);                 // one configuration

// The name the device reports for itself. USB strings are UTF-16LE, and the two-byte
// header in front of them carries the total length - which is why the characters live
// in their own array: the length is then computed from it, and editing the name cannot
// leave a stale byte count behind.
static const uint16_t PRODUCT_STRING[] PROGMEM = {
    'U', 'S', 'B', ' ', 'K', 'e', 'y', 'b', 'o', 'a', 'r', 'd'
};

// USBCore.cpp has a helper for this, but it is not declared in any header a sketch can
// reach, so the descriptor is assembled here. Two USB_SendControl calls append to the
// same control transfer; the header is in RAM and the characters in flash, which is
// the only reason it takes two.
static int send_product_string() {
    const uint8_t header[2] = { (uint8_t)(2 + sizeof(PRODUCT_STRING)),
                                USB_STRING_DESCRIPTOR_TYPE };
    int head = USB_SendControl(0, header, sizeof(header));
    if (head < 0) return -1;
    int body = USB_SendControl(TRANSFER_PGM, PRODUCT_STRING, sizeof(PRODUCT_STRING));
    if (body < 0) return -1;
    return head + body;
}

// Interface 0. The literal USB boot keyboard report: no report ID, so the bytes on the
// wire are the same in boot protocol and in report protocol. That is what lets the
// interface honestly declare SubClass 1 / Protocol 1 below - a host that switches us
// to boot protocol gets exactly what it expects, which is also why this works in a
// BIOS/UEFI setup screen where the old report-ID version did not.
static const uint8_t KEYBOARD_DESCRIPTOR[] PROGMEM = {
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x06,        //   Usage (Keyboard)
    0xa1, 0x01,        //   Collection (Application)
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

// Interface 1. Nothing is ever sent on it - it exists so the device has two interfaces
// and therefore an MI_nn - but it carries the two collections a real keyboard puts
// there, so Windows binds it to the HID class driver and splits it into two children
// rather than parking it under "Other devices" with a yellow warning triangle:
//
//   HID\VID_0461&PID_0010&REV_0104&MI_01&Col01   UP:0001_U:0080  system control
//   HID\VID_0461&PID_0010&REV_0104&MI_01&Col02   UP:000C_U:0001  consumer control
//
// Two top-level collections on one interface is exactly what report IDs are for: they
// are what tells the host which collection a report belongs to, so unlike the keyboard
// on interface 0 - where the report ID had to go, to keep the boot protocol honest -
// here they are required.
static const uint8_t SYSTEM_CONSUMER_DESCRIPTOR[] PROGMEM = {
    0x05, 0x01,        // Usage Page (Generic Desktop)
    0x09, 0x80,        //   Usage (System Control)
    0xa1, 0x01,        //   Collection (Application)
    0x85, 0x01,        //     Report ID (1)
    0x19, 0x81,        //     Usage Minimum (System Power Down)
    0x29, 0x83,        //     Usage Maximum (System Wake Up)
    0x15, 0x00,        //     Logical Minimum (0)
    0x25, 0x01,        //     Logical Maximum (1)
    0x75, 0x01,        //     Report Size (1)
    0x95, 0x03,        //     Report Count (3)
    0x81, 0x02,        //     Input (Data,Var,Abs) - power, sleep, wake
    0x95, 0x05,        //     Report Count (5)
    0x81, 0x01,        //     Input (Cnst) - pad the byte out
    0xc0,              //   End Collection

    0x05, 0x0c,        // Usage Page (Consumer)
    0x09, 0x01,        //   Usage (Consumer Control)
    0xa1, 0x01,        //   Collection (Application)
    0x85, 0x02,        //     Report ID (2)
    0x19, 0x00,        //     Usage Minimum (0)
    0x2a, 0xff, 0x03,  //     Usage Maximum (1023)
    0x15, 0x00,        //     Logical Minimum (0)
    0x26, 0xff, 0x03,  //     Logical Maximum (1023)
    0x75, 0x10,        //     Report Size (16)
    0x95, 0x01,        //     Report Count (1)
    0x81, 0x00,        //     Input (Data,Ary,Abs)
    0xc0               //   End Collection
};

// Shared by both interfaces: the same class requests, answered the same way, differing
// only in which report descriptor they hand back.
class UsbHidInterface : public PluggableUSBModule {
public:
    UsbHidInterface(const uint8_t *report, uint16_t reportLen,
                    uint8_t subClass, uint8_t hidProtocol)
        : PluggableUSBModule(1, 1, epType),
          report(report), reportLen(reportLen),
          subClass(subClass), hidProtocol(hidProtocol),
          protocol(HID_REPORT_PROTOCOL), idle(0) {
        epType[0] = EP_TYPE_INTERRUPT_IN;
        PluggableUSB().plug(this);
    }

protected:
    int getInterface(uint8_t *interfaceCount) {
        *interfaceCount += 1;
        HIDDescriptor iface = {
            D_INTERFACE(pluggedInterface, 1, USB_DEVICE_CLASS_HUMAN_INTERFACE,
                        subClass, hidProtocol),
            D_HIDREPORT(reportLen),
            D_ENDPOINT(USB_ENDPOINT_IN(pluggedEndpoint), USB_ENDPOINT_TYPE_INTERRUPT,
                       USB_EP_SIZE, 0x01)
        };
        return USB_SendControl(0, &iface, sizeof(iface));
    }

    int getDescriptor(USBSetup &setup) {
        if (setup.bmRequestType == REQUEST_DEVICETOHOST_STANDARD_INTERFACE
            && setup.wValueH == HID_REPORT_DESCRIPTOR_TYPE
            && setup.wIndex == pluggedInterface) {
            // A re-read of the report descriptor means the host is enumerating us
            // afresh, and the spec says it may then assume nothing about the protocol.
            // Windows and Linux assume report protocol regardless, so match them.
            protocol = HID_REPORT_PROTOCOL;
            return USB_SendControl(TRANSFER_PGM, report, reportLen);
        }
        return 0;  // not ours: let the next module, then the core, answer
    }

    bool setup(USBSetup &setup) {
        if (setup.wIndex != pluggedInterface) {
            return false;  // the other interface's requests are not ours to answer
        }
        // Windows sends SET_IDLE and SET_PROTOCOL to every HID interface during
        // enumeration and wants an acknowledgement, not a stall.
        if (setup.bmRequestType == REQUEST_HOSTTODEVICE_CLASS_INTERFACE) {
            if (setup.bRequest == HID_SET_PROTOCOL) { protocol = setup.wValueL; return true; }
            if (setup.bRequest == HID_SET_IDLE)     { idle     = setup.wValueL; return true; }
            return false;
        }
        // The idle rate is stored and answered but not acted on: nothing here resends
        // an unchanged report on a timer. Same as the core's HID_, and the hosts that
        // matter set the rate to 0 (never resend) for keyboards anyway.
        if (setup.bmRequestType == REQUEST_DEVICETOHOST_CLASS_INTERFACE) {
            if (setup.bRequest == HID_GET_PROTOCOL) return USB_SendControl(0, &protocol, 1) >= 0;
            if (setup.bRequest == HID_GET_IDLE)     return USB_SendControl(0, &idle, 1) >= 0;
            return false;
        }
        return false;
    }

    uint8_t epType[1];
    const uint8_t  *report;
    const uint16_t  reportLen;
    const uint8_t   subClass;
    const uint8_t   hidProtocol;
    uint8_t         protocol;
    uint8_t         idle;
};

// Interface 0. SubClass 1 / Protocol 1 is "boot interface, keyboard" - the pair a real
// keyboard declares, and the reason KEYBOARD_DESCRIPTOR carries no report ID.
//
// It also answers the device descriptor request, which is not an interface matter at
// all: it lands here only because this is the first module plugged, so it is the first
// one PluggableUSB asks.
class UsbKeyboard : public UsbHidInterface {
public:
    UsbKeyboard()
        : UsbHidInterface(KEYBOARD_DESCRIPTOR, sizeof(KEYBOARD_DESCRIPTOR),
                          HID_SUBCLASS_BOOT_INTERFACE, HID_PROTOCOL_KEYBOARD) { }

    int send(const uint8_t *report) {
        return USB_Send(pluggedEndpoint | TRANSFER_RELEASE, report, KEYBOARD_REPORT_LEN);
    }

protected:
    int getDescriptor(USBSetup &setup) {
        if (setup.bmRequestType == (REQUEST_DEVICETOHOST | REQUEST_STANDARD | REQUEST_DEVICE)) {
            if (setup.wValueH == USB_DEVICE_DESCRIPTOR_TYPE) {
                return USB_SendControl(TRANSFER_PGM, &DEVICE_DESCRIPTOR,
                                       sizeof(DEVICE_DESCRIPTOR));
            }
            // Index 0 is the list of supported languages, which the core answers with
            // English. Anything else was never announced, so it is left to stall.
            if (setup.wValueH == USB_STRING_DESCRIPTOR_TYPE
                && setup.wValueL == PRODUCT_STRING_INDEX) {
                return send_product_string();
            }
        }
        return UsbHidInterface::getDescriptor(setup);
    }
};

static UsbKeyboard *usb_keyboard = NULL;

// Call this from initVariant(), before USBDevice.attach(): PluggableUSB hands out
// interface numbers in plug order, and the keyboard has to come out as interface 0 for
// Windows to call it MI_00.
static void usb_identity_begin() {
    static UsbKeyboard keyboard;
    static UsbHidInterface consumer(SYSTEM_CONSUMER_DESCRIPTOR,
                                    sizeof(SYSTEM_CONSUMER_DESCRIPTOR),
                                    HID_SUBCLASS_NONE, HID_PROTOCOL_NONE);
    (void)consumer;
    usb_keyboard = &keyboard;
}

static int usb_keyboard_send(const uint8_t *report) {
    return usb_keyboard ? usb_keyboard->send(report) : -1;
}
