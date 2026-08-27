/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * SeeTheBeat top level -- FFT engine + VGA visual back-end.
 *
 *   - ui_in[7:0]  : 8-bit read-data from the MCU (into the bus)
 *   - uio[7:0]    : the MCU bus -- uio[5:0] cmd (out), uio[7]=resp_valid, uio[6]=frame-ready
 *   - uo_out[7:0] : VGA via the Tiny VGA Pmod -- {hsync, B0,G0,R0, vsync, B1,G1,R1}
 *
 * TWO INDEPENDENT LOOPS SHARE ONE BUS:
 *   1. On the rising edge of frame-ready the chip runs a 512-point FFT in place in MCU SRAM
 *      and pulses done. The transform is left where the MCU wants it -- in its own memory --
 *      and firmware takes it from there: magnitude, log, band summing, beat detection and
 *      the zone colour map all run on the MCU, where they cost no silicon.
 *   2. Once per frame, at the start of vblank, the chip fetches the 17-word `visual_state`
 *      block (16 bands + flash) back from the MCU's config region and latches it. Updating
 *      only during blanking is what stops a bar changing height halfway down the screen.
 *
 * Both loops are driven by fft_ctrl, which is the chip's single bus master -- see the note
 * there for why there is no arbiter. Pixels are then generated procedurally as
 * f(px, py, visual_state): no frame buffer, no stored objects.
 * See docs/visual_design.md for the full visual specification.
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

  // ---- VGA output path ----
  // vga_timing owns the beam position; pixel_gen is pure f(px, py, visual_state).
  wire [10:0] px;
  wire [9:0]  py;
  wire        active, hsync, vsync, vblank, frame_start;
  wire [1:0]  vga_r, vga_g, vga_b;
  wire [3:0]  zone;
  wire [4:0]  band, flash, cfg, cfg2;

  // ---- the animation clock ----
  // 800x600 pixels cannot be stored, so nothing animates by being remembered. The only
  // clock a stateless renderer has is this counter, and every effect must be a function of
  // (position, time, energy). It wraps every 256 frames (~4.3 s at 60 Hz).
  reg [7:0] frame;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n)          frame <= 8'd0;
    else if (frame_start) frame <= frame + 8'd1;
  end

  vga_timing u_vga (
      .clk(clk), .rst_n(rst_n),
      .px(px), .py(py), .active(active),
      .hsync(hsync), .vsync(vsync),
      .vblank(vblank), .frame_start(frame_start)
  );

  // The only visual state on the chip. Its power-on defaults draw a readable picture
  // before any firmware exists, so the output path can be validated on a monitor with
  // nothing else connected. Refreshed once per frame, in vblank: `frame_start` asks the bus master to fetch the
  // 17 words (16 bands + flash) from the MCU's config region. Updating only during
  // blanking is what keeps a bar from changing height halfway down the screen.
  visual_state u_vs (
      .clk(clk), .rst_n(rst_n),
      .wr_en(vs_wr_en), .wr_addr(vs_wr_addr), .wr_data(vs_wr_data),
      .rd_zone(zone), .band(band), .flash(flash), .cfg(cfg), .cfg2(cfg2)
  );

  // Combinational chain, no loop: pixel_gen decodes (px,py) -> zone, visual_state muxes
  // zone -> band, pixel_gen turns band -> colour. All inside one pixel clock.
  pixel_gen u_pix (
      .px(px), .py(py), .active(active),
      .zone(zone), .band(band), .flash(flash), .frame(frame), .cfg(cfg), .cfg2(cfg2),
      .r(vga_r), .g(vga_g), .b(vga_b)
  );

  // ---- FFT engine, and the chip's single MCU-bus master ----
  // It owns the bus for both jobs -- the transform and the once-per-frame visual_state
  // refresh -- which is why no arbiter exists: mcu_bus returns responses strictly in order
  // with no tags, so two interleaved masters would mis-route each other's data.
  wire       fft_done, fft_busy;
  wire [7:0] fft_uio_out, fft_uio_oe;
  wire       vs_wr_en;
  wire [4:0] vs_wr_addr, vs_wr_data;

  fft_ctrl #(.LOGN(9)) u_fft (
      .clk(clk), .rst_n(rst_n), .start(start), .done(fft_done),
      .refresh_req(frame_start),
      .vs_wr_en(vs_wr_en), .vs_wr_addr(vs_wr_addr), .vs_wr_data(vs_wr_data),
      .busy(fft_busy),
      .uio_out(fft_uio_out), .uio_oe(fft_uio_oe), .uio_in(uio_in), .ui_in(ui_in)
  );

  assign uio_out = fft_uio_out;
  assign uio_oe  = fft_uio_oe;

  // Tiny VGA Pmod packing: uo_out = {hsync, B0, G0, R0, vsync, B1, G1, R1}.
  // The pin NAMES are the trap -- the pin labelled R1 carries r[1], the MSB.
  assign uo_out = {hsync, vga_b[0], vga_g[0], vga_r[0],
                   vsync, vga_b[1], vga_g[1], vga_r[1]};

  // `vblank` is available for a future effect; `fft_done`/`busy` are observable on the bus
  // (the MCU knows when it last asserted frame-ready). Sink them for lint.
  wire _unused = &{1'b0, fft_done, fft_busy, vblank};

endmodule
