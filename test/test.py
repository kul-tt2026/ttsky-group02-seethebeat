# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Top-level smoke test for tt_um_group02_seethebeat.

Confirms the integrated FFT engine elaborates, resets cleanly, configures the bus, and
stays parked while no frame-ready is asserted (no MCU slave is modelled here -- the deep
functional verification lives in test_units/ against the golden models).
"""

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


@cocotb.test()
async def test_reset_and_idle(dut):
    dut._log.info("start smoke test")

    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())

    # reset; frame-ready (uio_in[6]) held low so the engine does not start
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    # bus is configured: uio[5:0] outputs, uio[7:6] inputs
    assert dut.uio_oe.value == 0x3F, f"uio_oe should be 0x3F, got {dut.uio_oe.value}"
    # parked (no frame-ready): master drives NOP (0) on the command lane, status idle
    assert dut.uio_out.value == 0, f"idle uio_out should be 0 (NOP), got {dut.uio_out.value}"
    assert dut.uo_out.value == 0, f"idle uo_out should be 0, got {dut.uo_out.value}"

    # poking read-data must not disturb an idle (un-triggered) engine
    dut.ui_in.value = 0xA5
    await ClockCycles(dut.clk, 10)
    assert dut.uio_oe.value == 0x3F, "bus direction must stay 0x3F"
    assert dut.uio_out.value == 0, "engine must stay parked without frame-ready"

    dut._log.info("smoke test passed")
