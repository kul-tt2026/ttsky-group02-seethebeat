/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * VGA timing generator for SeeTheBeat -- the front of the Part 2 output path.
 *
 * Two counters and some comparators, nothing more. Defaults are VESA 800x600 @ 60 Hz on a
 * 40 MHz pixel clock; the 640x480 @ 25.175 MHz fallback is reachable purely by overriding
 * the parameters (see model/vga_ref.py MODE_640x480), which is why every count here is a
 * parameter rather than a literal.
 *
 * Each scanline and each frame runs: visible -> front porch -> sync pulse -> back porch.
 * There is NO clock on a VGA cable -- the monitor recovers all timing from the sync edges,
 * so these counts are a hard contract. Bit-exact to model/vga_ref.py, checked cycle-by-cycle
 * over a whole frame by test_units/test_vga_timing.py.
 *
 * TWO THINGS THE CONSUMER MUST HONOUR:
 *   1. Colour MUST be forced black whenever `active` is low. Light in the porches makes a
 *      monitor refuse to lock or shift the image. (`rgb = active ? colour : 6'b0`.)
 *   2. `px`/`py` are only meaningful while `active` is high. Outside it they keep counting
 *      through the blanking regions and exceed the visible range.
 *
 * `vblank` / `frame_start` exist for Part 2 Phase 2: the once-per-frame `visual_state`
 * refresh from the MCU runs in the blanking interval (183,168 of the 663,168 clocks in a
 * frame -- 27.6% -- are blanking, which is the per-frame budget).
 */

`default_nettype none

// NOTE ON PARAMETER TYPES: the count parameters below are deliberately UNTYPED, not
// `parameter integer`. `integer` would make every expression built from them explicitly
// 32-bit signed, so deriving a narrow constant from one (e.g. a counter's wrap value) reads
// to the linter as a real width truncation and trips WIDTHTRUNC -- even though the value
// provably fits. Left untyped, the arithmetic stays an unsized constant expression that is
// sized to its context. HW/VW keep `integer` because they are only ever used AS widths.
module vga_timing #(
    // ---- horizontal, in pixels ----
    parameter H_VIS  = 800,
    parameter H_FP   = 40,
    parameter H_SYNC = 128,
    parameter H_BP   = 88,
    // ---- vertical, in lines ----
    parameter V_VIS  = 600,
    parameter V_FP   = 1,
    parameter V_SYNC = 4,
    parameter V_BP   = 23,
    // ---- sync polarity: 1 = positive (idle low, pulse high). 800x600 is (1,1);
    //      640x480 is (0,0). Monitors use the PAIR to disambiguate modes. ----
    parameter H_POL = 1'b1,
    parameter V_POL = 1'b1,
    // ---- DERIVED -- do not override (the guard below rejects it) ----
    parameter H_TOTAL         = H_VIS + H_FP + H_SYNC + H_BP,
    parameter V_TOTAL         = V_VIS + V_FP + V_SYNC + V_BP,
    parameter integer HW      = $clog2(H_TOTAL),
    parameter integer VW      = $clog2(V_TOTAL)
) (
    input  wire          clk,
    input  wire          rst_n,

    output wire [HW-1:0] px,           // pixel x -- valid only while `active`
    output wire [VW-1:0] py,           // pixel y -- valid only while `active`
    output wire          active,       // inside the visible area: show colour
    output wire          hsync,        // polarity already applied
    output wire          vsync,
    output wire          vblank,       // inside vertical blanking
    output wire          frame_start   // 1-clock pulse at the start of vertical blanking
);

  // Build guard: HW/VW are derived; overriding them would silently truncate the counters
  // and corrupt the timing in a way that only shows up on a monitor.
  initial begin
    if (HW != $clog2(H_TOTAL) || VW != $clog2(V_TOTAL))
      $error("vga_timing: HW/VW are derived parameters and must not be overridden");
  end

  // ---- region boundaries ----
  // Left UNSIZED on purpose (see the parameter-type note above): each is only ever compared
  // against hcount/vcount, where an unsized constant is sized to the counter's width. Giving
  // them an explicit [HW-1:0] would re-introduce the 32-bit -> narrow assignment that
  // WIDTHTRUNC flags. Every value is < 2**HW (resp. 2**VW) by construction, because
  // HW = $clog2(H_TOTAL) and all four boundaries are <= H_TOTAL.
  localparam H_MAX      = H_TOTAL - 1;
  localparam H_VIS_C    = H_VIS;
  localparam H_SYNC_ON  = H_VIS + H_FP;                 // first sync clock
  localparam H_SYNC_OFF = H_VIS + H_FP + H_SYNC;        // first clock after it

  localparam V_MAX      = V_TOTAL - 1;
  localparam V_VIS_C    = V_VIS;
  localparam V_SYNC_ON  = V_VIS + V_FP;
  localparam V_SYNC_OFF = V_VIS + V_FP + V_SYNC;

  // ---- these two DO need explicit widths: they are assigned INTO the counters, so their
  //      width must equal the counter's exactly. Built from concatenations, so they are
  //      HW/VW bits by construction with no truncation for the linter to flag. ----
  localparam [HW-1:0] H_ZERO = {HW{1'b0}};
  localparam [HW-1:0] H_ONE  = {{(HW-1){1'b0}}, 1'b1};
  localparam [VW-1:0] V_ZERO = {VW{1'b0}};
  localparam [VW-1:0] V_ONE  = {{(VW-1){1'b0}}, 1'b1};

  // ---- the two counters: hcount every clock, vcount when a line completes ----
  reg [HW-1:0] hcount;
  reg [VW-1:0] vcount;

  wire h_last = (hcount == H_MAX);
  wire v_last = (vcount == V_MAX);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      hcount <= H_ZERO;
      vcount <= V_ZERO;
    end else if (h_last) begin
      hcount <= H_ZERO;
      vcount <= v_last ? V_ZERO : (vcount + V_ONE);
    end else begin
      hcount <= hcount + H_ONE;
    end
  end

  // ---- everything else is a comparator on the current counter values ----
  wire h_pulse = (hcount >= H_SYNC_ON) && (hcount < H_SYNC_OFF);
  wire v_pulse = (vcount >= V_SYNC_ON) && (vcount < V_SYNC_OFF);

  assign hsync       = H_POL ? h_pulse : ~h_pulse;
  assign vsync       = V_POL ? v_pulse : ~v_pulse;
  assign active      = (hcount < H_VIS_C) && (vcount < V_VIS_C);
  assign vblank      = (vcount >= V_VIS_C);
  assign frame_start = (vcount == V_VIS_C) && (hcount == H_ZERO);
  assign px          = hcount;
  assign py          = vcount;

endmodule
