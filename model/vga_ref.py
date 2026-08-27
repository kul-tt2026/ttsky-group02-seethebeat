# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
vga_ref.py -- golden model of the SeeTheBeat VGA timing generator (src/vga_timing.v).

A VGA controller is two counters and some comparators. There is NO clock on a VGA cable:
the monitor recovers everything from the HSync/VSync edges, so the counts below are a hard
contract, not an approximation. See the learning notes, "Part 2, Phase 0".

Each scanline and each frame is split into four regions, in this order:

    visible -> front porch -> sync pulse -> back porch

`VGAMode` holds one such timing set; `step()` advances one pixel clock and returns the
signals the RTL must produce on that same clock. `src/vga_timing.v` is compared to this
cycle-by-cycle, bit-exact, over a whole frame.

Pure standard library, like the other models.
"""


class VGAMode(object):
    """One VESA timing set. Counts are in pixels (horizontal) / lines (vertical)."""

    def __init__(self, name, pixel_clock_hz,
                 h_vis, h_fp, h_sync, h_bp,
                 v_vis, v_fp, v_sync, v_bp,
                 h_pol, v_pol):
        self.name = name
        self.pixel_clock_hz = pixel_clock_hz
        self.h_vis, self.h_fp, self.h_sync, self.h_bp = h_vis, h_fp, h_sync, h_bp
        self.v_vis, self.v_fp, self.v_sync, self.v_bp = v_vis, v_fp, v_sync, v_bp
        self.h_pol, self.v_pol = h_pol, v_pol      # 1 = positive (idle low, pulse high)

    # ---- derived geometry ----
    @property
    def h_total(self):
        return self.h_vis + self.h_fp + self.h_sync + self.h_bp

    @property
    def v_total(self):
        return self.v_vis + self.v_fp + self.v_sync + self.v_bp

    @property
    def h_sync_on(self):
        """First hcount of the horizontal sync pulse."""
        return self.h_vis + self.h_fp

    @property
    def h_sync_off(self):
        """First hcount AFTER the horizontal sync pulse."""
        return self.h_vis + self.h_fp + self.h_sync

    @property
    def v_sync_on(self):
        return self.v_vis + self.v_fp

    @property
    def v_sync_off(self):
        return self.v_vis + self.v_fp + self.v_sync

    # ---- derived rates ----
    @property
    def line_rate_hz(self):
        return self.pixel_clock_hz / float(self.h_total)

    @property
    def frame_rate_hz(self):
        return self.pixel_clock_hz / float(self.h_total * self.v_total)

    @property
    def clocks_per_frame(self):
        return self.h_total * self.v_total

    @property
    def visible_pixels(self):
        return self.h_vis * self.v_vis

    @property
    def blanking_clocks(self):
        """Clocks per frame spent outside the visible area -- Part 2's per-frame budget."""
        return self.clocks_per_frame - self.visible_pixels

    # ---- counter widths the RTL must use ----
    @property
    def hw(self):
        return _clog2(self.h_total)

    @property
    def vw(self):
        return _clog2(self.v_total)

    def __repr__(self):
        return "<VGAMode {} {}x{}@{:.2f}Hz>".format(
            self.name, self.h_vis, self.v_vis, self.frame_rate_hz)


def _clog2(n):
    """$clog2: bits needed to represent 0..n-1 (matches Verilog's $clog2)."""
    b = 0
    while (1 << b) < n:
        b += 1
    return b


# ---- the committed mode, and the documented fallback ----
# VESA DMT. Both syncs are POSITIVE for 800x600@60; both NEGATIVE for 640x480@60 --
# monitors use the polarity pair to disambiguate modes, so this is not cosmetic.
MODE_800x600 = VGAMode("800x600@60", 40000000,
                       800, 40, 128, 88,
                       600, 1, 4, 23,
                       h_pol=1, v_pol=1)

MODE_640x480 = VGAMode("640x480@60", 25175000,
                       640, 16, 96, 48,
                       480, 10, 2, 33,
                       h_pol=0, v_pol=0)


class VGATiming(object):
    """
    Cycle-stepped model. One `step()` == one pixel clock.

    Returns a dict of the signals `src/vga_timing.v` drives on that clock:
        hcount, vcount : raw counter values
        px, py         : pixel coordinates (only meaningful while `active`)
        hsync, vsync   : sync outputs, already polarity-adjusted
        active         : inside the visible area -> colour must be shown
        vblank         : inside vertical blanking -> safe window for per-frame work
        frame_start    : 1-clock pulse at the very start of vertical blanking
    """

    def __init__(self, mode=MODE_800x600):
        self.m = mode
        self.reset()

    def reset(self):
        self.hcount = 0
        self.vcount = 0

    def sample(self):
        """The combinational outputs for the CURRENT counter values."""
        m = self.m
        h, v = self.hcount, self.vcount

        h_pulse = (h >= m.h_sync_on) and (h < m.h_sync_off)
        v_pulse = (v >= m.v_sync_on) and (v < m.v_sync_off)

        return {
            "hcount": h,
            "vcount": v,
            "px": h,
            "py": v,
            "hsync": int(h_pulse if m.h_pol else not h_pulse),
            "vsync": int(v_pulse if m.v_pol else not v_pulse),
            "active": int(h < m.h_vis and v < m.v_vis),
            "vblank": int(v >= m.v_vis),
            "frame_start": int(v == m.v_vis and h == 0),
        }

    def step(self):
        """Sample this clock's outputs, then advance the counters."""
        out = self.sample()
        m = self.m
        if self.hcount == m.h_total - 1:
            self.hcount = 0
            self.vcount = 0 if self.vcount == m.v_total - 1 else self.vcount + 1
        else:
            self.hcount += 1
        return out


def frame(mode=MODE_800x600):
    """Yield one full frame of samples (h_total * v_total of them)."""
    t = VGATiming(mode)
    for _ in range(mode.clocks_per_frame):
        yield t.step()
