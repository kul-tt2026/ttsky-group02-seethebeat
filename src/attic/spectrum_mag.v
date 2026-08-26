/*
 * Copyright (c) 2026 Jonas Creyns, Giel Swenters
 * SPDX-License-Identifier: Apache-2.0
 *
 * =============================================================================
 *  ATTIC -- NOT PART OF THE HARDENED DESIGN.
 *
 *  This block is deliberately excluded from the chip: it is NOT in info.yaml
 *  `source_files`, NOT in test/Makefile `PROJECT_SOURCES`, and NOT instantiated
 *  by project.v. Nothing here reaches silicon. It is kept for reference and for
 *  the write-up -- a worked example of a CORDIC-vectoring magnitude core with a
 *  priority-encoder log2 -- and in case area frees up later.
 *
 *  WHY IT WAS PULLED (2026-08-25): sharing the one CORDIC between the butterfly
 *  (ROTATE) and this core (VECTOR) meant a mux on every CORDIC operand input.
 *  Those muxes drove utilisation to ~80% and the GDS render failed. Magnitude is
 *  free on the MCU -- it already holds the whole spectrum -- so the function moved
 *  to firmware. model/spectrum_ref.py is the LIVE reference the firmware must
 *  match bit-for-bit, and its self-test still runs in CI.
 *
 *  TO BRING IT BACK: restore the `op`/`log_mag` mux in src/fft_alu.v and the
 *  S_MREAD/S_MCOMP phase in src/fft_ctrl.v (both are in git history before
 *  2026-08-25, along with the op=1 vectors in test_units/test_fft_alu.py and the
 *  magnitude-stream check in test_units/test_fft_ctrl.py), then re-add the file
 *  to info.yaml and the Makefiles. Only do this against a real post-harden area
 *  number.
 *
 *  It is still lint-checked -- `make lint-attic` -- so it cannot silently rot.
 * =============================================================================
 *
 * Per-bin magnitude + log read-out for SeeTheBeat. CORDIC-LESS core: it drives
 * the SHARED CORDIC (owned by fft_alu) in VECTOR mode to get |X| = sqrt(re^2+im^2), then
 * a cheap piecewise-linear log2 = { MSB index , 2 mantissa bits below it }.
 * Bit-exact to model/spectrum_ref.py. Multi-cycle: pulse `start`; `done` pulses when
 * `log_mag` is valid. fft_alu multiplexes the one CORDIC between this and butterfly.
 */
`default_nettype none

module spectrum_mag #(
    parameter integer DW    = 16,
    parameter integer AW    = 20,
    parameter integer XYW   = 22,
    parameter integer LOG_W = 7      // packed log = {msb[4:0], frac[1:0]}
) (
    input  wire                  clk,
    input  wire                  rst_n,
    input  wire                  start,
    input  wire signed [DW-1:0]  re,
    input  wire signed [DW-1:0]  im,
    output reg  [LOG_W-1:0]      log_mag,
    output reg                   done,

    // ---- shared-CORDIC handshake (VECTOR): driven here, muxed/resolved in fft_alu ----
    output wire                  c_start,
    output wire signed [DW-1:0]  c_x_in,       // re -> CORDIC x_in
    output wire signed [DW-1:0]  c_y_in,       // im -> CORDIC y_in
    output wire signed [AW-1:0]  c_ang_in,     // 0 (CORDIC ignores ang_in in VECTOR mode)
    input  wire signed [XYW-1:0] c_x_out,      // |X| = CORDIC x_out
    input  wire                  c_done
);

  localparam [1:0] ST_IDLE = 2'd0, ST_START = 2'd1, ST_WAIT = 2'd2;
  reg [1:0] state;
  reg signed [DW-1:0] re_r, im_r;

  assign c_start  = (state == ST_START);
  assign c_x_in   = re_r;
  assign c_y_in   = im_r;
  assign c_ang_in = {AW{1'b0}};       // unused by the CORDIC in VECTOR mode

  // ---- log2 encoder (combinational on the vectoring magnitude) ----
  wire [XYW-1:0] mag = c_x_out;        // magnitude is non-negative; treat bits as unsigned

  function [4:0] msb_idx(input [XYW-1:0] v);   // index of the most-significant set bit
    integer i;
    begin
      msb_idx = 5'd0;
      for (i = 0; i < XYW; i = i + 1)
        if (v[i]) msb_idx = i[4:0];
    end
  endfunction

  wire [4:0]       msb  = msb_idx(mag);
  wire [4:0]       sh   = (msb >= 5'd2) ? (msb - 5'd2) : 5'd0;
  wire [1:0]       frac = (msb >= 5'd2) ? mag[sh +: 2] : 2'b00;   // 2 bits below the MSB
  wire [LOG_W-1:0] log_code = {msb, frac};

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= ST_IDLE; done <= 1'b0; log_mag <= {LOG_W{1'b0}};
      re_r <= 0; im_r <= 0;
    end else begin
      done <= 1'b0;
      case (state)
        ST_IDLE:  if (start) begin re_r <= re; im_r <= im; state <= ST_START; end
        ST_START: state <= ST_WAIT;      // c_start pulses -> shared CORDIC latches re/im
        ST_WAIT:  if (c_done) begin log_mag <= log_code; done <= 1'b1; state <= ST_IDLE; end
        default:  state <= ST_IDLE;
      endcase
    end
  end

endmodule
