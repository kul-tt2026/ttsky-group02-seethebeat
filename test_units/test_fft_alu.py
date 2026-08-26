# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/fft_alu.v -- the FFT butterfly plus THE one CORDIC.
Must be BIT-EXACT to model/butterfly.py, including the full-scale corners where the
saturation path fires (the CORDIC can exceed |B| by ~1 LSB).

NOTE (2026-08-25): this file used to also drive op=1 against model/spectrum_ref.py, when
fft_alu multiplexed the CORDIC between the butterfly and a magnitude core. That core moved
to MCU firmware (see src/attic/spectrum_mag.v) and the mux went with it, so fft_alu now has
no `op` input. The magnitude function is still verified -- in Python, by
model/test_spectrum_ref.py, which CI runs and which the firmware must match bit-for-bit.
"""

import math
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import cordic as model_cordic     # noqa: E402
import butterfly as model_bf      # noqa: E402

DW = 16
AW = 20


def _mask(v, w):
    return v & ((1 << w) - 1)


def _signed(v, w):
    v &= (1 << w) - 1
    return v - (1 << w) if v & (1 << (w - 1)) else v


async def _run(dut, a_re, a_im, b_re, b_im, angle):
    dut.a_re.value = _mask(a_re, DW)
    dut.a_im.value = _mask(a_im, DW)
    dut.b_re.value = _mask(b_re, DW)
    dut.b_im.value = _mask(b_im, DW)
    dut.angle.value = _mask(angle, AW)
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(40):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            break
    else:
        raise AssertionError("fft_alu did not assert done")
    return (_signed(int(dut.a_re_o.value), DW), _signed(int(dut.a_im_o.value), DW),
            _signed(int(dut.b_re_o.value), DW), _signed(int(dut.b_im_o.value), DW))


@cocotb.test()
async def test_fft_alu(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.start.value = 0
    for s in ("a_re", "a_im", "b_re", "b_im", "angle"):
        getattr(dut, s).value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    # butterfly, bit-exact vs model.butterfly (incl. full-scale corners)
    bvecs = [(15000, 0, 10000, 0), (8000, -6000, -9000, 4000),
             (-12000, 10000, 7000, -5000), (0, 14000, -3000, -11000),
             (32767, 32767, 32767, 32767), (-32768, -32768, -32768, -32768),
             (-32768, 32767, -32768, 32767)]
    for deg in range(-170, 171, 20):
        ang = model_cordic.rad_to_ang(math.radians(deg))
        for (ar, ai, br, bi) in bvecs:
            bf = await _run(dut, ar, ai, br, bi, ang)
            exp = model_bf.butterfly(ar, ai, br, bi, ang)
            assert bf == exp, \
                "butterfly({},{},{},{},{}deg): rtl={} model={}".format(ar, ai, br, bi, deg, bf, exp)
