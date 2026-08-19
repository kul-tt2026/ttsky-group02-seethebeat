<!---

This file is used to generate your project datasheet. Please fill in the information below and delete any unused
sections.

You can also include images in this folder and reference them in the markdown. Each image must be less than
512 kb in size, and the combined size of all images must be less than 1 MB.
-->

## How it works

**SeeTheBeat** is a real-time audio visualizer. The chip does the digital
signal-processing and video generation; the Tiny Tapeout demo board's microcontroller
(RP2040/RP2350) acts as the memory and audio front-end.

1. **Samples in.** The microcontroller captures 16-bit audio samples into its SRAM.
2. **512-point FFT (on chip).** The chip streams samples over its bus and computes a
   512-point Fast Fourier Transform to obtain the frequency spectrum. Because a
   512-point working buffer (~2 KB) is far larger than on-chip memory allows, the buffer
   lives in the microcontroller's SRAM and the chip ping-pongs data with it on each of
   the 9 FFT stages. The butterfly uses a CORDIC rotator (no multiplier, no twiddle
   ROM); the transform is a scaled fixed-point (Q1.15) radix-2 design.
3. **Visualization (on chip).** Spectrum magnitudes drive procedural, beat-reactive visuals.
4. **VGA out.** Pixels are streamed over a Tiny VGA Pmod at 800x600 @ 60 Hz (40 MHz
   pixel clock), 6-bit color (RRGGBB) plus HSync/VSync.

## How to test

The microcontroller loads a block of 512 audio samples into its SRAM, then acts as a
memory slave answering the chip's read/write requests while the FFT runs. Verification
is done with cocotb testbenches that compare each RTL block **bit-for-bit** against a
Python golden model. Once the visual path is in, connect a VGA monitor to the Tiny VGA
Pmod to see the output.

## External hardware

- **Tiny VGA Pmod** (on the dedicated outputs) driving a VGA monitor.
- The demo-board **microcontroller** (RP2040 or RP2350) as the sample memory and audio
  ADC front-end, connected over the input and bidirectional buses.
