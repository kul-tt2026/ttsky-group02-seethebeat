# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Top-level smoke test for tt_um_group02_seethebeat.

Confirms the integrated design elaborates, resets cleanly, configures the MCU bus, stays
parked while no frame-ready is asserted, AND drives a live VGA signal on uo_out.

Deep functional verification lives in test_units/ against the golden models; this is the
whole-chip sanity check, and it is also what runs at gate level (GATES=yes).

The assertions below are deliberately STRUCTURAL rather than pixel-exact -- they check the
blanking gate and the sync position, which must hold for every visual we ever put on this
chip, so Phase 1+ can replace the test pattern without touching this file.
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

# VGA 800x600@60 -- see model/vga_ref.py (the authoritative copy of these numbers)
H_VIS = 800
H_SYNC_ON = 840          # 800 visible + 40 front porch
COLOUR_BITS = 0x77       # uo_out[6:4] = {B0,G0,R0}, uo_out[2:0] = {B1,G1,R1}
HSYNC_BIT = 0x80
VSYNC_BIT = 0x08


@cocotb.test()
async def test_reset_and_idle(dut):
    dut._log.info("start smoke test")

    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())   # 40 MHz pixel clock

    # reset; frame-ready (uio_in[6]) held low so the FFT engine does not start
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    # counters are at (0,0) here; every ClockCycles below advances hcount by that many

    # ---- MCU bus is configured and parked ----
    assert dut.uio_oe.value == 0x3F, f"uio_oe should be 0x3F, got {dut.uio_oe.value}"
    assert dut.uio_out.value == 0, f"idle uio_out should be 0 (NOP), got {dut.uio_out.value}"

    # ---- VGA: both syncs idle low (800x600 is positive polarity) ----
    uo = int(dut.uo_out.value)
    assert uo & HSYNC_BIT == 0, "hsync must idle LOW (positive polarity)"
    assert uo & VSYNC_BIT == 0, "vsync must idle LOW (positive polarity)"

    # ---- the blanking gate: no light in the horizontal front porch ----
    await ClockCycles(dut.clk, H_VIS)                 # hcount = 800, first blanked pixel
    uo = int(dut.uo_out.value)
    assert uo & COLOUR_BITS == 0, (
        f"colour must be BLACK during blanking, got {uo:#04x} at hcount={H_VIS}")
    assert uo & HSYNC_BIT == 0, "hsync has not started yet at hcount=800"

    # ---- hsync fires at exactly hcount = 840 ----
    await ClockCycles(dut.clk, H_SYNC_ON - H_VIS - 1)  # hcount = 839, last pre-sync clock
    assert int(dut.uo_out.value) & HSYNC_BIT == 0, "hsync asserted one clock too early"
    await ClockCycles(dut.clk, 1)                      # hcount = 840
    uo = int(dut.uo_out.value)
    assert uo & HSYNC_BIT != 0, f"hsync must be asserted at hcount={H_SYNC_ON}"
    assert uo & COLOUR_BITS == 0, "still blanking during the sync pulse"

    # ---- poking read-data must not disturb an un-triggered engine ----
    dut.ui_in.value = 0xA5
    await ClockCycles(dut.clk, 10)
    assert dut.uio_oe.value == 0x3F, "bus direction must stay 0x3F"
    assert dut.uio_out.value == 0, "engine must stay parked without frame-ready"

    dut._log.info("smoke test passed")
