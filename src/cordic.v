/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * Iterative (folded) CORDIC rotator for SeeTheBeat -- rotation + vectoring modes.
 * Bit-exact match to model/cordic.py (constants taken verbatim from that model).
 *
 *   mode = 0 (ROTATE) : rotate (x_in, y_in) by ang_in  -> the butterfly's W*B
 *   mode = 1 (VECTOR) : (x_out = |.|, ang_out = atan2) -> the FFT bin magnitude
 *
 * One add-shift per clock (ITERS cycles), then a gain-compensation multiply. Outputs
 * are gain-compensated but NOT saturated (the butterfly saturates after A +/- W*B).
 * Angles use "full circle = 2**AW" units. See SeeTheBeat_LearningNotes.pdf, Step 2.1.
 */

`default_nettype none

module cordic #(
    parameter integer DW    = 16,   // data I/O width (Q1.15)
    parameter integer XYW   = 22,   // internal x/y datapath + output width
    parameter integer AW    = 20,   // angle I/O width (full circle = 2**AW)
    parameter integer ZW    = 24,   // internal angle-accumulator width
    parameter integer ITERS = 16    // iterations (~16-bit angular precision)
) (
    input  wire                   clk,
    input  wire                   rst_n,
    input  wire                   start,     // pulse (1 clk) to begin an operation
    input  wire                   mode,      // 0 = ROTATE, 1 = VECTOR
    input  wire signed [DW-1:0]   x_in,
    input  wire signed [DW-1:0]   y_in,
    input  wire signed [AW-1:0]   ang_in,    // ROTATE: target angle (VECTOR: ignored)
    output reg  signed [XYW-1:0]  x_out,     // gain-compensated (unsaturated)
    output reg  signed [XYW-1:0]  y_out,
    output reg  signed [AW-1:0]   ang_out,   // ROTATE: residual z; VECTOR: atan2 (wrapped)
    output reg                    done
);

  localparam MODE_ROTATE = 1'b0;   // mode input: 0 = ROTATE, 1 = VECTOR

  localparam signed [ZW-1:0]   QUART = 24'sd262144;   // pi/2 in angle units
  localparam signed [16:0]     INV_K = 17'sd19898;    // Q1.15 gain-compensation (1/K)

  localparam [1:0] ST_IDLE = 2'd0, ST_ITER = 2'd1, ST_GAIN = 2'd2;
  localparam [4:0] ITER_LAST = ITERS - 1;   // last iteration index (5-bit, width-clean)
  reg [1:0]              state;
  reg [4:0]             i;                    // iteration index 0..ITERS
  reg                   mode_reg;
  reg signed [XYW-1:0]  x_reg, y_reg;
  reg signed [ZW-1:0]   z_reg;

  // sign-extend inputs
  wire signed [XYW-1:0] x_ext = {{(XYW-DW){x_in[DW-1]}}, x_in};
  wire signed [XYW-1:0] y_ext = {{(XYW-DW){y_in[DW-1]}}, y_in};
  wire signed [ZW-1:0]  a_ext = {{(ZW-AW){ang_in[AW-1]}}, ang_in};

  // ---- pre-rotation into the CORDIC convergence range (combinational) ----
  reg signed [XYW-1:0] px, py;
  reg signed [ZW-1:0]  pz;
  always @* begin
    px = x_ext;
    py = y_ext;
    pz = (mode == MODE_ROTATE) ? a_ext : {ZW{1'b0}};
    if (mode == MODE_ROTATE) begin
      if (a_ext > QUART) begin            // > +90 deg
        px = -y_ext; py = x_ext; pz = a_ext - QUART;
      end else if (a_ext < -QUART) begin  // < -90 deg
        px = y_ext; py = -x_ext; pz = a_ext + QUART;
      end
    end else begin                        // VECTOR: bring x >= 0 (right half-plane)
      if (x_ext < 0) begin
        if (y_ext >= 0) begin
          px = y_ext; py = -x_ext; pz = QUART;
        end else begin
          px = -y_ext; py = x_ext; pz = -QUART;
        end
      end
    end
  end

  // ---- one CORDIC iteration (combinational) ----
  reg signed [ZW-1:0] atan_i;
  always @* begin
    case (i)
      5'd0:  atan_i = 24'sd131072;
      5'd1:  atan_i = 24'sd77376;
      5'd2:  atan_i = 24'sd40884;
      5'd3:  atan_i = 24'sd20753;
      5'd4:  atan_i = 24'sd10417;
      5'd5:  atan_i = 24'sd5213;
      5'd6:  atan_i = 24'sd2607;
      5'd7:  atan_i = 24'sd1304;
      5'd8:  atan_i = 24'sd652;
      5'd9:  atan_i = 24'sd326;
      5'd10: atan_i = 24'sd163;
      5'd11: atan_i = 24'sd81;
      5'd12: atan_i = 24'sd41;
      5'd13: atan_i = 24'sd20;
      5'd14: atan_i = 24'sd10;
      5'd15: atan_i = 24'sd5;
      default: atan_i = 24'sd0;
    endcase
  end

  wire                 d_pos = (mode_reg == MODE_ROTATE) ? (z_reg >= 0) : (y_reg < 0);
  wire signed [XYW-1:0] y_sh = y_reg >>> i;   // arithmetic (sign-preserving)
  wire signed [XYW-1:0] x_sh = x_reg >>> i;
  wire signed [XYW-1:0] nx = d_pos ? (x_reg - y_sh) : (x_reg + y_sh);
  wire signed [XYW-1:0] ny = d_pos ? (y_reg + x_sh) : (y_reg - x_sh);
  wire signed [ZW-1:0]  nz = d_pos ? (z_reg - atan_i) : (z_reg + atan_i);

  // ---- gain compensation:  (x * INV_K) >> 15  ----
  wire signed [XYW+16:0] gx = x_reg * INV_K;
  wire signed [XYW+16:0] gy = y_reg * INV_K;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= ST_IDLE; done <= 1'b0; i <= 5'd0; mode_reg <= 1'b0;
      x_reg <= 0; y_reg <= 0; z_reg <= 0;
      x_out <= 0; y_out <= 0; ang_out <= 0;
    end else begin
      done <= 1'b0;
      case (state)
        ST_IDLE: begin
          if (start) begin
            x_reg <= px; y_reg <= py; z_reg <= pz;
            mode_reg <= mode; i <= 5'd0; state <= ST_ITER;
          end
        end
        ST_ITER: begin
          x_reg <= nx; y_reg <= ny; z_reg <= nz;
          i <= i + 5'd1;
          if (i == ITER_LAST) state <= ST_GAIN;
        end
        ST_GAIN: begin
          // (gx >>> 15) truncated to XYW bits == gx[XYW+14:15]; value provably fits.
          x_out   <= $signed(gx[XYW+14:15]);
          y_out   <= $signed(gy[XYW+14:15]);
          ang_out <= z_reg[AW-1:0];   // wrap to [-pi, pi): low AW bits, signed
          done    <= 1'b1;
          state   <= ST_IDLE;
        end
        default: state <= ST_IDLE;
      endcase
    end
  end

endmodule
