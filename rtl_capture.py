"""
RTL-SDR capture + OOK/ASK demodulation.

Captures raw IQ from an RTL-SDR at a user-chosen frequency, demodulates
amplitude (OOK/ASK) into a pulse train, recovers the symbol (baud) rate,
and returns the payload as bits/bytes so it can be inspected and replayed.

Most simple sub-GHz remotes (gates, garage doors, 433/315 MHz sensors,
doorbells, some TPMS) use OOK/ASK, which this handles. FSK/GFSK signals
are *not* demodulated by the amplitude path -- see demodulate_fsk note.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

try:
    from rtlsdr import RtlSdr
    RTLSDR_AVAILABLE = True
except Exception:  # pragma: no cover - import guard
    RtlSdr = None
    RTLSDR_AVAILABLE = False


@dataclass
class CaptureResult:
    freq_hz: float
    sample_rate: float
    bits: str = ""                 # e.g. "101100..."
    hex: str = ""                  # e.g. "B2 F0 ..."
    baud: float = 0.0              # detected symbol rate (symbols/sec)
    symbol_us: float = 0.0         # detected symbol duration in microseconds
    n_symbols: int = 0
    raw_magnitude: np.ndarray = field(default=None, repr=False)
    envelope: np.ndarray = field(default=None, repr=False)
    threshold: float = 0.0
    snr_db: float = 0.0            # peak-to-noise contrast
    longest_pulse_us: float = 0.0  # longest sustained 'on' pulse found
    signal_present: bool = False   # did a coherent OOK burst gate pass?
    symbol_centers: np.ndarray = field(default=None, repr=False)  # sample idx per bit
    max_baud: float = 100_000.0    # debounce reference used (for re-slicing)
    frames_detected: int = 1       # how many repeats of the frame were seen
    messages: list = field(default_factory=list)  # URH-style per-burst decode
    note: str = ""


def capture_iq(freq_hz: float, sample_rate: float, duration_s: float,
               gain="auto", device_index: int = 0,
               freq_offset: float = 250_000.0) -> np.ndarray:
    """
    Capture raw complex IQ samples from the RTL-SDR.

    The hardware is tuned `freq_offset` ABOVE the target so the signal does
    not sit on the RTL-SDR's DC spike / 1-f LO leakage (which is at the tuned
    center). The offset is undone digitally in `channelize()`. This is the
    standard SDR practice that makes OOK captures clean -- the same thing URH
    does under the hood.
    """
    if not RTLSDR_AVAILABLE:
        raise RuntimeError(
            "pyrtlsdr / librtlsdr not available. Install pyrtlsdr and the "
            "RTL-SDR driver (Zadig WinUSB on Windows)."
        )
    sdr = RtlSdr(device_index=device_index)
    try:
        sdr.sample_rate = sample_rate
        sdr.center_freq = freq_hz + freq_offset
        sdr.gain = gain  # 'auto' or a float in dB
        n = int(sample_rate * duration_s)
        # read_samples wants a power-of-two-ish block count; round up to 256k
        block = 256 * 1024
        n = ((n + block - 1) // block) * block
        samples = sdr.read_samples(n)
        return np.asarray(samples, dtype=np.complex64)
    finally:
        sdr.close()


def _lowpass_taps(cutoff_hz: float, fs: float, numtaps: int = 201) -> np.ndarray:
    """Windowed-sinc (Hamming) low-pass FIR -- scipy-free channel filter."""
    fc = cutoff_hz / fs  # normalized cutoff (cycles/sample), 0..0.5
    n = np.arange(numtaps) - (numtaps - 1) / 2.0
    h = 2 * fc * np.sinc(2 * fc * n)
    h *= np.hamming(numtaps)
    h /= np.sum(h)
    return h.astype(np.float32)


def channelize(iq: np.ndarray, sample_rate: float, freq_offset: float,
               bandwidth: float = 200_000.0):
    """
    Digital down-conversion + channel filter + decimation.

    1. Mix the offset carrier down to 0 Hz (undo the hardware `freq_offset`),
       which moves the DC spike away from the signal.
    2. Low-pass to `bandwidth`/2 so only the signal's channel survives --
       this is what dramatically improves OOK SNR vs. full-band |IQ|.
    3. Decimate to a lower rate for fast, clean demodulation.

    Returns (baseband_iq, new_sample_rate).
    """
    n = len(iq)
    t = np.arange(n, dtype=np.float64) / sample_rate
    # Signal sits at -freq_offset in baseband; shift it to 0 Hz.
    bb = iq * np.exp(1j * 2 * np.pi * freq_offset * t)

    taps = _lowpass_taps(bandwidth / 2.0, sample_rate, numtaps=201)
    bb = np.convolve(bb, taps, mode="same")

    # Decimate so the new rate keeps ~4x the channel bandwidth.
    target_rate = max(4.0 * bandwidth, 400_000.0)
    decim = max(1, int(sample_rate // target_rate))
    return bb[::decim].astype(np.complex64), sample_rate / decim


def _moving_average(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1:
        return x
    kernel = np.ones(w, dtype=np.float32) / w
    return np.convolve(x, kernel, mode="same")


def _otsu_threshold(env: np.ndarray, nbins: int = 256) -> float:
    """
    Otsu's method: the envelope amplitude that best separates the 'off' and
    'on' clusters (maximizes between-class variance). Independent of duty
    cycle, unlike a noise-floor-plus-margin threshold.
    """
    lo, hi = float(env.min()), float(env.max())
    if hi - lo < 1e-12:
        return hi
    hist, edges = np.histogram(env, bins=nbins, range=(lo, hi))
    hist = hist.astype(np.float64)
    mids = (edges[:-1] + edges[1:]) / 2.0
    total = hist.sum()
    w0 = np.cumsum(hist)
    w1 = total - w0
    csum = np.cumsum(hist * mids)
    mu_t = csum[-1]
    mu0 = csum / np.maximum(w0, 1)
    mu1 = (mu_t - csum) / np.maximum(w1, 1)
    sigma_b = w0 * w1 * (mu0 - mu1) ** 2
    sigma_b[w0 == 0] = 0
    sigma_b[w1 == 0] = 0
    return float(mids[int(np.argmax(sigma_b))])


def _debounce(digital: np.ndarray, min_run: int) -> np.ndarray:
    """
    Binary majority (median) filter: removes on/off glitches shorter than
    ~min_run/2 samples, so noise dips inside a symbol don't fragment it.
    This is the fix that lets the symbol-rate estimator see whole symbols.
    """
    if min_run <= 1:
        return digital
    w = int(min_run) | 1  # odd window
    maj = _moving_average(digital.astype(np.float32), w)
    return (maj >= 0.5).astype(np.int8)


def estimate_symbol_width(env: np.ndarray, sample_rate: float, threshold: float,
                          min_baud: float = 500.0, max_baud: float = 100_000.0,
                          seed_hint: float = 0.0):
    """
    Auto-detect the base symbol (bit) width directly from the signal.

    In an OOK frame every pulse/gap is an integer number of symbols, so the
    base width is the (fuzzy) GCD of all run lengths. We seed from the
    shortest run cluster, then search nearby for the width that makes every
    run land closest to an integer multiple -- robust to the transmitter's
    real baud drifting from its nominal value.

    Returns (symbol_samples, baud). (0, 0) if nothing usable.
    """
    min_len = sample_rate / max_baud
    max_len = sample_rate / min_baud
    digital = _debounce((env > threshold).astype(np.int8), int(max(2, min_len)))
    edges = np.flatnonzero(np.diff(digital)) + 1
    if edges.size == 0:
        return 0.0, 0.0
    bounds = np.concatenate(([0], edges, [len(digital)]))
    runs = np.diff(bounds).astype(np.float64)

    # Keep plausible runs; drop huge inter-frame gaps that would dominate.
    runs = runs[(runs >= min_len) & (runs <= max_len)]
    if runs.size < 2:
        return 0.0, 0.0

    # Seed: a hint (e.g. the user's nominal baud) if given, else the median
    # of the shortest ~20% of runs (approx one symbol).
    if seed_hint > 0:
        seed = max(seed_hint, min_len)
        lo, hi = seed * 0.7, seed * 1.3   # tight search: refine near the hint
    else:
        short = runs[runs <= np.percentile(runs, 20)]
        seed = float(np.median(short)) if short.size else float(runs.min())
        seed = max(seed, min_len)
        lo, hi = seed * 0.5, seed * 1.5

    # Fuzzy-GCD search around the seed.
    def fit_score(T):
        r = runs / T
        m = r <= 32            # ignore very long runs for fit scoring
        if not np.any(m):
            return 1e9
        return float(np.mean(np.abs(r[m] - np.round(r[m]))))

    candidates = np.linspace(lo, hi, 400)
    best_T = min(candidates, key=fit_score)

    # Harmonic guard: if every run is an *even* multiple, the true symbol is
    # half (seed landed on 2 symbols). Correct down a couple of times.
    while seed_hint == 0:
        mult = np.round(runs / best_T)
        nz = mult[mult > 0]
        # Require enough runs so coincidental all-even patterns don't halve it.
        if nz.size >= 6 and np.all(nz % 2 == 0) and best_T / 2 >= min_len:
            best_T /= 2.0
        else:
            break

    return best_T, sample_rate / best_T


def _prep_slice(env, sample_rate, threshold, max_baud):
    """
    Precompute the baud-independent parts once: debounced burst start/end and
    the envelope's prefix-sum (for O(1) per-symbol means). Reused across every
    baud/phase trial so clock refinement stays fast.
    """
    min_run = int(max(2, sample_rate / max_baud))
    digital = _debounce((env > threshold).astype(np.int8), min_run)
    on_idx = np.flatnonzero(digital == 1)
    if on_idx.size == 0:
        return None
    cs = np.concatenate(([0.0], np.cumsum(env.astype(np.float64))))
    return {"cs": cs, "start": int(on_idx[0]), "end": int(on_idx[-1]),
            "n": len(env), "thr": float(threshold)}


def _score_baud(ctx, sample_rate, baud):
    """Matched-filter slice at one baud with phase search. Uses precomputed ctx.
    Returns (bits_array, centers_array, eye_score) or (None, None, -1)."""
    if ctx is None or baud <= 0:
        return None, None, -1.0
    cs, start, end, n, thr = ctx["cs"], ctx["start"], ctx["end"], ctx["n"], ctx["thr"]
    sps = sample_rate / baud
    nsym = int(round((end - start) / sps)) + 1
    if nsym < 1:
        return None, None, -1.0
    idx = np.arange(nsym + 1)
    best = (None, None, None, -1.0)
    for phase in np.linspace(-sps / 2.0, sps / 2.0, 21):
        e = start + phase + idx * sps
        a = np.clip(e[:-1].astype(int), 0, n - 1)
        b = np.clip(e[1:].astype(int), 1, n)
        b = np.maximum(b, a + 1)
        means = (cs[b] - cs[a]) / (b - a)
        score = float(np.mean(np.abs(means - thr)))  # mean => baud-comparable
        if score > best[3]:
            best = (means, a, b, score)
    means, a, b, score = best
    return (means > thr).astype(np.int8), ((a + b) // 2).astype(int), score


def refine_clock(env, sample_rate, baud0, threshold, max_baud,
                 span: float = 0.03, steps: int = 181, _ctx=None) -> float:
    """
    Lock the symbol clock precisely using the whole frame as the timing
    reference: search baud within +/-`span` of `baud0` for the value whose
    matched-filter sampling stays aligned across every symbol (max eye score).
    Removes the slow drift that corrupts long frames when the baud is off by a
    fraction of a percent.
    """
    ctx = _ctx if _ctx is not None else _prep_slice(env, sample_rate, threshold, max_baud)
    best_baud, best_score = baud0, -1.0
    for baud in np.linspace(baud0 * (1 - span), baud0 * (1 + span), steps):
        _, _, score = _score_baud(ctx, sample_rate, baud)
        if score > best_score:
            best_score, best_baud = score, baud
    return best_baud


def detect_frame(digital, sample_rate, min_frame_ms: float = 8.0):
    """
    Find the repeat period of a continuously-transmitted frame via
    autocorrelation of the on/off pulse train. Remotes send the same frame
    over and over; decoding one frame (not the whole concatenated stream) is
    what lets the clock stay locked and matches what URH shows.

    Returns (frame_start, frame_len_samples, n_frames). n_frames == 1 means
    no clear repetition (treat the whole burst as one frame).
    """
    on = np.flatnonzero(digital == 1)
    if on.size == 0:
        return 0, len(digital), 1
    start, end = int(on[0]), int(on[-1])
    span = end - start + 1
    x = digital[start:end + 1].astype(np.float64)
    x = x - x.mean()
    if np.all(x == 0):
        return start, span, 1

    n = x.size
    f = np.fft.rfft(x, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    ac0 = ac[0]
    if ac0 <= 0:
        return start, span, 1

    min_lag = int(sample_rate * min_frame_ms / 1000.0)
    max_lag = n // 2                      # need >= 2 repeats to call it periodic
    if min_lag >= max_lag:
        return start, span, 1

    seg = ac[min_lag:max_lag]
    peak_rel = seg / ac0
    k = int(np.argmax(peak_rel))
    # A real frame repeat matches almost perfectly (peak ~1.0); internal
    # sub-patterns within one frame correlate only partially. Require a strong
    # peak AND at least 3 repeats so a single frame is never mis-split.
    if peak_rel[k] < 0.6:
        return start, span, 1
    period = min_lag + k
    n_frames = int(round(span / period))
    if n_frames < 3:                      # need several clear repeats
        return start, span, 1
    return start, period, n_frames


def _pick_fundamental(env, sample_rate, seed_bauds, threshold, max_baud,
                      tol: float = 0.15) -> float:
    """
    Choose the true baud from several candidates. A 2x harmonic samples each
    symbol twice and scores about as well as (often slightly better than) the
    real rate, so among all candidates whose refined eye-score is within `tol`
    of the best, prefer the LOWEST baud -- the fundamental, not the harmonic.
    A true sub-harmonic undersamples and its score collapses, so it never
    falls inside the tolerance; the margin only has to cover the small edge
    penalty the fundamental pays versus its own 2x oversampling.
    """
    ctx = _prep_slice(env, sample_rate, threshold, max_baud)
    scored = []
    for sb in seed_bauds:
        rb = refine_clock(env, sample_rate, sb, threshold, max_baud, _ctx=ctx)
        _, _, score = _score_baud(ctx, sample_rate, rb)
        if score > 0:
            scored.append((rb, score))
    if not scored:
        return max(seed_bauds) if seed_bauds else 0.0
    best = max(s for _, s in scored)
    near = [rb for rb, s in scored if s >= best * (1 - tol)]
    return min(near)


def slice_known_baud(env: np.ndarray, sample_rate: float, baud: float,
                     threshold: float, max_baud: float = 100_000.0):
    """
    Synchronous OOK slicer for a fixed baud. Returns (bitstring, centers).
    Matched-filter integration per symbol + clock-phase search; trailing '0'
    symbols (indistinguishable from silence) are trimmed, URH-style. Pure
    function of (env, baud, threshold) so the GUI can re-run it live.
    """
    ctx = _prep_slice(env, sample_rate, threshold, max_baud)
    bits, centers, _ = _score_baud(ctx, sample_rate, baud)
    if bits is None:
        return "", np.array([], dtype=int)
    last_one = np.flatnonzero(bits == 1)
    if last_one.size:
        cut = int(last_one[-1]) + 1
        bits, centers = bits[:cut], centers[:cut]
    return "".join(str(int(x)) for x in bits), centers


def decode_messages(env, sample_rate, threshold, base_baud, max_baud,
                    min_baud, pause_symbols: float = 6.0):
    """
    Split the capture into separate messages on pauses and NRZ-decode each --
    exactly how URH presents a burst transmission (one row per burst). Remotes
    send a short preamble burst, then a code burst, repeated; each becomes its
    own message. The baud is refined per burst so long bursts stay aligned.

    Returns a list of dicts: {hex, bits, baud, dur_ms, pause_samples}.
    """
    min_run = int(max(2, sample_rate / max_baud))
    digital = _debounce((env > threshold).astype(np.int8), min_run)
    edges = np.flatnonzero(np.diff(digital)) + 1
    if edges.size == 0:
        return []
    bounds = np.concatenate(([0], edges, [len(digital)]))
    runs = np.diff(bounds)
    levels = digital[bounds[:-1]]

    sps = sample_rate / base_baud
    pause = int(pause_symbols * sps)

    # Walk runs, grouping into bursts separated by long OFF pauses.
    bursts = []           # (start, end, pause_after)
    pos = 0
    cur_start = None
    for length, level in zip(runs, levels):
        if level == 1 and cur_start is None:
            cur_start = pos
        if level == 0 and length > pause and cur_start is not None:
            bursts.append((cur_start, pos, int(length)))
            cur_start = None
        pos += length
    if cur_start is not None:
        bursts.append((cur_start, pos, 0))

    if not bursts:
        return []

    # Refine the clock ONCE on the longest burst (most reliable), then use that
    # same precise baud for every burst -- per-burst refinement is unstable on
    # short preamble bursts. Phase is still searched per burst by the slicer.
    ls, le, _ = max(bursts, key=lambda t: t[1] - t[0])
    seeds = {base_baud, base_baud / 2, base_baud * 2}
    baud = _pick_fundamental(env[ls:le], sample_rate, seeds, threshold, max_baud)

    pad = int(2 * sample_rate / baud)   # trailing silence so trim works
    messages = []
    for s, e, pause_after in bursts:
        seg = env[s:min(len(env), e + pad)]
        if seg.size < sps:            # shorter than one symbol -> skip noise
            continue
        bits, _ = slice_known_baud(seg, sample_rate, baud, threshold, max_baud)
        if not bits:
            continue
        messages.append({
            "hex": bits_to_hex(bits),
            "bits": bits,
            "baud": baud,
            "dur_ms": (e - s) / sample_rate * 1e3,
            "pause_samples": pause_after,
        })
    return messages


def demodulate_ook(iq: np.ndarray, sample_rate: float,
                   min_baud: float = 500.0,
                   max_baud: float = 100_000.0,
                   min_snr_db: float = 8.0,
                   min_pulse_us: float = 40.0,
                   known_baud: float = 0.0) -> CaptureResult:
    """
    Amplitude (OOK/ASK) demodulation with a signal-presence gate.

    Steps: magnitude -> smooth -> threshold above noise floor ->
    run-length of on/off -> REJECT if no coherent (sustained) pulse exists,
    i.e. nothing above threshold lasts longer than `min_pulse_us` -> else
    estimate base symbol length -> quantize -> emit bits.

    The coherence gate is what keeps noise from being turned into bits:
    RF noise crosses any threshold for only a sample or two, while a real
    OOK symbol stays high for tens-to-hundreds of microseconds.
    """
    result = CaptureResult(freq_hz=0.0, sample_rate=sample_rate)

    mag = np.abs(iq).astype(np.float32)
    # Smooth over ~ a few microseconds to kill noise but keep edges.
    smooth_win = max(1, int(sample_rate * 2e-6))  # ~2 us
    env = _moving_average(mag, smooth_win)
    result.raw_magnitude = mag
    result.envelope = env

    # Otsu threshold: split the envelope histogram into off/on clusters. This
    # works whatever the duty cycle -- median-as-noise-floor fails when the
    # transmission fills the capture (continuous repeats), landing mid-signal.
    thr = _otsu_threshold(env)
    low = env[env <= thr]
    high = env[env > thr]
    noise_floor = float(low.mean()) if low.size else float(env.min())
    sig_level = float(high.mean()) if high.size else float(env.max())
    noise_floor = max(noise_floor, 1e-9)
    result.snr_db = 20.0 * np.log10(sig_level / noise_floor)

    if sig_level - noise_floor < 1e-6:
        result.note = "No signal detected (flat amplitude). Check freq/gain."
        return result
    result.threshold = float(thr)

    digital = (env > thr).astype(np.int8)

    # Remove glitches far shorter than the shortest allowed symbol, so noise
    # dips don't fragment real symbols (see _debounce).
    min_len = sample_rate / max_baud
    digital = _debounce(digital, int(max(2, min_len)))

    # Run-length encode on/off durations (in samples).
    edges = np.flatnonzero(np.diff(digital)) + 1
    if edges.size == 0:
        result.note = "No edges found -- carrier may be continuous or absent."
        return result
    bounds = np.concatenate(([0], edges, [len(digital)]))
    run_lengths = np.diff(bounds)
    run_levels = digital[bounds[:-1]]

    # --- Coherence gate: is there a sustained 'on' pulse, or just noise? ---
    on_lengths = run_lengths[run_levels == 1]
    longest_on = int(on_lengths.max()) if on_lengths.size else 0
    result.longest_pulse_us = (longest_on / sample_rate) * 1e6
    if (result.longest_pulse_us < min_pulse_us) or (result.snr_db < min_snr_db):
        result.signal_present = False
        result.note = (
            f"No clear signal -- looks like noise "
            f"(SNR {result.snr_db:.1f} dB, longest pulse "
            f"{result.longest_pulse_us:.0f} us). "
            f"Trigger the transmitter DURING capture; verify frequency/gain."
        )
        return result
    result.signal_present = True
    result.max_baud = max_baud

    # --- Determine the base symbol rate for the whole capture. -----------
    if known_baud > 0:
        _, b_hint = estimate_symbol_width(env, sample_rate, thr, min_baud,
                                          max_baud, seed_hint=sample_rate / known_baud)
        _, b_auto = estimate_symbol_width(env, sample_rate, thr, min_baud, max_baud)
        seeds = {b for b in (b_hint, b_auto, known_baud) if b > 0}
    else:
        _, b_auto = estimate_symbol_width(env, sample_rate, thr, min_baud, max_baud)
        if b_auto <= 0:
            result.note = "Could not determine symbol width -- set a Known baud."
            return result
        seeds = {b_auto}
    seeds |= {b / 2 for b in list(seeds) if b / 2 >= min_baud}
    base_baud = _pick_fundamental(env, sample_rate, seeds, thr, max_baud)

    # --- Split into separate messages on pauses (URH-style) and decode each.
    result.messages = decode_messages(env, sample_rate, thr, base_baud,
                                      max_baud, min_baud)
    result.frames_detected = len(result.messages)

    if not result.messages:
        result.note = "Signal present but no decodable bursts found."
        return result

    # Primary = the richest message (the code, not the short preamble); this
    # feeds the HEX/BITS view and the transmit payload by default.
    primary = max(result.messages, key=lambda m: len(m["bits"]))
    result.bits = primary["bits"]
    result.n_symbols = len(primary["bits"])
    result.baud = primary["baud"]
    result.symbol_us = 1e6 / primary["baud"] if primary["baud"] else 0.0
    result.hex = primary["hex"]
    hint = f" [refined from {known_baud:.0f}]" if known_baud > 0 else ""
    result.note = (f"OK (OOK/ASK, baud {primary['baud']:.0f}{hint}, "
                   f"{len(result.messages)} messages, SNR {result.snr_db:.1f} dB)")
    return result


def bits_to_hex(bits: str) -> str:
    """
    MSB-first continuous hex string, URH-style (one char per 4 bits). A
    trailing partial nibble is right-padded with zeros. Not byte-grouped, so
    non-byte-aligned frames (common for remotes) print exactly as URH shows.
    """
    bits = "".join(c for c in bits if c in "01")
    if not bits:
        return ""
    if len(bits) % 4:
        bits = bits.ljust((len(bits) + 3) // 4 * 4, "0")
    return "".join(f"{int(bits[i:i + 4], 2):x}" for i in range(0, len(bits), 4))


def capture_and_demodulate(freq_hz: float, sample_rate: float,
                           duration_s: float, gain="auto",
                           device_index: int = 0,
                           freq_offset: float = 250_000.0,
                           bandwidth: float = 200_000.0,
                           known_baud: float = 0.0) -> CaptureResult:
    """
    Full pipeline (URH-style): capture off-center -> digital down-convert +
    channel-filter + decimate -> OOK-demodulate the clean narrowband signal.

    Pass `known_baud` (e.g. 4000) to lock the symbol clock instead of
    auto-detecting it -- much more reliable on noisy real-world captures.
    """
    iq = capture_iq(freq_hz, sample_rate, duration_s, gain, device_index,
                    freq_offset=freq_offset)
    bb, bb_rate = channelize(iq, sample_rate, freq_offset, bandwidth)
    res = demodulate_ook(bb, bb_rate, known_baud=known_baud)
    res.freq_hz = freq_hz
    return res


# --- Demo / offline mode -------------------------------------------------

def synthetic_capture(freq_hz: float, sample_rate: float = 2_048_000,
                      baud: float = 2000.0) -> CaptureResult:
    """
    Generate a fake OOK burst so the GUI works without hardware attached.
    Encodes the byte pattern 0xB4 0x2D as an OOK pulse train.
    """
    payload_bits = "10110100" "00101101"
    sps = int(sample_rate / baud)  # samples per symbol
    sig = np.repeat([int(b) for b in payload_bits], sps).astype(np.float32)
    # Add preamble gap + noise, wrap as IQ.
    gap = np.zeros(sps * 20, dtype=np.float32)
    full = np.concatenate([gap, sig, gap])
    iq = (full + 0.03 * np.random.randn(full.size)).astype(np.complex64)
    res = demodulate_ook(iq, sample_rate)
    res.freq_hz = freq_hz
    res.note = "SYNTHETIC demo capture (no hardware)."
    return res
