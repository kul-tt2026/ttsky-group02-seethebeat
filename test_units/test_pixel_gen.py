# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Cocotb unit test: RTL src/pixel_gen.v vs model/visual_ref.py.

pixel_gen is purely combinational, so this drives px/py/active/band/flash directly rather
than running a beam. Note the two-step handshake: pixel_gen decodes (px,py) -> `zone`, and
the caller (visual_state, in the real design) supplies that zone's `band` back. The helper
below mirrors exactly that, which also proves the zone decode itself.

Coverage is chosen where a comparator bug can actually hide -- region seams, fill edges at
every band value, the blanking gate, flash saturation -- plus a strided sweep on strides
coprime to the zone splits (200/120/140), so the sampling phase keeps moving instead of
locking onto one alignment. model/test_visual_ref.py separately checks all 480,000 visible
pixels exhaustively in Python.
"""

import os
import sys

import cocotb
from cocotb.triggers import Timer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "model"))
import visual_ref as V  # noqa: E402

H, VV = V.H_VIS, V.V_VIS
ALL_OFF = [0] * V.NBANDS
ALL_FULL = [V.BAND_MAX] * V.NBANDS


async def _check(dut, px, py, active, bands, flash, frame=0, cfg=0, cfg2=0):
    """Drive a pixel through the real zone->band->colour chain and compare to the model."""
    dut.px.value = px
    dut.py.value = py
    dut.active.value = 1 if active else 0
    dut.flash.value = flash
    dut.frame.value = frame
    dut.cfg.value = cfg
    dut.cfg2.value = cfg2
    await Timer(1, unit="ns")

    z = int(dut.zone.value)
    if active:                      # zone is meaningless during blanking
        exp_z = V.zone_of(px, py)[0]
        assert z == exp_z, "({},{}) zone rtl={} model={}".format(px, py, z, exp_z)

    dut.band.value = bands[z]
    await Timer(1, unit="ns")

    got = (int(dut.r.value), int(dut.g.value), int(dut.b.value))
    exp = V.pixel(px, py, active, bands, flash, frame, cfg, cfg2) if active else (0, 0, 0)
    assert got == exp, (
        "({},{}) act={} flash={} frame={} cfg={}: rtl={} model={}".format(
            px, py, active, flash, frame, cfg, got, exp))


@cocotb.test()
async def test_region_seams(dut):
    """Every boundary between the four regions, and the pixels either side of it."""
    xs = [0, 1, V.WING_W - 1, V.WING_W, V.WING_W + 1,
          V.CENTRE_R - 1, V.CENTRE_R, V.CENTRE_R + 1, H - 2, H - 1]
    ys = [0, 1, V.BOTTOM_TOP - 1, V.BOTTOM_TOP, V.BOTTOM_TOP + 1, VV - 2, VV - 1]
    bands = V.DEFAULT_BANDS
    for py in ys:
        for px in xs:
            await _check(dut, px, py, True, bands, 0)


@cocotb.test()
async def test_zone_sub_splits(dut):
    """The sub-index boundaries inside each region -- 200 px bass columns, 120 px wing
    rows, 140 px centre columns. Off-by-one here swaps two bands on screen."""
    bands = V.DEFAULT_BANDS
    for k in range(1, 4):
        e = k * V.BOTTOM_SPLIT
        for px in (e - 1, e):
            await _check(dut, px, VV - 1, True, bands, 0)
        e = k * V.WING_SPLIT
        for py in (e - 1, e):
            await _check(dut, 0, py, True, bands, 0)
            await _check(dut, H - 1, py, True, bands, 0)
        e = V.CENTRE_L + k * V.CENTRE_SPLIT
        for px in (e - 1, e):
            await _check(dut, px, 0, True, bands, 0)


@cocotb.test()
async def test_fill_edge_at_every_band_value(dut):
    """For each band 0..31, check the exact pixel where the fill stops. This is the one
    comparison that makes the meter musical, so walk its edge across the whole range."""
    BASS_DEEP = VV - V.BOTTOM_TOP          # 240
    CENTRE_DEEP = V.BOTTOM_TOP             # 360
    for band in range(V.BAND_MAX + 1):
        bands = [band] * V.NBANDS
        # bass column: 240 deep, x8, fills UPWARD from the bottom of the screen
        e = band * V.MUL_BASS
        for depth in (0, e - 1, e, e + 1, BASS_DEEP - 1):
            if 0 <= depth < BASS_DEEP:
                await _check(dut, 100, VV - 1 - depth, True, bands, 0)
        # centre column: 360 deep, x12, HANGS DOWN from py = 0
        e = band * V.MUL_CENTRE
        for depth in (0, e - 1, e, e + 1, CENTRE_DEEP - 1):
            if 0 <= depth < CENTRE_DEEP:
                await _check(dut, 400, depth, True, bands, 0)
        # a wing: 120 deep, x4, fills inward from the screen edge
        e = band * V.MUL_WING
        for depth in (0, e - 1, e, e + 1, V.WING_W - 1):
            if 0 <= depth < V.WING_W:
                await _check(dut, depth, 100, True, bands, 0)


@cocotb.test()
async def test_silence_is_black(dut):
    """No music, no light -- the mostly-black DJ default."""
    for py in range(0, VV, 23):
        for px in range(0, H, 31):
            await _check(dut, px, py, True, ALL_OFF, 0)


@cocotb.test()
async def test_blanking_forces_black(dut):
    """The single most important rule of the output path, checked at full scale so a
    broken gate cannot hide behind dark pixels."""
    pts = [(0, 0), (400, 300), (H - 1, VV - 1), (50, 550), (H, VV), (1055, 627)]
    for (px, py) in pts:
        await _check(dut, px, py, False, ALL_FULL, V.BAND_MAX)
        assert (int(dut.r.value), int(dut.g.value), int(dut.b.value)) == (0, 0, 0)


@cocotb.test()
async def test_flash_saturates_never_wraps(dut):
    """A wrapped flash would read as a BLACK frame exactly on the beat -- the worst
    possible artefact. Prove saturation across the whole flash range at full colour."""
    for flash in range(V.BAND_MAX + 1):
        for (px, py) in [(100, VV - 1), (400, 100), (0, 0), (H - 1, 200)]:
            await _check(dut, px, py, True, ALL_FULL, flash)
    # max flash must whiten everything, lit or not
    for (px, py) in [(400, 100), (100, VV - 1)]:
        await _check(dut, px, py, True, ALL_OFF, V.BAND_MAX)
        assert (int(dut.r.value), int(dut.g.value), int(dut.b.value)) == (3, 3, 3)


@cocotb.test()
async def test_strided_sweep(dut):
    """Whole-area sweep on strides coprime to 200/120/140 so the phase keeps moving."""
    bands = [(i * 5 + 2) & V.BAND_MAX for i in range(V.NBANDS)]
    n = 0
    for py in range(0, VV, 17):
        for px in range(0, H, 13):
            await _check(dut, px, py, True, bands, 3)
            n += 1
    dut._log.info("strided sweep: %d points, all bit-exact", n)


@cocotb.test()
async def test_breathing_edge_across_frames(dut):
    """The Phase 1 animation: the fill threshold gains a time-varying offset, so a bar's tip
    drifts in and out. Walk a full breathing cycle at the bar edge, where the effect lives.
    """
    bands = [8] * V.NBANDS
    for amp in (0, 8, 31):
        peak = min(amp * V.WOBBLE_STEP, V.WOBBLE_MAX)
        for frame in range(0, 1 << V.FRAME_W, 9):
            base = 8 * V.MUL_BASS
            for depth in (base - 1, base, base + peak, base + peak + 1, base + 2):
                if 0 <= depth < VV - V.BOTTOM_TOP:
                    await _check(dut, 100, VV - 1 - depth, True, bands, 0, frame, 0, amp)


@cocotb.test()
async def test_silence_stays_black_at_every_frame(dut):
    """The invariant the wobble could most easily break: it may extend a lit bar but must
    never light a silent one, or the screen shimmers through quiet passages."""
    for frame in range(0, 1 << V.FRAME_W, 11):
        for (px, py) in [(100, VV - 1), (100, VV - 60), (0, 0), (60, 200),
                         (400, 0), (400, 200), (799, 100), (260, 5)]:
            await _check(dut, px, py, True, ALL_OFF, 0, frame, 0, 31)
            assert (int(dut.r.value), int(dut.g.value),
                    int(dut.b.value)) == (0, 0, 0), (
                "silence lit up at frame {} ({},{})".format(frame, px, py))


@cocotb.test()
async def test_frame_zero_is_the_unanimated_picture(dut):
    """wobble(0) == 0 by construction, so a frame-0 render must equal the static design --
    which is what every other test in this file assumes when it omits `frame`."""
    bands = V.DEFAULT_BANDS
    for py in range(0, VV, 37):
        for px in range(0, H, 41):
            await _check(dut, px, py, True, bands, 0, 0)


@cocotb.test()
async def test_every_config_value(dut):
    """All 32 values of the config register, sampled across the screen. cfg selects the
    palette, greyscale and the brightness ceiling -- all of which sit on the critical
    uo_out path, so a decode slip here is both visible and timing-relevant."""
    for cfg in range(1 << V.BAND_W):
        for py in range(0, VV, 97):
            for px in range(0, H, 89):
                await _check(dut, px, py, True, V.DEFAULT_BANDS, 9, 61, cfg)


@cocotb.test()
async def test_cfg_zero_matches_the_original_design(dut):
    """The safety property: an unwritten MCU config region reads back 0, so cfg == 0 must
    render exactly the classic look -- full colour, full brightness, palette 0."""
    for py in range(0, VV, 43):
        for px in range(0, H, 47):
            await _check(dut, px, py, True, V.DEFAULT_BANDS, 5, 33, 0)


@cocotb.test()
async def test_bw_and_dim_extremes(dut):
    """Greyscale must give r == g == b; full dim must black the screen even at full scale."""
    bw = 1 << V.CFG_BW_BIT
    for py in range(0, VV, 53):
        for px in range(0, H, 59):
            await _check(dut, px, py, True, ALL_FULL, 0, 0, bw)
            assert int(dut.r.value) == int(dut.g.value) == int(dut.b.value), (
                "B&W not grey at ({},{})".format(px, py))

    black = 3 << V.CFG_DIM_SHIFT
    for py in range(0, VV, 53):
        for px in range(0, H, 59):
            await _check(dut, px, py, True, ALL_FULL, 31, 0, black)
            assert (int(dut.r.value), int(dut.g.value), int(dut.b.value)) == (0, 0, 0), (
                "full dim must black everything, even the kick flash")


@cocotb.test()
async def test_breathing_amplitude_is_firmware_controlled(dut):
    """cfg2 (config word 18) sets the breathing amplitude. Two things matter: 0 must hold
    the bar perfectly still, and a higher setting must visibly move it -- the original fixed
    7 px was below one band step in the bass and centre, so it could not be seen at all."""
    bands = [8] * V.NBANDS
    col, base = 100, 8 * V.MUL_BASS

    def lit_at(frame_vals, amp):
        return frame_vals, amp

    for amp, expect in ((0, 0), (8, min(8 * V.WOBBLE_STEP, V.WOBBLE_MAX)),
                        (31, min(31 * V.WOBBLE_STEP, V.WOBBLE_MAX))):
        seen = set()
        for frame in range(0, 1 << V.FRAME_W, 7):
            n = 0
            for depth in range(base - 2, min(base + V.WOBBLE_MAX + 2, VV - V.BOTTOM_TOP)):
                await _check(dut, col, VV - 1 - depth, True, bands, 0, frame, 0, amp)
                if (int(dut.r.value), int(dut.g.value), int(dut.b.value)) != (0, 0, 0):
                    n += 1
            seen.add(n)
        span = max(seen) - min(seen)
        assert span == expect, "amp {}: bar breathed {} px, expected {}".format(
            amp, span, expect)
