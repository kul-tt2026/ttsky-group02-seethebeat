/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * SeeTheBeat top level (FFT complete).
 *
 * Wires the FFT + magnitude engine (fft_ctrl = mcu_bus + fft_alu[cordic]) to the pins:
 *   - ui_in[7:0]  : 8-bit read-data from the MCU (into the bus)
 *   - uio[7:0]    : the MCU bus -- uio[5:0] cmd (out), uio[7]=resp_valid, uio[6]=frame-ready
 *   - uo_out[7:0] : the spectrum MAGNITUDE stream -- uo_out[7]=valid, uo_out[6:0]=log-mag
 * On the rising edge of frame-ready the chip runs a 512-point FFT (ping-ponging the buffer
 * with MCU SRAM), then streams the N/2 bin log-magnitudes out on uo_out.
 *
 * NOTE (Part 2): uo_out is reserved for VGA; here it carries the real FFT end product (the
 * log-magnitudes). Part 2 replaces this with the VGA pixel stream driven by those bands.
 */

`default_nettype none

module tt_um_group02_seethebeat (
    input  wire [7:0] ui_in,    // read data from MCU
    output wire [7:0] uo_out,   // magnitude stream (VGA in Part 2)
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

  // ---- FFT + magnitude engine ----
  wire       fft_done, mag_valid;
  wire [6:0] mag_data;
  wire [7:0] fft_uio_out, fft_uio_oe;

  fft_ctrl #(.LOGN(9)) u_fft (
      .clk(clk), .rst_n(rst_n), .start(start), .done(fft_done),
      .mag_valid(mag_valid), .mag_data(mag_data),
      .uio_out(fft_uio_out), .uio_oe(fft_uio_oe), .uio_in(uio_in), .ui_in(ui_in)
  );

  assign uio_out = fft_uio_out;
  assign uio_oe  = fft_uio_oe;

  // ---- uo_out: the spectrum magnitude stream (the FFT's real end product) ----
  // [7] = valid strobe (a new bin this cycle), [6:0] = that bin's log-magnitude (held).
  reg [6:0] mag_hold;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n)         mag_hold <= 7'd0;
    else if (mag_valid) mag_hold <= mag_data;
  end
  assign uo_out = {mag_valid, mag_hold};

  wire _unused = &{1'b0, fft_done};   // FFT-complete pulse not routed to a pin (yet)

endmodule
