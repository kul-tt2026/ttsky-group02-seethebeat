# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/fft_alu.v -- the ONE shared CORDIC wrapping the butterfly and
spectrum_mag cores. Both operations must be BIT-EXACT to their golden models, proving one
CORDIC serves both:
  op=0 (butterfly) vs model/butterfly.py
  op=1 (magnitude) vs model/spectrum_ref.py   (the bin is fed on b_re/b_im)
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
import spectrum_ref as model_sp   # noqa: E402

DW = 16
AW = 20


def _mask(v, w):
    return v & ((1 << w) - 1)


def _signed(v, w):
    v &= (1 << w) - 1
    return v - (1 << w) if v & (1 << (w - 1)) else v


async def _run(dut, op, a_re, a_im, b_re, b_im, angle):
    dut.op.value = op
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
    bf = (_signed(int(dut.a_re_o.value), DW), _signed(int(dut.a_im_o.value), DW),
          _signed(int(dut.b_re_o.value), DW), _signed(int(dut.b_im_o.value), DW))
    return bf, int(dut.log_mag.value)


@cocotb.test()
async def test_fft_alu(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.start.value = 0
    dut.op.value = 0
    for s in ("a_re", "a_im", "b_re", "b_im", "angle"):
        getattr(dut, s).value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    # ---- op=0: butterfly, bit-exact vs model.butterfly (incl. full-scale corners) ----
    bvecs = [(15000, 0, 10000, 0), (8000, -6000, -9000, 4000),
             (-12000, 10000, 7000, -5000), (0, 14000, -3000, -11000),
             (32767, 32767, 32767, 32767), (-32768, -32768, -32768, -32768),
             (-32768, 32767, -32768, 32767)]
    for deg in range(-170, 171, 20):
        ang = model_cordic.rad_to_ang(math.radians(deg))
        for (ar, ai, br, bi) in bvecs:
            bf, _ = await _run(dut, 0, ar, ai, br, bi, ang)
            exp = model_bf.butterfly(ar, ai, br, bi, ang)
            assert bf == exp, \
                "op0 butterfly({},{},{},{},{}deg): rtl={} model={}".format(ar, ai, br, bi, deg, bf, exp)

    # ---- op=1: magnitude, bit-exact vs model.spectrum_ref (bin on b_re/b_im) ----
    mvecs = [(20000, 0), (12000, 12000), (-9000, 15000), (-14000, -8000), (0, -17000),
             (300, 400), (0, 0), (1, 1), (2, 0), (7, 0),
             (32767, 32767), (-32768, -32768), (-32768, 32767), (0, -32768)]
    for (re, im) in mvecs:
        _, log = await _run(dut, 1, 0, 0, re, im, 0)
        exp = model_sp.log_mag(re, im)
        assert log == exp, "op1 log_mag({},{}): rtl={} model={}".format(re, im, log, exp)
