# Unit-test harness for individual RTL blocks (cocotb drives the block directly as
# TOPLEVEL -- no Verilog testbench needed). No PDK required.
#
#   make            # run the default block test (cordic)
#   make clean

SIM            ?= icarus
TOPLEVEL_LANG  ?= verilog

# Which block to test (override on the command line, e.g. `make BLOCK=cordic`).
BLOCK          ?= cordic

VERILOG_SOURCES = $(PWD)/../src/$(BLOCK).v
# vga_timing is standalone (pure counters + comparators) -- no extra sources needed.
# VGA_LINES=<n> walks only the first n lines instead of a whole frame, for fast local
# iteration: `make BLOCK=vga_timing VGA_LINES=4`. It is exported so cocotb sees it, and
# the test skips the frame-level checks it then cannot reach. CI always runs the full
# frame -- the vertical wrap happens exactly ONCE per frame and is invisible below it.
export VGA_LINES
# blocks that instantiate submodules must list them here too:
ifeq ($(BLOCK),fft_alu)
VERILOG_SOURCES += $(PWD)/../src/butterfly.v $(PWD)/../src/cordic.v
endif
ifeq ($(BLOCK),fft_ctrl)
VERILOG_SOURCES += $(PWD)/../src/mcu_bus.v $(PWD)/../src/fft_alu.v $(PWD)/../src/butterfly.v $(PWD)/../src/cordic.v
# N = 2^FFT_LOGN. Default 6 keeps the full-FFT sim quick for iteration, but note what a
# small N CANNOT reach: word addresses only span 0..2N-1, so addr[9] is never driven below
# LOGN=9. Run `make BLOCK=fft_ctrl FFT_LOGN=9` (the real chip size) to exercise the whole
# 10-bit bus address space -- CI does this as a separate full-size step.
FFT_LOGN       ?= 6
COMPILE_ARGS   += -Pfft_ctrl.LOGN=$(FFT_LOGN)
endif
TOPLEVEL        = $(BLOCK)
COCOTB_TEST_MODULES = test_$(BLOCK)

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: clean
clean::
	rm -rf sim_build __pycache__ results.xml *.fst
