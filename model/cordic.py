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

GAIN COMPENSATION IS MULTIPLIER-FREE. The CORDIC's processing gain K is
undone by SCALE_STEPS -- a short chain of `x <- x +/- (x >> p)` scaling iterations whose
product approximates 1/K -- instead of a Q1.15 multiply by INV_K. In hardware these steps
reuse the very same adders and shifters as the rotation iterations, so the two constant
multipliers disappear for the price of a few extra clock cycles (we have cycles in huge
surplus). Because a chain of shift-adds truncates once per step, the
datapath carries GUARD extra fractional bits during the whole operation, which also
protects the 16 rotation iterations -- so this is both smaller AND more accurate than
the multiply it replaces (~+4..6 dB end-to-end FFT SNR).

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
INV_K = round((1.0 / _K) * (1 << Q))   # Q1.15 1/K -- the exact target, NOT multiplied by

# ---- multiplier-free gain compensation -------------------------------------
# 1/K is realised as a product of (1 +/- 2**-p) factors, one shift-add each:
#     (1 - 2^-1)(1 + 2^-2)(1 - 2^-5)(1 + 2^-9)(1 + 2^-10) = 0.60724374
# vs the exact 1/K = 0.60725294  ->  relative error 1.5e-5, far below one Q1.15 LSB.
# (p, s): x <- x + s * (x >> p). Exhaustively searched over p in 1..12, 2..5 factors;
# this is the best 5-factor set. Fewer factors are too coarse (4 -> 4.1 LSB rms error),
# more factors add truncation faster than they add accuracy.
SCALE_STEPS = [(1, -1), (2, +1), (5, -1), (9, +1), (10, +1)]

# Extra fractional bits carried through the whole operation so the chained shift-adds
# (and the rotation iterations) do not lose a bit each. GUARD = 3 keeps the worst-case
# internal magnitude at 610,505 (reached at x = y = -32768) -> 21 signed bits, inside the
# RTL's XYW = 22 datapath, i.e. exactly one bit of margin. Verified by exhaustive search
# over all 2**20 angles x the four full-scale corners.
# GUARD = 4 would need exactly 22 bits, leaving no margin -- do not raise it without
# widening XYW.
#
# NOTE this model deliberately uses unbounded Python ints (no masking anywhere), while the
# RTL registers are XYW bits wide. That asymmetry is what makes the RTL-vs-model bit-exact
# cocotb test an overflow detector: if the RTL ever wraps, the two diverge. Keep it that
# way -- masking here would silently destroy that property.
GUARD = 3

# Achieved compensation factor (for documentation / self-checks).
SCALE_GAIN = 1.0
for _p, _s in SCALE_STEPS:
    SCALE_GAIN *= 1.0 + _s * 2.0 ** -_p


def _wrap(z):
    """Wrap an angle (in angle units) into [-pi, pi)."""
    return ((z + HALF) % FULL) - HALF


def _compensate(x, y):
    """Undo the CORDIC gain with shift-adds only, then drop the guard bits.

    Mirrors the RTL's scaling states exactly: one `+/- (v >> p)` per step, each with an
    arithmetic (floor) shift, then a final arithmetic `>> GUARD`.
    """
    for p, s in SCALE_STEPS:
        x += s * (x >> p)
        y += s * (y >> p)
    return x >> GUARD, y >> GUARD


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
    x <<= GUARD              # enter the guarded datapath (exact, just a left shift)
    y <<= GUARD
    for i in range(ITERS):
        d = 1 if z >= 0 else -1        # rotation mode: drive z -> 0
        nx = x - d * (y >> i)
        ny = y + d * (x >> i)
        z -= d * ATAN[i]
        x, y = nx, ny
    return _compensate(x, y)


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
    x <<= GUARD
    y <<= GUARD
    for i in range(ITERS):
        d = -1 if y >= 0 else 1            # vectoring mode: drive y -> 0
        nx = x - d * (y >> i)
        ny = y + d * (x >> i)
        z -= d * ATAN[i]
        x, y = nx, ny
    mag, _ = _compensate(x, y)
    return mag, _wrap(z)


# ---- unit conversions used by callers/tests --------------------------------
def rad_to_ang(theta):
    """Radians -> angle units (rounded)."""
    return round(theta * FULL / (2.0 * math.pi))


def ang_to_rad(a):
    """Angle units -> radians."""
    return a * 2.0 * math.pi / FULL
