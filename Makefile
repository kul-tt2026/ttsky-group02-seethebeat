.PHONY: harden test test_gates png clean lint

# Environment variable checks for targets that need the PDK
define check_env
	$(if $(PDK_ROOT),,$(error PDK_ROOT is not set. Export it before running this target))
	$(if $(PDK),,$(error PDK is not set. Export it before running this target (e.g. sky130A)))
endef

# ---------------------------------------------------------------------------
# This linter was added by Claude Code
#
# lint: static RTL checks with Verilator (no PDK needed).
# Runs inside the tt2026 devcontainer where verilator is installed.
# Keeps -Wall for discipline; waives only DECLFILENAME (TT puts the tt_um_*
# module in project.v, so module name != file name is expected).
# ---------------------------------------------------------------------------
LINT_SOURCES ?= $(wildcard src/*.v)
LINT_FLAGS   ?= -Wall -Wno-DECLFILENAME

lint:
	@command -v verilator >/dev/null 2>&1 || { \
		echo "verilator not found — run this inside the tt2026 devcontainer"; exit 1; }
	verilator --lint-only $(LINT_FLAGS) $(LINT_SOURCES)
	@echo "lint: OK"

harden:
	$(call check_env)
	./tt/tt_tool.py --create-user-config
	./tt/tt_tool.py --harden
	./tt/tt_tool.py --print-warnings

test:
	$(MAKE) -C test

test_gates:
	$(call check_env)
	$(eval TOP_MODULE := $(shell ./tt/tt_tool.py --print-top-module))
	@if [ ! -f runs/wokwi/final/pnl/$(TOP_MODULE).pnl.v ]; then \
		echo "Error: Gate-level netlist not found. Run 'make harden' first."; \
		exit 1; \
	fi
	cp runs/wokwi/final/pnl/$(TOP_MODULE).pnl.v test/gate_level_netlist.v
	$(MAKE) -C test GATES=yes

png:
	$(call check_env)
	@if [ ! -d runs/wokwi ]; then \
		echo "Error: Harden has not been run. Run 'make harden' first."; \
		exit 1; \
	fi
	./tt/tt_tool.py --create-png

clean:
	$(MAKE) -C test clean
	rm -rf runs/ src/config_merged.json src/user_config.json
