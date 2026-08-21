/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * On-chip MCU-bus master for SeeTheBeat.  Implements docs/bus_protocol.md exactly and is
 * bit-compatible with model/mcu_bus_model.py (the cocotb test co-simulates that slave).
 *
 * Two concurrent engines share one clock:
 *   - Command engine: serialises READ (2 transfers) / WRITE (5 transfers) onto uio[5:0].
 *   - Response engine: captures read-data bytes from ui_in whenever resp_valid (uio[7])
 *     is high, pairing HI then LO into a 16-bit word.
 * They overlap: up to MAX_OUT reads may be outstanding, so a burst of reads streams its
 * commands while earlier responses come back (full-duplex pipelining). Responses are
 * strictly in order, so the controller matches the n-th rd_valid to the n-th read it
 * issued. Writes are never accepted while reads are outstanding (no RAW hazard).
 *
 * The master WAITS on resp_valid -- it assumes no fixed latency -- so it is robust to any
 * MCU timing inconsistencies (and to the 1-cycle modelling delay in the cocotb co-simulation).
 */

`default_nettype none

module mcu_bus #(
    parameter integer AW = 10,    // word address width (10 -> 0..1023 = 512 complex x2)
    parameter integer DW = 16     // data width (Q1.15 component)
) (
    input  wire            clk,
    input  wire            rst_n,

    // ---- controller (chip) side ----
    input  wire            rd_req,     // request a read of rd_addr
    input  wire [AW-1:0]   rd_addr,
    output wire            rd_accept,   // high when a read request is taken this cycle
    output reg  [DW-1:0]   rd_data,     // returned word (in issue order)
    output reg             rd_valid,    // 1-cycle pulse when rd_data is valid

    input  wire            wr_req,     // request a write of wr_data to wr_addr
    input  wire [AW-1:0]   wr_addr,
    input  wire [DW-1:0]   wr_data,
    output wire            wr_accept,   // high when a write request is taken this cycle

    // ---- pin side (see docs/bus_protocol.md) ----
    output wire [7:0]      uio_out,     // [5:0]=cmd lane, [7:6]=0 (inputs, oe=0)
    output wire [7:0]      uio_oe,      // constant 0x3F
    input  wire [7:0]      uio_in,      // [7]=resp_valid (others ignored here)
    input  wire [7:0]      ui_in        // read-data byte
);

  // ---- opcodes (top 2 bits of transfer T0); NOP=00 is the cmd-mux default ----
  localparam [1:0] OP_READ  = 2'b01;
  localparam [1:0] OP_WRITE = 2'b10;

  // ---- max outstanding reads: MUST match MAX_OUTSTANDING in model/mcu_bus_model.py ----
  localparam [2:0] MAX_OUT = 3'd4;

  // ---- command-engine states ---- new state for every transfer
  localparam [2:0] S_IDLE = 3'd0,
                   S_R0   = 3'd1, S_R1 = 3'd2,
                   S_W0   = 3'd3, S_W1 = 3'd4, S_W2 = 3'd5, S_W3 = 3'd6, S_W4 = 3'd7;

  reg [2:0]      state;
  reg [AW-1:0]   addr_r;
  reg [DW-1:0]   data_r;

  reg [2:0]      outstanding;   // reads issued but not yet fully returned
  reg            lo_phase;      // 0 = next resp byte is HI, 1 = LO
  reg [7:0]      hi_byte;       // captured HI awaiting its LO

  wire resp_v     = uio_in[7];
  wire word_done  = resp_v & lo_phase;      // a full 16-bit word completes this cycle
  wire issue_done = (state == S_R1);        // a read command finishes issuing this cycle

  // accept a read only in IDLE and below the outstanding cap; a write additionally needs
  // no reads in flight (keeps responses unambiguous, avoids read-after-write hazards).
  assign rd_accept = (state == S_IDLE) && (outstanding <  MAX_OUT);
  assign wr_accept = (state == S_IDLE) && (outstanding == 3'd0);

  // ---- command lane (combinational from state + latched addr/data) ----
  reg [5:0] cmd;
  always @(*) begin
    case (state)
      S_R0:    cmd = {OP_READ,  addr_r[9:6]};
      S_R1:    cmd = addr_r[5:0];
      S_W0:    cmd = {OP_WRITE, addr_r[9:6]};
      S_W1:    cmd = addr_r[5:0];
      S_W2:    cmd = data_r[15:10];
      S_W3:    cmd = data_r[9:4];
      S_W4:    cmd = {data_r[3:0], 2'b00};
      default: cmd = 6'b000000;              // NOP (S_IDLE and any spare state)
    endcase
  end
  assign uio_out = {2'b00, cmd};
  assign uio_oe  = 8'h3F;                     // uio[5:0] out, uio[7:6] in

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state       <= S_IDLE;
      addr_r      <= {AW{1'b0}};
      data_r      <= {DW{1'b0}};
      outstanding <= 3'd0;
      lo_phase    <= 1'b0;
      hi_byte     <= 8'd0;
      rd_data     <= {DW{1'b0}};
      rd_valid    <= 1'b0;
    end else begin
      rd_valid <= 1'b0;

      // ---- response engine: pair HI then LO into a word ----
      if (resp_v) begin
        if (!lo_phase) begin
          hi_byte  <= ui_in;
          lo_phase <= 1'b1;
        end else begin
          rd_data  <= {hi_byte, ui_in};
          rd_valid <= 1'b1;
          lo_phase <= 1'b0;
        end
      end

      // ---- outstanding counter (issue at S_R1, retire on each completed word) ----
      if (issue_done && !word_done)      outstanding <= outstanding + 3'd1;
      else if (!issue_done && word_done) outstanding <= outstanding - 3'd1;

      // ---- command engine ----
      case (state)
        S_IDLE: begin
          if (wr_req && wr_accept) begin
            addr_r <= wr_addr;
            data_r <= wr_data;
            state  <= S_W0;
          end else if (rd_req && rd_accept) begin
            addr_r <= rd_addr;
            state  <= S_R0;
          end
        end
        S_R0: state <= S_R1;
        S_R1: state <= S_IDLE;
        S_W0: state <= S_W1;
        S_W1: state <= S_W2;
        S_W2: state <= S_W3;
        S_W3: state <= S_W4;
        S_W4: state <= S_IDLE;
        default: state <= S_IDLE;
      endcase
    end
  end

  // uio_in[6:0] (mcu_status + unused) are not consumed by the bus master
  wire _unused = &{1'b0, uio_in[6:0]};

endmodule
