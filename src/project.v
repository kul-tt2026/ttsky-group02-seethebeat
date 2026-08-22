/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * SeeTheBeat top level.
 *
 * Wires the verified FFT engine (fft_ctrl = mcu_bus + butterfly + CORDIC) to the pins:
 *   - ui_in[7:0]  : 8-bit read-data from the MCU (into the bus)
 *   - uio[7:0]    : the MCU bus -- uio[5:0] cmd (out), uio[7]=resp_valid, uio[6]=frame-ready
 *   - uo_out[7:0] : interim status/debug (until Part 2 drives it as VGA)
 * The chip kicks off a 512-point FFT on the rising edge of frame-ready (uio_in[6]) and
 * ping-pongs the working buffer with MCU SRAM over the bus.
 *
 * NOTE (Part 2): uo_out is reserved for VGA; here it carries a live status snapshot so the
 * design hardens with every pin connected. The magnitude read-out (spectrum_mag) and VGA
 * come in Part 2, where the CORDIC is shared between the FFT and the read-out.
 */

`default_nettype none

module tt_um_group02_seethebeat (
    input  wire [7:0] ui_in,    // read data from MCU
    output wire [7:0] uo_out,   // VGA
    input  wire [7:0] uio_in,   // [7]=resp_valid, [6]=frame-ready, rest bus
    output wire [7:0] uio_out,  // [5:0]=cmd lane to MCU
    output wire [7:0] uio_oe,   // bus direction (0x3F)
    input  wire       ena,      // high while selected
    input  wire       clk,      // 40 MHz target
    input  wire       rst_n     // active-low reset
);

  // ---- start an FFT on the rising edge of frame-ready, when enabled ----
  reg mcu_status_d;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) mcu_status_d <= 1'b0;
    else        mcu_status_d <= uio_in[6];
  end
  wire start = ena & uio_in[6] & ~mcu_status_d;     // 1-cycle pulse (ignored unless idle)

  // ---- the FFT engine (bus + butterfly + CORDIC) ----
  wire       fft_done;
  wire [7:0] fft_uio_out;
  wire [7:0] fft_uio_oe;

  fft_ctrl #(.LOGN(9)) u_fft (
      .clk    (clk),
      .rst_n  (rst_n),
      .start  (start),
      .done   (fft_done),
      .uio_out(fft_uio_out),
      .uio_oe (fft_uio_oe),
      .uio_in (uio_in),
      .ui_in  (ui_in)
  );

  assign uio_out = fft_uio_out;
  assign uio_oe  = fft_uio_oe;

  // {done, resp_valid, current 6-bit bus command}. Part 2 replaces this with VGA.
  reg [7:0] status;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) status <= 8'd0;
    else        status <= {fft_done, uio_in[7], fft_uio_out[5:0]};
  end
  assign uo_out = status;

endmodule
