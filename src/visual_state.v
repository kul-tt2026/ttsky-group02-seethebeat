/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * visual_state -- the small register file the MCU publishes each frame, and the ONLY
 * visual state on the chip.
 *
 * Architecture (CLAUDE.md sec.8): the MCU decides, the chip draws. Firmware computes bin
 * magnitude, band energies, beat detection and decay, then writes them here during vblank;
 * the beam then re-reads a band roughly 600x per frame while rendering. That read rate is
 * exactly why this one piece of state cannot live on the MCU even though everything that
 * PRODUCES it does -- fetching it per pixel over the bus is impossible.
 *
 * Area: NBANDS*BAND_W + FLASH_W + 3*BAND_W flops (16*5 + 5 + 15 = 100) plus a NBANDS:1
 * read mux. This is
 * the single biggest area knob in Part 2, because it is raw DFFs: cost scales directly
 * with NBANDS x BAND_W. If utilisation gets tight, cut bands first, then bits -- the Pmod
 * only has 4 brightness levels per channel, so >4-5 bits per band is undisplayable anyway
 * (the fill height uses the fine detail, not the colour).
 *
 * Bit-exact to model/visual_ref.py's VisualState.
 *
 * Parameter typing follows CLAUDE.md sec.7: counts are UNTYPED so derived constants stay
 * unsized constant expressions and do not trip WIDTHTRUNC.
 */

`default_nettype none

module visual_state #(
    parameter NBANDS  = 16,           // number of frequency zones
    parameter BAND_W  = 5,            // bits per band value
    parameter FLASH_W = 5,            // bits of global kick-flash level
    // ---- DERIVED -- do not override ----
    parameter integer ZW = $clog2(NBANDS),        // 4: zone selector width
    parameter integer AW = $clog2(NBANDS + 1)     // 5: write-address width
) (
    input  wire               clk,
    input  wire               rst_n,

    // ---- write port: driven by the once-per-vblank refresh burst from the MCU ----
    input  wire               wr_en,
    input  wire [AW-1:0]      wr_addr,    // 0..NBANDS-1 = bands, NBANDS = flash
    input  wire [BAND_W-1:0]  wr_data,

    // ---- read port: combinational, driven by the beam every pixel ----
    input  wire [ZW-1:0]      rd_zone,
    output wire [BAND_W-1:0]  band,
    output wire [FLASH_W-1:0] flash,
    output wire [BAND_W-1:0]  cfg,         // {dim[1:0], palette[1:0], bw} -- see pixel_gen
    output wire [BAND_W-1:0]  cfg2,        // breathing amplitude -- see pixel_gen
    output wire [BAND_W-1:0]  cfg3         // {--, fade_sh[1:0], fade_en} -- see pixel_gen
);

  localparam ADDR_FLASH = NBANDS;        // 16
  localparam ADDR_CFG   = NBANDS + 1;    // 17
  localparam ADDR_CFG2  = NBANDS + 2;    // 18: breathing amplitude
  localparam ADDR_CFG3  = NBANDS + 3;    // 19: soft fade + ordered dither

  // The power-on default ramp below packs the zone index into the band value as
  // {index, 1'b1}, which needs BAND_W == ZW + 1. Guard it rather than truncating silently.
  initial begin
    if (BAND_W != ZW + 1)
      $error("visual_state: the default ramp assumes BAND_W == $clog2(NBANDS)+1 (got %0d, %0d)",
             BAND_W, ZW);
    if (FLASH_W > BAND_W)
      $error("visual_state: FLASH_W (%0d) cannot exceed the write-data width BAND_W (%0d)",
             FLASH_W, BAND_W);
  end

  reg [BAND_W-1:0]  bands [0:NBANDS-1];
  reg [FLASH_W-1:0] flash_r;
  // Config resets to ZERO, and zero is defined to mean "behave exactly as before":
  // classic palette, full colour, full brightness. That matters because an unwritten MCU
  // config region reads back 0 -- firmware that only publishes bands must still get a
  // normal picture. It is why brightness is encoded as a DIM amount rather than a CAP.
  reg [BAND_W-1:0]  cfg_r;
  reg [BAND_W-1:0]  cfg2_r;   // breathing amplitude; 0 = off
  reg [BAND_W-1:0]  cfg3_r;   // fade/dither; 0 = hard bar tips, exactly as before

  integer k;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      // POWER-ON DEFAULTS = the bring-up picture, in shipping code.
      // A ramp 1, 3, 5, ... 31 across the zones, so before any firmware exists the chip
      // already draws every zone at a different height. One look at a monitor then checks
      // the geometry, the colour mapping and the blanking gate at once -- which is what a
      // throwaway test pattern would have been for.
      for (k = 0; k < NBANDS; k = k + 1)
        bands[k] <= {k[ZW-1:0], 1'b1};
      flash_r <= {FLASH_W{1'b0}};
      cfg_r   <= {BAND_W{1'b0}};
      cfg2_r  <= {BAND_W{1'b0}};
      cfg3_r  <= {BAND_W{1'b0}};
    end else if (wr_en) begin
      if (wr_addr < ADDR_FLASH)
        bands[wr_addr[ZW-1:0]] <= wr_data;
      else if (wr_addr == ADDR_FLASH)
        flash_r <= wr_data[FLASH_W-1:0];
      else if (wr_addr == ADDR_CFG)
        cfg_r <= wr_data;
      else if (wr_addr == ADDR_CFG2)
        cfg2_r <= wr_data;
      else if (wr_addr == ADDR_CFG3)
        cfg3_r <= wr_data;
      // addresses above ADDR_CFG3 are ignored -- reserved for further config
    end
  end

  assign band  = bands[rd_zone];
  assign flash = flash_r;
  assign cfg   = cfg_r;
  assign cfg2  = cfg2_r;
  assign cfg3  = cfg3_r;

endmodule
