# racksdock-platform

The owner's side and the user's side. One Python package, `racksdock_platform`, installed as `racksdock-platform`.

```
src/racksdock_platform/
  marketplace/   REST API (Shadeform-shaped instances, SSH keys, templates, billing), host-agent orchestration over WebSocket,
                 atomic leases, independent verification of every boot evidence, evidence store and public verification pages,
                 validator and miner endpoints; the web console is the sibling repo racksdock-ui, loaded through the racksdock.ui entry point
  cli/           `racksdock` user CLI; `racksdock ssh` re-verifies attestation client-side and pins the attested host key
dev/             bootstrap.sh (venv + editable installs of all three repos), test.sh / Makefile (all suites), sim/run_sim.py (whole system as processes)
tests/           conftest.py plus one directory per subpackage
docs/            per-subpackage notes
```

Install and test (requires the siblings `racksdock-subnet` and `racksdock-node`):

```
pip install -e ../racksdock-subnet -e ../racksdock-node -e ".[test]"
pytest
```

Entry points: `racksdock-marketplace serve`, `racksdock`. Run everything locally with `dev/sim/run_sim.py`.
