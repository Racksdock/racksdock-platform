ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../..)
PY := $(ROOT)/.venv/bin/python
REPOS := racksdock-subnet racksdock-node racksdock-platform

.PHONY: test $(REPOS) sim lint
test: $(REPOS)
$(REPOS):
	cd $(ROOT)/$@ && $(PY) -m pytest
lint:
	for r in $(REPOS); do (cd $(ROOT)/$$r && $(PY) -m ruff check src tests); done
sim:
	$(PY) $(ROOT)/racksdock-platform/dev/sim/run_sim.py --nodes 2 --duration 90
