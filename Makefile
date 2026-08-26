.PHONY: harden test test_gates png clean lint lint-attic

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
# -Wall stays FULLY on, INCLUDING width checks (WIDTHTRUNC/WIDTHEXPAND), so any
# *unintended* width bug is still caught everywhere in the design. The handful of
# *intentional* fixed-point truncations (Q1.15 scaling, CORDIC gain-comp, ...) are
# silenced at the exact line with inline pragmas:
#     /* verilator lint_off WIDTHTRUNC */  <one deliberate line>  /* verilator lint_on WIDTHTRUNC */
# Global waivers (kept minimal):
#   DECLFILENAME           - TT puts the tt_um_* module in project.v (name != file).
#   MULTITOP  (temporary)  - during development each block (cordic, butterfly, ...) is
#                            its own top; they unify under tt_um_* at integration.
#     TODO(integration): once project.v instantiates the whole design (single top),
#     REMOVE -Wno-MULTITOP so lint again flags any orphaned/forgotten module.
# ---------------------------------------------------------------------------
#
# src/attic/ is DELIBERATELY excluded from LINT_SOURCES (the `src/*.v` wildcard does not
# recurse). Those blocks are not part of the hardened design -- see src/attic/*.v -- but they
# are still linted, separately, by `lint-attic` so they cannot silently rot. Keeping them out
# of the main lint is what lets the MULTITOP waiver above be removed at integration without a
# deliberately-orphaned module tripping it.
LINT_SOURCES ?= $(wildcard src/*.v)
ATTIC_SOURCES ?= $(wildcard src/attic/*.v)
LINT_FLAGS   ?= -Wall -Wno-DECLFILENAME -Wno-MULTITOP

lint: lint-attic
	@command -v verilator >/dev/null 2>&1 || { \
		echo "verilator not found — run this inside the tt2026 devcontainer"; exit 1; }
	verilator --lint-only $(LINT_FLAGS) $(LINT_SOURCES)
	@echo "lint: OK"

# Attic blocks are standalone (each is its own top), so they are linted one file at a time.
lint-attic:
	@command -v verilator >/dev/null 2>&1 || { \
		echo "verilator not found — run this inside the tt2026 devcontainer"; exit 1; }
	@for f in $(ATTIC_SOURCES); do \
		echo "lint (attic): $$f"; \
		verilator --lint-only $(LINT_FLAGS) $$f || exit 1; \
	done
	@echo "lint-attic: OK"

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
