# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
spectrum_ref.py -- reference for the magnitude + log step.

*** THIS IS NOW AN MCU-FIRMWARE SPEC, NOT AN RTL SPEC (changed 2026-08-25). ***
The magnitude read-out was removed from the chip -- sharing the one CORDIC between the
butterfly and a vectoring magnitude core cost a mux on every CORDIC operand, which pushed
utilisation to ~80% and broke the GDS render. The MCU already holds the whole spectrum, so
this step is free there. The RTL that used to implement it is kept, out of the build, at
src/attic/spectrum_mag.v. This module is what the RP2350 firmware must reproduce; keep its
self-test (test_spectrum_ref.py) green in CI so the target cannot drift.

For each FFT bin X[k] = re + j*im:
  * magnitude |X[k]| = sqrt(re^2 + im^2) via the CORDIC in VECTORING mode
    (model/cordic.py's vector()). Firmware may instead use an exact integer sqrt or
    max + min/2 -- but then it is no longer bit-exact to this model, so decide which
    definition is authoritative before writing the band mapping against it.
  * log-magnitude = position of the most-significant set bit + LOG_FRAC mantissa bits
    just below it (a cheap piecewise-linear log2). This is the small value the visuals
    map to brightness; a real logarithm is not needed.

Pure standard library, like the other models.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cordic  # noqa: E402

LOG_FRAC = 2                 # mantissa bits kept below the MSB (2 -> log2 resolution 0.25)
LOG_W = 7                    # width of the packed log value {msb, frac} (msb up to XYW-1)


def magnitude(re, im):
    """Exact CORDIC-vectoring bin magnitude -- identical to the RTL's cordic x_out."""
    mag, _ = cordic.vector(re, im)
    return mag


def log2_encode(mag):
    """Piecewise-linear log2 as {MSB index, LOG_FRAC bits below it}; 0 for mag <= 0.

    e.g. mag = 0b1_01... -> msb + the two bits after the leading 1. The packed result
    divided by 2**LOG_FRAC approximates log2(mag).
    """
    mag = int(mag)
    if mag <= 0:
        return 0
    msb = mag.bit_length() - 1
    if msb >= LOG_FRAC:
        frac = (mag >> (msb - LOG_FRAC)) & ((1 << LOG_FRAC) - 1)
    else:
        frac = 0                                     # too few bits below the MSB
    return (msb << LOG_FRAC) | frac


def log_mag(re, im):
    """Full read-out: one complex bin -> its small log-magnitude code."""
    return log2_encode(magnitude(re, im))


def log_to_float(code):
    """Decode {msb, frac} back to the approximate log2 value it stands for."""
    return code / float(1 << LOG_FRAC)
