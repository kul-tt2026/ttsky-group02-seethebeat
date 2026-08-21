# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb full-FFT test: RTL src/fft_ctrl.v (controller + mcu_bus + butterfly + cordic)
driven against the Python golden model model/fft_ref.py.

A background coroutine plays the MCU memory slave (model/mcu_bus_model.py) at the pin
level. We preload the slave's SRAM with the input in BIT-REVERSED order (the MCU firmware's
job for v1), pulse start, wait for done, then read the buffer back and compare it
BIT-EXACT to fft_ref.fft_fixed(). N is set via the Makefile (FFT_LOGN -> -Pfft_ctrl.LOGN).

CAVEAT -- a small N does NOT cover everything. The control logic is the same shape at any
N, but the bus address space is not: word addresses only span 0..2N-1, so below LOGN=9 the
top address bits are never driven (LOGN=6 reaches address 127 only). A bug that drops an
address MSB is therefore invisible at small N -- that is a real bug this test once passed
straight through. CI runs this at FFT_LOGN=9 (the real chip size, addresses 0..1023) as
well as at the fast default.
"""

import math
import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import mcu_bus_model as bus   # noqa: E402
import fft_ref                # noqa: E402


def _signed(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


async def _slave_proc(dut, slave):
    """Play the MCU memory slave at the pins: sample uio[5:0], step, drive the response."""
    while True:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        cmd = int(dut.uio_out.value) & 0x3F
        rv, b = slave.step(cmd)
        dut.uio_in.value = (rv & 1) << 7
        dut.ui_in.value = b & 0xFF


@cocotb.test()
async def test_fft_ctrl(dut):
    try:
        logn = int(dut.LOGN.value)
    except Exception:
        logn = 6
    N = 1 << logn

    # ---- build a structured real input (Q1.15), safely inside [-1, 1) ----
    x_re = []
    for n in range(N):
        v = 0.5 * math.cos(2 * math.pi * 3 * n / N) + 0.25 * math.sin(2 * math.pi * 7 * n / N)
        x_re.append(fft_ref._q15(v))

    # golden output (uses the CORDIC rotation -- exactly what the RTL computes)
    re_exp, im_exp = fft_ref.fft_fixed(x_re, N=N)

    # ---- preload MCU memory: bit-reversed input, interleaved (2i=re, 2i+1=im) ----
    slave = bus.MCUSlave(latency=2)
    br = fft_ref.bitrev_table(N)
    for i in range(N):
        slave.sram[2 * i] = x_re[br[i]] & 0xFFFF
        slave.sram[2 * i + 1] = 0

    # ---- reset ----
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.start.value = 0
    dut.uio_in.value = 0
    dut.ui_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)

    cocotb.start_soon(_slave_proc(dut, slave))

    # ---- run one FFT ----
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0

    for _ in range(400000):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        if int(dut.done.value) == 1:
            break
    else:
        raise AssertionError("fft_ctrl never asserted done")

    # ---- read the buffer back and compare bit-exact ----
    re_got = [_signed(slave.sram.get(2 * i, 0)) for i in range(N)]
    im_got = [_signed(slave.sram.get(2 * i + 1, 0)) for i in range(N)]

    for i in range(N):
        assert re_got[i] == re_exp[i] and im_got[i] == im_exp[i], (
            "bin {} mismatch: got ({},{}) exp ({},{})".format(
                i, re_got[i], im_got[i], re_exp[i], im_exp[i]))
