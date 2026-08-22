/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * fft_alu -- the SeeTheBeat arithmetic unit: ONE CORDIC, time-shared between the FFT
 * butterfly and the spectrum magnitude read-out (they run at different times, so a single
 * CORDIC muxed by `op` costs a few muxes and saves a whole second CORDIC ~0.7 tile).
 *
 *   op = 0 : run the `butterfly` core (CORDIC ROTATE)  -> A'/B' on a_re_o..b_im_o
 *   op = 1 : run the `spectrum_mag` core (CORDIC VECTOR) -> log-magnitude on log_mag
 *
 * The two cores are CORDIC-LESS: each drives a CORDIC handshake, and this wrapper hands the
 * single CORDIC to whichever `op` selects. For op=1 the bin (re,im) is fed on b_re/b_im.
 * Same external interface a controller would expect of the butterfly, plus `op`/`log_mag`.
 */

`default_nettype none

module fft_alu #(
    parameter integer DW    = 16,
    parameter integer AW    = 20,
    parameter integer XYW   = 22,
    parameter integer LOG_W = 7
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 start,
    input  wire                 op,        // 0 = butterfly (ROTATE), 1 = magnitude (VECTOR)
    input  wire signed [DW-1:0] a_re,
    input  wire signed [DW-1:0] a_im,
    input  wire signed [DW-1:0] b_re,      // op=1: also the bin real part (re)
    input  wire signed [DW-1:0] b_im,      // op=1: also the bin imag part (im)
    input  wire signed [AW-1:0] angle,     // op=0 twiddle angle (op=1: ignored)
    output wire signed [DW-1:0] a_re_o,     // butterfly A' (op=0)
    output wire signed [DW-1:0] a_im_o,
    output wire signed [DW-1:0] b_re_o,     // butterfly B' (op=0)
    output wire signed [DW-1:0] b_im_o,
    output wire [LOG_W-1:0]     log_mag,    // magnitude log (op=1)
    output wire                 done
);

  // ---- the ONE shared CORDIC's outputs ----
  wire signed [XYW-1:0] c_x, c_y;
  wire signed [AW-1:0]  c_ang;
  wire                  c_done;

  // ---- each core's CORDIC request (only the selected one is routed) ----
  wire                 bf_c_start;  wire signed [DW-1:0] bf_cx, bf_cy;  wire signed [AW-1:0] bf_cang;
  wire                 sm_c_start;  wire signed [DW-1:0] sm_cx, sm_cy;  wire signed [AW-1:0] sm_cang;
  wire                 bf_done, sm_done;

  butterfly #(.DW(DW), .AW(AW), .XYW(XYW)) u_bf (
      .clk(clk), .rst_n(rst_n), .start(op ? 1'b0 : start),
      .a_re(a_re), .a_im(a_im), .b_re(b_re), .b_im(b_im), .angle(angle),
      .a_re_o(a_re_o), .a_im_o(a_im_o), .b_re_o(b_re_o), .b_im_o(b_im_o), .done(bf_done),
      .c_start(bf_c_start), .c_x_in(bf_cx), .c_y_in(bf_cy), .c_ang_in(bf_cang),
      .c_x_out(c_x), .c_y_out(c_y), .c_done(c_done)
  );

  spectrum_mag #(.DW(DW), .AW(AW), .XYW(XYW), .LOG_W(LOG_W)) u_sm (
      .clk(clk), .rst_n(rst_n), .start(op ? start : 1'b0),
      .re(b_re), .im(b_im), .log_mag(log_mag), .done(sm_done),
      .c_start(sm_c_start), .c_x_in(sm_cx), .c_y_in(sm_cy), .c_ang_in(sm_cang),
      .c_x_out(c_x), .c_done(c_done)
  );

  // ---- hand the single CORDIC to the selected core (temporally disjoint) ----
  cordic #(.DW(DW), .XYW(XYW), .AW(AW)) u_cordic (
      .clk(clk), .rst_n(rst_n),
      .start (op ? sm_c_start : bf_c_start),
      .mode  (op),                              // 0 = ROTATE, 1 = VECTOR
      .x_in  (op ? sm_cx   : bf_cx),
      .y_in  (op ? sm_cy   : bf_cy),
      .ang_in(op ? sm_cang : bf_cang),
      .x_out (c_x), .y_out(c_y), .ang_out(c_ang), .done(c_done)
  );

  wire _unused_ang = &{c_ang, 1'b0};   // CORDIC residual angle / phase: unused here
  assign done = op ? sm_done : bf_done;

endmodule
