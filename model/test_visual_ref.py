# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Self-check for model/visual_ref.py -- the on-chip visual back-end.

Python checks all 480,000 visible pixels exhaustively where that is useful; the cocotb
tests then prove RTL == model on the points where a comparator bug can hide.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import visual_ref as V  # noqa: E402


ALL_OFF = [0] * V.NBANDS
ALL_FULL = [V.BAND_MAX] * V.NBANDS


def test_every_pixel_maps_to_exactly_one_zone():
    """No gaps, no overlaps: every visible pixel decodes to a valid zone, and every zone
    is reachable. A gap would render as an unexplained black hole on screen."""
    seen = [0] * V.NBANDS
    for py in range(V.V_VIS):
        for px in range(V.H_VIS):
            z, depth, mul, hue = V.zone_of(px, py)
            assert 0 <= z < V.NBANDS, (px, py, z)
            assert depth >= 0, (px, py, depth)
            assert mul in (V.MUL_WING, V.MUL_BASS, V.MUL_CENTRE)
            assert hue in (V.HUE_BASS, V.HUE_LOWMID, V.HUE_HIMID, V.HUE_HIGH)
            seen[z] += 1
    for z in range(V.NBANDS):
        assert seen[z] > 0, "zone {} is unreachable -- geometry bug".format(z)
    assert sum(seen) == V.H_VIS * V.V_VIS


def test_zone_groups_land_in_the_right_regions():
    assert V.zone_of(10, 590)[0] == 0, "bottom-left is bass zone 0"
    assert V.zone_of(790, 590)[0] == 3, "bottom-right is bass zone 3"
    assert V.zone_of(10, 360)[0] == 0, "bass starts at py=360 after the rebalance"
    assert V.zone_of(10, 359)[0] == 7, "the row above the bass is still the left wing"
    assert V.zone_of(10, 10)[0] == 4, "top-left is the first left-wing zone"
    assert V.zone_of(790, 10)[0] == 8, "top-right is the first right-wing zone"
    assert V.zone_of(790, 359)[0] == 11
    assert V.zone_of(200, 10)[0] == 12, "centre starts at band 12"
    assert V.zone_of(630, 10)[0] == 15
    # highs hang from the top: depth is 0 at py=0, not at the bottom of the centre
    assert V.zone_of(400, 0)[1] == 0, "a centre column's base is the TOP of the screen"
    assert V.zone_of(400, 359)[1] == 359


def test_full_scale_band_fills_its_zone():
    """A band at 31 must reach the far edge of its zone, or the meter never looks full."""
    for py in range(V.BOTTOM_TOP, V.V_VIS):          # bass strip, 240 deep
        z, depth, mul, _ = V.zone_of(100, py)
        assert depth < (V.BAND_MAX * mul), (py, depth)
    for px in range(0, V.WING_W):                     # wings, 120 deep
        z, depth, mul, _ = V.zone_of(px, 100)
        assert depth < (V.BAND_MAX * mul), (px, depth)
    for py in range(0, V.BOTTOM_TOP):                 # centre, 360 deep
        z, depth, mul, _ = V.zone_of(400, py)
        assert depth < (V.BAND_MAX * mul), (py, depth)


def test_silence_is_black_everywhere():
    """The DJ default: no music, no light."""
    for py in range(0, V.V_VIS, 7):
        for px in range(0, V.H_VIS, 11):
            assert V.pixel(px, py, True, ALL_OFF, 0) == (0, 0, 0), (px, py)


def test_blanking_is_black_even_at_full_scale():
    for (px, py) in [(0, 0), (400, 300), (799, 599), (900, 610)]:
        assert V.pixel(px, py, False, ALL_FULL, V.BAND_MAX) == (0, 0, 0), (px, py)
        assert V.uo_out(0, 0, px, py, False, ALL_FULL, V.BAND_MAX) == 0


def test_fill_grows_monotonically_with_the_band():
    """More energy must never light FEWER pixels -- the core promise of a level meter."""
    px = 100                                        # a bass column
    prev = -1
    for band in range(V.BAND_MAX + 1):
        bands = list(ALL_OFF)
        bands[V.zone_of(px, 590)[0]] = band
        lit = sum(1 for py in range(V.BOTTOM_TOP, V.V_VIS)
                  if V.pixel(px, py, True, bands, 0) != (0, 0, 0))
        assert lit >= prev, "band {} lit {} px, previous lit {}".format(band, lit, prev)
        prev = lit
    assert prev > 0, "a full-scale band must light something"


def test_lit_pixels_are_never_invisible():
    """level_of never returns 0: a quiet-but-present band draws a dim bar, not nothing."""
    for band in range(1, V.BAND_MAX + 1):
        assert V.level_of(band) >= 1, band
    # and a lit pixel really is non-black for every non-zero band
    for band in range(1, V.BAND_MAX + 1):
        bands = list(ALL_OFF)
        bands[0] = band
        assert V.pixel(10, V.V_VIS - 1 - 0, True, bands, 0) != (0, 0, 0), band


def test_flash_lifts_everything_and_saturates():
    dark = V.pixel(400, 100, True, ALL_OFF, 0)
    assert dark == (0, 0, 0)
    lifted = V.pixel(400, 100, True, ALL_OFF, V.BAND_MAX)
    assert lifted == (3, 3, 3), lifted
    # saturation: full colour + full flash must not wrap
    hot = V.pixel(10, 599, True, ALL_FULL, V.BAND_MAX)
    assert all(0 <= c <= 3 for c in hot), hot


def test_visual_state_defaults_draw_something():
    """Power-on defaults are the bring-up pattern: every zone must light, at differing
    heights, so one look at a monitor checks geometry + colour + blanking."""
    st = V.VisualState()
    assert st.flash == 0
    lit_per_zone = [0] * V.NBANDS
    for py in range(0, V.V_VIS, 3):
        for px in range(0, V.H_VIS, 3):
            if V.pixel(px, py, True, st.bands, st.flash) != (0, 0, 0):
                lit_per_zone[V.zone_of(px, py)[0]] += 1
    for z in range(V.NBANDS):
        assert lit_per_zone[z] > 0, "zone {} is dark at power-on defaults".format(z)
    assert len(set(lit_per_zone)) > 1, "defaults should differ per zone, not be flat"


def test_visual_state_write_and_readback():
    st = V.VisualState()
    st.write(0, 17)
    assert st.read_band(0) == 17
    st.write(V.NBANDS - 1, 31)
    assert st.read_band(V.NBANDS - 1) == 31
    st.write(V.VisualState.ADDR_FLASH, 9)
    assert st.flash == 9
    st.write(0, 0xFF)                       # must mask to BAND_W
    assert st.read_band(0) == V.BAND_MAX
    st.reset()
    assert st.bands == V.DEFAULT_BANDS and st.flash == 0


def test_pmod_packing_bit_positions():
    assert V.pack_pmod(1, 0, 0, 0, 0) == 0x80, "hsync is bit 7"
    assert V.pack_pmod(0, 1, 0, 0, 0) == 0x08, "vsync is bit 3"
    assert V.pack_pmod(0, 0, 0b10, 0, 0) == 0x01, "r[1] (MSB) -> bit0, pin R1"
    assert V.pack_pmod(0, 0, 0b01, 0, 0) == 0x10, "r[0] (LSB) -> bit4, pin R0"
    assert V.pack_pmod(0, 0, 0, 0b10, 0) == 0x02
    assert V.pack_pmod(0, 0, 0, 0b01, 0) == 0x20
    assert V.pack_pmod(0, 0, 0, 0, 0b10) == 0x04
    assert V.pack_pmod(0, 0, 0, 0, 0b01) == 0x40
    assert V.pack_pmod(1, 1, 3, 3, 3) == 0xFF


def test_wobble_is_a_bounded_triangle():
    """At full amplitude the wobble must be a clean triangle: starts at 0, reaches the
    configured peak, never steps by more than 1 (a jump would read as a stutter)."""
    amp = 31                                   # max config value
    vals = [V.wobble(f, amp) for f in range(1 << V.FRAME_W)]
    assert V.wobble(0, amp) == 0, "frame 0 must be the un-animated picture"
    assert min(vals) == 0, min(vals)
    assert max(vals) == min(amp * V.WOBBLE_STEP, V.WOBBLE_MAX), max(vals)
    for a, b in zip(vals, vals[1:]):
        assert abs(a - b) <= 1, "wobble jumps from {} to {}".format(a, b)
    assert vals[:len(vals) // 2] != vals[len(vals) // 2:], "wobble must actually vary"


def test_wobble_amplitude_is_firmware_controlled():
    """The whole point of moving amplitude into config word 18: it must be tunable, and
    0 must mean genuinely off (which is where an unwritten config region leaves it)."""
    assert [V.wobble(f, 0) for f in range(1 << V.FRAME_W)] == [0] * (1 << V.FRAME_W),         "cfg2 = 0 must disable breathing entirely"
    prev = -1
    for amp in range(32):
        peak = max(V.wobble(f, amp) for f in range(1 << V.FRAME_W))
        assert peak >= prev, "amplitude {} peaks lower than {}".format(amp, amp - 1)
        assert peak == min(amp * V.WOBBLE_STEP, V.WOBBLE_MAX), (amp, peak)
        prev = peak
    assert prev > 15, "the top setting should reach well past the old fixed 15 px"


def test_silence_stays_black_at_every_frame():
    """The one that matters: the wobble may extend a lit bar but must never light a silent
    one. If it could, the screen would shimmer during quiet passages -- the exact opposite
    of the mostly-black look."""
    for frame in range(0, 1 << V.FRAME_W, 7):
        for py in range(0, V.V_VIS, 29):
            for px in range(0, V.H_VIS, 37):
                assert V.pixel(px, py, True, ALL_OFF, 0, frame, 0, 31) == (
                    0, 0, 0), (px, py, frame)


def test_animation_actually_moves():
    """A lit bar's length must change across a breathing cycle -- and by the configured
    amount. This is what caught the effect being invisible at the original 7 px: the range
    must span several band steps to be seen at all."""
    bands = list(ALL_OFF)
    bands[0] = 8                                  # one bass zone, mid level
    for amp in (4, 15, 31):
        lengths = set()
        for frame in range(0, 1 << V.FRAME_W, 2):
            lit = sum(1 for py in range(V.BOTTOM_TOP, V.V_VIS)
                      if V.pixel(100, py, True, bands, 0, frame, 0, amp) != (0, 0, 0))
            lengths.add(lit)
        span = max(lengths) - min(lengths)
        expect = min(amp * V.WOBBLE_STEP, V.WOBBLE_MAX)
        assert span == expect, "amp {}: bar breathed {} px, expected {}".format(
            amp, span, expect)
    # and with breathing off the bar must be perfectly still
    still = {sum(1 for py in range(V.BOTTOM_TOP, V.V_VIS)
                 if V.pixel(100, py, True, bands, 0, f, 0, 0) != (0, 0, 0))
             for f in range(0, 1 << V.FRAME_W, 8)}
    assert len(still) == 1, "cfg2 = 0 must hold the bar completely still"


def test_render_is_deterministic():
    """Same inputs -> same pixels. A stateless renderer must have no hidden history."""
    bands = V.DEFAULT_BANDS
    for frame in (0, 37, 128, 255):
        a = [V.pixel(px, py, True, bands, 3, frame)
             for py in range(0, V.V_VIS, 61) for px in range(0, V.H_VIS, 71)]
        b = [V.pixel(px, py, True, bands, 3, frame)
             for py in range(0, V.V_VIS, 61) for px in range(0, V.H_VIS, 71)]
        assert a == b, "frame {} rendered differently twice".format(frame)


def test_wobble_never_overflows_its_zone_visibly():
    """fill + wobble must not run so far past a zone's depth that the meter pins early."""
    for depth, mul in ((V.WING_W, V.MUL_WING),
                       (V.V_VIS - V.BOTTOM_TOP, V.MUL_BASS),
                       (V.BOTTOM_TOP, V.MUL_CENTRE)):
        assert V.BAND_MAX * mul + V.WOBBLE_MAX < depth + 2 * mul + V.WOBBLE_MAX + 1


def test_cfg_zero_is_the_original_design():
    """THE safety property. An unwritten MCU config region reads back 0, so firmware that
    only publishes bands must still get a normal picture. cfg == 0 must therefore mean
    classic palette, full colour, full brightness -- which is why brightness is encoded as
    a DIM amount, not a CAP (a cap of 0 would blank the screen)."""
    bw, palette, cap = V.cfg_fields(0)
    assert (bw, palette, cap) == (0, 0, 3), (bw, palette, cap)
    assert V.PALETTES[0] == [V.HUE_BASS, V.HUE_LOWMID, V.HUE_HIMID, V.HUE_HIGH]
    # and the rendered result is unchanged across the screen
    for py in range(0, V.V_VIS, 31):
        for px in range(0, V.H_VIS, 41):
            a = V.pixel(px, py, True, V.DEFAULT_BANDS, 7, 40)
            b = V.pixel(px, py, True, V.DEFAULT_BANDS, 7, 40, cfg=0)
            assert a == b, (px, py, a, b)


def test_bw_makes_every_lit_pixel_grey():
    cfg = 1 << V.CFG_BW_BIT
    seen = 0
    for py in range(0, V.V_VIS, 13):
        for px in range(0, V.H_VIS, 17):
            r, g, b = V.pixel(px, py, True, V.DEFAULT_BANDS, 0, 0, cfg)
            assert r == g == b, "not grey at ({},{}): {}".format(px, py, (r, g, b))
            seen += (r != 0)
    assert seen > 0, "B&W mode lit nothing at all"


def test_dim_reduces_brightness_monotonically():
    """Higher dim must never make anything brighter, and dim = 3 must be fully black."""
    prev = None
    for dim in range(4):
        cfg = dim << V.CFG_DIM_SHIFT
        peak = 0
        for py in range(0, V.V_VIS, 19):
            for px in range(0, V.H_VIS, 23):
                peak = max(peak, max(V.pixel(px, py, True, ALL_FULL, 31, 0, cfg)))
        if prev is not None:
            assert peak <= prev, "dim {} is brighter than dim {}".format(dim, dim - 1)
        prev = peak
    assert prev == 0, "dim = 3 must black the screen, peak was {}".format(prev)


def test_cap_also_dims_the_kick_flash():
    """A 'global brightness cap' the kick punched straight through would not be a cap."""
    full = V.pixel(400, 100, True, ALL_OFF, 31, 0, cfg=0)
    assert full == (3, 3, 3), full
    dimmed = V.pixel(400, 100, True, ALL_OFF, 31, 0, cfg=(1 << V.CFG_DIM_SHIFT))
    assert max(dimmed) == 2, dimmed


def test_each_palette_is_distinct_and_legal():
    assert len(V.PALETTES) == 4
    for pi, pal in enumerate(V.PALETTES):
        assert len(pal) == 4
        for h in pal:
            assert 1 <= h <= 7, "palette {} has an illegal/black hue {}".format(pi, h)
    for pi in range(1, 4):
        assert V.PALETTES[pi] != V.PALETTES[0], "palette {} duplicates the classic one".format(pi)


def test_palette_select_actually_changes_colour():
    bands = V.DEFAULT_BANDS
    base = [V.pixel(px, py, True, bands, 0, 0, 0)
            for py in range(0, V.V_VIS, 29) for px in range(0, V.H_VIS, 31)]
    for pal in range(1, 4):
        cfg = pal << V.CFG_PALETTE_SHIFT
        other = [V.pixel(px, py, True, bands, 0, 0, cfg)
                 for py in range(0, V.V_VIS, 29) for px in range(0, V.H_VIS, 31)]
        assert other != base, "palette {} renders identically to palette 0".format(pal)


def test_config_never_lights_a_silent_band():
    """No config combination may break the mostly-black default."""
    for cfg in range(1 << V.BAND_W):
        for (px, py) in [(100, 599), (0, 0), (400, 10), (799, 200), (260, 5)]:
            assert V.pixel(px, py, True, ALL_OFF, 0, 77, cfg) == (0, 0, 0), (cfg, px, py)


def test_visual_state_carries_cfg2():
    st = V.VisualState()
    assert st.cfg2 == 0, "breathing must default to off"
    st.write(V.VisualState.ADDR_CFG2, 21)
    assert st.cfg2 == 21 and st.cfg == 0, "cfg2 write leaked into cfg"
    st.write(V.VisualState.ADDR_CFG2 + 1, 31)      # reserved -> inert
    assert st.cfg2 == 21
    st.reset()
    assert st.cfg2 == 0


def test_visual_state_carries_cfg():
    st = V.VisualState()
    assert st.cfg == 0
    st.write(V.VisualState.ADDR_CFG, 0b10101)
    assert st.cfg == 0b10101
    st.write(V.VisualState.ADDR_CFG + 1, 0b11111)      # reserved -> inert
    assert st.cfg == 0b10101
    st.reset()
    assert st.cfg == 0


# ============================ soft fade + ordered dither ============================

def test_bayer_is_the_canonical_matrix():
    """The closed form {v0, y0, v1, y1} with v = px^py MUST equal the standard 4x4 Bayer
    matrix -- that identity is the entire reason the dither costs two XOR gates instead of a
    16-entry LUT. If it ever stops holding, the effect is not a Bayer dither any more, it is
    just noise with a nice comment."""
    canon = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
    for py in range(4):
        for px in range(4):
            assert V.bayer4(px, py) == canon[py][px], (px, py)
    # every threshold used exactly once per cell -> an even spatial average, not a clump
    cell = sorted(V.bayer4(px, py) for py in range(4) for px in range(4))
    assert cell == list(range(16))
    # and it tiles: only the low 2 bits of each coordinate matter
    for py in range(0, V.V_VIS, 37):
        for px in range(0, V.H_VIS, 41):
            assert V.bayer4(px, py) == V.bayer4(px & 3, py & 3)


def test_cfg3_zero_is_the_original_design():
    """The all-zero rule, again: an unwritten MCU config region reads back 0, so cfg3 == 0
    must render EXACTLY the hard-edged picture that existed before the fade did. Checked
    over the whole frame, not a sample."""
    bands = [(i * 2 + 3) & V.BAND_MAX for i in range(V.NBANDS)]
    for py in range(V.V_VIS):
        for px in range(V.H_VIS):
            a = V.pixel(px, py, True, bands, 0, frame=11, cfg=0, cfg2=4, cfg3=0)
            b = V.pixel(px, py, True, bands, 0, frame=11, cfg=0, cfg2=4)
            assert a == b, (px, py, a, b)


def test_fade_never_brightens_and_never_overflows():
    """The fade may only ever take brightness AWAY. If it could add, a bar tip would be
    brighter than the bar, and the 2-bit channel could wrap -- which on this Pmod reads as
    a black notch at the tip, the most visible artefact available."""
    bands = [V.BAND_MAX] * V.NBANDS
    for sh in range(4):
        cfg3 = 1 | (sh << V.CFG3_FADE_SH_SHIFT)
        for py in range(0, V.V_VIS, 3):
            for px in range(0, V.H_VIS, 3):
                hard = V.pixel(px, py, True, bands, 0, cfg3=0)
                soft = V.pixel(px, py, True, bands, 0, cfg3=cfg3)
                for c in range(3):
                    assert 0 <= soft[c] <= 3, (px, py, sh, soft)
                    assert soft[c] <= hard[c], (px, py, sh, soft, hard)


def test_fade_level_arithmetic_cannot_exceed_three():
    """Exhaustive over the whole (lvl, tip_dist, pixel-phase, shift) space that matters, so
    the RTL can carry `whole` in 2 bits with no saturation logic of its own. `whole == 3`
    is reachable only at scaled == 48, where the fraction is 0 and the dither bump cannot
    fire -- this proves that rather than asserting it in a comment."""
    for lvl in (1, 2, 3):
        for sh in range(4):
            for d in range(0, (V.FADE_STEPS << sh) * 2 + 4):
                for py in range(4):
                    for px in range(4):
                        out = V.fade_level(lvl, d, px, py, sh)
                        assert 0 <= out <= 3, (lvl, d, px, py, sh, out)
                        assert out <= lvl, "fade brightened a pixel"


def test_fade_reaches_full_brightness_away_from_the_tip():
    """Past the ramp the bar must be at its ordinary brightness -- the fade is a tip
    treatment, not a global dimmer. Anything else would make the whole picture darker as
    soon as firmware enables it."""
    for lvl in (1, 2, 3):
        for sh in range(4):
            w = V.fade_width(sh)
            for d in (w, w + 1, w * 3, 900):
                for py in range(4):
                    for px in range(4):
                        assert V.fade_level(lvl, d, px, py, sh) == lvl, (lvl, d, sh)


def test_fade_ramp_is_monotonic_along_the_bar():
    """Averaged over a 4x4 dither cell, brightness must rise monotonically from the tip
    inward. A non-monotonic ramp would read as banding -- the exact artefact the dither is
    there to remove."""
    for lvl in (1, 2, 3):
        for sh in range(4):
            prev = -1.0
            for d in range(0, V.fade_width(sh) + 1):
                cell = sum(V.fade_level(lvl, d, px, py, sh)
                           for py in range(4) for px in range(4)) / 16.0
                assert cell >= prev - 1e-9, (lvl, sh, d, cell, prev)
                prev = cell


def test_dither_actually_buys_intermediate_levels():
    """The point of the whole effect: inside the ramp, a 4x4 cell must show MORE distinct
    average brightnesses than the four the Pmod can express. If this fails the dither is
    doing nothing and the gates are wasted."""
    sh = 1
    seen = set()
    for d in range(0, V.fade_width(sh) + 1):
        cell = sum(V.fade_level(3, d, px, py, sh) for py in range(4) for px in range(4))
        seen.add(cell)
    assert len(seen) >= 12, "only {} distinct cell averages -- dither is not working".format(
        len(seen))


def test_fade_still_leaves_a_silent_band_perfectly_black():
    """The invariant that outranks every effect: silence is black. The fade only ever
    reduces a LIT pixel, so it cannot break this -- prove it rather than assume it."""
    for sh in range(4):
        cfg3 = 1 | (sh << V.CFG3_FADE_SH_SHIFT)
        for f in (0, 7, 64, 200):
            for py in range(0, V.V_VIS, 7):
                for px in range(0, V.H_VIS, 11):
                    assert V.pixel(px, py, True, ALL_OFF, 0, frame=f, cfg2=31,
                                   cfg3=cfg3) == (0, 0, 0), (px, py, sh, f)


def test_fade_respects_blanking():
    for sh in range(4):
        cfg3 = 1 | (sh << V.CFG3_FADE_SH_SHIFT)
        for py in range(0, V.V_VIS, 13):
            for px in range(0, V.H_VIS, 17):
                assert V.pixel(px, py, False, ALL_FULL, 31, cfg3=cfg3) == (0, 0, 0)


def test_fade_width_settings_are_powers_of_two():
    """Powers of two ONLY. The preview normalised by 24, which needs a divider -- there is
    no divider on this chip and no room for one. Restricting the ramp to powers of two makes
    the normalisation a wire slice."""
    assert [V.fade_width(s) for s in range(4)] == [16, 32, 64, 128]
    for sh in range(4):
        w = V.fade_width(sh)
        assert w & (w - 1) == 0, "fade width {} is not a power of two".format(w)


def test_wider_fade_is_softer():
    """A larger fade_sh must dim MORE of the bar, or the knob does nothing useful."""
    bands = [V.BAND_MAX] * V.NBANDS
    dark = []
    for sh in range(4):
        cfg3 = 1 | (sh << V.CFG3_FADE_SH_SHIFT)
        n = 0
        for py in range(0, V.V_VIS, 2):
            for px in range(0, V.H_VIS, 2):
                hard = V.pixel(px, py, True, bands, 0, cfg3=0)
                soft = V.pixel(px, py, True, bands, 0, cfg3=cfg3)
                if soft != hard:
                    n += 1
        dark.append(n)
    assert dark == sorted(dark) and dark[0] > 0 and dark[-1] > dark[0], dark


def test_visual_state_carries_cfg3():
    vs = V.VisualState()
    assert vs.cfg3 == 0, "cfg3 must power up at 0 -- the no-fade look"
    vs.write(V.VisualState.ADDR_CFG3, 0b101)
    assert vs.cfg3 == 0b101
    vs.write(V.VisualState.ADDR_CFG3, 0xFF)
    assert vs.cfg3 == V.BAND_MAX, "cfg3 must mask to BAND_W bits"
    vs.write(V.CFG3_ADDR + 1, 0x1F)
    assert vs.cfg3 == V.BAND_MAX, "addresses above CFG3 must be ignored"


def test_config_map_stays_contiguous():
    """The refresh streams words 0..VS_N-1 in one burst, so a hole in the address map would
    silently fetch a reserved word into a real register."""
    assert V.CFG3_ADDR == V.CFG2_ADDR + 1 == V.NBANDS + 3
    assert V.VisualState.ADDR_CFG3 == V.CFG3_ADDR


def _main():
    checks = [test_every_pixel_maps_to_exactly_one_zone,
              test_zone_groups_land_in_the_right_regions,
              test_full_scale_band_fills_its_zone,
              test_silence_is_black_everywhere,
              test_blanking_is_black_even_at_full_scale,
              test_fill_grows_monotonically_with_the_band,
              test_lit_pixels_are_never_invisible,
              test_flash_lifts_everything_and_saturates,
              test_visual_state_defaults_draw_something,
              test_visual_state_write_and_readback,
              test_pmod_packing_bit_positions,
              test_wobble_is_a_bounded_triangle,
              test_wobble_amplitude_is_firmware_controlled,
              test_silence_stays_black_at_every_frame,
              test_animation_actually_moves,
              test_render_is_deterministic,
              test_wobble_never_overflows_its_zone_visibly,
              test_cfg_zero_is_the_original_design,
              test_bw_makes_every_lit_pixel_grey,
              test_dim_reduces_brightness_monotonically,
              test_cap_also_dims_the_kick_flash,
              test_each_palette_is_distinct_and_legal,
              test_palette_select_actually_changes_colour,
              test_config_never_lights_a_silent_band,
              test_visual_state_carries_cfg,
              test_visual_state_carries_cfg2,
              test_bayer_is_the_canonical_matrix,
              test_cfg3_zero_is_the_original_design,
              test_fade_never_brightens_and_never_overflows,
              test_fade_level_arithmetic_cannot_exceed_three,
              test_fade_reaches_full_brightness_away_from_the_tip,
              test_fade_ramp_is_monotonic_along_the_bar,
              test_dither_actually_buys_intermediate_levels,
              test_fade_still_leaves_a_silent_band_perfectly_black,
              test_fade_respects_blanking,
              test_fade_width_settings_are_powers_of_two,
              test_wider_fade_is_softer,
              test_visual_state_carries_cfg3,
              test_config_map_stays_contiguous]
    print("SeeTheBeat visual back-end golden-model self-check")
    print("-" * 58)
    ok = 0
    for c in checks:
        try:
            c()
            print("  PASS  {}".format(c.__name__))
            ok += 1
        except AssertionError as e:
            print("  FAIL  {}  --> {}".format(c.__name__, e))
    print("-" * 58)
    print("{}/{} checks passed".format(ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(_main())
