# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Top-level smoke test for tt_um_group02_seethebeat.

Confirms the integrated design elaborates, resets cleanly, configures the MCU bus, stays
parked while no frame-ready is asserted, AND drives a live VGA signal on uo_out.

Deep functional verification lives in test_units/ against the golden models; this is the
whole-chip sanity check, and it is also what runs at gate level (GATES=yes).

The VGA checks are deliberately PHASE-INDEPENDENT: they walk a whole scanline and assert
invariants over it (the sync pulse is 128 clocks wide; the colour is black for every clock
the sync is asserted) rather than asserting what happens at one exact cycle. An earlier
version asserted "hsync is still low at hcount=839", which was brittle for two reasons --
it depended on reset deasserting on exactly the right edge, and it read combinational
outputs with no settle delay. Exact per-cycle timing is verified properly in
test_units/test_vga_timing.py, against the golden model, over a full frame.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer

# VGA 800x600@60 -- see model/vga_ref.py for the authoritative copy of these numbers
H_TOTAL = 1056
H_SYNC_W = 128
COLOUR_BITS = 0x77       # uo_out[6:4] = {B0,G0,R0}, uo_out[2:0] = {B1,G1,R1}
HSYNC_BIT = 0x80
VSYNC_BIT = 0x08


async def _settle():
    """Let combinational outputs resolve after a clock edge before sampling them."""
    await Timer(1, unit="ns")


@cocotb.test()
async def test_reset_and_idle(dut):
    dut._log.info("start smoke test")

    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())   # 40 MHz pixel clock

    # frame-ready (uio_in[6]) held low so the FFT engine never starts
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0

    # Deassert reset AFTER an edge, not on one: assigning rst_n at the same simulation
    # instant as a rising edge races with it and can shift the counters by a cycle.
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    await _settle()
    dut.rst_n.value = 1
    await _settle()

    # ---- MCU bus is configured and parked ----
    assert dut.uio_oe.value == 0x3F, f"uio_oe should be 0x3F, got {dut.uio_oe.value}"
    assert dut.uio_out.value == 0, f"idle uio_out should be 0 (NOP), got {dut.uio_out.value}"

    # ---- both syncs idle low (800x600 is positive polarity) ----
    uo = int(dut.uo_out.value)
    assert uo & HSYNC_BIT == 0, "hsync must idle LOW (positive polarity)"
    assert uo & VSYNC_BIT == 0, "vsync must idle LOW (positive polarity)"

    # ---- walk one complete scanline and check the invariants over it ----
    hsync_clocks = 0
    lit_clocks = 0
    for i in range(H_TOTAL):
        uo = int(dut.uo_out.value)
        if uo & HSYNC_BIT:
            hsync_clocks += 1
            # the blanking gate: no light anywhere in the sync pulse
            assert uo & COLOUR_BITS == 0, (
                f"colour must be BLACK while hsync is asserted, got {uo:#04x} at clock {i}")
        elif uo & COLOUR_BITS:
            lit_clocks += 1
        assert uo & VSYNC_BIT == 0, f"vsync must stay low during line 0, got {uo:#04x}"
        await ClockCycles(dut.clk, 1)
        await _settle()

    assert hsync_clocks == H_SYNC_W, (
        f"hsync asserted for {hsync_clocks} clocks in a line, expected {H_SYNC_W}")
    assert lit_clocks > 0, (
        "no lit pixels in the whole first scanline -- the renderer is not driving uo_out")
    dut._log.info("scanline OK: hsync %d clocks, %d lit pixels", hsync_clocks, lit_clocks)

    # ---- poking read-data must not disturb an un-triggered engine ----
    dut.ui_in.value = 0xA5
    await ClockCycles(dut.clk, 10)
    await _settle()
    assert dut.uio_oe.value == 0x3F, "bus direction must stay 0x3F"
    assert dut.uio_out.value == 0, "engine must stay parked without frame-ready"

    dut._log.info("smoke test passed")
