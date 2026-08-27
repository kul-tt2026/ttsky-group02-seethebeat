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
    parameter FRAME_W = 8,            // frame-counter width (wraps every 256 frames)
    parameter WOBBLE_MAX = 63,        // HARD CEILING on the breathing amplitude, px
    // ---- DERIVED -- do not override ----
    parameter integer ZW = $clog2(NBANDS)
) (
    input  wire [PXW-1:0]      px,
    input  wire [PYW-1:0]      py,
    input  wire                active,      // low during blanking -> forced black

    output wire [ZW-1:0]       zone,        // -> visual_state.rd_zone
    input  wire [BAND_W-1:0]   band,        // <- visual_state.band (for `zone`)
    input  wire [FLASH_W-1:0]  flash,
    input  wire [FRAME_W-1:0]  frame,       // increments once per frame: the animation clock
    input  wire [BAND_W-1:0]   cfg,         // {dim[1:0], palette[1:0], bw}, 0 = classic
    input  wire [BAND_W-1:0]   cfg2,        // breathing amplitude, in 2-pixel units

    output wire [1:0]          r,
    output wire [1:0]          g,
    output wire [1:0]          b
);

  // The breathing triangle below reads phase[6] as its direction bit and phase[5:0] as the
  // ramp, i.e. it assumes a 7-bit phase == frame[FRAME_W-1:1] with FRAME_W == 8. Guard it
  // rather than let a changed FRAME_W silently reshape the animation.
  initial if (FRAME_W != 8)
    $error("pixel_gen: the breathing triangle assumes FRAME_W == 8 (got %0d)", FRAME_W);

  // ---- geometry (REBALANCED 2026-08-27: more screen for bass, less for highs) ----
  localparam BOTTOM_TOP   = 360;                  // bass strip: py >= 360, so 240 px deep
  // MUST match model/visual_ref.py WING_W. 120, not 160: at 160 the wings are 160 px deep
  // but a full-scale band only reaches 31*MUL_WING = 124, so they could never fill --
  // and every zone boundary from px=120 onward shifts, which is what broke test_pixel_gen.
  // model/test_geometry_sync.py now fails CI if this drifts from the model again.
  localparam WING_W       = 120;                  // wings: px < 120 and px >= 680
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
  wire [PXW-1:0] fill_raw = in_bottom ? base2           :          // bass   x8
                            in_centre ? (base2 + base)  :          // centre x12
                                        base;                      // wings  x4

  // ---- breathing edge (Phase 1.2 / 5.2): a small time-varying offset on the fill
  //      threshold, so each bar's tip drifts in and out by a few pixels instead of sitting
  //      still between beats.
  //
  //      A TRIANGLE, not a sine, and deliberately so: the CORDIC is iterative (21 clocks
  //      per result) while the renderer needs a value EVERY pixel clock, so a per-pixel
  //      sine is impossible by construction. A triangle off the counter's own bits costs a
  //      few gates and is indistinguishable once it drives a soft edge.
  wire [6:0] phase = frame[FRAME_W-1:1];                    // advance every 2 frames
  wire [5:0] tri_wave = phase[6] ? (6'd63 - phase[5:0]) : phase[5:0];   // 0..63
  // (`tri` is a Verilog reserved net type -- do not use it as a signal name.)

  // AMPLITUDE IS FIRMWARE-CONTROLLED (config word 18), not a fixed parameter: a value you
  // cannot retune after tape-out is a value you will get wrong. cfg2 gives the peak in
  // WOBBLE_STEP-pixel units, so 5 bits reach 0..62 px; WOBBLE_MAX is only the ceiling the
  // hardware can express. The triangle is CLIPPED to the cap rather than scaled -- one
  // comparator, and a low setting reads as a swell that reaches its cap and holds.
  // cfg2 == 0 means no breathing, which is a legitimate setting and where an unwritten
  // config region leaves the chip.
  wire [6:0] amp_raw = {cfg2, 1'b0};                        // cfg2 * 2
  wire [5:0] amp_cap = (amp_raw > {1'b0, WOBBLE_MAX[5:0]}) ? WOBBLE_MAX[5:0] : amp_raw[5:0];
  wire [5:0] wob     = (tri_wave > amp_cap) ? amp_cap : tri_wave;

  // A SILENT band must stay perfectly black: the wobble may only extend a bar that is
  // already lit, never light one that should be dark. Without this guard the whole screen
  // would shimmer faintly through quiet passages -- the opposite of the mostly-black look.
  wire silent = (band == {BAND_W{1'b0}});
  wire [PXW-1:0] fill = silent ? {PXW{1'b0}}
                               : (fill_raw + {{(PXW-6){1'b0}}, wob});

  wire lit = (depth < fill);

  // ---- brightness: top 2 bits of the band, but a lit pixel is never level 0, or a
  //      quiet-but-present band would draw an invisible bar and look broken. ----
  wire [1:0] top = band[BAND_W-1:BAND_W-2];
  wire [1:0] lvl = (top == 2'b00) ? 2'b01 : top;

  // ---- config: firmware-selectable look, fetched with visual_state each vblank ----
  // ALL-ZERO means "behave exactly as before" -- classic palette, colour, full brightness.
  // An unwritten MCU config region reads back 0, so firmware that only publishes bands
  // still gets a normal picture. That is why brightness is a DIM amount, not a CAP.
  wire       cfg_bw   = cfg[0];
  wire [1:0] cfg_pal  = cfg[2:1];
  wire [1:0] cfg_cap  = 2'd3 - cfg[4:3];        // dim 0 -> cap 3 (full), dim 3 -> cap 0

  // ---- hue: one of four palettes, indexed by [palette][group] ----
  // group: 0 bass, 1 low-mid, 2 high-mid, 3 highs. Palette 0 is the original scheme.
  // With 1 bit per channel there are 7 usable hues: R G B, yellow, magenta, cyan, white.
  wire [1:0] group = in_bottom ? 2'd0 : in_left ? 2'd1 : in_right ? 2'd2 : 2'd3;

  reg [2:0] pal_hue;
  always @(*) begin
    case ({cfg_pal, group})
      // palette 0 -- classic: red / magenta / cyan / green
      4'b00_00: pal_hue = 3'b100;  4'b00_01: pal_hue = 3'b101;
      4'b00_10: pal_hue = 3'b011;  4'b00_11: pal_hue = 3'b010;
      // palette 1 -- ice: blue / cyan / white / cyan
      4'b01_00: pal_hue = 3'b001;  4'b01_01: pal_hue = 3'b011;
      4'b01_10: pal_hue = 3'b111;  4'b01_11: pal_hue = 3'b011;
      // palette 2 -- fire: red / yellow / white / yellow
      4'b10_00: pal_hue = 3'b100;  4'b10_01: pal_hue = 3'b110;
      4'b10_10: pal_hue = 3'b111;  4'b10_11: pal_hue = 3'b110;
      // palette 3 -- neon: magenta / blue / green / white
      4'b11_00: pal_hue = 3'b101;  4'b11_01: pal_hue = 3'b001;
      4'b11_10: pal_hue = 3'b010;  default:  pal_hue = 3'b111;
    endcase
  end

  wire [2:0] hue = cfg_bw ? 3'b111 : pal_hue;   // B&W: drive all three channels equally
  wire hue_r = hue[2];
  wire hue_g = hue[1];
  wire hue_b = hue[0];

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

  // frame[0] is dropped on purpose: the breathing phase advances once every 2 frames, which
  // with a 128-step triangle is what sets the ~4.3 s period.
  // (amp_raw[6] is NOT sunk -- the 7-bit comparison in amp_cap reads it.)
  wire _unused_anim = &{1'b0, frame[0]};

  wire [2:0] rsum = {1'b0, zr} + {1'b0, fl};
  wire [2:0] gsum = {1'b0, zg} + {1'b0, fl};
  wire [2:0] bsum = {1'b0, zb} + {1'b0, fl};

  // Saturate to the CONFIGURED ceiling rather than a fixed 3. At cfg_cap == 3 this is
  // identical to plain 2-bit saturation, so the default path is unchanged; below that it
  // dims the flash as well as the zones -- a "global brightness cap" the kick punched
  // straight through would not be much of a cap.
  wire [2:0] cap_e = {1'b0, cfg_cap};
  wire [1:0] rsat  = (rsum > cap_e) ? cfg_cap : rsum[1:0];
  wire [1:0] gsat  = (gsum > cap_e) ? cfg_cap : gsum[1:0];
  wire [1:0] bsat  = (bsum > cap_e) ? cfg_cap : bsum[1:0];

  // ---- blanking gate: light in the porches makes a monitor refuse to lock ----
  assign r = active ? rsat : 2'b00;
  assign g = active ? gsat : 2'b00;
  assign b = active ? bsat : 2'b00;

endmodule
