# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/mcu_bus.v (the on-chip bus master) driven against the Python
golden model model/mcu_bus_model.py (the MCU memory slave).

A background coroutine plays the slave: every clock it reads the master's uio[5:0], steps
the model, and drives back resp_valid (uio[7]) + the read byte (ui_in). Because the master
waits on resp_valid, the 1-cycle modelling delay is harmless. The test then checks
write->read round-trips, including a pipelined burst (several reads outstanding at once).
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer, ReadOnly

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import mcu_bus_model as bus   # noqa: E402


def _pattern(addr):
    return (addr * 40503 + 0x1234) & 0xFFFF


async def _reset(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rd_req.value = 0
    dut.wr_req.value = 0
    dut.rd_addr.value = 0
    dut.wr_addr.value = 0
    dut.wr_data.value = 0
    dut.uio_in.value = 0
    dut.ui_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def _slave_proc(dut, slave):
    """Play the MCU memory slave: sample uio[5:0], step the model, drive its response."""
    while True:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")                 # let combinational uio_out settle
        cmd = int(dut.uio_out.value) & 0x3F
        rv, b = slave.step(cmd)
        dut.uio_in.value = (rv & 1) << 7          # resp_valid on bit 7
        dut.ui_in.value = b & 0xFF


async def _rd_monitor(dut, out):
    """Collect read words in the order the master returns them."""
    while True:
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        if int(dut.rd_valid.value) == 1:
            out.append(int(dut.rd_data.value))


async def _write(dut, addr, data):
    # valid/ready handshake: sample accept at end-of-cycle (ReadOnly); the *next* edge,
    # where req & accept are both high, is the single commit -- avoids a double-commit.
    dut.wr_addr.value = addr
    dut.wr_data.value = data
    dut.wr_req.value = 1
    await ReadOnly()
    while int(dut.wr_accept.value) != 1:
        await RisingEdge(dut.clk)
        await ReadOnly()
    await RisingEdge(dut.clk)                      # commit edge
    dut.wr_req.value = 0
    await ClockCycles(dut.clk, 6)                  # let the 5-transfer write drain


async def _issue_reads(dut, addrs):
    """Issue reads back-to-back (rd_req stays high) -> exercises pipelining."""
    dut.rd_req.value = 1
    for a in addrs:
        dut.rd_addr.value = a
        await ReadOnly()                          # sample accept before the commit edge
        while int(dut.rd_accept.value) != 1:
            await RisingEdge(dut.clk)
            await ReadOnly()
        await RisingEdge(dut.clk)                  # commit this read (exactly once)
    dut.rd_req.value = 0


async def _wait_for(dut, cond, limit=2000):
    for _ in range(limit):
        if cond():
            return
        await RisingEdge(dut.clk)
    raise AssertionError("timeout waiting for condition")


@cocotb.test()
async def test_mcu_bus(dut):
    slave = bus.MCUSlave(latency=3)
    await _reset(dut)
    cocotb.start_soon(_slave_proc(dut, slave))
    results = []
    cocotb.start_soon(_rd_monitor(dut, results))

    addrs = [5, 700, 1023, 0, 37, 512]            # includes addresses > 511 (10-bit)

    # ---- writes land in the model's SRAM ----
    for a in addrs:
        await _write(dut, a, _pattern(a))
    for a in addrs:
        assert slave.sram[a] == _pattern(a), \
            "write addr {}: sram={} exp={}".format(a, slave.sram.get(a), _pattern(a))

    # ---- single reads return the written pattern, in order ----
    await _issue_reads(dut, addrs)
    await _wait_for(dut, lambda: len(results) >= len(addrs))
    exp = [_pattern(a) for a in addrs]
    assert results[:len(addrs)] == exp, "single reads: got {} exp {}".format(results, exp)

    # ---- pipelined burst of 4 (MAX_OUTSTANDING) returns correct words in order ----
    results.clear()
    burst = [20, 21, 900, 901]                    # one butterfly's A_re,A_im,B_re,B_im
    for a in burst:
        await _write(dut, a, _pattern(a ^ 0x55))
    await _issue_reads(dut, burst)
    await _wait_for(dut, lambda: len(results) >= len(burst))
    exp_b = [_pattern(a ^ 0x55) for a in burst]
    assert results[:len(burst)] == exp_b, "burst reads: got {} exp {}".format(results, exp_b)
