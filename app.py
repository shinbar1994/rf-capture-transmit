"""
RF Capture & Transmit
=====================
Capture a sub-GHz signal with an RTL-SDR, inspect it as hex + bits,
then replay the payload with a YARD Stick One.

    python app.py

If no hardware / drivers are present the app still runs: "Capture" falls
back to a synthetic OOK burst and "Transmit" does a dry run, so you can
learn the workflow offline.

LEGAL: You are responsible for what you transmit. Only transmit on
frequencies/bands you are licensed or authorized to use, and only replay
signals you own or are explicitly permitted to test. Intentional
interference and unauthorized access are illegal in most jurisdictions.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, messagebox

import rtl_capture as rc
import ys1_transmit as tx


COMMON_FREQS = ["315.000", "390.000", "418.000", "433.920", "868.350", "915.000"]


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RF Capture & Transmit  —  RTL-SDR → YARD Stick One")
        self.geometry("920x680")
        self.minsize(820, 600)

        self.last_result: rc.CaptureResult | None = None

        self._build_freq_bar()
        self._build_capture_panel()
        self._build_payload_panel()
        self._build_transmit_panel()
        self._build_status_bar()

    # ---------------- Frequency (shared by capture + transmit) ----------
    def _build_freq_bar(self):
        f = ttk.LabelFrame(self, text="Frequency (manual)")
        f.pack(fill="x", padx=10, pady=(10, 4))

        ttk.Label(f, text="Center freq (MHz):").grid(row=0, column=0, padx=6, pady=8, sticky="w")
        self.freq_var = tk.StringVar(value="433.920")
        self.freq_entry = ttk.Combobox(f, textvariable=self.freq_var,
                                       values=COMMON_FREQS, width=14)
        self.freq_entry.grid(row=0, column=1, padx=6, pady=8, sticky="w")

        ttk.Label(f, text="(used for both capture and transmit)").grid(
            row=0, column=2, padx=6, sticky="w")

    # ---------------- Capture -------------------------------------------
    def _build_capture_panel(self):
        f = ttk.LabelFrame(self, text="1 · Capture  (RTL-SDR)")
        f.pack(fill="x", padx=10, pady=4)

        ttk.Label(f, text="Sample rate (Hz):").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self.sr_var = tk.StringVar(value="2048000")
        ttk.Entry(f, textvariable=self.sr_var, width=12).grid(row=0, column=1, padx=6, sticky="w")

        ttk.Label(f, text="Duration (s):").grid(row=0, column=2, padx=6, sticky="w")
        self.dur_var = tk.StringVar(value="2.0")
        ttk.Entry(f, textvariable=self.dur_var, width=8).grid(row=0, column=3, padx=6, sticky="w")

        ttk.Label(f, text="Gain:").grid(row=0, column=4, padx=6, sticky="w")
        self.gain_var = tk.StringVar(value="auto")
        ttk.Entry(f, textvariable=self.gain_var, width=8).grid(row=0, column=5, padx=6, sticky="w")

        self.capture_btn = ttk.Button(f, text="▶ Capture", command=self.on_capture)
        self.capture_btn.grid(row=0, column=6, padx=10, pady=6)

        # Row 1: channelization controls (dodge DC spike + filter the channel).
        ttk.Label(f, text="Offset (kHz):").grid(row=1, column=0, padx=6, pady=(0, 6), sticky="w")
        self.offset_var = tk.StringVar(value="250")
        ttk.Entry(f, textvariable=self.offset_var, width=12).grid(row=1, column=1, padx=6, pady=(0, 6), sticky="w")

        ttk.Label(f, text="Channel BW (kHz):").grid(row=1, column=2, padx=6, pady=(0, 6), sticky="w")
        self.bw_var = tk.StringVar(value="200")
        ttk.Entry(f, textvariable=self.bw_var, width=8).grid(row=1, column=3, padx=6, pady=(0, 6), sticky="w")

        ttk.Label(f, text="Known baud (0=auto):").grid(row=1, column=4, padx=6, pady=(0, 6), sticky="w")
        self.cap_baud_var = tk.StringVar(value="4000")
        ttk.Entry(f, textvariable=self.cap_baud_var, width=8).grid(row=1, column=5, padx=6, pady=(0, 6), sticky="w")

        hw = "RTL-SDR: ready" if rc.RTLSDR_AVAILABLE else "RTL-SDR: not found (synthetic demo mode)"
        ttk.Label(f, text=hw, foreground=("green" if rc.RTLSDR_AVAILABLE else "#b06000")).grid(
            row=2, column=0, columnspan=7, padx=6, pady=(0, 6), sticky="w")

    # ---------------- Payload view --------------------------------------
    def _build_payload_panel(self):
        f = ttk.LabelFrame(self, text="2 · Captured payload")
        f.pack(fill="both", expand=True, padx=10, pady=4)

        info = ttk.Frame(f)
        info.pack(fill="x", padx=6, pady=4)
        self.detect_var = tk.StringVar(value="No capture yet.")
        ttk.Label(info, textvariable=self.detect_var).pack(side="left")
        ttk.Button(info, text="📈 View signal / fix bits",
                   command=self.open_signal_view).pack(side="right")
        ttk.Button(info, text="💾 Save capture",
                   command=self.save_capture).pack(side="right", padx=(0, 6))
        ttk.Button(info, text="🗑 Clear",
                   command=self.clear_capture).pack(side="right", padx=(0, 6))

        nb = ttk.Notebook(f)
        nb.pack(fill="both", expand=True, padx=6, pady=6)

        self.hex_text = tk.Text(nb, height=8, wrap="word", font=("Consolas", 11))
        self.bits_text = tk.Text(nb, height=8, wrap="char", font=("Consolas", 11))
        nb.add(self.hex_text, text="HEX")
        nb.add(self.bits_text, text="BITS")

        edit = ttk.Frame(f)
        edit.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(edit, text="Payload to transmit (edit as hex, e.g. B4 2D):").pack(side="left")
        self.payload_var = tk.StringVar()
        ttk.Entry(edit, textvariable=self.payload_var, width=48).pack(
            side="left", padx=6, fill="x", expand=True)
        ttk.Button(edit, text="↓ Use captured", command=self.use_captured).pack(side="left")

    # ---------------- Transmit ------------------------------------------
    def _build_transmit_panel(self):
        f = ttk.LabelFrame(self, text="3 · Transmit  (YARD Stick One)")
        f.pack(fill="x", padx=10, pady=4)

        ttk.Label(f, text="Modulation:").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        self.mod_var = tk.StringVar(value="ASK/OOK")
        ttk.Combobox(f, textvariable=self.mod_var, values=list(tx.MODULATIONS.keys()),
                     width=10, state="readonly").grid(row=0, column=1, padx=6, sticky="w")

        ttk.Label(f, text="Baud:").grid(row=0, column=2, padx=6, sticky="w")
        self.baud_var = tk.StringVar(value="2000")
        ttk.Entry(f, textvariable=self.baud_var, width=10).grid(row=0, column=3, padx=6, sticky="w")

        ttk.Label(f, text="Repeats:").grid(row=0, column=4, padx=6, sticky="w")
        self.rep_var = tk.StringVar(value="3")
        ttk.Entry(f, textvariable=self.rep_var, width=6).grid(row=0, column=5, padx=6, sticky="w")

        ttk.Label(f, text="FSK dev (Hz):").grid(row=0, column=6, padx=6, sticky="w")
        self.dev_var = tk.StringVar(value="0")
        ttk.Entry(f, textvariable=self.dev_var, width=8).grid(row=0, column=7, padx=6, sticky="w")

        self.tx_btn = ttk.Button(f, text="📡 Transmit", command=self.on_transmit)
        self.tx_btn.grid(row=0, column=8, padx=10, pady=6)

        self.reset_btn = ttk.Button(f, text="⟳ Reset YS1", command=self.on_reset_ys1)
        self.reset_btn.grid(row=0, column=9, padx=(0, 6), pady=6)

        hw = "YARD Stick One: ready" if tx.RFLIB_AVAILABLE else "YARD Stick One: not found (dry-run mode)"
        ttk.Label(f, text=hw, foreground=("green" if tx.RFLIB_AVAILABLE else "#b06000")).grid(
            row=1, column=0, columnspan=9, padx=6, pady=(0, 6), sticky="w")

    def _build_status_bar(self):
        self.status_var = tk.StringVar(value="Ready.")
        bar = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w")
        bar.pack(fill="x", side="bottom")

    # ---------------- Helpers -------------------------------------------
    def _freq_hz(self) -> float:
        return float(self.freq_var.get()) * 1e6

    def set_status(self, msg: str):
        self.status_var.set(msg)
        self.update_idletasks()

    def _busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        self.capture_btn.config(state=state)
        self.tx_btn.config(state=state)

    # ---------------- Actions -------------------------------------------
    def on_capture(self):
        try:
            freq = self._freq_hz()
            sr = float(self.sr_var.get())
            dur = float(self.dur_var.get())
        except ValueError:
            messagebox.showerror("Input error", "Freq / sample rate / duration must be numbers.")
            return
        gain = self.gain_var.get().strip()
        gain = "auto" if gain.lower() == "auto" else float(gain)
        try:
            offset = float(self.offset_var.get()) * 1e3
            bw = float(self.bw_var.get()) * 1e3
            cap_baud = float(self.cap_baud_var.get())
        except ValueError:
            messagebox.showerror("Input error", "Offset / bandwidth / baud must be numbers.")
            return

        self._busy(True)
        self.set_status(f"Capturing on {freq/1e6:.4f} MHz ...")

        def work():
            try:
                if rc.RTLSDR_AVAILABLE:
                    res = rc.capture_and_demodulate(freq, sr, dur, gain,
                                                    freq_offset=offset, bandwidth=bw,
                                                    known_baud=cap_baud)
                else:
                    res = rc.synthetic_capture(freq, sr)
                self.after(0, lambda: self._capture_done(res))
            except Exception as e:
                self.after(0, lambda: self._capture_err(e))

        threading.Thread(target=work, daemon=True).start()

    def _capture_done(self, res: rc.CaptureResult):
        self.last_result = res
        self.hex_text.delete("1.0", "end")
        self.bits_text.delete("1.0", "end")

        if not res.signal_present:
            # Don't present noise as a payload.
            self.hex_text.insert("1.0", "(no signal)")
            self.bits_text.insert("1.0", "(no signal)")
            self.detect_var.set(res.note)
            self.payload_var.set("")
            self._busy(False)
            self.set_status("No clear signal captured.")
            return

        # Show one line per decoded message, URH-style (preamble, code, ...).
        if res.messages:
            hex_lines, bit_lines = [], []
            for i, m in enumerate(res.messages):
                tag = f"[{i+1}] {m['baud']:.0f}bd {m['dur_ms']:.0f}ms"
                pause = (f"  [pause {m['pause_samples']}]"
                         if m.get("pause_samples") else "")
                hex_lines.append(f"{tag}: {m['hex']}{pause}")
                bit_lines.append(f"{tag}: {m['bits']}")
            self.hex_text.insert("1.0", "\n".join(hex_lines))
            self.bits_text.insert("1.0", "\n".join(bit_lines))
        else:
            self.hex_text.insert("1.0", res.hex or "(empty)")
            self.bits_text.insert("1.0", res.bits or "(empty)")

        msg_txt = (f"   |   {res.frames_detected} messages"
                   if res.frames_detected > 1 else "")
        self.detect_var.set(
            f"{res.note}   |   baud≈{res.baud:,.0f}   "
            f"symbol≈{res.symbol_us:.1f} µs{msg_txt}"
        )
        if res.baud:
            self.baud_var.set(str(int(round(res.baud))))
        self.payload_var.set(res.hex)   # primary (richest) message
        self._busy(False)
        self.set_status("Capture complete.")

    def _capture_err(self, e: Exception):
        self._busy(False)
        self.set_status("Capture failed.")
        messagebox.showerror("Capture error", str(e))

    def use_captured(self):
        if self.last_result and self.last_result.hex:
            self.payload_var.set(self.last_result.hex)
        else:
            messagebox.showinfo("No capture", "Capture a signal first.")

    def clear_capture(self):
        """Clear the captured payload, bits/hex view and the transmit field."""
        self.last_result = None
        self.hex_text.delete("1.0", "end")
        self.bits_text.delete("1.0", "end")
        self.payload_var.set("")
        self.detect_var.set("No capture yet.")
        self.set_status("Cleared.")

    def on_reset_ys1(self):
        """Release and reopen the YARD Stick One USB connection."""
        if not tx.RFLIB_AVAILABLE:
            messagebox.showinfo("Reset YS1", "YARD Stick One not connected (dry-run mode).")
            return
        self.set_status("Resetting YARD Stick One ...")

        def work():
            try:
                msg = tx.reset_dongle()
                self.after(0, lambda: self.set_status(msg))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("Reset failed", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def save_capture(self):
        if not self.last_result or self.last_result.envelope is None:
            messagebox.showinfo("No capture", "Capture a signal first.")
            return
        import numpy as np, time, os, json
        res = self.last_result
        base = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            f"capture_{time.strftime('%Y%m%d_%H%M%S')}")
        try:
            # Envelope as a plain .npy (most robust), metadata as readable .json.
            np.save(base + ".npy", np.asarray(res.envelope, dtype=np.float32))
            meta = {
                "sample_rate": float(res.sample_rate),
                "freq_hz": float(res.freq_hz),
                "threshold": float(res.threshold),
                "baud": float(res.baud),
                "frames_detected": int(res.frames_detected),
                "snr_db": float(res.snr_db),
                "note": res.note,
                "hex": res.hex,
                "bits": res.bits,
                "envelope_npy": os.path.basename(base + ".npy"),
            }
            with open(base + ".json", "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
        except Exception as e:
            messagebox.showerror("Save failed", f"{type(e).__name__}: {e}")
            return
        self.set_status(f"Saved: {base}.npy / .json")
        messagebox.showinfo("Saved",
                            f"Saved two files in the project folder:\n"
                            f"{base}.npy  (envelope)\n{base}.json  (bits/hex/params)")

    def open_signal_view(self):
        if not self.last_result or self.last_result.envelope is None:
            messagebox.showinfo("No capture", "Capture a signal first.")
            return
        try:
            import signal_view
        except Exception as e:
            messagebox.showerror("Viewer unavailable",
                                 f"matplotlib is required for the signal view.\n{e}")
            return
        signal_view.SignalView(self, self.last_result,
                               on_use_bits=self.payload_var.set)

    def on_transmit(self):
        raw = self.payload_var.get().strip()
        if not raw:
            messagebox.showinfo("No payload", "Capture or enter a payload first.")
            return
        try:
            payload = tx.hex_to_bytes(raw)
        except Exception:
            messagebox.showerror("Payload error", "Could not parse payload as hex.")
            return
        try:
            freq = self._freq_hz()
            baud = float(self.baud_var.get())
            repeats = int(self.rep_var.get())
            dev = float(self.dev_var.get())
        except ValueError:
            messagebox.showerror("Input error", "Freq / baud / repeats / dev must be numbers.")
            return
        mod = self.mod_var.get()

        if tx.RFLIB_AVAILABLE and not messagebox.askyesno(
            "Confirm transmit",
            f"Transmit {len(payload)} byte(s) x{repeats}\n"
            f"on {freq/1e6:.4f} MHz ({mod}, {baud:.0f} baud)?\n\n"
            "Only transmit on bands you are authorized to use.",
        ):
            return

        self._busy(True)
        self.set_status("Transmitting ...")

        def work():
            try:
                fn = tx.transmit if tx.RFLIB_AVAILABLE else tx.transmit_demo
                msg = fn(payload, freq, baud, mod, repeats=repeats, deviation_hz=dev)
                self.after(0, lambda: self._tx_done(msg))
            except Exception as e:
                self.after(0, lambda: self._tx_err(e))

        threading.Thread(target=work, daemon=True).start()

    def _tx_done(self, msg: str):
        self._busy(False)
        self.set_status("Transmit complete.")
        messagebox.showinfo("Transmit", msg)

    def _tx_err(self, e: Exception):
        self._busy(False)
        self.set_status("Transmit failed.")
        messagebox.showerror("Transmit error", str(e))


if __name__ == "__main__":
    App().mainloop()
