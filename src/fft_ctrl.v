/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * FFT controller / sequencer for SeeTheBeat -- runs the full N-point radix-2 DIT FFT in
 * place in MCU memory, then a MAGNITUDE READ-OUT phase over the N/2 useful bins. Both
 * phases share the one MCU bus and the one CORDIC ALU (fft_alu), so no arbiter is needed.
 *
 *   FFT phase   (op=0): per butterfly, burst 4 reads (A/B), rotate+add+sat, write 4 back.
 *                       Mirrors model/fft_ref.py exactly (counters s,half,kstart,j;
 *                       twiddle via the angle accumulator angle_acc -= angle_step).
 *   MAG phase   (op=1): per bin k=0..N/2-1, read re@2k & im@2k+1, CORDIC-vector -> log,
 *                       emit on mag_data/mag_valid. Bit-exact to model/spectrum_ref.py.
 *
 * Memory: complex point i at word 2i (re) / 2i+1 (im); 10-bit word address. Input must be
 * in MCU memory in BIT-REVERSED order (MCU firmware) before `start`.
 */

`default_nettype none

module fft_ctrl #(
    parameter integer LOGN = 9,       // log2(N); N = 512 for the real chip
    parameter integer DW   = 16,      // Q1.15 component width
    parameter integer ANGW = 20       // CORDIC angle width (2^ANGW = full circle)
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,          // pulse: begin (data already loaded, bit-reversed)
    output reg        done,           // 1-cycle pulse when FFT + magnitude read-out finish

    output reg        mag_valid,      // 1-cycle pulse per magnitude bin (MAG phase)
    output reg [6:0]  mag_data,       // that bin's log-magnitude {msb[4:0], frac[1:0]}

    // pin-side bus to the MCU (see docs/bus_protocol.md)
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire [7:0] uio_in,
    input  wire [7:0] ui_in
);

  localparam [10:0] N  = (1'b1 << LOGN);
  localparam integer AW = 10;                 // protocol word-address width

  // Build guard: the AW-bit word bus caps N at 2^(AW-1), i.e. LOGN <= AW-1.
  initial if (LOGN > AW - 1)
    $error("fft_ctrl: LOGN=%0d exceeds the %0d-bit word bus (max LOGN=%0d)",
           LOGN, AW, AW - 1);

  localparam [ANGW-1:0] ANG_STEP0 = {1'b1, {(ANGW-1){1'b0}}};   // 2^(ANGW-1), angle step s=1
  localparam [10:0] N_VAL    = N[10:0];
  localparam [4:0]  LOGN_VAL = LOGN[4:0];
  localparam [9:0]  NH_M1    = (N / 2) - 1;   // last magnitude bin (N/2 useful bins: 0..N/2-1)

  localparam [2:0] S_IDLE=3'd0, S_READ=3'd1, S_COMP=3'd2, S_WRITE=3'd3,
                   S_NEXT=3'd4, S_DONE=3'd5, S_MREAD=3'd6, S_MCOMP=3'd7;
  reg [2:0] state;

  // ---- FFT loop state (mirrors fft_ref.py) ----
  reg [4:0]  s;             // stage 1..LOGN
  reg [9:0]  half;          // 2^(s-1)
  reg [10:0] kstart;        // group start (complex index)
  reg [9:0]  j;             // butterfly within group
  reg signed [ANGW-1:0] angle_step;    // 2^(ANGW-s)
  reg signed [ANGW-1:0] angle_acc;     // -j*angle_step (twiddle angle; signed for CORDIC)
  reg [9:0]  mbin;          // magnitude read-out bin counter

  wire [10:0] i0 = kstart + {1'b0, j};
  wire [10:0] i1 = i0 + {1'b0, half};
  wire [10:0] mstep = {1'b0, half} << 1;
  wire [10:0] next_k = kstart + mstep;

  wire last_bf    = (j == (half - 10'd1));
  wire last_group = (next_k == N_VAL);
  wire last_stage = (s == LOGN_VAL);
  wire last_mbin  = (mbin == NH_M1);

  // ---- operand / result registers ----
  reg [DW-1:0] a_re, a_im, b_re, b_im;         // read operands (MAG: bin re/im in b_re/b_im)
  reg [DW-1:0] wr_are, wr_aim, wr_bre, wr_bim; // butterfly results to write
  reg [2:0]    ri, rc, wi;                      // read-issue / read-collect / write-issue
  reg          bf_started;

  // ---- bus master ----
  wire            rd_req, rd_accept, rd_valid, wr_req, wr_accept;
  wire [AW-1:0]   rd_addr, wr_addr;
  wire [DW-1:0]   rd_data, wr_data;

  // read address: FFT (S_READ) uses the butterfly pair i0/i1; MAG (S_MREAD) uses bin mbin
  reg [AW-1:0] rd_addr_c;
  always @(*) begin
    if (state == S_MREAD) begin
      rd_addr_c = {mbin[AW-2:0], ri[0]};            // 2*mbin (re) / 2*mbin+1 (im)
    end else begin
      case (ri)
        3'd0:    rd_addr_c = {i0[AW-2:0], 1'b0};    // A_re
        3'd1:    rd_addr_c = {i0[AW-2:0], 1'b1};    // A_im
        3'd2:    rd_addr_c = {i1[AW-2:0], 1'b0};    // B_re
        default: rd_addr_c = {i1[AW-2:0], 1'b1};    // B_im
      endcase
    end
  end

  reg [AW-1:0] wr_addr_c;
  reg [DW-1:0] wr_data_c;
  always @(*) begin
    case (wi)
      3'd0:    begin wr_addr_c = {i0[AW-2:0], 1'b0}; wr_data_c = wr_are; end
      3'd1:    begin wr_addr_c = {i0[AW-2:0], 1'b1}; wr_data_c = wr_aim; end
      3'd2:    begin wr_addr_c = {i1[AW-2:0], 1'b0}; wr_data_c = wr_bre; end
      default: begin wr_addr_c = {i1[AW-2:0], 1'b1}; wr_data_c = wr_bim; end
    endcase
  end

  assign rd_req  = (state == S_READ  && ri != 3'd4) || (state == S_MREAD && ri != 3'd2);
  assign rd_addr = rd_addr_c;
  assign wr_req  = (state == S_WRITE) && (wi != 3'd4);
  assign wr_addr = wr_addr_c;
  assign wr_data = wr_data_c;

  mcu_bus #(.AW(AW), .DW(DW)) u_bus (
      .clk(clk), .rst_n(rst_n),
      .rd_req(rd_req), .rd_addr(rd_addr), .rd_accept(rd_accept),
      .rd_data(rd_data), .rd_valid(rd_valid),
      .wr_req(wr_req), .wr_addr(wr_addr), .wr_data(wr_data), .wr_accept(wr_accept),
      .uio_out(uio_out), .uio_oe(uio_oe), .uio_in(uio_in), .ui_in(ui_in)
  );

  // ---- shared CORDIC ALU: op=0 butterfly (S_COMP), op=1 magnitude (S_MCOMP) ----
  wire            alu_op   = (state == S_MCOMP);
  wire            bf_start = !bf_started && ((state == S_COMP) || (state == S_MCOMP));
  wire            bf_done;
  wire [DW-1:0]   bf_are_o, bf_aim_o, bf_bre_o, bf_bim_o;
  wire [6:0]      alu_log_mag;

  fft_alu #(.DW(DW), .AW(ANGW), .XYW(22)) u_alu (
      .clk(clk), .rst_n(rst_n), .start(bf_start), .op(alu_op),
      .a_re(a_re), .a_im(a_im), .b_re(b_re), .b_im(b_im),   // MAG: bin re/im on b_re/b_im
      .angle(angle_acc),
      .a_re_o(bf_are_o), .a_im_o(bf_aim_o), .b_re_o(bf_bre_o), .b_im_o(bf_bim_o),
      .log_mag(alu_log_mag),
      .done(bf_done)
  );

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= S_IDLE; done <= 1'b0; mag_valid <= 1'b0; mag_data <= 7'd0;
      s <= 5'd0; half <= 10'd0; kstart <= 11'd0; j <= 10'd0; mbin <= 10'd0;
      angle_step <= {ANGW{1'b0}}; angle_acc <= {ANGW{1'b0}};
      ri <= 3'd0; rc <= 3'd0; wi <= 3'd0; bf_started <= 1'b0;
      a_re <= 0; a_im <= 0; b_re <= 0; b_im <= 0;
      wr_are <= 0; wr_aim <= 0; wr_bre <= 0; wr_bim <= 0;
    end else begin
      done <= 1'b0; mag_valid <= 1'b0;
      case (state)
        S_IDLE: begin
          if (start) begin
            s <= 5'd1; half <= 10'd1; kstart <= 11'd0; j <= 10'd0;
            angle_step <= ANG_STEP0; angle_acc <= {ANGW{1'b0}};
            ri <= 3'd0; rc <= 3'd0;
            state <= S_READ;
          end
        end

        S_READ: begin
          if (rd_req && rd_accept) ri <= ri + 3'd1;
          if (rd_valid) begin
            case (rc)
              3'd0:    a_re <= rd_data;
              3'd1:    a_im <= rd_data;
              3'd2:    b_re <= rd_data;
              default: b_im <= rd_data;
            endcase
            rc <= rc + 3'd1;
          end
          if (rc == 3'd4) begin bf_started <= 1'b0; state <= S_COMP; end
        end

        S_COMP: begin
          bf_started <= 1'b1;
          if (bf_done) begin
            wr_are <= bf_are_o; wr_aim <= bf_aim_o;
            wr_bre <= bf_bre_o; wr_bim <= bf_bim_o;
            wi <= 3'd0; state <= S_WRITE;
          end
        end

        S_WRITE: begin
          if (wr_req && wr_accept) wi <= wi + 3'd1;
          if (wi == 3'd4) state <= S_NEXT;
        end

        S_NEXT: begin
          ri <= 3'd0; rc <= 3'd0;
          if (last_bf) begin
            j <= 10'd0; angle_acc <= {ANGW{1'b0}};
            if (last_group) begin
              kstart <= 11'd0;
              if (last_stage) begin
                mbin <= 10'd0; state <= S_MREAD;    // FFT done -> magnitude read-out
              end else begin
                s <= s + 5'd1; half <= half << 1; angle_step <= angle_step >> 1;
                state <= S_READ;
              end
            end else begin
              kstart <= next_k; state <= S_READ;
            end
          end else begin
            j <= j + 10'd1; angle_acc <= angle_acc - angle_step; state <= S_READ;
          end
        end

        // ---- magnitude read-out: read (re,im) of bin mbin, then vector -> log ----
        S_MREAD: begin
          if (rd_req && rd_accept) ri <= ri + 3'd1;
          if (rd_valid) begin
            if (rc == 3'd0) b_re <= rd_data; else b_im <= rd_data;  // bin re/im -> b_re/b_im
            rc <= rc + 3'd1;
          end
          if (rc == 3'd2) begin bf_started <= 1'b0; state <= S_MCOMP; end
        end

        S_MCOMP: begin
          bf_started <= 1'b1;                        // op=1 magnitude; start pulses cycle 1
          if (bf_done) begin
            mag_data  <= alu_log_mag;
            mag_valid <= 1'b1;
            if (last_mbin) begin
              state <= S_DONE;
            end else begin
              mbin <= mbin + 10'd1; ri <= 3'd0; rc <= 3'd0; state <= S_MREAD;
            end
          end
        end

        S_DONE: begin done <= 1'b1; state <= S_IDLE; end
        default: state <= S_IDLE;
      endcase
    end
  end

  // high bits always 0 for N<=512; sink for lint
  wire _unused = &{1'b0, i0[10:9], i1[10:9], next_k[10], mbin[9]};

endmodule
