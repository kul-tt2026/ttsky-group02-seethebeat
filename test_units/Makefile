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
# blocks that instantiate submodules must list them here too:
ifeq ($(BLOCK),butterfly)
VERILOG_SOURCES += $(PWD)/../src/cordic.v
endif
TOPLEVEL        = $(BLOCK)
COCOTB_TEST_MODULES = test_$(BLOCK)

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: clean
clean::
	rm -rf sim_build __pycache__ results.xml *.fst
