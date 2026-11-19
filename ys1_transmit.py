"""
YARD Stick One transmit via RfCat (rflib).

Takes a payload (bits or hex), configures the YS1 for OOK/ASK or 2-FSK at a
chosen frequency and baud rate, and transmits it, optionally repeated.

Requires the RfCat firmware on the YARD Stick One and the `rflib` Python
package (comes with rfcat). On Windows you also need the libusb driver
bound to the YS1 (Zadig).
"""

from __future__ import annotations

import time


def _patch_libusb0_windows():
    """
    Work around a pyusb + libusb0 bug on Windows. When a USB read has to
    resolve the device's active configuration, libusb0's get_configuration
    hits `assert ret == 1` and crashes (it returns 0 on Windows). This fires
    on the *second* USB op, which is why the YS1 seemed to die after the first
    transmit. The YARD Stick One has a single configuration (value 1), so if
    the query fails we safely assume 1 instead of letting the assertion kill
    the transmit. Harmless no-op if the backend/internals differ.
    """
    try:
        import usb.backend.libusb0 as _lb0
    except Exception:
        return
    cls = getattr(_lb0, "_LibUSB", None)
    if cls is None or getattr(cls, "_ys1_patched", False):
        return
    orig = cls.get_configuration

    def _safe_get_configuration(self, dev_handle):
        try:
            return orig(self, dev_handle)
        except AssertionError:
            return 1   # YS1 has one configuration, value 1

    cls.get_configuration = _safe_get_configuration
    cls._ys1_patched = True


_patch_libusb0_windows()

try:
    import rflib
    from rflib import RfCat, MOD_ASK_OOK, MOD_2FSK
    RFLIB_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    rflib = None
    RfCat = None
    MOD_ASK_OOK = "ASK/OOK"
    MOD_2FSK = "2FSK"
    RFLIB_AVAILABLE = False


MODULATIONS = {
    "ASK/OOK": "MOD_ASK_OOK",
    "2-FSK": "MOD_2FSK",
}


def bits_to_bytes(bits: str) -> bytes:
    """Pack an MSB-first bitstring into bytes (last byte zero-padded)."""
    bits = "".join(c for c in bits if c in "01")
    if not bits:
        return b""
    padded = bits.ljust((len(bits) + 7) // 8 * 8, "0")
    return bytes(int(padded[i:i + 8], 2) for i in range(0, len(padded), 8))


def hex_to_bytes(hex_str: str) -> bytes:
    """Parse 'B4 2D' / 'B42D' / '0xB4,0x2D' into bytes."""
    cleaned = (
        hex_str.replace("0x", "").replace(",", " ")
        .replace(":", " ").strip()
    )
    tokens = cleaned.split()
    if len(tokens) == 1:
        s = tokens[0]
        if len(s) % 2:          # odd nibble count (non-byte-aligned frame)
            s += "0"            # right-pad the final nibble to a full byte
        tokens = [s[i:i + 2] for i in range(0, len(s), 2)]
    return bytes(int(t, 16) for t in tokens)


# A single, reused RfCat connection. Opening a new RfCat() per transmit leaks
# the USB handle, so the *second* transmit can't claim the interface and the
# dongle appears to "stop responding". We open once and reuse.
_dongle = None


def _get_dongle():
    global _dongle
    if _dongle is None:
        _dongle = RfCat()
    return _dongle


def reset_dongle():
    """
    Release and reopen the YARD Stick One. Call this if it stops responding
    (also wired to the GUI's 'Reset YS1' button). Safe to call any time.
    """
    global _dongle
    if _dongle is not None:
        for method in ("setModeIDLE", "cleanup"):
            try:
                getattr(_dongle, method)()
            except Exception:
                pass
        _dongle = None
    if RFLIB_AVAILABLE:
        try:
            _dongle = RfCat()
            return "YARD Stick One reset (USB reconnected)."
        except Exception as e:
            _dongle = None
            return f"YARD Stick One not found: {e}"
    return "rflib not available -- nothing to reset."


# YARD Stick One USB IDs (RfCat firmware + bootloader). Vendor 0x1d50.
YS1_USB_IDS = [(0x1d50, 0x605b), (0x1d50, 0x6047),
               (0x1d50, 0x604b), (0x1d50, 0x6048)]


def ys1_device_present() -> bool:
    """
    True only if a YARD Stick One is physically plugged in right now.

    We check the USB bus by vendor/product id instead of opening it, because
    RfCat()/resetup() does NOT raise when no dongle is present -- it loops
    forever printing "No Dongle Found", which would hang the probe. A bus scan
    is instant and non-blocking. RFLIB_AVAILABLE only means the library
    imported, so it stays True with the dongle unplugged.
    """
    if not RFLIB_AVAILABLE:
        return False
    try:
        import usb.core
        for vid, pid in YS1_USB_IDS:
            if usb.core.find(idVendor=vid, idProduct=pid) is not None:
                return True
        return False
    except Exception:
        return False


def transmit(payload: bytes, freq_hz: float, baud: float,
             modulation: str = "ASK/OOK", power: int = 0,
             repeats: int = 1, deviation_hz: float = 0.0):
    """
    Transmit `payload` with the YARD Stick One (reusing one USB connection).

    freq_hz      : carrier frequency in Hz (e.g. 433_920_000)
    baud         : symbol rate in symbols/sec (match the captured baud)
    modulation   : 'ASK/OOK' or '2-FSK'
    power        : TX power register value (0..0xC0, hardware dependent)
    repeats      : how many times to send the frame back-to-back
    deviation_hz : FSK deviation (only used for 2-FSK)
    """
    if not RFLIB_AVAILABLE:
        raise RuntimeError(
            "rflib (rfcat) not available. Install rfcat and flash RfCat "
            "firmware onto the YARD Stick One."
        )
    if not payload:
        raise ValueError("Empty payload -- nothing to transmit.")

    d = None
    try:
        d = _get_dongle()
        d.setModeIDLE()                 # make sure we start from a clean state
        d.setFreq(int(freq_hz))
        if modulation == "2-FSK":
            d.setMdmModulation(MOD_2FSK)
            if deviation_hz > 0:
                d.setMdmDeviatn(deviation_hz)
        else:
            d.setMdmModulation(MOD_ASK_OOK)
        d.setMdmDRate(int(baud))
        d.makePktFLEN(len(payload))
        d.setMaxPower() if power == 0 else d.setPower(power)
        # Disable preamble/sync so we send the raw payload bytes verbatim.
        d.setMdmSyncMode(0)

        # Repeat by looping in Python: RFxmit's firmware `repeat` argument is
        # unreliable across rfcat versions (often sends only once). Looping
        # guarantees each repeat is a real send, with a small gap between them
        # like a held-down remote.
        n_tx = max(1, int(repeats))
        for i in range(n_tx):
            d.RFxmit(payload)
            if i < n_tx - 1:
                time.sleep(0.06)        # ~60 ms inter-repeat gap
        return f"Transmitted {len(payload)} byte(s) x{n_tx} @ {freq_hz/1e6:.4f} MHz"
    except Exception:
        # A failed transmit can leave the USB state wedged; drop & reopen so
        # the next attempt starts fresh instead of compounding the error.
        try:
            reset_dongle()
        except Exception:
            pass
        raise
    finally:
        # From field experience: ALWAYS return the YS1 to IDLE at the end of
        # every transmission, or it can stop responding to the next command.
        if d is not None:
            try:
                d.setModeIDLE()
            except Exception:
                pass


def transmit_demo(payload: bytes, freq_hz: float, baud: float,
                  modulation: str = "ASK/OOK", power: int = 0,
                  repeats: int = 1, deviation_hz: float = 0.0):
    """No-hardware dry run: validates params and reports what *would* be sent."""
    hexs = " ".join(f"{b:02X}" for b in payload)
    return (
        f"[DRY RUN] Would transmit {len(payload)} byte(s) x{repeats}\n"
        f"  freq={freq_hz/1e6:.4f} MHz  baud={baud:.0f}  mod={modulation}\n"
        f"  payload={hexs}"
    )
