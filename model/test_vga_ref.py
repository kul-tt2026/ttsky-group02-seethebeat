# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
Self-check for model/vga_ref.py -- the VGA timing golden model.

These checks encode the VESA contract independently of the model's own arithmetic: the
totals, the derived rates, the sync pulse positions and widths, the size of the visible
area, and the polarity pair. If someone "fixes" a porch value, this goes red before any
RTL is touched.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vga_ref  # noqa: E402


M = vga_ref.MODE_800x600


def test_totals_match_vesa():
    assert M.h_total == 1056, "h_total {} != 1056".format(M.h_total)
    assert M.v_total == 628, "v_total {} != 628".format(M.v_total)
    assert M.pixel_clock_hz == 40000000
    # the fallback mode too -- it must stay genuinely reachable, not nominal
    f = vga_ref.MODE_640x480
    assert f.h_total == 800, "640x480 h_total {} != 800".format(f.h_total)
    assert f.v_total == 525, "640x480 v_total {} != 525".format(f.v_total)


def test_derived_rates():
    assert abs(M.line_rate_hz - 37878.79) < 0.5, M.line_rate_hz
    assert abs(M.frame_rate_hz - 60.317) < 0.01, M.frame_rate_hz
    assert M.clocks_per_frame == 663168, M.clocks_per_frame
    assert M.visible_pixels == 480000, M.visible_pixels
    # the per-frame budget quoted in the learning notes / plans
    assert M.blanking_clocks == 183168, M.blanking_clocks
    frac = M.blanking_clocks / float(M.clocks_per_frame)
    assert abs(frac - 0.276) < 0.001, frac


def test_counter_widths():
    # 1056 needs 11 bits, 628 needs 10 -- the widths src/vga_timing.v must use
    assert M.hw == 11, M.hw
    assert M.vw == 10, M.vw
    assert vga_ref._clog2(1024) == 10 and vga_ref._clog2(1025) == 11


def test_sync_windows():
    assert M.h_sync_on == 840 and M.h_sync_off == 968, (M.h_sync_on, M.h_sync_off)
    assert M.v_sync_on == 601 and M.v_sync_off == 605, (M.v_sync_on, M.v_sync_off)


def test_polarity_is_positive_for_800x600():
    """Both syncs positive for 800x600, both negative for 640x480. Monitors use the
    polarity PAIR to tell similar modes apart, so an inverted sync can read as
    'out of range' even when every count is correct."""
    assert (M.h_pol, M.v_pol) == (1, 1)
    assert (vga_ref.MODE_640x480.h_pol, vga_ref.MODE_640x480.v_pol) == (0, 0)

    t = vga_ref.VGATiming(M)
    t.hcount, t.vcount = 0, 0
    assert t.sample()["hsync"] == 0, "positive polarity must idle LOW"
    t.hcount = M.h_sync_on
    assert t.sample()["hsync"] == 1, "positive polarity must pulse HIGH"

    n = vga_ref.VGATiming(vga_ref.MODE_640x480)
    n.hcount, n.vcount = 0, 0
    assert n.sample()["hsync"] == 1, "negative polarity must idle HIGH"
    n.hcount = vga_ref.MODE_640x480.h_sync_on
    assert n.sample()["hsync"] == 0, "negative polarity must pulse LOW"


def test_one_frame_shape():
    """Walk a whole frame and count what came out. This is the check that would catch an
    off-by-one in the wrap conditions, which no amount of staring at comparators will."""
    n_active = n_hsync = n_vsync = n_vblank = n_framestart = 0
    seen = set()
    for s in vga_ref.frame(M):
        n_active += s["active"]
        n_hsync += (s["hsync"] == M.h_pol)
        n_vsync += (s["vsync"] == M.v_pol)
        n_vblank += s["vblank"]
        n_framestart += s["frame_start"]
        seen.add((s["hcount"], s["vcount"]))

    assert n_active == 480000, "active clocks {} != 800*600".format(n_active)
    assert n_hsync == 128 * M.v_total, "hsync clocks {}".format(n_hsync)
    assert n_vsync == 4 * M.h_total, "vsync clocks {}".format(n_vsync)
    assert n_vblank == 28 * M.h_total, "vblank clocks {}".format(n_vblank)
    assert n_framestart == 1, "frame_start must pulse exactly once per frame, got {}".format(
        n_framestart)
    # every (h,v) pair visited exactly once
    assert len(seen) == M.clocks_per_frame, len(seen)


def test_counters_wrap_and_repeat():
    """After exactly one frame the model must be back at (0,0) -- no drift."""
    t = vga_ref.VGATiming(M)
    for _ in range(M.clocks_per_frame):
        t.step()
    assert (t.hcount, t.vcount) == (0, 0), (t.hcount, t.vcount)
    # and hcount must never exceed its total
    t2 = vga_ref.VGATiming(M)
    for _ in range(3 * M.h_total):
        s = t2.step()
        assert s["hcount"] < M.h_total and s["vcount"] < M.v_total


def test_active_implies_coordinates_in_range():
    """px/py are only claimed valid while active -- prove that claim holds."""
    for s in vga_ref.frame(M):
        if s["active"]:
            assert 0 <= s["px"] < M.h_vis, s["px"]
            assert 0 <= s["py"] < M.v_vis, s["py"]
            assert not s["vblank"], "active and vblank must be mutually exclusive"


def _main():
    checks = [test_totals_match_vesa, test_derived_rates, test_counter_widths,
              test_sync_windows, test_polarity_is_positive_for_800x600,
              test_one_frame_shape, test_counters_wrap_and_repeat,
              test_active_implies_coordinates_in_range]
    print("SeeTheBeat VGA timing golden-model self-check")
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
