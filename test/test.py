# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles


@cocotb.test()
async def test_reset_and_idle(dut):
    """smoke test. no functional logic yet"""

    dut._log.info("start smoke test")

    # 40 MHz target clock -> 25 ns period
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())

    # reset 
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)

    # skeleton makes every output low and keeps bidertional pins as inputs
    assert dut.uo_out.value == 0, f"uo_out should be zero, got {dut.uo_out.value}"
    assert dut.uio_out.value == 0, f"uio_out should be zero, got {dut.uio_out.value}"
    assert dut.uio_oe.value == 0, f"uio_oe should be zero, got {dut.uio_oe.value}"

    # poke at inputs, skeleton should not react
    dut.ui_in.value = 0xA5
    dut.uio_in.value = 0x5A
    await ClockCycles(dut.clk, 3)
    assert dut.uo_out.value == 0, "skeleton should stay idle"
    assert dut.uio_oe.value == 0, "skeleton must keep bidirectional bus in input mode"

    dut._log.info("smoke test passed")