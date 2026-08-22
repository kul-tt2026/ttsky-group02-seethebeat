"""
test_spectrum_ref.py -- self-checks for the magnitude/log golden model.

    python model/test_spectrum_ref.py     # standalone, prints a report
    pytest model/                          # as unit tests

Checks the CORDIC-vectoring magnitude against a float reference, the exact bit-packing of
log2_encode at hand-computed corners, monotonicity, and that the decoded log tracks the
true log2 within the 2-bit-mantissa resolution.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spectrum_ref as sp   # noqa: E402


def test_encode_corners():
    # hand-computed: mag -> {msb, 2 mantissa bits}
    assert sp.log2_encode(0) == 0
    assert sp.log2_encode(1) == 0            # msb 0
    assert sp.log2_encode(2) == (1 << 2)     # msb 1, frac 0 (too few bits) = 4
    assert sp.log2_encode(4) == (2 << 2)     # 8
    assert sp.log2_encode(5) == (2 << 2) | 1 # 9
    assert sp.log2_encode(6) == (2 << 2) | 2 # 10
    assert sp.log2_encode(7) == (2 << 2) | 3 # 11
    assert sp.log2_encode(8) == (3 << 2)     # 12
    assert sp.log2_encode(0xFFFF) == (15 << 2) | 3   # msb 15, top mantissa bits 11


def test_powers_of_two_are_exact():
    for p in range(0, 17):
        assert sp.log2_encode(1 << p) == (p << 2), p


def test_monotonic():
    prev = -1
    for mag in range(0, 5000):
        code = sp.log2_encode(mag)
        assert code >= prev, "not monotonic at {}".format(mag)
        prev = code


def test_log_tracks_log2():
    """Decoded code/4 must track true log2 within the inherent resolution of a 2-bit
    TRUNCATED mantissa: quantization (~0.25) + log2 curvature within a step (~0.086),
    so a hair under ~0.34. That coarseness is invisible on a ~5-bit brightness scale."""
    worst = 0.0
    for mag in range(16, 200000, 7):
        approx = sp.log_to_float(sp.log2_encode(mag))
        worst = max(worst, abs(approx - math.log2(mag)))
    assert worst <= 0.34, "log error too large: {}".format(worst)


def test_magnitude_vs_float():
    """CORDIC-vectoring magnitude must match sqrt(re^2+im^2) within a few LSB."""
    worst = 0
    pts = [(20000, 0), (12000, 12000), (-9000, 15000), (-14000, -8000), (0, -17000),
           (32767, 32767), (-32768, -32768), (100, -50), (1, 1), (0, 0)]
    for (re, im) in pts:
        m = sp.magnitude(re, im)
        f = math.hypot(re, im)
        worst = max(worst, abs(m - f))
    assert worst <= 8, "magnitude error too large: {} LSB".format(worst)


def test_log_mag_endtoend():
    """log_mag(re,im) tracks log2 of the true magnitude."""
    for (re, im) in [(20000, 5000), (300, 400), (-32768, 32767), (2, 0)]:
        f = math.hypot(re, im)
        if f >= 4:
            approx = sp.log_to_float(sp.log_mag(re, im))
            assert abs(approx - math.log2(f)) <= 0.5, (re, im, approx, math.log2(f))


def _main():
    checks = [test_encode_corners, test_powers_of_two_are_exact, test_monotonic,
              test_log_tracks_log2, test_magnitude_vs_float, test_log_mag_endtoend]
    print("SeeTheBeat spectrum (magnitude/log) golden-model self-check")
    print("-" * 58)
    ok = 0
    for c in checks:
        try:
            c()
            print("  PASS  {}".format(c.__name__))
            ok += 1
        except AssertionError as e:
            print("  FAIL  {}  --> {}".format(c.__name__, e))
    print("-" * 58)
    print("{}/{} checks passed".format(ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
