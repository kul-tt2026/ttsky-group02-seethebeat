"""
test_butterfly.py -- self-checks for the butterfly golden model.

    python model/test_butterfly.py     # standalone, prints a report
    pytest model/                      # as unit tests

Validates the fixed-point butterfly against an exact floating-point reference and
against the identity cases, and checks that saturation clips (never wraps).
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cordic     # noqa: E402
import butterfly  # noqa: E402

VECS = [(15000, 0, 10000, 0),
        (8000, -6000, -9000, 4000),
        (-12000, 10000, 7000, -5000),
        (0, 14000, -3000, -11000),
        (32000, 32000, 32000, 32000)]   # full-scale-ish: exercises saturation


def _float_ref(a_re, a_im, b_re, b_im, angle):
    """Exact float butterfly: W*B = rotate B by `angle`, then (A +/- W*B)/2."""
    th = cordic.ang_to_rad(angle)
    wr = b_re * math.cos(th) - b_im * math.sin(th)
    wi = b_re * math.sin(th) + b_im * math.cos(th)
    return ((a_re + wr) / 2.0, (a_im + wi) / 2.0,
            (a_re - wr) / 2.0, (a_im - wi) / 2.0)


def _worst_err():
    worst = 0
    for deg in range(-170, 171, 20):
        ang = cordic.rad_to_ang(math.radians(deg))
        for (ar, ai, br, bi) in VECS[:-1]:      # skip the saturating case for accuracy
            m = butterfly.butterfly(ar, ai, br, bi, ang)
            f = _float_ref(ar, ai, br, bi, ang)
            for mi, fi in zip(m, f):
                worst = max(worst, abs(mi - fi))
    return worst


def test_matches_float():
    err = _worst_err()
    assert err <= 8, "butterfly error too large: {} LSB".format(err)


def test_angle_zero():
    """W = e^-j0 = 1, so A' = (A+B)/2, B' = (A-B)/2 (CORDIC gain ~exact)."""
    ar, ai, br, bi = 20000, -8000, 10000, 6000
    aro, aio, bro, bio = butterfly.butterfly(ar, ai, br, bi, 0)
    assert abs(aro - ((ar + br) >> 1)) <= 4
    assert abs(aio - ((ai + bi) >> 1)) <= 4
    assert abs(bro - ((ar - br) >> 1)) <= 4
    assert abs(bio - ((ai - bi) >> 1)) <= 4


def test_saturation_function():
    assert butterfly.sat(40000) == 32767
    assert butterfly.sat(-40000) == -32768
    assert butterfly.sat(100) == 100
    assert butterfly.sat(-100) == -100


def test_fullscale_in_range():
    """Full-scale inputs must yield outputs inside int16 (saturated, not wrapped)."""
    for deg in (0, 30, 90, -120):
        ang = cordic.rad_to_ang(math.radians(deg))
        for v in butterfly.butterfly(32000, 32000, 32000, 32000, ang):
            assert -32768 <= v <= 32767, "out of range: {}".format(v)


def _main():
    checks = [test_matches_float, test_angle_zero,
              test_saturation_function, test_fullscale_in_range]
    print("SeeTheBeat butterfly golden-model self-check")
    print("-" * 48)
    ok = 0
    for c in checks:
        try:
            c()
            print("  PASS  {}".format(c.__name__))
            ok += 1
        except AssertionError as e:
            print("  FAIL  {}  --> {}".format(c.__name__, e))
    print("-" * 48)
    print("  worst error vs float = {} LSB (of 32768)".format(_worst_err()))
    print("-" * 48)
    print("{}/{} checks passed".format(ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
