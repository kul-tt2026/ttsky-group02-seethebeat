/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

module tt_um_group02_seethebeat (
    input  wire [7:0] ui_in,    // Dedicated inputs -- sample/read data from MCU
    output wire [7:0] uo_out,   // Dedicated outputs -- VGA outputs
    input  wire [7:0] uio_in,   // IOs: Input path -- MCU bus to read data
    output wire [7:0] uio_out,  // IOs: Output path -- MCU bus for cmd/address/writes
    output wire [7:0] uio_oe,   // IOs: Enable path -- per-pin connection (1 = out)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock (target 40 MHz)
    input  wire       rst_n     // reset, active low
);

  // All output pins must be assigned. If not used, assign to 0.
  assign uo_out  = 8'b0;  // VGA outputs
  assign uio_out = 8'b0;  // MCU bus outputs
  assign uio_oe  = 8'b0;  // all bidirectional pins in input mode for test.py

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, clk, rst_n, ui_in, uio_in, 1'b0};

endmodule
