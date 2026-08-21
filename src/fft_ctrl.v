/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * FFT controller / sequencer for SeeTheBeat -- the "brain" that turns one butterfly unit
 * and one MCU bus into a full N-point radix-2 DIT FFT, in place in MCU memory.
 *
 * It mirrors model/fft_ref.py's nested loops exactly (so the result is bit-identical):
 *   for s = 1..LOGN:  (m=2^s, half=2^(s-1))
 *     for kstart = 0,m,2m,..:
 *       for j = 0..half-1:
 *         i0 = kstart+j ; i1 = i0+half
 *         t  = rotate(A[i1], -j*2^(20-s))          <- butterfly.v (CORDIC)
 *         A[i0], A[i1] = sat((A[i0]+/-t) >> 1)
 * Instead of dilation it keeps the loop counters directly (kstart,j,half,angle_step),
 * which is the same schedule with only adders/shifters -- no barrel shifters.
 *
 * Memory: complex point i lives at word 2i (re) and 2i+1 (im); word address is 10-bit
 * (0..1023). Input must already be in MCU memory in BIT-REVERSED order (done by the MCU
 * firmware for now); the controller runs the in-place stages on it.
 *
 * Per butterfly: burst 4 reads (A_re,A_im,B_re,B_im) over the pipelined bus, run the
 * butterfly, write 4 words back. Reads/writes never overlap (simple, no RAW hazard).
 */

`default_nettype none

module fft_ctrl #(
    parameter integer LOGN = 9,       // log2(N); N = 512 for the real chip
    parameter integer DW   = 16,      // Q1.15 component width
    parameter integer ANGW = 20       // CORDIC angle width (2^ANGW = full circle)
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,          // pulse: begin an FFT (data already loaded, bit-reversed)
    output reg        done,           // 1-cycle pulse when the FFT is complete

    // pin-side bus to the MCU (see docs/bus_protocol.md)
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire [7:0] uio_in,
    input  wire [7:0] ui_in
);

  localparam integer N  = (1 << LOGN);
  localparam integer AW = 10;                 // protocol word-address width

  // Build guard: the AW-bit word bus caps the transform at N <= 2^(AW-1), i.e.
  // LOGN <= AW-1. A larger LOGN would silently alias word addresses (dropped MSBs) with
  // no lint/sim warning -- the exact failure the bus-width bug had. A constant-true
  // condition makes this an elaboration error, not a silent wrong chip.
  initial if (LOGN > AW - 1)
    $error("fft_ctrl: LOGN=%0d exceeds the %0d-bit word bus (max LOGN=%0d)",
           LOGN, AW, AW - 1);

  // 2^(ANGW-1) = half a circle: the angle step at s=1 (halved per stage from s>=2).
  // Derived from ANGW so it tracks the parameter instead of hard-coding 20'h80000.
  localparam [ANGW-1:0] ANG_STEP0 = {1'b1, {(ANGW-1){1'b0}}};
  localparam [10:0] N_VAL    = N[10:0];
  localparam [4:0]  LOGN_VAL = LOGN[4:0];

  localparam [2:0] S_IDLE=3'd0, S_READ=3'd1, S_COMP=3'd2,
                   S_WRITE=3'd3, S_NEXT=3'd4, S_DONE=3'd5;
  reg [2:0] state;

  // ---- loop state (mirrors fft_ref.py) ----
  reg [4:0]  s;             // stage 1..LOGN
  reg [9:0]  half;          // 2^(s-1)
  reg [10:0] kstart;        // group start (complex index)
  reg [9:0]  j;             // butterfly within group
  reg [ANGW-1:0] angle_step;    // 2^(ANGW-s)
  reg [ANGW-1:0] angle_acc;     // -j*angle_step (two's-complement twiddle angle)

  wire [10:0] i0 = kstart + {1'b0, j};        // top complex index
  wire [10:0] i1 = i0 + {1'b0, half};         // bottom complex index
  wire [10:0] mstep = {1'b0, half} << 1;      // m = 2*half
  wire [10:0] next_k = kstart + mstep;

  wire last_bf    = (j == (half - 10'd1));     // last butterfly in this group
  wire last_group = (next_k == N_VAL);         // last group in this stage
  wire last_stage = (s == LOGN_VAL);           // last stage

  // ---- per-butterfly operand / result registers ----
  reg [DW-1:0] a_re, a_im, b_re, b_im;         // read operands
  reg [DW-1:0] wr_are, wr_aim, wr_bre, wr_bim; // butterfly results to write
  reg [2:0]    ri, rc, wi;                      // read-issue / read-collect / write-issue
  reg          bf_started;

  // ---- bus master ----
  wire            rd_req, rd_accept, rd_valid, wr_req, wr_accept;
  wire [AW-1:0]   rd_addr, wr_addr;
  wire [DW-1:0]   rd_data, wr_data;

  // read address by issue index (word = 2*index + re/im)
  reg [AW-1:0] rd_addr_c;
  always @(*) begin
    case (ri)
      3'd0:    rd_addr_c = {i0[AW-2:0], 1'b0};    // A_re
      3'd1:    rd_addr_c = {i0[AW-2:0], 1'b1};    // A_im
      3'd2:    rd_addr_c = {i1[AW-2:0], 1'b0};    // B_re
      default: rd_addr_c = {i1[AW-2:0], 1'b1};    // B_im
    endcase
  end

  // write address + data by issue index
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

  assign rd_req  = (state == S_READ)  && (ri != 3'd4);
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

  // ---- shared butterfly (A +/- W*B, >>1, saturate) ----
  wire            bf_start = (state == S_COMP) && !bf_started;
  wire            bf_done;
  wire [DW-1:0]   bf_are_o, bf_aim_o, bf_bre_o, bf_bim_o;

  butterfly #(.DW(DW), .AW(ANGW), .XYW(22)) u_bf (
      .clk(clk), .rst_n(rst_n), .start(bf_start),
      .a_re(a_re), .a_im(a_im), .b_re(b_re), .b_im(b_im),
      .angle($signed(angle_acc)),
      .a_re_o(bf_are_o), .a_im_o(bf_aim_o), .b_re_o(bf_bre_o), .b_im_o(bf_bim_o),
      .done(bf_done)
  );

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= S_IDLE; done <= 1'b0;
      s <= 5'd0; half <= 10'd0; kstart <= 11'd0; j <= 10'd0;
      angle_step <= {ANGW{1'b0}}; angle_acc <= {ANGW{1'b0}};
      ri <= 3'd0; rc <= 3'd0; wi <= 3'd0; bf_started <= 1'b0;
      a_re <= 0; a_im <= 0; b_re <= 0; b_im <= 0;
      wr_are <= 0; wr_aim <= 0; wr_bre <= 0; wr_bim <= 0;
    end else begin
      done <= 1'b0;
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
          if (rd_req && rd_accept) ri <= ri + 3'd1;      // issue next read
          if (rd_valid) begin                             // collect (in order)
            case (rc)
              3'd0:    a_re <= rd_data;
              3'd1:    a_im <= rd_data;
              3'd2:    b_re <= rd_data;
              default: b_im <= rd_data;
            endcase
            rc <= rc + 3'd1;
          end
          if (rc == 3'd4) begin
            bf_started <= 1'b0;
            state <= S_COMP;
          end
        end

        S_COMP: begin
          bf_started <= 1'b1;                              // bf_start pulses cycle 1 only
          if (bf_done) begin
            wr_are <= bf_are_o; wr_aim <= bf_aim_o;
            wr_bre <= bf_bre_o; wr_bim <= bf_bim_o;
            wi <= 3'd0;
            state <= S_WRITE;
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
                state <= S_DONE;
              end else begin
                s <= s + 5'd1;
                half <= half << 1;
                angle_step <= angle_step >> 1;
                state <= S_READ;
              end
            end else begin
              kstart <= next_k;
              state <= S_READ;
            end
          end else begin
            j <= j + 10'd1;
            angle_acc <= angle_acc - angle_step;
            state <= S_READ;
          end
        end

        S_DONE: begin
          done <= 1'b1;
          state <= S_IDLE;
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  // high bits of i0/i1 are always 0 for N<=512 (index < 512); sink them for lint
  wire _unused = &{1'b0, i0[10:9], i1[10:9], next_k[10]};

endmodule
