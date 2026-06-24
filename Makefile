.PHONY: harden test test_gates png clean

# Environment variable checks for targets that need the PDK
define check_env
	$(if $(PDK_ROOT),,$(error PDK_ROOT is not set. Export it before running this target))
	$(if $(PDK),,$(error PDK is not set. Export it before running this target (e.g. sky130A)))
endef

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
