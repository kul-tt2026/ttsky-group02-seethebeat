# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/vga_timing.v vs the golden model model/vga_ref.py, compared
CYCLE-BY-CYCLE and BIT-EXACT.

Why the default is a COMPLETE frame at the REAL mode (1056 x 628 = 663,168 clocks):
the Part 1 lesson was that a small case cannot reach what the real one does -- the FFT test
ran at LOGN=6, never drove address bit 9, and a dropped bit went unnoticed for two days. The
same trap is here. A reduced VGA mode would use narrower counters and never exercise the
11-bit H wrap at 1055 nor the 10-bit V wrap at 627; worse, the V wrap happens exactly ONCE
per frame, so anything short of a full frame cannot see it at all. Hence: real mode, whole
frame, every clock.

`VGA_LINES=<n>` walks only the first n lines instead, for fast local iteration. It skips the
frame-level assertions it structurally cannot reach -- CI always runs the full frame.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ClockCycles, Timer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import vga_ref  # noqa: E402

MODE = vga_ref.MODE_800x600
SIGNALS = ("px", "py", "active", "hsync", "vsync", "vblank", "frame_start")


def _clocks_to_run():
    """Full frame unless VGA_LINES asks for fewer. Returns (clocks, is_full_frame)."""
    n = os.environ.get("VGA_LINES", "").strip()
    if n and n.isdigit() and int(n) > 0:
        lines = min(int(n), MODE.v_total)
        if lines < MODE.v_total:
            return lines * MODE.h_total, False
    return MODE.clocks_per_frame, True


def _get(dut, name):
    v = getattr(dut, name).value
    assert v.is_resolvable, "{} is X/Z -- reset not applied?".format(name)
    return int(v)


async def _release_reset(dut):
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 3)
    dut.rst_n.value = 1
    await Timer(1, unit="ns")


@cocotb.test()
async def test_vga_timing(dut):
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())     # 40 MHz
    await _release_reset(dut)

    n_clocks, full_frame = _clocks_to_run()
    dut._log.info("walking %d clocks (%s)", n_clocks,
                  "FULL FRAME" if full_frame else "partial -- VGA_LINES set")

    # After reset both counters are 0 and the model's first sample() is for (0,0), so we
    # read the outputs and THEN advance -- exactly mirroring VGATiming.step().
    model = vga_ref.VGATiming(MODE)

    n_active = n_hsync = n_vsync = n_vblank = n_framestart = 0
    first_frame_start = None

    for clk_i in range(n_clocks):
        exp = model.step()
        got = {s: _get(dut, s) for s in SIGNALS}

        for s in SIGNALS:
            assert got[s] == exp[s], (
                "clock {} (h={},v={}): {} rtl={} model={}".format(
                    clk_i, exp["hcount"], exp["vcount"], s, got[s], exp[s]))

        n_active += got["active"]
        n_hsync += (got["hsync"] == MODE.h_pol)
        n_vsync += (got["vsync"] == MODE.v_pol)
        n_vblank += got["vblank"]
        if got["frame_start"]:
            n_framestart += 1
            if first_frame_start is None:
                first_frame_start = clk_i

        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")

    if not full_frame:
        dut._log.warning("VGA_LINES set: frame-level checks (V wrap, totals) NOT run")
        return

    # ---- aggregates: a readable failure for each classic off-by-one ----
    assert n_active == 480000, "active clocks {} != 800*600".format(n_active)
    assert n_hsync == 128 * MODE.v_total, "hsync-asserted clocks {}".format(n_hsync)
    assert n_vsync == 4 * MODE.h_total, "vsync-asserted clocks {}".format(n_vsync)
    assert n_vblank == 28 * MODE.h_total, "vblank clocks {}".format(n_vblank)
    assert n_framestart == 1, "frame_start pulsed {} times in a frame".format(n_framestart)
    assert first_frame_start == MODE.v_vis * MODE.h_total, (
        "frame_start at clock {}, expected {}".format(
            first_frame_start, MODE.v_vis * MODE.h_total))

    # ---- the V wrap: only reachable here, and only once ----
    assert _get(dut, "px") == 0 and _get(dut, "py") == 0, (
        "after one full frame the counters must wrap to (0,0), got ({},{})".format(
            _get(dut, "px"), _get(dut, "py")))

    dut._log.info("1 frame OK: %d clocks, %d active, %d blanking (%.1f%%)",
                  MODE.clocks_per_frame, n_active, MODE.blanking_clocks,
                  100.0 * MODE.blanking_clocks / MODE.clocks_per_frame)


@cocotb.test()
async def test_reset_is_clean(dut):
    """rst_n is asserted on the board long after power-up, so reset must work mid-frame:
    return to (0,0) and restart the sequence identically."""
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())
    await _release_reset(dut)

    await ClockCycles(dut.clk, 5000)          # well into the visible area
    await Timer(1, unit="ns")
    assert _get(dut, "px") != 0 or _get(dut, "py") != 0, "counters did not advance at all"

    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 2)
    await Timer(1, unit="ns")
    assert _get(dut, "px") == 0 and _get(dut, "py") == 0, "reset did not clear the counters"
    assert _get(dut, "active") == 1, "at (0,0) the beam is inside the visible area"

    dut.rst_n.value = 1
    await Timer(1, unit="ns")

    # the first line after reset must match the model from the top
    model = vga_ref.VGATiming(MODE)
    for clk_i in range(MODE.h_total):
        exp = model.step()
        for s in SIGNALS:
            assert _get(dut, s) == exp[s], (
                "post-reset clock {}: {} rtl={} model={}".format(
                    clk_i, s, _get(dut, s), exp[s]))
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
