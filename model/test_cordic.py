"""
test_cordic.py -- self-checks for the CORDIC golden model.

    python model/test_cordic.py     # standalone, prints a report
    pytest model/                   # as unit tests

Validates rotation mode against cos/sin and vectoring mode against hypot/atan2 across
the full angle range and all quadrants, and reports the worst-case error in LSB.
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cordic  # noqa: E402

ONE = 1 << cordic.Q          # 32768 == 1.0
AMP = 20000                  # test vector magnitude (< 32768/GAIN, stays in range)


def _rotate_errors():
    """Max component error (LSB) rotating (AMP,0) by many angles vs exact cos/sin."""
    worst = 0
    for deg in range(-180, 180, 3):
        th = math.radians(deg)
        gx, gy = cordic.rotate(AMP, 0, cordic.rad_to_ang(th))
        ex = AMP * math.cos(th)
        ey = AMP * math.sin(th)
        worst = max(worst, abs(gx - ex), abs(gy - ey))
    return worst


def _vector_errors():
    """Max magnitude error (LSB) over vectors in all quadrants vs hypot."""
    worst_mag = 0
    worst_ang = 0
    for deg in range(-180, 180, 3):
        th = math.radians(deg)
        x = round(AMP * math.cos(th))
        y = round(AMP * math.sin(th))
        mag, ang = cordic.vector(x, y)
        worst_mag = max(worst_mag, abs(mag - round(math.hypot(x, y))))
        # angle error in degrees (skip near-origin where atan2 is ill-conditioned)
        if math.hypot(x, y) > 1000:
            aerr = abs(cordic.ang_to_rad(ang) - math.atan2(y, x))
            aerr = min(aerr, 2 * math.pi - aerr)
            worst_ang = max(worst_ang, math.degrees(aerr))
    return worst_mag, worst_ang


def test_rotation_matches_cos_sin():
    err = _rotate_errors()
    assert err <= 8, "rotation error too large: {} LSB".format(err)


def test_vector_matches_magnitude():
    mag_err, _ = _vector_errors()
    assert mag_err <= 8, "magnitude error too large: {} LSB".format(mag_err)


def test_vector_matches_angle():
    _, ang_err = _vector_errors()
    assert ang_err <= 0.05, "angle error too large: {} deg".format(ang_err)


def test_known_points():
    # rotate (AMP,0) by +90 deg -> (0, AMP)
    x, y = cordic.rotate(AMP, 0, cordic.QUART)
    assert abs(x) <= 8 and abs(y - AMP) <= 8, "90 deg rotation wrong: {}".format((x, y))
    # magnitude of (a,a) -> a*sqrt(2)
    mag, _ = cordic.vector(10000, 10000)
    assert abs(mag - round(10000 * math.sqrt(2))) <= 8, "diagonal magnitude wrong"


def _main():
    print("SeeTheBeat CORDIC golden-model self-check "
          "(iters={}, angle_bits={})".format(cordic.ITERS, cordic.ANG_W))
    print("-" * 56)
    checks = [test_rotation_matches_cos_sin, test_vector_matches_magnitude,
              test_vector_matches_angle, test_known_points]
    ok = 0
    for c in checks:
        try:
            c()
            print("  PASS  {}".format(c.__name__))
            ok += 1
        except AssertionError as e:
            print("  FAIL  {}  --> {}".format(c.__name__, e))
    print("-" * 56)
    rerr = _rotate_errors()
    merr, aerr = _vector_errors()
    print("  gain K            = {:.5f}".format(cordic.GAIN))
    print("  worst rotate err  = {} LSB  (of 32768)".format(rerr))
    print("  worst |.| err     = {} LSB".format(merr))
    print("  worst angle err   = {:.4f} deg".format(aerr))
    print("-" * 56)
    print("{}/{} checks passed".format(ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
