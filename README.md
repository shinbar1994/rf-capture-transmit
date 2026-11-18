# RF Capture & Transmit

Capture a sub-GHz signal with an **RTL-SDR**, inspect it as **hex + bits**,
then replay the payload with a **YARD Stick One**. Frequency is set manually
and shared by both capture and transmit.

```
RTL-SDR  ──capture IQ──►  OOK/ASK demod  ──►  hex + bits  ──►  YARD Stick One ──transmit──►
```

## Run it

```powershell
cd rf-capture-transmit
python app.py
```

The GUI runs **even with no hardware connected**: *Capture* generates a
synthetic OOK burst and *Transmit* does a dry run, so you can learn the flow
offline. When hardware + drivers are present it uses them automatically (the
panels show "ready" vs "not found").

## Workflow

1. **Frequency** — type the center frequency in MHz (e.g. `433.920`), or pick
   a common one from the dropdown. Used for both capture and transmit.
2. **Capture** — set sample rate / duration / gain, hit **Capture**. The app
   demodulates OOK/ASK, auto-detects the symbol (baud) rate, and shows the
   payload in the **HEX** and **BITS** tabs. If it reports *"looks like
   noise"*, there was no real transmission in the window — trigger the device
   *during* the capture and confirm the frequency.

   The capture front-end does proper channelization (the same thing URH does):
   it tunes **Offset** kHz away from the target so the signal avoids the
   RTL-SDR's DC spike, mixes it back in software, low-pass filters to
   **Channel BW**, decimates, then demodulates. Defaults (250 kHz offset,
   200 kHz BW) suit most 315/433 MHz remotes; widen BW for faster/wider
   signals, narrow it to reject adjacent interference.

   **Known baud** — a hint, not a fixed value. The app **measures the actual
   symbol width from the signal** (every pulse/gap is an integer multiple of
   one symbol, so the base width is their fuzzy GCD) and refines your entered
   baud to it — real remotes drift from their nominal rate, so entering `4000`
   might decode at the true `3850`. Set it to `0` to auto-detect with no hint.
   Bits are recovered by integrating the envelope over each whole symbol
   (matched filter) with an automatic clock-phase search, which decodes
   cleanly even in heavy noise.

   **Bursts / messages (URH-style)** — a remote sends short bursts (a preamble,
   then the code) separated by pauses, repeated many times. The app splits the
   capture on those pauses and decodes **each burst as its own message**,
   listing one line per burst (e.g. `aaaaaa` then `936d36d369…`) just like URH's
   rows. The baud is measured once on the cleanest burst and applied to all;
   the threshold uses **Otsu's method** (works at any duty cycle). Because
   captures are noisy, some bursts glitch — read any clean repeat (there are
   usually many). The transmit payload defaults to the richest message.

   **Save capture** — **💾 Save capture** writes the envelope + metadata to a
   `capture_*.npz` in the project folder, for offline analysis or comparison.

   **Signal view (fix the bits yourself)** — click **📈 View signal / fix bits**
   after a capture to open a URH-style window: the amplitude envelope with the
   threshold line and a coloured dot at every symbol sample point (green = 1,
   grey = 0). Change the **Baud**, drag the **Threshold**, hit **Auto baud**
   (measures the symbol width from what's on screen) or **Auto threshold**, and
   the bits re-slice live — so you can eyeball exactly where the decode goes
   wrong and correct it. Zoom/pan with the toolbar, then **Send bits to
   payload** to push the corrected hex to the transmitter. This also works on a
   capture flagged as noise, so you can hunt for a weak signal by hand.
3. **Transmit** — the detected baud is filled in automatically. Choose
   modulation (ASK/OOK or 2-FSK), repeats, then **Transmit**. You can edit the
   payload hex by hand before sending.

## Install

```powershell
pip install -r requirements.txt
```

### Windows drivers (Zadig)
Both devices need a libusb/WinUSB driver bound via [Zadig](https://zadig.akeo.ie/):
- **RTL-SDR** → WinUSB (so `pyrtlsdr` can open it)
- **YARD Stick One** → libusb-win32/WinUSB (so `rfcat`/`rflib` can open it).
  The YS1 must be running **RfCat firmware**.

## What it does and doesn't handle

- **OOK / ASK** (on-off keying) — the modulation used by most simple remotes,
  gate/garage fobs, 315/433/868/915 MHz sensors and doorbells. Fully handled:
  envelope → threshold → run-length → symbol-rate recovery → bits.
- **FSK/GFSK** — the amplitude demodulator won't recover these. You can still
  transmit in 2-FSK mode if you supply the payload and deviation manually.
- Rolling-code systems (KeeLoq etc.) won't work by replay — each press uses a
  new code. Fixed-code remotes replay fine.

## Files

| File              | Purpose                                             |
|-------------------|-----------------------------------------------------|
| `app.py`          | Tkinter GUI tying capture → view → transmit         |
| `rtl_capture.py`  | RTL-SDR IQ capture + OOK/ASK demodulation           |
| `signal_view.py`  | Interactive envelope/bit-slicing viewer (matplotlib)|
| `ys1_transmit.py` | YARD Stick One transmit via RfCat (`rflib`)         |

## ⚠️ Legal / responsible use

You are responsible for everything you transmit. Only transmit on frequencies
and bands you are **licensed or authorized** to use, and only replay signals
you **own or have explicit permission** to test. Unauthorized transmission,
jamming, and accessing systems you don't own are illegal in most countries.
This tool is intended for education, authorized security testing, and work on
your own devices.
