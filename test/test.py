# SPDX-FileCopyrightText: © 2024 Tiny Tapeout
# SPDX-License-Identifier: Apache-2.0
"""
Top-level test for tt_um_group02_seethebeat.

THIS FILE IS ALSO WHAT RUNS AT GATE LEVEL. The `gl_test` job in `.github/workflows/gds.yaml`
builds the post-synthesis netlist and runs exactly these tests against it, which — with no
demo board available before tape-out (CLAUDE.md §11) — is the closest thing we have to
testing the actual chip. Deep functional verification lives in `test_units/` against the
golden models; what belongs *here* is whatever is worth proving on the real netlist.

Three things a netlist can be wrong about that RTL simulation structurally cannot:
  1. **X-propagation.** A flop the reset tree does not actually reach comes up X in gate
     level and stays X forever. In RTL it silently initialises. `test_no_x_after_reset`.
  2. **Whether the big blocks are connected at all.** Synthesis happily optimises away
     anything unreachable. `test_bus_runs_a_transform` drives the MCU side so the FFT
     engine and `mcu_bus` actually *run* in the netlist rather than sitting parked.
  3. **The output path end to end**, pins included. `test_reset_and_idle`.

The VGA checks are deliberately PHASE-INDEPENDENT: they walk a whole scanline and assert
invariants over it (the sync pulse is 128 clocks wide; the colour is black for every clock
the sync is asserted) rather than asserting what happens at one exact cycle. An earlier
version asserted "hsync is still low at hcount=839", which was brittle for two reasons --
it depended on reset deasserting on exactly the right edge, and it read combinational
outputs with no settle delay. Exact per-cycle timing is verified properly in
test_units/test_vga_timing.py, against the golden model, over a full frame.
"""

import os
import sys

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, Timer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import mcu_bus_model as bus  # noqa: E402

# VGA 800x600@60 -- see model/vga_ref.py for the authoritative copy of these numbers
H_TOTAL = 1056
H_SYNC_W = 128
COLOUR_BITS = 0x77       # uo_out[6:4] = {B0,G0,R0}, uo_out[2:0] = {B1,G1,R1}
HSYNC_BIT = 0x80
VSYNC_BIT = 0x08

# uio bit roles (docs/bus_protocol.md). uio[5:0] is the chip->MCU command lane.
RESP_VALID = 7           # MCU -> chip
FRAME_READY = 6          # MCU -> chip: rising edge starts a transform
CMD_MASK = 0x3F

GATES = os.environ.get("GATES") == "yes"


async def _settle():
    """Let combinational outputs resolve after a clock edge before sampling them."""
    await Timer(1, unit="ns")


async def _reset(dut, frame_ready=0):
    """Start the ONLY clock in this file and reset the chip.

    Everything here is one `@cocotb.test()` with three phases rather than three tests, and
    that is deliberate: `test_units/test_fft_ctrl.py` was split into two tests once, each
    starting its own Clock, and it failed in CI in a way that took a long time to diagnose
    (CLAUDE.md, 2026-08-27). One test, one clock, one slave is correct under any cocotb
    version -- and this file is the one that runs against the netlist, where debugging is
    hardest of all.
    """
    cocotb.start_soon(Clock(dut.clk, 25, unit="ns").start())   # 40 MHz pixel clock
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = (frame_ready & 1) << FRAME_READY
    # Deassert reset AFTER an edge, not on one: assigning rst_n at the same simulation
    # instant as a rising edge races with it and can shift the counters by a cycle.
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    await _settle()
    dut.rst_n.value = 1
    await _settle()


@cocotb.test()
async def test_chip(dut):
    """ONE test, ONE clock, three phases -- see the note on _reset() for why it is one test.

    Phase 1: reset, bus direction, sync polarity, a clean scanline.
    Phase 2: no X anywhere on the output pins (the gate-level-specific check).
    Phase 3: run the MCU bus so the FFT engine is actually exercised in the netlist.
    Phase 4: reset MID-TRANSFORM and prove the chip restarts identically.
    """
    dut._log.info("start (gate level: %s)", GATES)
    await _phase1_reset_and_idle(dut)
    await _phase2_no_x(dut)
    await _phase3_bus_runs_a_transform(dut)
    await _phase4_reset_midrun(dut)


async def _phase1_reset_and_idle(dut):
    """Reset, bus direction, sync polarity, one clean scanline."""
    await _reset(dut)

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


async def _phase2_no_x(dut):
    """Every output pin must be a real 0 or 1 once reset is released.

    THIS IS THE GATE-LEVEL TEST THAT MATTERS MOST. In RTL simulation a reg without an
    explicit reset still initialises to a defined value, so an incomplete reset tree is
    invisible. In the netlist it comes up X, and X on a colour bit or a bus lane is a chip
    that never works. Because the outputs are combinational functions of state, an X
    anywhere in a cone reaches a pin here.

    Sampled over a full scanline so state that only becomes visible mid-line (the pixel
    pipeline registers, the zone decode) is covered too.
    """

    for i in range(H_TOTAL + 64):
        for name in ("uo_out", "uio_out", "uio_oe"):
            sig = getattr(dut, name).value
            # int() raises on an unresolvable value in BOTH cocotb 1.x and 2.x. Checking
            # `.is_resolvable` would be a bet on the version -- and this project has already
            # been burned once by assuming cocotb API semantics (CLAUDE.md, 2026-08-27).
            try:
                int(sig)
            except Exception:
                raise AssertionError(
                    f"{name} is {sig} at clock {i} after reset -- X/Z on an output pin. "
                    "Something in its cone is not reset in the netlist.")
        await ClockCycles(dut.clk, 1)
        await _settle()

    dut._log.info("no X on any output across the sampled window")


async def _pulse_reset(dut, cycles=7):
    """Assert rst_n for `cycles` clocks. Does NOT start a clock -- there is only ever one.

    Frame-ready is dropped as well, so the restart afterwards gets a clean rising edge
    (`start` in project.v is `ena & uio_in[6] & ~mcu_status_d`, an edge detector).
    """
    dut.rst_n.value = 0
    dut.uio_in.value = 0
    dut.ui_in.value = 0
    await ClockCycles(dut.clk, cycles)
    await _settle()
    dut.rst_n.value = 1
    await _settle()


async def _drive_bus(dut, slave, n_clocks, frame_ready=1):
    """Play the MCU memory slave at the pins for `n_clocks`, decoding what the chip emits.

    Returns (ops, addrs, dec): opcode counts, every address seen, and the framing
    state it ended in -- "IDLE" means the last command completed cleanly.

    The decode is done here rather than trusting the slave model, so command FRAMING is
    checked independently: a T0 must carry a legal opcode, and a READ/WRITE T0 must be
    followed by its address transfer. Tracking WD0..WD2 is not optional -- a write's payload
    byte can be 0x3f, which a naive decoder reads as opcode 11 (CFGRD).

    Timing mirrors `_slave_proc` in test_units/test_fft_ctrl.py cycle for cycle: sample the
    command lane after the edge, step the slave, present its answer for the next edge.
    """
    ops = {bus.OP_NOP: 0, bus.OP_READ: 0, bus.OP_WRITE: 0, bus.OP_CFGRD: 0}
    dec, hi, addrs = "IDLE", 0, []

    for i in range(n_clocks):
        try:
            cmd = int(dut.uio_out.value) & CMD_MASK
        except Exception:
            raise AssertionError(
                f"uio_out is {dut.uio_out.value} at clock {i} -- X on the command lane")

        if dec == "IDLE":
            op = cmd >> 4
            assert op in ops, f"illegal opcode {op:#04b} on the command lane at clock {i}"
            ops[op] += 1
            if op == bus.OP_READ or op == bus.OP_CFGRD:
                dec, hi = "A1", cmd & 0xF
            elif op == bus.OP_WRITE:
                dec, hi = "W1", cmd & 0xF
        elif dec == "A1":
            addrs.append((hi << 6) | (cmd & 0x3F)); dec = "IDLE"
        elif dec == "W1":
            addrs.append((hi << 6) | (cmd & 0x3F)); dec = "WD0"
        elif dec == "WD0":
            dec = "WD1"
        elif dec == "WD1":
            dec = "WD2"
        elif dec == "WD2":
            dec = "IDLE"

        rv, b = slave.step(cmd)
        dut.uio_in.value = ((rv & 1) << RESP_VALID) | ((frame_ready & 1) << FRAME_READY)
        dut.ui_in.value = b & 0xFF
        await ClockCycles(dut.clk, 1)
        await _settle()

    return ops, addrs, dec


def _fresh_slave():
    """A slave with a recognisable memory pattern, so a mis-framed address reads as garbage
    rather than as plausible zeros."""
    sl = bus.MCUSlave(latency=2)
    for a in range(1024):
        sl.sram[a] = (a * 7 + 3) & 0xFFFF
    return sl


async def _phase3_bus_runs_a_transform(dut):
    """Start a transform and service the MCU side, so `mcu_bus` + `fft_ctrl` actually RUN.

    Without this the gate-level run never asserts frame-ready, so the entire FFT engine --
    the most complex thing on the chip -- sits parked and is never exercised in the
    netlist. Synthesis and STA would both be perfectly happy with an engine that cannot
    run at all.

    This is NOT a functional FFT check (that is `test_units/test_fft_ctrl.py`, bit-exact
    against the golden model, and far too long to repeat at gate level). What it proves is
    that on the real netlist the chip: leaves idle when asked, emits only legal opcodes,
    frames every command correctly, and keeps making progress against a responding slave.
    """

    slave = _fresh_slave()

    # rising edge of frame-ready starts the transform
    dut.uio_in.value = 1 << FRAME_READY
    await ClockCycles(dut.clk, 1)
    await _settle()

    # Same window at RTL and gate level. Measured from the CI artifact, GL runs at
    # ~3500 clocks/s, so the whole file is a few seconds either way -- there is no
    # reason to trade coverage for that.
    N = 12000
    ops, addrs, dec = await _drive_bus(dut, slave, N)

    dut._log.info("bus activity over %d clocks: %s", N,
                  {"NOP": ops[bus.OP_NOP], "READ": ops[bus.OP_READ],
                   "WRITE": ops[bus.OP_WRITE], "CFGRD": ops[bus.OP_CFGRD]})

    assert ops[bus.OP_READ] > 0, (
        "the engine never issued a READ -- it never left idle, so the FFT datapath is "
        "untested in this netlist")
    assert ops[bus.OP_WRITE] > 0, (
        "the engine issued reads but never a WRITE -- it is not completing butterflies")
    assert addrs, "no addresses were framed on the command lane"
    assert max(addrs) < 1024, f"address {max(addrs)} exceeds the 10-bit space"
    assert len(set(addrs)) > 4, (
        f"only {len(set(addrs))} distinct addresses -- the address generator is stuck")
    assert dec == "IDLE" or ops[bus.OP_NOP] > 0, "command framing never returned to idle"

    # the VGA output must keep running throughout -- the FFT must not disturb the beam
    lit = 0
    for _ in range(H_TOTAL):
        uo = int(dut.uo_out.value)
        assert uo & HSYNC_BIT == 0 or uo & COLOUR_BITS == 0, (
            "colour leaked into the hsync pulse while the FFT was running")
        if uo & COLOUR_BITS:
            lit += 1
        await ClockCycles(dut.clk, 1)
        await _settle()
    assert lit > 0, "the picture stopped while the FFT was running"
    dut._log.info("VGA still live during the transform (%d lit pixels in a line)", lit)


async def _phase4_reset_midrun(dut):
    """Reset in the MIDDLE of a transform, then prove the chip restarts identically.

    WHY THIS EXISTS (Giel's assistant, 2026-09-01, and it is the right criticism): every
    other test in this repo resets once, at the start. That proves reset brings the chip UP.
    It does not prove reset brings the chip BACK -- and "back" is the only recovery mechanism
    a fabricated chip has. A reset that fails to clear some register only misbehaves when it
    is asserted while that register holds something, i.e. mid-computation, which is precisely
    the case nothing was covering.

    The check is a comparison, not an inspection: run a transform partway, reset it, run a
    second transform against an identical memory, and require the command stream to be the
    SAME. The FFT is deterministic, so any state that survived the reset -- a stage counter, a
    butterfly index, a CORDIC iteration, the bus framing FSM -- would make run B start from
    somewhere else and the addresses would diverge. We do not have to guess which register
    might be at fault; if any of them is, the sequences differ.

    The reset also lands at an arbitrary point in the bus traffic, so it covers being reset
    with reads in flight and `resp_valid` asserted.
    """
    N_RUN = 900          # long enough to be well inside the first stage of butterflies
    N_CMP = 24           # addresses compared between the two runs

    # ---------- run A: from a clean reset ----------
    await _pulse_reset(dut)
    slave_a = _fresh_slave()
    dut.uio_in.value = 1 << FRAME_READY          # rising edge -> start
    await ClockCycles(dut.clk, 1)
    await _settle()
    ops_a, addrs_a, _ = await _drive_bus(dut, slave_a, N_RUN)
    assert len(addrs_a) >= N_CMP, (
        f"run A only framed {len(addrs_a)} addresses in {N_RUN} clocks -- cannot compare")
    assert ops_a[bus.OP_READ] > 0 and ops_a[bus.OP_WRITE] > 0, (
        "run A never got going, so this phase would compare nothing")

    # ---------- reset it mid-flight ----------
    # No waiting for `done`: interrupting real work is the entire point.
    await _pulse_reset(dut)

    # ---------- the chip must look exactly like power-on again ----------
    assert dut.uio_oe.value == 0x3F, (
        f"after a mid-run reset uio_oe is {dut.uio_oe.value}, expected 0x3F -- the bus "
        "direction register did not reset")
    assert dut.uio_out.value == 0, (
        f"after a mid-run reset uio_out is {dut.uio_out.value}, expected 0 (NOP) -- the bus "
        "master did not return to idle")
    uo = int(dut.uo_out.value)
    assert uo & HSYNC_BIT == 0 and uo & VSYNC_BIT == 0, (
        f"after a mid-run reset the syncs are not idle low (uo_out={uo:#04x}) -- the VGA "
        "counters did not reset")

    for i in range(256):                      # no X anywhere, same check as phase 2
        for name in ("uo_out", "uio_out", "uio_oe"):
            try:
                int(getattr(dut, name).value)
            except Exception:
                raise AssertionError(
                    f"{name} is X/Z {i} clocks after a mid-run reset -- something in its "
                    "cone is only reset from power-on, not from a reset during operation")
        await ClockCycles(dut.clk, 1)
        await _settle()

    # ---------- run B: identical stimulus, must produce an identical command stream ----------
    slave_b = _fresh_slave()
    dut.uio_in.value = 1 << FRAME_READY
    await ClockCycles(dut.clk, 1)
    await _settle()
    ops_b, addrs_b, _ = await _drive_bus(dut, slave_b, N_RUN)

    assert addrs_b[:N_CMP] == addrs_a[:N_CMP], (
        "after a mid-run reset the engine did NOT restart from the beginning.\n"
        f"  run A (clean start) : {addrs_a[:N_CMP]}\n"
        f"  run B (post-reset)  : {addrs_b[:N_CMP]}\n"
        "State survived the reset, so a reset in the field would not recover the chip.")

    assert ops_b[bus.OP_READ] == ops_a[bus.OP_READ], (
        f"read count differs after reset: {ops_a[bus.OP_READ]} vs {ops_b[bus.OP_READ]}")
    assert ops_b[bus.OP_WRITE] == ops_a[bus.OP_WRITE], (
        f"write count differs after reset: {ops_a[bus.OP_WRITE]} vs {ops_b[bus.OP_WRITE]}")

    dut._log.info("mid-run reset OK: %d addresses identical, %d reads / %d writes both runs",
                  N_CMP, ops_a[bus.OP_READ], ops_a[bus.OP_WRITE])

    # ---------- and the picture still works afterwards ----------
    # Drop resp_valid first: _drive_bus leaves whatever it last drove, and a stuck-high
    # resp_valid would have the bus master capturing bytes nobody sent. Harmless for the
    # VGA check, but there is no reason to leave the chip in a state the MCU never creates.
    dut.uio_in.value = 1 << FRAME_READY
    hsync_clocks = 0
    for _ in range(H_TOTAL):
        uo = int(dut.uo_out.value)
        if uo & HSYNC_BIT:
            hsync_clocks += 1
            assert uo & COLOUR_BITS == 0, "colour leaked into hsync after a mid-run reset"
        await ClockCycles(dut.clk, 1)
        await _settle()
    assert hsync_clocks == H_SYNC_W, (
        f"hsync is {hsync_clocks} clocks wide after a mid-run reset, expected {H_SYNC_W}")
    dut._log.info("VGA timing intact after the mid-run reset")
