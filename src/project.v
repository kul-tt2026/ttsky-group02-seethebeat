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

  // ---- VGA output path (Part 2, Phase 0.3) ----
  // vga_timing owns the beam position; test_pattern is pure f(px, py). uo_out now carries
  // the real VGA signals, as info.yaml's pinout always said it would -- the Part 1
  // `fft_ready` bring-up flag has been retired, since the FFT engine is still fully
  // observable on the uio bus.
  wire [10:0] px;
  wire [9:0]  py;
  wire        active, hsync, vsync, vblank, frame_start;
  wire [1:0]  vga_r, vga_g, vga_b;
  wire [3:0]  zone;
  wire [4:0]  band, flash;

  vga_timing u_vga (
      .clk(clk), .rst_n(rst_n),
      .px(px), .py(py), .active(active),
      .hsync(hsync), .vsync(vsync),
      .vblank(vblank), .frame_start(frame_start)
  );

  // The only visual state on the chip. Its power-on defaults draw a readable picture
  // before any firmware exists, so the output path can be validated on a monitor with
  // nothing else connected. The MCU refresh burst (Phase 2) drives the write port.
  visual_state u_vs (
      .clk(clk), .rst_n(rst_n),
      .wr_en(1'b0), .wr_addr(5'd0), .wr_data(5'd0),      // TODO(Phase 2): MCU refresh
      .rd_zone(zone), .band(band), .flash(flash)
  );

  // Combinational chain, no loop: pixel_gen decodes (px,py) -> zone, visual_state muxes
  // zone -> band, pixel_gen turns band -> colour. All inside one pixel clock.
  pixel_gen u_pix (
      .px(px), .py(py), .active(active),
      .zone(zone), .band(band), .flash(flash),
      .r(vga_r), .g(vga_g), .b(vga_b)
  );

  // Tiny VGA Pmod packing: uo_out = {hsync, B0, G0, R0, vsync, B1, G1, R1}.
  // The pin NAMES are the trap -- the pin labelled R1 carries r[1], the MSB.
  assign uo_out = {hsync, vga_b[0], vga_g[0], vga_r[0],
                   vsync, vga_b[1], vga_g[1], vga_r[1]};

  // vblank/frame_start are Phase 2's hooks (the once-per-frame visual_state refresh reads
  // the MCU during blanking); fft_done is observable on the bus. Sink them for now.
  wire _unused = &{1'b0, fft_done, vblank, frame_start};

endmodule
