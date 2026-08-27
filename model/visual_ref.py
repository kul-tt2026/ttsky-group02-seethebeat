# SPDX-FileCopyrightText: © 2026 Jonas Creyns, Giel Swenters
# SPDX-License-Identifier: Apache-2.0
"""
visual_ref.py -- golden model of the SeeTheBeat on-chip visual back-end:
`src/visual_state.v` (what the MCU publishes each frame) and `src/pixel_gen.v`
(the procedural renderer). Bit-exact reference for both.

ARCHITECTURE RECAP (CLAUDE.md sec.8, revised 2026-08-25)
  The MCU decides, the chip draws. Firmware computes bin magnitude, band energies, beat
  detection and decay, then publishes a small `visual_state` block. The chip reads it once
  per frame during vblank and renders every pixel as f(px, py, visual_state) -- no frame
  buffer, no stored objects.

WHAT IS COMMITTED TO SILICON HERE  (the part worth reviewing before tape-out)
  * NBANDS = 16 zones, BAND_W = 5 bits each, plus a 5-bit flash level.
  * The zone GEOMETRY below -- which screen region belongs to which band, and which way it
    fills. This is comparators on (px, py), so it is cheap, but it IS in silicon.
  * The per-group HUE. Everything else about the look -- which frequency feeds which band,
    how loud is "full", attack/decay, beat sensitivity -- is firmware and can change after
    tape-out.

  Layout (800x600), band 0 = lowest frequency, following CLAUDE.md sec.8's sketch
  "bass -> bottom strip, mids -> side wings, highs -> centre":

      px:  0        120                           680        800
           +--------+------------------------------+----------+  py=0
           |   L4   | C12 | C13 | C14 | C15         |    R8    |   highs HANG DOWN
           +--------+   (4 columns of 140,          +----------+   from the top,
           |   L5   |    filling DOWNWARD           |    R9    |   360 deep
           +--------+     from py=0, 360 deep)      +----------+
           |   L6   |                               |    R10   |
           +--------+                               +----------+
           |   L7   |                               |    R11   |
           +--------+------------------------------+----------+  py=360
           |   B0   |    B1     |    B2    |    B3             |
           |          bass, 240 deep, fills UPWARD             |
           +--------------------------------------------------+  py=600
            wings 120 deep, fill inward, rows of 90

  Each zone behaves as a level meter: the band value sets how far the zone fills from its
  base ("energy bloom"), and the top bits of the band set the brightness. A silent band is
  black, which is the DJ aesthetic we want as the default.
"""

# ---- geometry (must match the vga_timing instance driving it) ----
H_VIS = 800
V_VIS = 600

# ---- visual_state shape: THIS IS THE SILICON COMMITMENT ----
NBANDS = 16
BAND_W = 5                      # 0..31 per band
FLASH_W = 5                     # 0..31 global kick flash
BAND_MAX = (1 << BAND_W) - 1

# ---- region boundaries ----
# REBALANCED 2026-08-27 (Giel): bass deserves more of the screen, highs less.
#   bass   800 x 240 = 192,000 px  (was 800 x 120 =  96,000)  -> +100%
#   highs  560 x 360 = 201,600 px  (was 560 x 480 = 268,800)  ->  -25%
#   wings  120 x 360 x2 = 86,400   (was 120 x 480 x2 = 115,200) -> -25%
# and the highs now hang DOWNWARD FROM THE TOP of the screen instead of growing up from the
# middle, so high frequencies read as high on the screen and bass rises to meet them.
BOTTOM_TOP = 360                # bass strip occupies py >= 360 (240 px tall)
WING_W = 120                    # left wing px < 120, right wing px >= 680
CENTRE_L = WING_W               # centre spans [120, 680)
CENTRE_R = H_VIS - WING_W

BOTTOM_SPLIT = H_VIS // 4       # 200 px per bass zone
WING_SPLIT = BOTTOM_TOP // 4    # 90 px per wing zone
CENTRE_SPLIT = (CENTRE_R - CENTRE_L) // 4   # 140 px per centre column

# ---- fill scaling: depth < band * MUL ----
# MUL is chosen per region so a full-scale band (31) just covers that zone's depth without
# wasting range. All three are multiples of 4, so the hardware computes base = band<<2 once
# and then base, base<<1, or base<<1 + base -- one shift and one adder, no multiplier.
#   wings   120 deep -> x4  = 124  (band 30 fills)
#   bass    240 deep -> x8  = 248  (band 30 fills)
#   centre  360 deep -> x12 = 372  (band 30 fills)
# Getting this wrong is not cosmetic: with 160 px wings in the first draft a full-scale band
# reached only 124 of 160, so the wings could never look full. The golden model's
# `test_full_scale_band_fills_its_zone` caught it before any RTL was written -- and it is
# what keeps this rebalance honest too.
MUL_WING = 4
MUL_BASS = 8
MUL_CENTRE = 12

# ---- animation: the "breathing" zone edge (Part 2, Phase 1.2 / 5.2) ----
# 800x600 pixels cannot be stored, so nothing can MOVE by being remembered -- the only
# clock available to a stateless renderer is a frame counter, and every animation has to be
# a function of (position, time, energy). A bouncing SPRITE would need per-object storage;
# a bouncing BRIGHTNESS EDGE is just arithmetic on `frame`, so it is nearly free.
#
# Here the fill threshold gains a small time-varying offset, so each bar's tip drifts in and
# out by a few pixels: the picture breathes instead of sitting still between beats.
FRAME_W = 8                     # frame counter width (wraps every 256 frames ~ 4.3 s)
WOBBLE_STEP = 2                 # config units are 2 px, so a 5-bit field reaches 0..62
# History worth keeping: the first version used 7 px, and the whole breathing range was then
# SMALLER than a single band increment in the bass (8 px) and centre (12 px) zones -- 82% of
# the screen -- so the effect sat below the quantisation of the thing it modulates and was
# simply invisible. The lesson generalises: an effect that modulates a quantised quantity
# must span at least a few of its steps to be seen at all.
#
# The amplitude is now FIRMWARE-CONTROLLED (config word 18) rather than a fixed parameter,
# because a value you cannot retune after tape-out is a value you will get wrong. 63 is only
# the ceiling the hardware can express.
# WOBBLE_MAX is DERIVED, not chosen: the ceiling is whatever the config field can ask for.
# An independent ceiling parameter in the RTL was dead logic -- it could only ever be >= the
# field's maximum, so its clamp never fired (Verilator CMPCONST), and deleting the clamp
# then left the parameter unused. The encoding IS the ceiling.
WOBBLE_MAX = ((1 << BAND_W) - 1) * WOBBLE_STEP   # 62 px

def wobble(frame, amp_cfg=0):
    """A triangle wave on the frame counter: 0 -> 7 -> 0 over 256 frames (~4.3 s at 60 Hz).

    A triangle, not a sine: the CORDIC cannot help here. It is iterative (21 clocks per
    result) and the renderer needs a value EVERY pixel clock, so a per-pixel sine is
    impossible by construction. A triangle from the counter's own bits costs a handful of
    gates and reads identically once it is driving a soft edge.

    `wobble(0) == 0` deliberately, so a frame-0 render is the un-animated picture.

    `amp_cfg` is config word 18: the peak amplitude in units of WOBBLE_STEP pixels. The
    triangle is CLIPPED to it rather than scaled, so a low setting gives a swell that
    reaches its cap and holds briefly -- which reads well and costs one comparator.
    amp_cfg == 0 means no breathing at all, which is a legitimate setting and the state an
    unwritten config region leaves the chip in.
    """
    phase = (frame >> 1) & 0x7F                  # advance every 2 frames, 128 steps
    tri = (63 - (phase & 0x3F)) if (phase & 0x40) else (phase & 0x3F)   # 0..63
    cap = amp_cfg * WOBBLE_STEP                  # 0..62, mirroring {cfg2, 1'b0}
    return cap if tri > cap else tri

# ---- per-group hue as a 3-bit mask {r, g, b} ----
# With 1 bit per channel in the mask there are 7 non-black hues available:
#   100 red   010 green  001 blue   110 yellow  101 magenta  011 cyan  111 white
HUE_BASS = 0b100                # red
HUE_LOWMID = 0b101              # magenta
HUE_HIMID = 0b011               # cyan
HUE_HIGH = 0b010                # green

# ---- PALETTES: firmware picks one of four hue sets (cfg.palette) ----
# Indexed [palette][group], group = 0 bass, 1 low-mid, 2 high-mid, 3 highs.
# Palette 0 MUST be the original scheme, so cfg = 0 behaves exactly as before.
# These are artistic placeholders -- easy to retune, and worth judging in the preview
# rather than on paper.
PALETTES = [
    [HUE_BASS,  HUE_LOWMID, HUE_HIMID, HUE_HIGH],   # 0 classic: red / magenta / cyan / green
    [0b001,     0b011,      0b111,     0b011],      # 1 ice:     blue / cyan / white / cyan
    [0b100,     0b110,      0b111,     0b110],      # 2 fire:    red / yellow / white / yellow
    [0b101,     0b001,      0b010,     0b111],      # 3 neon:    magenta / blue / green / white
]

# ---- config register (CFG address 17), 5 bits ----
# Layout is chosen so that ALL-ZERO means "behave exactly as before". That is a safety
# property, not a convenience: an unwritten MCU config region reads back 0, so firmware
# that only publishes bands must still get a normal picture. Encoding brightness as a
# DIM amount rather than a CAP is what makes that true -- a cap of 0 would blank the
# screen on any firmware that forgot to set it.
CFG_ADDR = NBANDS + 1           # 17
CFG2_ADDR = NBANDS + 2          # 18: wobble amplitude, in WOBBLE_STEP-pixel units
CFG_BW_BIT = 0                  # 1 = greyscale
CFG_PALETTE_SHIFT = 1           # bits [2:1]
CFG_DIM_SHIFT = 3               # bits [4:3], 0 = full brightness


def cfg_fields(cfg):
    """Unpack the 5-bit config register -> (bw, palette, cap)."""
    bw = (cfg >> CFG_BW_BIT) & 1
    palette = (cfg >> CFG_PALETTE_SHIFT) & 0b11
    dim = (cfg >> CFG_DIM_SHIFT) & 0b11
    return bw, palette, 3 - dim          # cap: 3 = full, 0 = black

# ---- power-on defaults: a ramp across all 16 bands, so the chip draws a readable
#      picture BEFORE any firmware exists. This is the bring-up pattern, in shipping
#      code -- every zone lights at a different height, which checks the geometry, the
#      colour mapping and the blanking gate in one look at a monitor.
DEFAULT_BANDS = [(i << 1) | 1 for i in range(NBANDS)]     # 1, 3, 5, ... 31
DEFAULT_FLASH = 0


def zone_of(px, py):
    """
    Decode a pixel to (zone, depth, mul, hue).

    `depth` is the distance from the zone's BASE -- the edge it fills FROM -- so one
    comparison `depth < band * mul` serves all four fill directions.
    """
    if py >= BOTTOM_TOP:
        # bass: bottom strip, four 200 px columns, 240 deep, filling UPWARD
        z = px // BOTTOM_SPLIT
        if z > 3:
            z = 3
        return z, (V_VIS - 1 - py), MUL_BASS, HUE_BASS

    if px < WING_W:
        # low-mid: left wing, four 90 px rows, 120 deep, filling RIGHTWARD
        z = 4 + (py // WING_SPLIT)
        return z, px, MUL_WING, HUE_LOWMID

    if px >= CENTRE_R:
        # high-mid: right wing, four rows, filling LEFTWARD
        z = 8 + (py // WING_SPLIT)
        return z, (H_VIS - 1 - px), MUL_WING, HUE_HIMID

    # highs: centre, four 140 px columns, 360 deep, hanging DOWNWARD FROM THE TOP
    z = 12 + ((px - CENTRE_L) // CENTRE_SPLIT)
    if z > 15:
        z = 15
    return z, py, MUL_CENTRE, HUE_HIGH


def level_of(band):
    """Brightness 0..3 from a 5-bit band value.

    A lit pixel is never level 0 -- otherwise quiet-but-present bands would draw an
    invisible bar and the meter would look broken. The FILL HEIGHT carries the fine
    detail (all 5 bits); brightness is the coarse cue, and the Pmod only has 4 levels
    per channel anyway.
    """
    top = (band >> (BAND_W - 2)) & 0b11
    return top if top else 1


def _sat3(v):
    return 3 if v > 3 else v


def _cap(v, cap):
    """Saturate to the configured ceiling. cap == 3 is ordinary 2-bit saturation."""
    return cap if v > cap else v


def pixel(px, py, active, bands, flash, frame=0, cfg=0, cfg2=0):
    """The colour at (px, py) on frame `frame`. Returns (r, g, b), each 0..3.

    `frame` defaults to 0 (the un-animated picture, wobble(0) == 0) and `cfg` to 0
    (classic palette, colour, full brightness) -- so both defaults reproduce the design
    exactly as it was before either was added.
    """
    if not active:
        return (0, 0, 0)        # blanking MUST be black

    bw, palette, cap = cfg_fields(cfg)
    z, depth, mul, group_hue = zone_of(px, py)
    group = z >> 2                                   # 0 bass, 1 low-mid, 2 high-mid, 3 highs
    hue = 0b111 if bw else PALETTES[palette][group]
    band = bands[z]

    # A SILENT band must stay perfectly black -- the wobble may only ever extend a bar that
    # is already lit, never light one that should be dark. Getting this wrong would make the
    # whole screen shimmer faintly during quiet passages, which is exactly the opposite of
    # the mostly-black look we want.
    fill = 0 if band == 0 else (band * mul) + wobble(frame, cfg2)

    r = g = b = 0
    if depth < fill:                  # inside the filled part of the zone
        lvl = level_of(band)
        r = lvl if (hue >> 2) & 1 else 0
        g = lvl if (hue >> 1) & 1 else 0
        b = lvl if (hue >> 0) & 1 else 0

    # kick flash: a global white lift on every pixel, decaying in firmware.
    # The brightness cap is applied HERE, as the saturation ceiling, so it dims the flash
    # too -- a "global brightness cap" that the kick punched straight through would not be
    # much of a cap. At cap == 3 this is identical to plain saturation.
    f = (flash >> (FLASH_W - 2)) & 0b11
    r, g, b = _cap(r + f, cap), _cap(g + f, cap), _cap(b + f, cap)
    return (r, g, b)


def pack_pmod(hsync, vsync, r, g, b):
    """uo_out = {hsync, B0, G0, R0, vsync, B1, G1, R1} for the Tiny VGA Pmod.

    The pin NAMES are the trap: the pin labelled R1 carries r[1], the MSB.
    """
    return ((hsync & 1) << 7 | ((b >> 0) & 1) << 6 | ((g >> 0) & 1) << 5 |
            ((r >> 0) & 1) << 4 | (vsync & 1) << 3 | ((b >> 1) & 1) << 2 |
            ((g >> 1) & 1) << 1 | ((r >> 1) & 1) << 0)


def uo_out(hsync, vsync, px, py, active, bands, flash, frame=0, cfg=0, cfg2=0):
    """Full path: zone -> fill(+wobble) -> palette -> flash/cap -> blanking -> Pmod."""
    return pack_pmod(hsync, vsync,
                     *pixel(px, py, active, bands, flash, frame, cfg, cfg2))


class VisualState(object):
    """Model of src/visual_state.v -- the register file the MCU writes each vblank.

    Address map: 0..NBANDS-1 are the bands, NBANDS is the flash level.
    """

    ADDR_FLASH = NBANDS
    ADDR_CFG = CFG_ADDR
    ADDR_CFG2 = CFG2_ADDR

    def __init__(self):
        self.reset()

    def reset(self):
        self.bands = list(DEFAULT_BANDS)
        self.flash = DEFAULT_FLASH
        self.cfg = 0                     # all-zero = classic palette, colour, full bright
        self.cfg2 = 0                    # wobble amplitude; 0 = breathing off

    def write(self, addr, data):
        if addr < NBANDS:
            self.bands[addr] = data & BAND_MAX
        elif addr == self.ADDR_FLASH:
            self.flash = data & ((1 << FLASH_W) - 1)
        elif addr == self.ADDR_CFG:
            self.cfg = data & BAND_MAX
        elif addr == self.ADDR_CFG2:
            self.cfg2 = data & BAND_MAX
        # addresses above ADDR_CFG2 are ignored (reserved for further config)

    def read_band(self, zone):
        return self.bands[zone & (NBANDS - 1)]
