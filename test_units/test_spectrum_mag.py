# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/spectrum_mag.v vs the Python golden model model/spectrum_ref.py.

Both compute |X| via CORDIC vectoring and the same MSB-position log, so they must agree
BIT-FOR-BIT. Any mismatch is a real RTL bug.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import spectrum_ref as model  # noqa: E402

DW = 16


def _mask(v, w):
    return v & ((1 << w) - 1)


async def _reset(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.start.value = 0
    dut.re.value = 0
    dut.im.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def _run(dut, re, im):
    dut.re.value = _mask(re, DW)
    dut.im.value = _mask(im, DW)
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0
    for _ in range(40):
        await RisingEdge(dut.clk)
        if dut.done.value == 1:
            break
    else:
        raise AssertionError("spectrum_mag did not assert done")
    return int(dut.log_mag.value)


@cocotb.test()
async def test_spectrum_mag(dut):
    """log_mag must match model.log_mag() exactly across magnitudes, quadrants, corners."""
    await _reset(dut)
    vecs = [(20000, 0), (12000, 12000), (-9000, 15000), (-14000, -8000), (0, -17000),
            (5000, -5000), (300, 400), (100, -50),
            (0, 0), (1, 1), (2, 0), (3, 0), (7, 0),        # small-magnitude log edge cases
            (32767, 32767), (-32768, -32768), (32767, -32768), (-32768, 32767),
            (-32768, 0), (0, -32768)]                       # full-scale corners
    for (re, im) in vecs:
        got = await _run(dut, re, im)
        exp = model.log_mag(re, im)
        assert got == exp, "log_mag({},{}): rtl={} model={}".format(re, im, got, exp)
