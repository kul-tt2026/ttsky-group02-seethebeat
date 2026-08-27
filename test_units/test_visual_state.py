# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/visual_state.v vs model/visual_ref.py's VisualState.

This is the only visual state on the chip and the biggest area knob in Part 2, so the
things worth proving are: the power-on defaults are exactly right (they are the bring-up
picture, and firmware may never write at all during early board tests), writes land where
addressed and nowhere else, and the reserved address space is genuinely inert.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import visual_ref as V  # noqa: E402

NB = V.NBANDS


async def _reset(dut):
    dut.wr_en.value = 0
    dut.wr_addr.value = 0
    dut.wr_data.value = 0
    dut.rd_zone.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await Timer(1, unit="ns")


async def _read(dut, zone):
    dut.rd_zone.value = zone
    await Timer(1, unit="ns")
    return int(dut.band.value)


async def _write(dut, addr, data):
    dut.wr_en.value = 1
    dut.wr_addr.value = addr
    dut.wr_data.value = data
    await RisingEdge(dut.clk)
    dut.wr_en.value = 0
    await Timer(1, unit="ns")


async def _dump(dut):
    return [await _read(dut, z) for z in range(NB)]


@cocotb.test()
async def test_power_on_defaults(dut):
    """The defaults ARE the bring-up pattern: with no firmware at all the chip must still
    draw every zone, at differing heights."""
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    got = await _dump(dut)
    assert got == V.DEFAULT_BANDS, "reset ramp {} != model {}".format(got, V.DEFAULT_BANDS)
    assert int(dut.flash.value) == V.DEFAULT_FLASH
    assert all(v > 0 for v in got), "a zero default would leave that zone dark on power-up"
    assert len(set(got)) == NB, "defaults must differ per zone, or geometry is unreadable"


@cocotb.test()
async def test_write_readback_every_band(dut):
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    ref = V.VisualState()
    # a value that is distinct per band and exercises both halves of the range
    for z in range(NB):
        val = (z * 7 + 3) & V.BAND_MAX
        await _write(dut, z, val)
        ref.write(z, val)
    got = await _dump(dut)
    assert got == ref.bands, "rtl={} model={}".format(got, ref.bands)


@cocotb.test()
async def test_write_touches_only_its_own_band(dut):
    """A decode bug that writes two bands is invisible in a sequential fill, so check it
    directly: write one band and prove every other one is untouched."""
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    before = await _dump(dut)
    target = 9
    await _write(dut, target, 0)                 # 0 is distinct from every default
    after = await _dump(dut)
    for z in range(NB):
        if z == target:
            assert after[z] == 0, "target band not written"
        else:
            assert after[z] == before[z], "band {} changed from {} to {}".format(
                z, before[z], after[z])


@cocotb.test()
async def test_flash_port(dut):
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    bands_before = await _dump(dut)
    for val in (1, 17, V.BAND_MAX, 0):
        await _write(dut, V.VisualState.ADDR_FLASH, val)
        assert int(dut.flash.value) == val, "flash={} expected {}".format(
            int(dut.flash.value), val)
    assert await _dump(dut) == bands_before, "writing flash must not disturb the bands"


@cocotb.test()
async def test_reserved_addresses_are_inert(dut):
    """Addresses above the flash slot are reserved for later config bytes. Until they mean
    something they must do nothing -- not alias onto a band."""
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    before = await _dump(dut)
    flash_before = int(dut.flash.value)
    max_addr = (1 << len(dut.wr_addr.value)) - 1
    for addr in range(V.VisualState.ADDR_FLASH + 1, max_addr + 1):
        await _write(dut, addr, V.BAND_MAX)
    assert await _dump(dut) == before, "a reserved-address write aliased onto a band"
    assert int(dut.flash.value) == flash_before, "a reserved write hit flash"


@cocotb.test()
async def test_writes_need_wr_en(dut):
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    before = await _dump(dut)
    dut.wr_en.value = 0
    dut.wr_addr.value = 3
    dut.wr_data.value = 0
    await ClockCycles(dut.clk, 4)
    await Timer(1, unit="ns")
    assert await _dump(dut) == before, "state changed with wr_en low"


@cocotb.test()
async def test_config_register(dut):
    """CFG address 17 holds the look config. It must reset to ZERO (= classic look), accept
    writes, and not be disturbed by band or flash traffic."""
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    assert int(dut.cfg.value) == 0, "cfg must reset to 0 (= behave as before)"

    bands_before = await _dump(dut)
    for val in (0b00001, 0b10110, V.BAND_MAX, 0):
        await _write(dut, V.VisualState.ADDR_CFG, val)
        assert int(dut.cfg.value) == val, "cfg={} expected {}".format(
            int(dut.cfg.value), val)
    assert await _dump(dut) == bands_before, "writing cfg disturbed the bands"

    # a band write must not disturb cfg
    await _write(dut, V.VisualState.ADDR_CFG, 0b10101)
    await _write(dut, 3, 7)
    assert int(dut.cfg.value) == 0b10101, "a band write clobbered cfg"
    assert int(dut.flash.value) == 0, "a band write clobbered flash"


@cocotb.test()
async def test_second_config_register(dut):
    """CFG address 18 holds the breathing amplitude. Reset to 0 (= breathing off), writable,
    and isolated from the bands, flash and the first config word."""
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _reset(dut)

    assert int(dut.cfg2.value) == 0, "breathing must default to off"
    bands_before = await _dump(dut)

    await _write(dut, V.VisualState.ADDR_CFG, 0b10101)
    for val in (1, 8, V.BAND_MAX, 0):
        await _write(dut, V.VisualState.ADDR_CFG2, val)
        assert int(dut.cfg2.value) == val, "cfg2={} expected {}".format(
            int(dut.cfg2.value), val)
        assert int(dut.cfg.value) == 0b10101, "cfg2 write clobbered cfg"
    assert await _dump(dut) == bands_before, "cfg2 writes disturbed the bands"
    assert int(dut.flash.value) == 0, "cfg2 writes disturbed flash"
