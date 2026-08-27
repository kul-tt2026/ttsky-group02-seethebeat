/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * pixel_gen -- the procedural renderer. Purely combinational f(px, py, visual_state):
 * no frame buffer, no stored objects, nothing that moves independently. An 800x600 frame
 * at 6 bpp is 360 kB against ~160 BYTES of on-chip memory, so the colour of a pixel must
 * be COMPUTED in the single clock that pixel is emitted. That constraint is the whole
 * reason the visuals are zone-based rather than particle-based (CLAUDE.md sec.8).
 *
 * Each zone is a level meter: its band value sets how far it fills from its base (the
 * "energy bloom"), and the top bits of the band set its brightness. A silent band is
 * black -- the mostly-black default.
 *
 * COMMITTED TO SILICON HERE (worth reviewing before tape-out): the zone geometry and the
 * per-group hue. Which frequency feeds which band, how loud counts as full, attack/decay
 * and beat sensitivity are all firmware and remain changeable after tape-out.
 *
 *      px:  0        120                           680        800
 *           +--------+------------------------------+----------+  py=0
 *           |   L4   | C12 | C13 | C14 | C15         |    R8    |  highs HANG DOWN
 *           +--------+   (4 columns of 140,          +----------+  from the top,
 *           |   L5   |    filling DOWNWARD           |    R9    |  360 deep
 *           +--------+     from py=0, 360 deep)      +----------+
 *           |   L6   |                               |    R10   |
 *           +--------+                               +----------+
 *           |   L7   |                               |    R11   |
 *           +--------+------------------------------+----------+  py=360
 *           |   B0   |    B1     |    B2    |    B3             |
 *           |          bass, 240 deep, fills UPWARD             |
 *           +--------------------------------------------------+  py=600
 *            wings 120 deep, fill inward, rows of 90
 *
 * REBALANCED 2026-08-27 (Giel): bass +100% of its former area, highs -25% and moved to the
 * top of the screen so high frequencies read as high. Fill scaling is per-region so a
 * full-scale band just covers its zone: wings x4, bass x8, centre x12 -- all multiples of 4,
 * so one shift and one adder do it, no multiplier. Bit-exact to model/visual_ref.py.
 *
 * Parameter typing follows CLAUDE.md sec.7 (untyped counts -> no spurious WIDTHTRUNC).
 */

`default_nettype none

module pixel_gen #(
    parameter H_VIS   = 800,
    parameter V_VIS   = 600,
    parameter NBANDS  = 16,
    parameter BAND_W  = 5,
    parameter FLASH_W = 5,
    parameter PXW     = 11,           // width of px (vga_timing's HW)
    parameter PYW     = 10,           // width of py (vga_timing's VW)
    // ---- DERIVED -- do not override ----
    parameter integer ZW = $clog2(NBANDS)
) (
    input  wire [PXW-1:0]      px,
    input  wire [PYW-1:0]      py,
    input  wire                active,      // low during blanking -> forced black

    output wire [ZW-1:0]       zone,        // -> visual_state.rd_zone
    input  wire [BAND_W-1:0]   band,        // <- visual_state.band (for `zone`)
    input  wire [FLASH_W-1:0]  flash,

    output wire [1:0]          r,
    output wire [1:0]          g,
    output wire [1:0]          b
);

  // ---- geometry (REBALANCED 2026-08-27: more screen for bass, less for highs) ----
  localparam BOTTOM_TOP   = 360;                  // bass strip: py >= 360, so 240 px deep
  localparam WING_W       = 160;                  // wings: px < 120 and px >= 680
  localparam CENTRE_L     = WING_W;
  localparam CENTRE_R     = H_VIS - WING_W;       // 680
  localparam BOTTOM_SPLIT = H_VIS / 4;            // 200 px per bass zone
  localparam WING_SPLIT   = BOTTOM_TOP / 4;       // 90 px per wing row
  localparam CENTRE_SPLIT = (CENTRE_R - CENTRE_L) / 4;   // 140 px per centre column

  localparam H_LAST  = H_VIS - 1;                 // 799
  localparam V_LAST  = V_VIS - 1;                 // 599

  // ---- which region ----
  wire in_bottom = (py >= BOTTOM_TOP);
  wire in_left   = !in_bottom && (px <  CENTRE_L);
  wire in_right  = !in_bottom && (px >= CENTRE_R);
  wire in_centre = !in_bottom && !in_left && !in_right;

  // ---- sub-index within the region (comparator chains: the splits are not powers of 2) ----
  wire [1:0] bottom_i = (px < BOTTOM_SPLIT    ) ? 2'd0 :
                        (px < BOTTOM_SPLIT * 2) ? 2'd1 :
                        (px < BOTTOM_SPLIT * 3) ? 2'd2 : 2'd3;

  wire [1:0] wing_i   = (py < WING_SPLIT    ) ? 2'd0 :
                        (py < WING_SPLIT * 2) ? 2'd1 :
                        (py < WING_SPLIT * 3) ? 2'd2 : 2'd3;

  wire [1:0] centre_i = (px < CENTRE_L + CENTRE_SPLIT    ) ? 2'd0 :
                        (px < CENTRE_L + CENTRE_SPLIT * 2) ? 2'd1 :
                        (px < CENTRE_L + CENTRE_SPLIT * 3) ? 2'd2 : 2'd3;

  wire [1:0] sub = in_bottom ? bottom_i : in_centre ? centre_i : wing_i;

  // zone numbering matches model/visual_ref.py: 0-3 bass, 4-7 left, 8-11 right, 12-15 centre
  assign zone = in_bottom ? {2'b00, sub} :
                in_left   ? {2'b01, sub} :
                in_right  ? {2'b10, sub} : {2'b11, sub};

  // ---- depth: distance from the edge the zone fills FROM, so one compare serves all
  //      four fill directions. Computed at PXW bits so nothing truncates; during blanking
  //      px/py run past the visible area and depth is meaningless, but `active` gates the
  //      output to black so it never reaches a pin. ----
  wire [PXW-1:0] py_e = {{(PXW-PYW){1'b0}}, py};

  wire [PXW-1:0] depth = in_bottom ? (V_LAST - py_e) :      // bass fills UPWARD
                         in_left   ? px              :      // fills rightward
                         in_right  ? (H_LAST - px)   :      // fills leftward
                                     py_e;                  // highs HANG DOWN from py=0

  // ---- fill threshold: depth < band * MUL, with MUL chosen per region so a full-scale
  //      band just covers that zone's depth (wings 120 -> x4, bass 240 -> x8,
  //      centre 360 -> x12). All are multiples of 4, so one shift and one adder do it --
  //      no multiplier.
  wire [PXW-1:0] base  = {{(PXW-BAND_W-2){1'b0}}, band, 2'b00};   // band * 4
  wire [PXW-1:0] base2 = {base[PXW-2:0], 1'b0};                    // band * 8
  wire [PXW-1:0] fill  = in_bottom ? base2           :             // bass   x8
                         in_centre ? (base2 + base)  :             // centre x12
                                     base;                         // wings  x4

  wire lit = (depth < fill);

  // ---- brightness: top 2 bits of the band, but a lit pixel is never level 0, or a
  //      quiet-but-present band would draw an invisible bar and look broken. ----
  wire [1:0] top = band[BAND_W-1:BAND_W-2];
  wire [1:0] lvl = (top == 2'b00) ? 2'b01 : top;

  // ---- per-group hue: bass red, low-mid magenta, high-mid cyan, highs green ----
  wire hue_r = in_bottom | in_left;
  wire hue_g = in_right  | in_centre;
  wire hue_b = in_left   | in_right;

  wire [1:0] zr = (lit && hue_r) ? lvl : 2'b00;
  wire [1:0] zg = (lit && hue_g) ? lvl : 2'b00;
  wire [1:0] zb = (lit && hue_b) ? lvl : 2'b00;

  // ---- kick flash: a global white lift on every pixel, saturating (never wrapping --
  //      a wrapped flash would read as a black frame on the beat, the worst possible
  //      artefact). Firmware owns the decay. ----
  wire [1:0] fl = flash[FLASH_W-1:FLASH_W-2];

  // Only the top 2 bits of `flash` can ever reach a pin: the Pmod has 4 levels per channel,
  // so there is nothing finer to display. The low bits are still carried because firmware
  // runs its decay envelope at full resolution and publishes the whole value, and because
  // the planned fade + ordered-dither upgrade (PART2) consumes them without needing a
  // visual_state change. Sinking them explicitly, per CLAUDE.md sec.7 -- never a global waiver.
  // (Assumes FLASH_W >= 3, which visual_state's elaboration guard already enforces.)
  wire _unused_flash = &{1'b0, flash[FLASH_W-3:0]};

  wire [2:0] rsum = {1'b0, zr} + {1'b0, fl};
  wire [2:0] gsum = {1'b0, zg} + {1'b0, fl};
  wire [2:0] bsum = {1'b0, zb} + {1'b0, fl};

  wire [1:0] rsat = rsum[2] ? 2'b11 : rsum[1:0];
  wire [1:0] gsat = gsum[2] ? 2'b11 : gsum[1:0];
  wire [1:0] bsat = bsum[2] ? 2'b11 : bsum[1:0];

  // ---- blanking gate: light in the porches makes a monitor refuse to lock ----
  assign r = active ? rsat : 2'b00;
  assign g = active ? gsat : 2'b00;
  assign b = active ? bsat : 2'b00;

endmodule
