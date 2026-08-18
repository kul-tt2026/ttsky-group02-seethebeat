"""
butterfly.py -- bit-accurate golden model of the radix-2 DIT butterfly.

    A' = A + W*B,   B' = A - W*B

W*B is computed by the CORDIC (rotate B by the twiddle angle), then a per-stage
scale (>>1, truncation) and SATURATION to Q1.15 (clip, not wrap).

This is the single source of truth for one butterfly: fft_ref.py calls it for every
butterfly, and src/butterfly.v matches it bit-for-bit.
Pure standard library.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cordic  # noqa: E402  (same-dir sibling)

Q = cordic.Q                  # 15
INT16_MIN = -(1 << 15)        # -32768
INT16_MAX = (1 << 15) - 1     #  32767


def sat(v):
    """Saturate to the Q1.15 int16 range (clip, not wrap)."""
    if v > INT16_MAX:
        return INT16_MAX
    if v < INT16_MIN:
        return INT16_MIN
    return v


def butterfly(a_re, a_im, b_re, b_im, angle):
    """One radix-2 DIT butterfly.

    Args (Q1.15 ints; `angle` in CORDIC angle units, i.e. the twiddle -2*pi*k/N):
        a_re, a_im : complex input A
        b_re, b_im : complex input B
        angle      : twiddle rotation angle for W*B
    Returns (a_re_o, a_im_o, b_re_o, b_im_o) = A' then B', each scaled (>>1) & saturated.
    """
    t_re, t_im = cordic.rotate(b_re, b_im, angle)     # t = W*B (gain-compensated)
    a_re_o = sat((a_re + t_re) >> 1)
    a_im_o = sat((a_im + t_im) >> 1)
    b_re_o = sat((a_re - t_re) >> 1)
    b_im_o = sat((a_im - t_im) >> 1)
    return a_re_o, a_im_o, b_re_o, b_im_o
