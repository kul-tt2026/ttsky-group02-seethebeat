/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * FFT controller / sequencer for SeeTheBeat -- runs the full N-point radix-2 DIT FFT in
 * place in MCU memory. Per butterfly: burst 4 reads (A/B), rotate+add+saturate, write 4
 * back. Mirrors model/fft_ref.py exactly (counters s,half,kstart,j; twiddle via the angle
 * accumulator angle_acc -= angle_step, no ROM).
 *
 * Memory: complex point i at word 2i (re) / 2i+1 (im); 10-bit word address. Input must be
 * in MCU memory in BIT-REVERSED order (MCU firmware) before `start`.
 *
 * CHANGED 2026-08-25: the magnitude read-out phase (S_MREAD/S_MCOMP + the mbin counter and
 * the mag_valid/mag_data stream) was REMOVED -- magnitude+log now runs in MCU firmware
 * against model/spectrum_ref.py. The FFT leaves the transformed buffer in MCU memory, which
 * is where the MCU wants it anyway; `done` says it is ready to read.
 */

`default_nettype none

module fft_ctrl #(
    parameter integer LOGN = 9,       // log2(N); N = 512 for the real chip
    parameter integer DW   = 16,      // Q1.15 component width
    parameter integer ANGW = 20,      // CORDIC angle width (2^ANGW = full circle)
    parameter integer VS_N = 19,      // visual_state words: 16 bands + flash + 2 config
    parameter integer VS_W = 5        // bits kept from each fetched word
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,          // pulse: begin (data already loaded, bit-reversed)
                                    //   latched -- see start_pending
    output reg        done,           // 1-cycle pulse when the in-place FFT is complete

    // ---- visual_state refresh (Part 2): pulse `refresh_req` in vblank; the fetched
    //      words are streamed out on vs_wr_*. Skipped if a transform is in flight. ----
    input  wire       refresh_req,
    output reg        vs_wr_en,
    output reg [4:0]  vs_wr_addr,
    output reg [VS_W-1:0] vs_wr_data,
    output wire       busy,           // high while the FFT or a refresh owns the bus

    // pin-side bus to the MCU (see docs/bus_protocol.md)
    output wire [7:0] uio_out,
    output wire [7:0] uio_oe,
    input  wire [7:0] uio_in,
    input  wire [7:0] ui_in
);

  localparam integer AW = 10;                 // protocol word-address width

  // Build guard: the AW-bit word bus caps N at 2^(AW-1), i.e. LOGN <= AW-1.
  initial if (LOGN > AW - 1)
    $error("fft_ctrl: LOGN=%0d exceeds the %0d-bit word bus (max LOGN=%0d)",
           LOGN, AW, AW - 1);

  // ---- counter widths, sized for the LOGN <= AW-1 = 9 the guard above enforces ----
  // A complex index is 0..N-1, so IXW bits. The group pointer needs ONE more bit than that,
  // because its successor next_k must be able to reach N itself (that is what ends a stage).
  localparam integer IXW = AW - 1;            // 9: complex index / half / j
  localparam integer KW  = AW;                // 10: group pointer, reaches N
  localparam integer SW  = 4;                 // stage counter, holds 1..9

  localparam [IXW-1:0]  IX_ONE    = {{(IXW-1){1'b0}}, 1'b1};
  localparam [SW-1:0]   S_ONE     = {{(SW-1){1'b0}}, 1'b1};
  localparam [KW-1:0]   N         = {{(KW-1){1'b0}}, 1'b1} << $unsigned(LOGN);
  localparam [SW-1:0]   LOGN_VAL  = LOGN[SW-1:0];
  localparam [ANGW-1:0] ANG_STEP0 = {1'b1, {(ANGW-1){1'b0}}};   // 2^(ANGW-1), stage s=1

  localparam [2:0] S_IDLE=3'd0, S_READ=3'd1, S_COMP=3'd2, S_WRITE=3'd3,
                   S_NEXT=3'd4, S_DONE=3'd5, S_VS=3'd6;
  reg [2:0] state;

  // ---- FFT loop state (mirrors fft_ref.py) ----
  reg [SW-1:0]  s;                     // stage 1..LOGN
  reg [IXW-1:0] half;                  // 2^(s-1), max 2^(LOGN-1) = 256
  reg [KW-1:0]  kstart;                // group start (complex index)
  reg [IXW-1:0] j;                     // butterfly within group, max half-1 = 255
  reg signed [ANGW-1:0] angle_step;    // 2^(ANGW-s)
  reg signed [ANGW-1:0] angle_acc;     // -j*angle_step (twiddle angle; signed for CORDIC)

  wire [KW-1:0] i0     = kstart + {1'b0, j};
  wire [KW-1:0] i1     = i0 + {1'b0, half};
  wire [KW-1:0] mstep  = {1'b0, half} << 1;
  wire [KW-1:0] next_k = kstart + mstep;

  wire last_bf    = (j == (half - IX_ONE));
  wire last_group = (next_k == N);
  wire last_stage = (s == LOGN_VAL);

  // ---- operand registers ----
  reg [DW-1:0] a_re, a_im, b_re, b_im;          // the 4 read operands
  reg [2:0]    ri, rc, wi;                       // read-issue / read-collect / write-issue
  reg          bf_started;

  // ---- visual_state refresh counters (issue / collect), one per fetched word ----
  reg [4:0]    vi, vc;

  // ---- START IS LATCHED, NOT SAMPLED ----
  // `start` is a one-cycle pulse derived from the frame-ready edge, and it is only acted on
  // in S_IDLE. Without this latch a frame-ready arriving while a transform is running -- or
  // during the once-per-frame visual_state refresh, which is a window the MCU cannot see --
  // would be silently DROPPED, and the MCU would wait forever for a transform that never
  // started. Neither `done` nor `busy` reaches a pin, so the MCU has no way to detect it
  // either. One flop removes the whole failure mode.
  reg          start_pending;

  // ---- the CORDIC ALU (butterfly + the one CORDIC) ----
  wire          bf_start = !bf_started && (state == S_COMP);
  wire          bf_done;
  wire [DW-1:0] bf_are_o, bf_aim_o, bf_bre_o, bf_bim_o;

  fft_alu #(.DW(DW), .AW(ANGW), .XYW(22)) u_alu (
      .clk(clk), .rst_n(rst_n), .start(bf_start),
      .a_re(a_re), .a_im(a_im), .b_re(b_re), .b_im(b_im),
      .angle(angle_acc),
      .a_re_o(bf_are_o), .a_im_o(bf_aim_o), .b_re_o(bf_bre_o), .b_im_o(bf_bim_o),
      .done(bf_done)
  );

  // ---- bus request muxes ----
  wire            rd_req, rd_cfg, rd_accept, rd_valid, wr_req, wr_accept;
  wire [AW-1:0]   rd_addr, wr_addr;
  wire [DW-1:0]   rd_data, wr_data;

  reg [AW-1:0] rd_addr_c;
  always @(*) begin
    case (ri)
      3'd0:    rd_addr_c = {i0[IXW-1:0], 1'b0};    // A_re
      3'd1:    rd_addr_c = {i0[IXW-1:0], 1'b1};    // A_im
      3'd2:    rd_addr_c = {i1[IXW-1:0], 1'b0};    // B_re
      default: rd_addr_c = {i1[IXW-1:0], 1'b1};    // B_im
    endcase
  end

  // The write mux reads the butterfly's OWN output registers directly -- no local copy.
  // Timing invariant that makes this safe: butterfly writes a_re_o..b_im_o only on its
  // c_done (in its ST_WAIT) and then parks in ST_IDLE; the next write needs bf_start, which
  // this FSM cannot re-assert until S_WRITE -> S_NEXT -> S_READ -> S_COMP has run. So the
  // results are stable for the whole write burst. (Do not re-order those states without
  // re-checking this.)
  reg [AW-1:0] wr_addr_c;
  reg [DW-1:0] wr_data_c;
  always @(*) begin
    case (wi)
      3'd0:    begin wr_addr_c = {i0[IXW-1:0], 1'b0}; wr_data_c = bf_are_o; end
      3'd1:    begin wr_addr_c = {i0[IXW-1:0], 1'b1}; wr_data_c = bf_aim_o; end
      3'd2:    begin wr_addr_c = {i1[IXW-1:0], 1'b0}; wr_data_c = bf_bre_o; end
      default: begin wr_addr_c = {i1[IXW-1:0], 1'b1}; wr_data_c = bf_bim_o; end
    endcase
  end

  // In S_VS the refresh streams config-reads at addresses 0..VS_N-1 of the CONFIG space
  // (a separate address space from the FFT buffer, which already fills all 1024 words).
  assign rd_req  = ((state == S_READ) && (ri != 3'd4)) ||
                   ((state == S_VS)   && (vi != VS_N[4:0]));
  assign rd_cfg  = (state == S_VS);
  assign rd_addr = (state == S_VS) ? {{(AW-5){1'b0}}, vi} : rd_addr_c;
  assign wr_req  = (state == S_WRITE) && (wi != 3'd4);
  assign wr_addr = wr_addr_c;
  assign wr_data = wr_data_c;

  mcu_bus #(.AW(AW), .DW(DW)) u_bus (
      .clk(clk), .rst_n(rst_n),
      .rd_req(rd_req), .rd_addr(rd_addr), .rd_cfg(rd_cfg), .rd_accept(rd_accept),
      .rd_data(rd_data), .rd_valid(rd_valid),
      .wr_req(wr_req), .wr_addr(wr_addr), .wr_data(wr_data), .wr_accept(wr_accept),
      .uio_out(uio_out), .uio_oe(uio_oe), .uio_in(uio_in), .ui_in(ui_in)
  );

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= S_IDLE; done <= 1'b0;
      s <= {SW{1'b0}}; half <= {IXW{1'b0}}; kstart <= {KW{1'b0}}; j <= {IXW{1'b0}};
      angle_step <= {ANGW{1'b0}}; angle_acc <= {ANGW{1'b0}};
      ri <= 3'd0; rc <= 3'd0; wi <= 3'd0; bf_started <= 1'b0;
      a_re <= 0; a_im <= 0; b_re <= 0; b_im <= 0;
      vi <= 5'd0; vc <= 5'd0; start_pending <= 1'b0;
      vs_wr_en <= 1'b0; vs_wr_addr <= 5'd0; vs_wr_data <= {VS_W{1'b0}};
    end else begin
      done <= 1'b0;
      vs_wr_en <= 1'b0;
      // Remember a start that lands while we are busy. Placed BEFORE the case so that the
      // clear inside S_IDLE (a later assignment in the same block) wins when a pulse and a
      // launch coincide -- i.e. it is consumed, not left pending.
      if (start) start_pending <= 1'b1;
      case (state)
        S_IDLE: begin
          // The FFT wins if both arrive together -- including a start that was latched
          // while we were busy: a transform is time-critical, a refresh is not. A refresh is only ever accepted from IDLE, so it can never interleave
          // with FFT traffic -- which matters because mcu_bus returns responses strictly
          // in order with no tags, so two interleaved readers would mis-route each other's
          // data. If a transform is still running at vblank the refresh is simply SKIPPED
          // for that frame; the visuals hold their previous values for one frame, which is
          // imperceptible at 60 Hz.
          if (start || start_pending) begin
            start_pending <= 1'b0;                     // consumed
            s <= S_ONE; half <= IX_ONE;                // stage 1, half = 1
            kstart <= {KW{1'b0}}; j <= {IXW{1'b0}};
            angle_step <= ANG_STEP0; angle_acc <= {ANGW{1'b0}};
            ri <= 3'd0; rc <= 3'd0;
            state <= S_READ;
          end else if (refresh_req) begin
            vi <= 5'd0; vc <= 5'd0;
            state <= S_VS;
          end
        end

        // ---- visual_state refresh: stream VS_N config-reads, write each returned word's
        //      low VS_W bits into visual_state. Responses are in order, so the n-th word
        //      belongs at address n. ----
        S_VS: begin
          if (rd_req && rd_accept) vi <= vi + 5'd1;
          if (rd_valid) begin
            vs_wr_en   <= 1'b1;
            vs_wr_addr <= vc;
            vs_wr_data <= rd_data[VS_W-1:0];
            vc         <= vc + 5'd1;
          end
          if (vc == VS_N[4:0]) state <= S_IDLE;
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
          // results stay in the butterfly's output registers -- see the write-mux note
          if (bf_done) begin wi <= 3'd0; state <= S_WRITE; end
        end

        S_WRITE: begin
          if (wr_req && wr_accept) wi <= wi + 3'd1;
          if (wi == 3'd4) state <= S_NEXT;
        end

        S_NEXT: begin
          ri <= 3'd0; rc <= 3'd0;
          if (last_bf) begin
            j <= {IXW{1'b0}}; angle_acc <= {ANGW{1'b0}};
            if (last_group) begin
              kstart <= {KW{1'b0}};
              if (last_stage) begin
                state <= S_DONE;                    // FFT complete, buffer left in MCU RAM
              end else begin
                s <= s + S_ONE;
                half <= half << 1; angle_step <= angle_step >> 1;
                state <= S_READ;
              end
            end else begin
              kstart <= next_k; state <= S_READ;
            end
          end else begin
            j <= j + IX_ONE;
            angle_acc <= angle_acc - angle_step;
            state <= S_READ;
          end
        end

        S_DONE: begin done <= 1'b1; state <= S_IDLE; end
        default: state <= S_IDLE;
      endcase
    end
  end

  // i0/i1 are complex indices < N <= 2^IXW, so their top bit is always 0 (only
  // next_k[KW-1] is real -- it is what makes last_group fire at N). Sink for lint.
  assign busy = (state != S_IDLE);

  wire _unused = &{1'b0, i0[KW-1], i1[KW-1], rd_data[DW-1:VS_W]};

endmodule
