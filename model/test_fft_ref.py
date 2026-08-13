"""
test_fft_ref.py -- self-checks for the fixed-point FFT golden model.

Runs two ways:
    python model/test_fft_ref.py     # standalone, prints a report
    pytest model/                    # as unit tests

Checks the model against known FFT properties and against an exact float FFT.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fft_ref  # noqa: E402
import vectors  # noqa: E402

N = 512


def _mag(re, im):
    return [math.hypot(re[k], im[k]) for k in range(len(re))]


def _snr_db(x_int, N=N):
    """SNR of the fixed-point model vs the exact float FFT, both scaled by 1/N."""
    re, im = fft_ref.fft_fixed(x_int, None, N)
    ours = [complex(re[k], im[k]) / fft_ref.ONE for k in range(N)]      # X/N, Q1.15->float
    xf = [v / fft_ref.ONE for v in x_int]
    ref = [v / N for v in fft_ref.fft_float(xf)]                        # exact X/N
    sig = sum(abs(r) ** 2 for r in ref)
    err = sum(abs(ours[k] - ref[k]) ** 2 for k in range(N))
    if err == 0:
        return float("inf")
    return 10 * math.log10(sig / err)


def test_impulse_is_flat():
    """FFT of an impulse must be flat (all bins ~equal)."""
    re, im = fft_ref.fft_fixed(vectors.impulse(N), None, N)
    mags = _mag(re, im)
    lo, hi = min(mags), max(mags)
    assert hi - lo <= 1.0, "impulse spectrum not flat: min={} max={}".format(lo, hi)
    assert hi > 0, "impulse spectrum is empty"


def test_tone_is_a_spike():
    """A cosine at integer bin b concentrates energy at b and N-b."""
    b = 40
    re, im = fft_ref.fft_fixed(vectors.tone(b, N), None, N)
    mags = _mag(re, im)
    peak = max(mags)
    peak_bins = {k for k, mm in enumerate(mags) if mm > 0.5 * peak}
    assert peak_bins == {b, N - b}, "tone peaks at {} (expected {} and {})".format(
        sorted(peak_bins), b, N - b)
    # energy outside the two peaks should be tiny
    leak = sum(mags[k] for k in range(N) if k not in (b, N - b))
    assert leak < 0.02 * (2 * peak), "too much spectral leakage: {}".format(leak)


def test_dc_only_bin0():
    re, im = fft_ref.fft_fixed(vectors.dc(N), None, N)
    mags = _mag(re, im)
    assert mags[0] == max(mags) and mags[0] > 0
    assert sum(mags[1:]) < 1.0, "DC signal leaked into non-zero bins"


def test_hermitian_symmetry():
    """Real input -> |X[k]| == |X[N-k]|, up to the truncation noise floor.

    Exact Hermitian symmetry holds only for the ideal transform. With our chosen
    TRUNCATION scaling (Step 1.2), a small DC-ward bias (a few LSB, concentrated in
    near-zero low bins) breaks it slightly -- this is expected, not a bug. We allow a
    few LSB; a real indexing/twiddle bug would misalign by hundreds of LSB and still
    fail loudly.
    """
    TOL = 8.0  # LSB; the measured worst case is ~6 LSB in near-zero bins
    re, im = fft_ref.fft_fixed(vectors.two_tone(23, 77, N), None, N)
    mags = _mag(re, im)
    for k in range(1, N // 2):
        assert abs(mags[k] - mags[N - k]) <= TOL, "asymmetry at bin {}".format(k)


def test_snr_reasonable():
    """Fixed-point model should track the exact FFT to a healthy SNR."""
    for name, x in (("tone", vectors.tone(40, N)),
                    ("two_tone", vectors.two_tone(23, 77, N)),
                    ("noise", vectors.noise(N, seed=1))):
        snr = _snr_db(x)
        assert snr > 35.0, "{}: SNR only {:.1f} dB".format(name, snr)


def test_no_overflow_fullscale():
    """Full-scale-ish inputs must not trip the internal overflow guard."""
    for x in (vectors.tone(1, N, amp=32767), vectors.noise(N, amp=32767, seed=2)):
        fft_ref.fft_fixed(x, None, N)   # raises if any stage overflows int16


def _main():
    checks = [test_impulse_is_flat, test_tone_is_a_spike, test_dc_only_bin0,
              test_hermitian_symmetry, test_snr_reasonable, test_no_overflow_fullscale]
    print("SeeTheBeat FFT golden-model self-check (N={})".format(N))
    print("-" * 52)
    ok = 0
    for c in checks:
        try:
            c()
            print("  PASS  {}".format(c.__name__))
            ok += 1
        except AssertionError as e:
            print("  FAIL  {}  --> {}".format(c.__name__, e))
    print("-" * 52)
    for name, x in (("impulse", vectors.impulse(N)), ("tone@40", vectors.tone(40, N)),
                    ("two_tone", vectors.two_tone(23, 77, N)), ("noise", vectors.noise(N, seed=1))):
        if name != "impulse":
            print("  SNR {:<9} = {:5.1f} dB".format(name, _snr_db(x)))
    print("-" * 52)
    print("{}/{} checks passed".format(ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
