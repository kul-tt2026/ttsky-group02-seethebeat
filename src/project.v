/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * SeeTheBeat top level (Part 1: the FFT engine).
 *
 * Wires the FFT engine (fft_ctrl = mcu_bus + fft_alu[butterfly + the one CORDIC]) to the pins:
 *   - ui_in[7:0]  : 8-bit read-data from the MCU (into the bus)
 *   - uio[7:0]    : the MCU bus -- uio[5:0] cmd (out), uio[7]=resp_valid, uio[6]=frame-ready
 *   - uo_out[7:0] : RESERVED FOR VGA (Part 2). Part 1 uses uo_out[0] as a bring-up flag.
 *
 * On the rising edge of frame-ready the chip runs a 512-point FFT in place in MCU SRAM
 * (ping-ponging over the bus) and pulses done. The transformed buffer is left where the MCU
 * wants it -- in its own memory -- and the MCU takes it from there: magnitude, log, band
 * summing, zone/colour mapping and beat detection all run in firmware
 * (model/spectrum_ref.py is the bit-exact reference for the magnitude+log step).
 *
 * Part 2 replaces uo_out with the VGA pixel stream, generated on chip as f(x, y, time,
 * visual_state) where visual_state is a small per-frame block the chip READS from a reserved
 * MCU region. Only the per-pixel arithmetic stays in silicon; every decision stays in
 * firmware, where it is free.
 */

`default_nettype none

module tt_um_group02_seethebeat (
    input  wire [7:0] ui_in,    // read data from MCU
    output wire [7:0] uo_out,   // VGA in Part 2; Part 1: [0] = fft_ready
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

  // ---- FFT engine ----
  wire       fft_done;
  wire [7:0] fft_uio_out, fft_uio_oe;

  fft_ctrl #(.LOGN(9)) u_fft (
      .clk(clk), .rst_n(rst_n), .start(start), .done(fft_done),
      .uio_out(fft_uio_out), .uio_oe(fft_uio_oe), .uio_in(uio_in), .ui_in(ui_in)
  );

  assign uio_out = fft_uio_out;
  assign uio_oe  = fft_uio_oe;

  // ---- uo_out: reserved for the VGA pixel stream (Part 2) ----
  // Until then one bit earns its keep for bring-up: fft_ready goes high when a transform
  // completes and clears when the next one starts, so a scope or the MCU can see the engine
  // turn over without decoding the bus. fft_ctrl's `done` is a single-cycle pulse; this
  // latches it into a level.
  reg fft_ready;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n)        fft_ready <= 1'b0;
    else if (start)    fft_ready <= 1'b0;
    else if (fft_done) fft_ready <= 1'b1;
  end
  assign uo_out = {7'b0000000, fft_ready};

endmodule
