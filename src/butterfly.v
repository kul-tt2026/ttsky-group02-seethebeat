/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * Radix-2 DIT butterfly for SeeTheBeat.   A' = A + W*B,   B' = A - W*B.
 * CORDIC-LESS core: it drives the SHARED CORDIC (owned by fft_alu) in ROTATE mode to get
 * t = W*B, then scales (>>1, truncation) and SATURATES to Q1.15 (clip, not wrap).
 * Bit-exact match to model/butterfly.py. Multi-cycle: pulse `start`; `done` pulses when
 * A'/B' are valid. fft_alu multiplexes the one CORDIC between this and spectrum_mag.
 */

`default_nettype none

module butterfly #(
    parameter integer DW  = 16,   // I/O data width (Q1.15)
    parameter integer AW  = 20,   // angle width
    parameter integer XYW = 22    // CORDIC datapath/output width
) (
    input  wire                  clk,
    input  wire                  rst_n,
    input  wire                  start,
    input  wire signed [DW-1:0]  a_re,
    input  wire signed [DW-1:0]  a_im,
    input  wire signed [DW-1:0]  b_re,
    input  wire signed [DW-1:0]  b_im,
    input  wire signed [AW-1:0]  angle,       // twiddle rotation angle (-2*pi*k/N)
    output reg  signed [DW-1:0]  a_re_o,       // A' = A + W*B  (scaled, saturated)
    output reg  signed [DW-1:0]  a_im_o,
    output reg  signed [DW-1:0]  b_re_o,       // B' = A - W*B  (scaled, saturated)
    output reg  signed [DW-1:0]  b_im_o,
    output reg                   done,

    // ---- shared-CORDIC handshake (ROTATE): driven here, muxed/resolved in fft_alu ----
    output wire                  c_start,
    output wire signed [DW-1:0]  c_x_in,       // B real  -> CORDIC x_in
    output wire signed [DW-1:0]  c_y_in,       // B imag  -> CORDIC y_in
    output wire signed [AW-1:0]  c_ang_in,     // twiddle -> CORDIC ang_in
    input  wire signed [XYW-1:0] c_x_out,      // t real  = CORDIC x_out
    input  wire signed [XYW-1:0] c_y_out,      // t imag  = CORDIC y_out
    input  wire                  c_done
);

  localparam [1:0] ST_IDLE = 2'd0, ST_START = 2'd1, ST_WAIT = 2'd2;
  reg [1:0] state;

  // hold inputs across the multi-cycle CORDIC
  reg signed [DW-1:0] a_re_r, a_im_r, b_re_r, b_im_r;
  reg signed [AW-1:0] ang_r;

  // this core requests the CORDIC (ROTATE) with B and the twiddle angle
  assign c_start  = (state == ST_START);
  assign c_x_in   = b_re_r;
  assign c_y_in   = b_im_r;
  assign c_ang_in = ang_r;

  // ---- butterfly arithmetic (combinational): A +/- t, then >>1 and saturate ----
  localparam integer W = XYW + 1;   // one guard bit for A +/- t before scaling
  wire signed [W-1:0] are = {{(W-DW){a_re_r[DW-1]}}, a_re_r};   // sign-extend A
  wire signed [W-1:0] aie = {{(W-DW){a_im_r[DW-1]}}, a_im_r};
  wire signed [W-1:0] tre = {c_x_out[XYW-1], c_x_out};          // sign-extend t (XYW->W)
  wire signed [W-1:0] tie = {c_y_out[XYW-1], c_y_out};

  wire signed [W-1:0] ar_s = (are + tre) >>> 1;   // A' real, pre-saturate
  wire signed [W-1:0] ai_s = (aie + tie) >>> 1;   // A' imag
  wire signed [W-1:0] br_s = (are - tre) >>> 1;   // B' real
  wire signed [W-1:0] bi_s = (aie - tie) >>> 1;   // B' imag

  // saturate a W-bit signed value to the DW-bit range (clip, not wrap)
  function signed [DW-1:0] sat(input signed [W-1:0] v);
    begin
      if ((~(|v[W-1:DW-1])) || (&v[W-1:DW-1]))
        sat = v[DW-1:0];                       // fits
      else if (v[W-1])
        sat = {1'b1, {(DW-1){1'b0}}};          // negative overflow -> min
      else
        sat = {1'b0, {(DW-1){1'b1}}};          // positive overflow -> max
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
        ST_START: state <= ST_WAIT;     // c_start pulses -> shared CORDIC latches B/angle
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
