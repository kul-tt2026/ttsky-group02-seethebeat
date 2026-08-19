/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * Radix-2 DIT butterfly for SeeTheBeat.   A' = A + W*B,   B' = A - W*B.
 * W*B is computed by one shared CORDIC (rotate B by the twiddle angle); the results are
 * then scaled per stage (>>1, truncation) and SATURATED to Q1.15 (clip, not wrap).
 * Multi-cycle: pulse `start` with A, B, angle; `done` pulses when A'/B' are valid.
 * Bit-exact match to model/butterfly.py.
 */

`default_nettype none

module butterfly #(
    parameter integer DW  = 16,   // I/O data width (Q1.15)
    parameter integer AW  = 20,   // angle width
    parameter integer XYW = 22    // CORDIC datapath/output width (must match cordic)
) (
    input  wire                  clk,
    input  wire                  rst_n,
    input  wire                  start,
    input  wire signed [DW-1:0]  a_re,
    input  wire signed [DW-1:0]  a_im,
    input  wire signed [DW-1:0]  b_re,
    input  wire signed [DW-1:0]  b_im,
    input  wire signed [AW-1:0]  angle,      // twiddle rotation angle (-2*pi*k/N)
    output reg  signed [DW-1:0]  a_re_o,      // A' = A + W*B  (scaled, saturated)
    output reg  signed [DW-1:0]  a_im_o,
    output reg  signed [DW-1:0]  b_re_o,      // B' = A - W*B  (scaled, saturated)
    output reg  signed [DW-1:0]  b_im_o,
    output reg                   done
);

  localparam [1:0] ST_IDLE = 2'd0, ST_START = 2'd1, ST_WAIT = 2'd2;
  reg [1:0] state;

  // hold inputs across the multi-cycle CORDIC
  reg signed [DW-1:0] a_re_r, a_im_r, b_re_r, b_im_r;
  reg signed [AW-1:0] ang_r;

  // shared CORDIC in rotation mode computes t = W*B (gain-compensated, unsaturated)
  wire                  c_start = (state == ST_START);
  wire                  c_done;
  wire signed [XYW-1:0] t_re, t_im;

  cordic #(.DW(DW), .XYW(XYW), .AW(AW)) u_cordic (
      .clk    (clk),
      .rst_n  (rst_n),
      .start  (c_start),
      .mode   (1'b0),            // 0 = ROTATE
      .x_in   (b_re_r),
      .y_in   (b_im_r),
      .ang_in (ang_r),
      .x_out  (t_re),
      .y_out  (t_im),
      .ang_out(),                // unused in rotate mode
      .done   (c_done)
  );

  // ---- butterfly arithmetic (combinational): A +/- t, then >>1 and saturate ----
  localparam integer W = XYW + 1;   // one guard bit for A +/- t before scaling
  wire signed [W-1:0] are = {{(W-DW){a_re_r[DW-1]}}, a_re_r};   // sign-extend A
  wire signed [W-1:0] aie = {{(W-DW){a_im_r[DW-1]}}, a_im_r};
  wire signed [W-1:0] tre = {t_re[XYW-1], t_re};                // sign-extend t (XYW->W)
  wire signed [W-1:0] tie = {t_im[XYW-1], t_im};

  wire signed [W-1:0] ar_s = (are + tre) >>> 1;   // A' real, pre-saturate
  wire signed [W-1:0] ai_s = (aie + tie) >>> 1;   // A' imag
  wire signed [W-1:0] br_s = (are - tre) >>> 1;   // B' real
  wire signed [W-1:0] bi_s = (aie - tie) >>> 1;   // B' imag

  // saturate a W-bit signed value to the DW-bit range (clip, not wrap):
  // it fits iff the redundant sign bits [W-1:DW-1] are all 0 or all 1.
  function signed [DW-1:0] sat(input signed [W-1:0] v);
    begin
      if ((~(|v[W-1:DW-1])) || (&v[W-1:DW-1]))
        sat = v[DW-1:0];                       // fits
      else if (v[W-1])
        sat = {1'b1, {(DW-1){1'b0}}};          // negative overflow -> min (-2^(DW-1))
      else
        sat = {1'b0, {(DW-1){1'b1}}};          // positive overflow -> max ( 2^(DW-1)-1)
    end
  endfunction

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state  <= ST_IDLE; done <= 1'b0;
      a_re_r <= 0; a_im_r <= 0; b_re_r <= 0; b_im_r <= 0; ang_r <= 0;
      a_re_o <= 0; a_im_o <= 0; b_re_o <= 0; b_im_o <= 0;
    end else begin
      done <= 1'b0;
      case (state)
        ST_IDLE: begin
          if (start) begin
            a_re_r <= a_re; a_im_r <= a_im;
            b_re_r <= b_re; b_im_r <= b_im; ang_r <= angle;
            state  <= ST_START;
          end
        end
        ST_START: state <= ST_WAIT;     // c_start pulses this cycle -> CORDIC latches B
        ST_WAIT: begin
          if (c_done) begin
            a_re_o <= sat(ar_s);
            a_im_o <= sat(ai_s);
            b_re_o <= sat(br_s);
            b_im_o <= sat(bi_s);
            done   <= 1'b1;
            state  <= ST_IDLE;
          end
        end
        default: state <= ST_IDLE;
      endcase
    end
  end

endmodule
