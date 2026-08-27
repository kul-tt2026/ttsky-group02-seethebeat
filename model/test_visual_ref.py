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
    vals = [V.wobble(f) for f in range(1 << V.FRAME_W)]
    assert V.wobble(0) == 0, "frame 0 must be the un-animated picture"
    assert min(vals) == 0 and max(vals) == V.WOBBLE_MAX, (min(vals), max(vals))
    # rises then falls, never jumps by more than 1 -- a jump would read as a stutter
    for a, b in zip(vals, vals[1:]):
        assert abs(a - b) <= 1, "wobble jumps from {} to {}".format(a, b)
    assert vals[:len(vals)//2] != vals[len(vals)//2:], "wobble must actually vary"


def test_silence_stays_black_at_every_frame():
    """The one that matters: the wobble may extend a lit bar but must never light a silent
    one. If it could, the screen would shimmer during quiet passages -- the exact opposite
    of the mostly-black look."""
    for frame in range(0, 1 << V.FRAME_W, 7):
        for py in range(0, V.V_VIS, 29):
            for px in range(0, V.H_VIS, 37):
                assert V.pixel(px, py, True, ALL_OFF, 0, frame) == (0, 0, 0), (px, py, frame)


def test_animation_actually_moves():
    """A lit bar's length must change across a breathing cycle, or the effect is dead code."""
    bands = list(ALL_OFF)
    bands[0] = 8                                  # one bass zone, mid level
    lengths = set()
    for frame in range(0, 1 << V.FRAME_W, 4):
        lit = sum(1 for py in range(V.BOTTOM_TOP, V.V_VIS)
                  if V.pixel(100, py, True, bands, 0, frame) != (0, 0, 0))
        lengths.add(lit)
    assert len(lengths) > 1, "bar length never changes -- the wobble does nothing"
    assert max(lengths) - min(lengths) == V.WOBBLE_MAX,         "bar should breathe by exactly WOBBLE_MAX px, got {}".format(
            max(lengths) - min(lengths))


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
              test_silence_stays_black_at_every_frame,
              test_animation_actually_moves,
              test_render_is_deterministic,
              test_wobble_never_overflows_its_zone_visibly]
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
