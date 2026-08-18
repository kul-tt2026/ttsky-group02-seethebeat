# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/cordic.v vs the Python golden model model/cordic.py.

Both implement the exact same fixed-point algorithm, so they must agree BIT-FOR-BIT.
Any mismatch is a real RTL bug.
"""

import math
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import cordic as model  # noqa: E402

XYW = 22
AW = 20


def _mask(v, w):
    return v & ((1 << w) - 1)


def _signed(v, w):
    v &= (1 << w) - 1
    return v - (1 << w) if v & (1 << (w - 1)) else v


async def _reset(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.start.value = 0
    dut.mode.value = 0
    dut.x_in.value = 0
    dut.y_in.value = 0
    dut.ang_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def _run(dut, mode, x, y, ang):
    dut.mode.value = mode
    dut.x_in.value = _mask(x, 16)
    dut.y_in.value = _mask(y, 16)
    dut.ang_in.value = _mask(ang, AW)
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(40):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            break
    else:
        raise AssertionError("CORDIC did not assert done")
    return (_signed(int(dut.x_out.value), XYW),
            _signed(int(dut.y_out.value), XYW),
            _signed(int(dut.ang_out.value), AW))


@cocotb.test()
async def test_rotate(dut):
    """ROTATE mode must match model.rotate() exactly across angles and quadrants."""
    await _reset(dut)
    vecs = [(20000, 0), (10000, 5000), (-8000, 12000), (-15000, -3000), (0, 18000)]
    for deg in range(-170, 171, 10):
        ang = model.rad_to_ang(math.radians(deg))
        for (x, y) in vecs:
            xo, yo, _ = await _run(dut, 0, x, y, ang)
            mx, my = model.rotate(x, y, ang)
            assert (xo, yo) == (mx, my), \
                "rotate({},{},{}deg): rtl=({},{}) model=({},{})".format(x, y, deg, xo, yo, mx, my)


@cocotb.test()
async def test_vector(dut):
    """VECTOR mode: magnitude and angle must match model.vector() exactly."""
    await _reset(dut)
    vecs = [(20000, 0), (12000, 12000), (-9000, 15000), (-14000, -8000), (0, -17000), (5000, -5000)]
    for (x, y) in vecs:
        xo, _, ao = await _run(dut, 1, x, y, 0)
        mmag, mang = model.vector(x, y)
        assert xo == mmag, "vector({},{}): |.| rtl={} model={}".format(x, y, xo, mmag)
        assert ao == mang, "vector({},{}): ang rtl={} model={}".format(x, y, ao, mang)
