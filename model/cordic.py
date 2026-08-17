"""
cordic.py -- bit-accurate CORDIC model (rotation + vectoring) for SeeTheBeat.

This is the golden model for the CORDIC hardware (src/cordic.v, Phase 2.3). It performs
vector rotation using only shifts and adds -- no multiplier -- per the theory in
SeeTheBeat_LearningNotes.pdf (Step 2.1).

Number formats (see CLAUDE.md / Step 1.2 & 2.1):
    data  : 16-bit signed, Q1.15 (x, y). Intermediate values may reach ~1.65x due to
            the CORDIC gain before compensation, so hardware needs ~2 guard integer bits.
    angle : ANG_W-bit signed, "full circle = 2**ANG_W" units (pi = 2**(ANG_W-1)).
            ANG_W = 20 (> 16) so the smallest micro-angle arctan(2**-15) stays > 0.
    iters : 16 -> ~16-bit angular precision, matching the datapath.

Two modes:
    rotate(x, y, angle) -> rotates (x,y) by `angle`  ..... the butterfly's W*B
    vector(x, y)        -> returns (|.|, atan2)      ..... the FFT bin magnitude |X[k]|

Pure standard library so it runs on the host and in the devcontainer. Python's `>>` on a
negative int is an arithmetic (floor) shift -- exactly the hardware arithmetic shift.
"""

import math

ITERS = 16                 # CORDIC iterations (~16-bit angular precision)
Q = 15                     # data fractional bits (Q1.15)
ANG_W = 20                 # angle width; full circle = 2**ANG_W
FULL = 1 << ANG_W          # == 2*pi
HALF = 1 << (ANG_W - 1)    # == pi
QUART = 1 << (ANG_W - 2)   # == pi/2

# arctan(2**-i) expressed in angle units.
ATAN = [round(math.atan(2.0 ** -i) * FULL / (2.0 * math.pi)) for i in range(ITERS)]

# CORDIC processing gain after ITERS iterations, and its Q1.15 reciprocal.
_K = 1.0
for _i in range(ITERS):
    _K *= math.sqrt(1.0 + 2.0 ** (-2 * _i))
GAIN = _K                              # ~1.6468
INV_K = round((1.0 / _K) * (1 << Q))   # Q1.15 gain-compensation constant


def _wrap(z):
    """Wrap an angle (in angle units) into [-pi, pi)."""
    return ((z + HALF) % FULL) - HALF


def rotate(x0, y0, angle):
    """Rotation mode: rotate (x0, y0) by `angle` (angle units), gain-compensated.

    Returns (x, y) ints. Positive angle = counter-clockwise. Used for the butterfly's
    W*B (load B into x,y and the twiddle angle into `angle`).
    """
    z = _wrap(angle)
    x, y = x0, y0
    # Quadrant pre-rotation: bring |z| <= pi/2 (CORDIC converges only to ~99.7 deg).
    if z > QUART:            # > +90 deg: rotate vector +90, subtract 90 from z
        x, y = -y, x
        z -= QUART
    elif z < -QUART:         # < -90 deg: rotate vector -90, add 90 to z
        x, y = y, -x
        z += QUART
    for i in range(ITERS):
        d = 1 if z >= 0 else -1        # rotation mode: drive z -> 0
        nx = x - d * (y >> i)
        ny = y + d * (x >> i)
        z -= d * ATAN[i]
        x, y = nx, ny
    # Gain compensation: multiply by 1/K (Q1.15), truncating arithmetic shift.
    return (x * INV_K) >> Q, (y * INV_K) >> Q


def vector(x0, y0):
    """Vectoring mode: return (magnitude, angle) of (x0, y0).

    Magnitude is gain-compensated (= sqrt(x0**2 + y0**2)); angle in angle units
    (= atan2(y0, x0)). Used for the exact FFT bin magnitude |X[k]|.
    """
    x, y, z = x0, y0, 0
    # Pre-rotate into the right half-plane (x >= 0) so vectoring converges.
    if x < 0:
        if y >= 0:
            x, y, z = y, -x, z + QUART     # rotate -90
        else:
            x, y, z = -y, x, z - QUART     # rotate +90
    for i in range(ITERS):
        d = -1 if y >= 0 else 1            # vectoring mode: drive y -> 0
        nx = x - d * (y >> i)
        ny = y + d * (x >> i)
        z -= d * ATAN[i]
        x, y = nx, ny
    return (x * INV_K) >> Q, _wrap(z)


# ---- unit conversions used by callers/tests --------------------------------
def rad_to_ang(theta):
    """Radians -> angle units (rounded)."""
    return round(theta * FULL / (2.0 * math.pi))


def ang_to_rad(a):
    """Angle units -> radians."""
    return a * 2.0 * math.pi / FULL
