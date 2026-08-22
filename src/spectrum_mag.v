/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * Per-bin magnitude + log read-out for SeeTheBeat (Phase 6). Given one FFT bin
 * X = re + j*im it produces a small log-magnitude code the visuals map to brightness:
 *   |X| = sqrt(re^2 + im^2)   via the CORDIC in VECTORING mode (exact, gain-compensated)
 *   log = { MSB index , 2 mantissa bits below it }   (a cheap piecewise-linear log2)
 * Bit-exact to model/spectrum_ref.py. Multi-cycle: pulse `start`; `done` pulses when
 * `log_mag` is valid.
 *
 * INTEGRATION NOTE: this instantiates its own CORDIC so it is testable standalone. At
 * top-level integration the FFT (butterfly, ROTATE) and this read-out (VECTORING) run
 * at DIFFERENT times, so they should share ONE physical CORDIC via a mode/operand mux --
 * a second CORDIC would cost more space we do not have. (Deferred to project.v wiring.)
 */

`default_nettype none

module spectrum_mag #(
    parameter integer DW  = 16,       // Q1.15 bin component
    parameter integer XYW = 22,       // CORDIC magnitude datapath width
    parameter integer AW  = 20,       // CORDIC angle width
    parameter integer LOG_W = 7       // packed log code width = 5-bit MSB index + 2 frac
) (
    input  wire                 clk,
    input  wire                 rst_n,
    input  wire                 start,
    input  wire signed [DW-1:0] re,
    input  wire signed [DW-1:0] im,
    output reg  [LOG_W-1:0]     log_mag,   // {msb_index[4:0], frac[1:0]}
    output reg                  done
);

  localparam [1:0] ST_IDLE = 2'd0, ST_START = 2'd1, ST_WAIT = 2'd2;
  reg [1:0] state;

  reg signed [DW-1:0] re_r, im_r;             // held across the multi-cycle CORDIC

  // ---- shared CORDIC in VECTORING mode: x_out = |X|, angle/ y unused ----
  wire                  c_start = (state == ST_START);
  wire                  c_done;
  wire signed [XYW-1:0] c_x, c_y;
  wire signed [AW-1:0]  c_ang;

  cordic #(.DW(DW), .XYW(XYW), .AW(AW)) u_cordic (
      .clk    (clk),
      .rst_n  (rst_n),
      .start  (c_start),
      .mode   (1'b1),                          // 1 = VECTOR
      .x_in   (re_r),
      .y_in   (im_r),
      .ang_in ({AW{1'b0}}),
      .x_out  (c_x),
      .y_out  (c_y),
      .ang_out(c_ang),
      .done   (c_done)
  );

  // y_out (~0) and ang_out (phase) are not needed for a magnitude read-out
  wire _unused = &{1'b0, c_y, c_ang};

  // ---- log2 encoder (combinational from the vectoring magnitude) ----
  // magnitude is non-negative in vector mode; treat the bits as unsigned.
  wire [XYW-1:0] mag = c_x;

  // priority encoder: index of the most-significant set bit (0 if mag == 0)
  function [4:0] msb_idx(input [XYW-1:0] v);
    integer i;
    begin
      msb_idx = 5'd0;
      for (i = 0; i < XYW; i = i + 1)
        if (v[i]) msb_idx = i[4:0];
    end
  endfunction

  wire [4:0]      msb = msb_idx(mag);
  // two mantissa bits just below the MSB (msb-1, msb-2); none exist for msb < 2
  wire [4:0]      sh  = (msb >= 5'd2) ? (msb - 5'd2) : 5'd0;
  wire [XYW-1:0]  shifted = mag >> sh;
  wire [1:0]      frac = (msb >= 5'd2) ? shifted[1:0] : 2'b00;
  wire [LOG_W-1:0] log_code = {msb, frac};      // {msb[4:0], frac[1:0]} = (msb<<2)|frac

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state   <= ST_IDLE;
      done    <= 1'b0;
      log_mag <= {LOG_W{1'b0}};
      re_r    <= {DW{1'b0}};
      im_r    <= {DW{1'b0}};
    end else begin
      done <= 1'b0;
      case (state)
        ST_IDLE: begin
          if (start) begin
            re_r <= re;
            im_r <= im;
            state <= ST_START;
          end
        end
        ST_START: state <= ST_WAIT;             // c_start pulses -> CORDIC latches re/im
        ST_WAIT: begin
          if (c_done) begin
            log_mag <= log_code;
            done    <= 1'b1;
            state   <= ST_IDLE;
          end
        end
        default: state <= ST_IDLE;
      endcase
    end
  end

endmodule
