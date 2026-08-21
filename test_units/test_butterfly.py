# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/butterfly.v vs the Python golden model model/butterfly.py.

Both implement the exact same fixed-point butterfly (A +/- W*B, >>1, saturate), so they
must agree BIT-FOR-BIT. Any mismatch is a real RTL bug.
"""

import math
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import cordic as model_cordic    # noqa: E402
import butterfly as model_bf     # noqa: E402

DW = 16
AW = 20


def _mask(v, w):
    return v & ((1 << w) - 1)


def _signed(v, w):
    v &= (1 << w) - 1
    return v - (1 << w) if v & (1 << (w - 1)) else v


async def _reset(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.start.value = 0
    for sig in ("a_re", "a_im", "b_re", "b_im", "angle"):
        getattr(dut, sig).value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def _run(dut, a_re, a_im, b_re, b_im, angle):
    dut.a_re.value = _mask(a_re, DW)
    dut.a_im.value = _mask(a_im, DW)
    dut.b_re.value = _mask(b_re, DW)
    dut.b_im.value = _mask(b_im, DW)
    dut.angle.value = _mask(angle, AW)
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(50):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            break
    else:
        raise AssertionError("butterfly did not assert done")
    return (_signed(int(dut.a_re_o.value), DW), _signed(int(dut.a_im_o.value), DW),
            _signed(int(dut.b_re_o.value), DW), _signed(int(dut.b_im_o.value), DW))


@cocotb.test()
async def test_butterfly(dut):
    """RTL must match model.butterfly() exactly across angles, quadrants, and full-scale."""
    await _reset(dut)
    vecs = [(15000, 0, 10000, 0),
            (8000, -6000, -9000, 4000),
            (-12000, 10000, 7000, -5000),
            (0, 14000, -3000, -11000),
            (32000, 32000, 32000, 32000),      # exercises the saturation path
            (-32000, 30000, 31000, -29000),
            # full-scale corners incl. -32768 (widest internal magnitude): a 22-bit RTL
            # wrap would diverge from the unbounded model here (review S2).
            (32767, 32767, 32767, 32767),
            (-32768, -32768, -32768, -32768),
            (-32768, 32767, -32768, 32767),
            (32767, -32768, 32767, -32768)]
    for deg in range(-170, 171, 20):
        ang = model_cordic.rad_to_ang(math.radians(deg))
        for (ar, ai, br, bi) in vecs:
            got = await _run(dut, ar, ai, br, bi, ang)
            exp = model_bf.butterfly(ar, ai, br, bi, ang)
            assert got == exp, \
                "butterfly({},{},{},{},{}deg): rtl={} model={}".format(ar, ai, br, bi, deg, got, exp)
