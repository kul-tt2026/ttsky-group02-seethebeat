/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * fft_alu -- the SeeTheBeat arithmetic unit: the FFT butterfly plus THE one CORDIC.
 *
 * Why this wrapper still exists now that it hosts a single core: the CORDIC is the design's
 * one expensive shared resource (~0.7 tile). Owning it here rather than inside `butterfly`
 * means a future second client -- Part 2's pixel generator needs a sine for the breathing
 * zone edges -- can be muxed in at this level without touching the butterfly or growing a
 * second CORDIC.
 *
 * REMOVED 2026-08-25: the magnitude read-out (op=1 -> spectrum_mag, CORDIC VECTOR) and with
 * it the operand muxes on every CORDIC input. Magnitude + log now runs in MCU firmware --
 * the MCU already holds the whole spectrum, so it is free there, while on chip those muxes
 * were the congestion that took utilisation to ~80% and broke the GDS render.
 * model/spectrum_ref.py is retained as the reference the firmware must match bit-for-bit.
 *
 * Note `mode` is tied to ROTATE below: synthesis constant-propagates the CORDIC's vectoring
 * path (the mode_reg flop and the d_pos mux) away, a further saving on top of the muxes.
 */

`default_nettype none

module fft_alu #(
    parameter integer DW  = 16,
    parameter integer AW  = 20,
    parameter integer XYW = 22
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 start,
    input  wire signed [DW-1:0] a_re,
    input  wire signed [DW-1:0] a_im,
    input  wire signed [DW-1:0] b_re,
    input  wire signed [DW-1:0] b_im,
    input  wire signed [AW-1:0] angle,      // twiddle angle
    output wire signed [DW-1:0] a_re_o,     // butterfly A' = A + W*B
    output wire signed [DW-1:0] a_im_o,
    output wire signed [DW-1:0] b_re_o,     // butterfly B' = A - W*B
    output wire signed [DW-1:0] b_im_o,
    output wire                 done
);

  // ---- the ONE CORDIC's outputs ----
  wire signed [XYW-1:0] c_x, c_y;
  wire signed [AW-1:0]  c_ang;
  wire                  c_done;

  // ---- the butterfly core's CORDIC request ----
  wire                 bf_c_start;
  wire signed [DW-1:0] bf_cx, bf_cy;
  wire signed [AW-1:0] bf_cang;

  butterfly #(.DW(DW), .AW(AW), .XYW(XYW)) u_bf (
      .clk(clk), .rst_n(rst_n), .start(start),
      .a_re(a_re), .a_im(a_im), .b_re(b_re), .b_im(b_im), .angle(angle),
      .a_re_o(a_re_o), .a_im_o(a_im_o), .b_re_o(b_re_o), .b_im_o(b_im_o), .done(done),
      .c_start(bf_c_start), .c_x_in(bf_cx), .c_y_in(bf_cy), .c_ang_in(bf_cang),
      .c_x_out(c_x), .c_y_out(c_y), .c_done(c_done)
  );

  cordic #(.DW(DW), .XYW(XYW), .AW(AW)) u_cordic (
      .clk(clk), .rst_n(rst_n),
      .start (bf_c_start),
      .mode  (1'b0),                            // ROTATE only (VECTOR moved to the MCU)
      .x_in  (bf_cx), .y_in(bf_cy), .ang_in(bf_cang),
      .x_out (c_x), .y_out(c_y), .ang_out(c_ang), .done(c_done)
  );

  wire _unused_ang = &{c_ang, 1'b0};   // CORDIC residual angle: unused in ROTATE mode

endmodule
