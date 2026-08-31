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
 * One add-shift per clock: ITERS rotation iterations, then SCALES gain-compensation
 * scaling iterations. Outputs are gain-compensated but NOT saturated (the butterfly
 * saturates after A +/- W*B). Angles use "full circle = 2**AW" units.
 *
 * MULTIPLIER-FREE GAIN COMPENSATION. The CORDIC gain K ~ 1.6468 used to be
 * undone by two constant multipliers, (x * INV_K) >> 15 with INV_K = 19898. That
 * constant has 9 set bits, so it synthesises to an 8-adder shift-add tree per channel
 * -- two of them, for one scaling step. Instead we now spend SCALES extra clock cycles
 * running scaling iterations  v <- v +/- (v >> p)  whose product approximates 1/K, on
 * the SAME adders and shifters the rotation iterations already use. The only added
 * logic is the operand mux below. Cycles are the one resource we have in surplus, so this is a pure area win.
 *
 * The datapath additionally carries GUARD extra fractional bits from load to final
 * shift-out, because a chain of shift-adds truncates once per step. Those guard bits
 * also protect the 16 rotation iterations, which makes this version MORE accurate than
 * the multiply it replaces (end-to-end FFT SNR +3..11 dB, see model/test_fft_ref.py).
 */

`default_nettype none

module cordic #(
    parameter integer DW    = 16,   // data I/O width (Q1.15)
    parameter integer XYW   = 22,   // internal x/y datapath + output width
    parameter integer AW    = 20,   // angle I/O width (full circle = 2**AW)
    parameter integer ZW    = 24,   // internal angle-accumulator width
    parameter integer ITERS = 16,   // rotation iterations (~16-bit angular precision)
    parameter integer GUARD = 3     // extra fractional bits held during the operation, do not increase independently from XYW
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

  localparam signed [ZW-1:0] QUART  = 24'sd262144;  // pi/2 in angle units
  localparam integer         SCALES = 5;            // gain-compensation shift-adds

  localparam [1:0] ST_IDLE = 2'd0, ST_ITER = 2'd1, ST_SCALE = 2'd2;
  /* verilator lint_off WIDTHTRUNC */
  localparam [4:0] ITER_LAST  = ITERS - 1;   // last rotation index (intentional 5-bit narrow)
  localparam [4:0] SCALE_LAST = SCALES - 1;  // last scaling index
  /* verilator lint_on WIDTHTRUNC */
  reg [1:0]              state;
  reg [4:0]             i;                    // rotation index 0..ITERS-1, then scaling index
  reg                   mode_reg;
  reg signed [XYW-1:0]  x_reg, y_reg;
  reg signed [ZW-1:0]   z_reg;

  wire scaling = (state == ST_SCALE);

  // sign-extend inputs, then enter the guarded datapath (exact -- just a left shift;
  // worst-case internal magnitude is 610_505, at x_in = y_in = -32768. That needs 21 of
  // the XYW = 22 bits, so exactly one bit of margin. Verified by exhaustive search over
  // all 2^20 angles x the four full-scale corners; test_units/test_cordic.py carries
  // those corners, and they do detect a datapath narrowed to 20 bits.
  wire signed [XYW-1:0] x_ext = {{(XYW-DW){x_in[DW-1]}}, x_in};
  wire signed [XYW-1:0] y_ext = {{(XYW-DW){y_in[DW-1]}}, y_in};
  wire signed [ZW-1:0]  a_ext = {{(ZW-AW){ang_in[AW-1]}}, ang_in};
  wire signed [XYW-1:0] x_g   = x_ext <<< GUARD;
  wire signed [XYW-1:0] y_g   = y_ext <<< GUARD;

  // ---- pre-rotation into the CORDIC convergence range (combinational) ----
  reg signed [XYW-1:0] px, py;
  reg signed [ZW-1:0]  pz;
  always @* begin
    px = x_g;
    py = y_g;
    pz = (mode == MODE_ROTATE) ? a_ext : {ZW{1'b0}};
    if (mode == MODE_ROTATE) begin
      if (a_ext > QUART) begin            // > +90 deg
        px = -y_g; py = x_g; pz = a_ext - QUART;
      end else if (a_ext < -QUART) begin  // < -90 deg
        px = y_g; py = -x_g; pz = a_ext + QUART;
      end
    end else begin                        // VECTOR: bring x >= 0 (right half-plane)
      if (x_g < 0) begin
        if (y_g >= 0) begin
          px = y_g; py = -x_g; pz = QUART;
        end else begin
          px = -y_g; py = x_g; pz = -QUART;
        end
      end
    end
  end

  // ---- rotation micro-angles: arctan(2**-i) in angle units ----
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

  // ---- gain-compensation scaling steps:  v <- v +/- (v >> sc_p)  ----
  // Product of (1 - 2^-1)(1 + 2^-2)(1 - 2^-5)(1 + 2^-9)(1 + 2^-10) = 0.60724374
  // vs the exact 1/K = 0.60725294 (relative error 1.5e-5). Verbatim from
  // model/cordic.py SCALE_STEPS -- change both together or the bit-exact test fails.
  reg [4:0] sc_p;     // shift amount
  reg       sc_neg;   // 1 = subtract (s = -1), 0 = add (s = +1)
  always @* begin
    case (i)
      5'd0: begin sc_p = 5'd1;  sc_neg = 1'b1; end
      5'd1: begin sc_p = 5'd2;  sc_neg = 1'b0; end
      5'd2: begin sc_p = 5'd5;  sc_neg = 1'b1; end
      5'd3: begin sc_p = 5'd9;  sc_neg = 1'b0; end
      5'd4: begin sc_p = 5'd10; sc_neg = 1'b0; end
      // unreachable (i only reaches SCALE_LAST while scaling); repeat the last step
      // rather than a shift-by-0, which would double the value.
      default: begin sc_p = 5'd10; sc_neg = 1'b0; end
    endcase
  end

  // ---- shared add-shift datapath (rotation AND scaling use these two adders) ----
  // ROTATE/VECTOR: nx = x -/+ (y >> i),  ny = y +/- (x >> i)
  // SCALE        : nx = x +/- (x >> p),  ny = y +/- (y >> p)   (same sign on both)
  wire                  d_pos = (mode_reg == MODE_ROTATE) ? (z_reg >= 0) : (y_reg < 0);
  wire [4:0]            sh    = scaling ? sc_p  : i;
  wire signed [XYW-1:0] a_in  = scaling ? x_reg : y_reg;   // operand of the nx shifter
  wire signed [XYW-1:0] b_in  = scaling ? y_reg : x_reg;   // operand of the ny shifter
  wire                  sub_x = scaling ? sc_neg : d_pos;
  wire                  sub_y = scaling ? sc_neg : !d_pos;

  wire signed [XYW-1:0] a_sh = a_in >>> sh;   // arithmetic (sign-preserving)
  wire signed [XYW-1:0] b_sh = b_in >>> sh;
  wire signed [XYW-1:0] nx = sub_x ? (x_reg - a_sh) : (x_reg + a_sh);
  wire signed [XYW-1:0] ny = sub_y ? (y_reg - b_sh) : (y_reg + b_sh);
  wire signed [ZW-1:0]  nz = d_pos ? (z_reg - atan_i) : (z_reg + atan_i);

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
          if (i == ITER_LAST) begin
            i     <= 5'd0;          // reuse i as the scaling-step index
            state <= ST_SCALE;
          end else begin
            i <= i + 5'd1;
          end
        end
        ST_SCALE: begin
          x_reg <= nx; y_reg <= ny;
          i <= i + 5'd1;
          if (i == SCALE_LAST) begin
            x_out   <= nx >>> GUARD;    // drop the guard bits (arithmetic, truncating)
            y_out   <= ny >>> GUARD;
            ang_out <= z_reg[AW-1:0];   // wrap to [-pi, pi): low AW bits, signed
            done    <= 1'b1;
            state   <= ST_IDLE;
          end
        end
        default: state <= ST_IDLE;
      endcase
    end
  end

endmodule
