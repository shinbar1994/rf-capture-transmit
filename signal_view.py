"""
URH-style interactive signal viewer.

Shows the amplitude envelope of a capture with the slice threshold and the
per-symbol sampling points overlaid, so you can *see* how the bits were
decoded and fix it: change the baud or drag the threshold and the bits
re-slice live. Zoom/pan with the matplotlib toolbar.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import numpy as np
import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk)

import rtl_capture as rc


class SignalView(tk.Toplevel):
    def __init__(self, parent, result: rc.CaptureResult, on_use_bits=None):
        super().__init__(parent)
        self.title("Signal view — envelope, threshold & bit slicing")
        self.geometry("1100x720")
        self.result = result
        self.on_use_bits = on_use_bits

        self.env = np.asarray(result.envelope, dtype=np.float32)
        self.fs = float(result.sample_rate)
        self.max_baud = float(result.max_baud or 100_000.0)
        self.t_ms = np.arange(self.env.size) / self.fs * 1e3

        env_max = float(self.env.max()) if self.env.size else 1.0
        init_baud = result.baud if result.baud > 0 else 4000.0
        init_thr = result.threshold if result.threshold > 0 else env_max * 0.5

        self.baud_var = tk.StringVar(value=str(int(round(init_baud))))
        self.thr_var = tk.DoubleVar(value=init_thr)

        self._build_controls(env_max)
        self._build_plot()
        self._build_bits_box()
        self.reslice()

    # ---------------- UI ------------------------------------------------
    def _build_controls(self, env_max):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=6)

        ttk.Label(bar, text="Baud:").pack(side="left")
        e = ttk.Entry(bar, textvariable=self.baud_var, width=8)
        e.pack(side="left", padx=(2, 12))
        e.bind("<Return>", lambda *_: self.reslice())

        ttk.Label(bar, text="Threshold:").pack(side="left")
        self.thr_scale = ttk.Scale(
            bar, from_=0.0, to=max(env_max, 1e-3), variable=self.thr_var,
            orient="horizontal", length=260, command=lambda *_: self.reslice())
        self.thr_scale.pack(side="left", padx=(2, 6))
        self.thr_lbl = ttk.Label(bar, text="")
        self.thr_lbl.pack(side="left", padx=(0, 12))

        ttk.Button(bar, text="Re-slice", command=self.reslice).pack(side="left", padx=4)
        ttk.Button(bar, text="Auto baud", command=self.auto_baud).pack(side="left", padx=4)
        ttk.Button(bar, text="Auto threshold", command=self.auto_threshold).pack(side="left", padx=4)
        ttk.Button(bar, text="↑ Send bits to payload", command=self.use_bits).pack(side="right", padx=4)

        self.info_lbl = ttk.Label(self, text="", anchor="w")
        self.info_lbl.pack(fill="x", padx=10)

    def _build_plot(self):
        self.fig = Figure(figsize=(10, 3.6), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel("time (ms)")
        self.ax.set_ylabel("amplitude")

        # Downsample the line for display only (slicing uses full-res env).
        n = self.env.size
        stride = max(1, n // 120_000)
        self.ax.plot(self.t_ms[::stride], self.env[::stride],
                     lw=0.6, color="#1f77b4", label="envelope")
        self.thr_line = self.ax.axhline(self.thr_var.get(), color="#d62728",
                                        lw=1.0, ls="--", label="threshold")
        # Scatter points for sampled symbols (set in reslice()).
        self.sc_one = self.ax.scatter([], [], s=18, color="#2ca02c", zorder=5, label="bit=1")
        self.sc_zero = self.ax.scatter([], [], s=18, color="#888888", zorder=5, label="bit=0")
        self.ax.legend(loc="upper right", fontsize=8)
        self.fig.tight_layout()

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=4)
        self.canvas = FigureCanvasTkAgg(self.fig, master=frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        NavigationToolbar2Tk(self.canvas, frame)

    def _build_bits_box(self):
        f = ttk.LabelFrame(self, text="Decoded bits / hex  (edit baud & threshold above to correct)")
        f.pack(fill="x", padx=8, pady=(0, 8))
        self.bits_text = tk.Text(f, height=6, wrap="char", font=("Consolas", 10))
        self.bits_text.pack(fill="x", padx=6, pady=6)

    # ---------------- Logic ---------------------------------------------
    def auto_threshold(self):
        self.thr_var.set(rc._otsu_threshold(self.env))
        self.reslice()

    def auto_baud(self):
        """Measure the symbol width from the signal at the current threshold."""
        thr = float(self.thr_var.get())
        base, baud = rc.estimate_symbol_width(self.env, self.fs, thr,
                                              max_baud=self.max_baud)
        if baud > 0:
            self.baud_var.set(str(int(round(baud))))
            self.reslice()
        else:
            self.info_lbl.config(text="Auto baud: could not measure a symbol width.")

    def reslice(self):
        try:
            baud = float(self.baud_var.get())
        except ValueError:
            return
        thr = float(self.thr_var.get())
        self.thr_lbl.config(text=f"{thr:.3f}")
        self.thr_line.set_ydata([thr, thr])

        bits, centers = rc.slice_known_baud(self.env, self.fs, baud, thr, self.max_baud)

        if centers.size:
            ct = centers / self.fs * 1e3
            vals = self.env[centers]
            barr = np.array([int(b) for b in bits])
            self.sc_one.set_offsets(np.c_[ct[barr == 1], vals[barr == 1]]
                                    if (barr == 1).any() else np.empty((0, 2)))
            self.sc_zero.set_offsets(np.c_[ct[barr == 0], vals[barr == 0]]
                                     if (barr == 0).any() else np.empty((0, 2)))
        else:
            self.sc_one.set_offsets(np.empty((0, 2)))
            self.sc_zero.set_offsets(np.empty((0, 2)))

        self.canvas.draw_idle()

        hexs = rc.bits_to_hex(bits)
        self.bits_text.delete("1.0", "end")
        self.bits_text.insert("1.0", f"BITS ({len(bits)}):\n{bits}\n\nHEX:\n{hexs}")
        self._last_hex = hexs
        self.info_lbl.config(
            text=f"baud={baud:.0f}   symbol≈{1e6/baud:.0f} µs   "
                 f"bits={len(bits)}   SNR={self.result.snr_db:.1f} dB")

    def use_bits(self):
        if self.on_use_bits and getattr(self, "_last_hex", ""):
            self.on_use_bits(self._last_hex)
