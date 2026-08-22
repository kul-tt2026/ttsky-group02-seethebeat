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

Scenarios (review S1/S2/S5): the two-tone case runs at every N (it is the address-coverage
run); the heavier cases -- full-scale-with-(-32768) for end-to-end saturation, an impulse,
varied MCU latency, and a stuck-MCU park+reset-recovery -- run only at the fast small N,
because they exercise control/datapath behaviour that is N-independent.
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
import spectrum_ref           # noqa: E402


def _signed(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


# ---- input builders (Q1.15) ----
def _twotone(N):
    return [fft_ref._q15(0.5 * math.cos(2 * math.pi * 3 * n / N)
                         + 0.25 * math.sin(2 * math.pi * 7 * n / N)) for n in range(N)]


def _fullscale(N):
    # full-amplitude Nyquist square wave, INCLUDING -32768: the widest internal magnitudes,
    # so the butterfly saturation path actually fires end-to-end (review S2/S5).
    return [32767 if (n % 2 == 0) else -32768 for n in range(N)]


def _impulse(N):
    x = [0] * N
    x[0] = 32767
    return x


async def _slave_proc(dut, st):
    """Play the MCU memory slave at the pins. `st` holds the live slave + a stall flag so
    scenarios can swap the memory or make the MCU 'go stuck' (stop answering)."""
    while True:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        sl = st["slave"]
        if sl is None or not dut.uio_out.value.is_resolvable:
            continue
        cmd = int(dut.uio_out.value) & 0x3F
        rv, b = sl.step(cmd)
        if st["stalled"]:
            rv, b = 0, 0                          # stuck MCU: withhold resp_valid
        dut.uio_in.value = (rv & 1) << 7
        dut.ui_in.value = b & 0xFF


def _load(N, x_re, latency):
    slave = bus.MCUSlave(latency=latency)
    br = fft_ref.bitrev_table(N)
    for i in range(N):
        slave.sram[2 * i] = x_re[br[i]] & 0xFFFF   # bit-reversed, interleaved 2i/2i+1
        slave.sram[2 * i + 1] = 0
    return slave


async def _reset(dut):
    dut.start.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def _go(dut):
    dut.start.value = 1
    await RisingEdge(dut.clk)
    dut.start.value = 0


async def _wait_done(dut, limit=400000):
    for _ in range(limit):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        if int(dut.done.value) == 1:
            return
    raise AssertionError("fft_ctrl never asserted done")


def _check(slave, x_re, N):
    re_exp, im_exp = fft_ref.fft_fixed(x_re, N=N)
    for i in range(N):
        re_got = _signed(slave.sram.get(2 * i, 0))
        im_got = _signed(slave.sram.get(2 * i + 1, 0))
        assert re_got == re_exp[i] and im_got == im_exp[i], (
            "bin {} mismatch: got ({},{}) exp ({},{})".format(
                i, re_got, im_got, re_exp[i], im_exp[i]))


async def _mag_monitor(dut, out):
    """Capture the magnitude read-out stream (log_mag on each mag_valid)."""
    while True:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        v = dut.mag_valid.value
        if v.is_resolvable and int(v) == 1:
            out.append(int(dut.mag_data.value))


def _expected_mags(x_re, N):
    """The N/2 log-magnitudes the read-out should emit = log2(|X[k]|) of the FFT output."""
    re_e, im_e = fft_ref.fft_fixed(x_re, N=N)
    return [spectrum_ref.log_mag(re_e[k], im_e[k]) for k in range(N // 2)]


async def _scenario(dut, st, N, x_re, latency):
    st["stalled"] = False
    st["slave"] = _load(N, x_re, latency)
    mags = []
    mon = cocotb.start_soon(_mag_monitor(dut, mags))
    await _reset(dut)
    await _go(dut)
    await _wait_done(dut)
    _check(st["slave"], x_re, N)                       # FFT buffer bit-exact
    mon.kill()
    exp = _expected_mags(x_re, N)                      # magnitude read-out stream bit-exact
    assert mags == exp, "mag stream mismatch: got {} exp {} (len {} vs {})".format(
        mags[:8], exp[:8], len(mags), len(exp))


@cocotb.test()
async def test_fft_ctrl(dut):
    try:
        logn = int(dut.LOGN.value)
    except Exception:
        logn = 6
    N = 1 << logn

    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    st = {"slave": None, "stalled": False}
    cocotb.start_soon(_slave_proc(dut, st))

    # two-tone: the address-coverage run, executed at every N (incl. full-size 512).
    await _scenario(dut, st, N, _twotone(N), latency=2)

    if N > 128:
        return  # keep the slow full-size run to one FFT; behaviour cases run at small N

    # full-scale (incl. -32768) -> saturation fires end-to-end; also a higher MCU latency
    await _scenario(dut, st, N, _fullscale(N), latency=5)
    # impulse (flat spectrum) at zero MCU latency -> varies the handshake timing
    await _scenario(dut, st, N, _impulse(N), latency=0)

    # ---- stuck-MCU park + reset recovery (review S1) ----
    st["stalled"] = False
    st["slave"] = _load(N, _twotone(N), latency=2)
    await _reset(dut)
    await _go(dut)
    await ClockCycles(dut.clk, 300)              # run well into the FFT
    assert int(dut.done.value) == 0, "done asserted far too early"
    st["stalled"] = True                          # MCU goes stuck mid-transform
    await ClockCycles(dut.clk, 800)
    assert int(dut.done.value) == 0, "chip did NOT stall on a stuck MCU (should park)"
    # recover: reset chip + MCU, reload fresh input, and a clean FFT must complete correctly
    await _scenario(dut, st, N, _twotone(N), latency=2)
