# marketplace (`racksdock_platform.marketplace`)

The platform (docs/02, docs/07): REST API, host-agent orchestration, validator and miner endpoints and billing. The web console is the sibling repo `racksdock-ui`; the app loads it through the `racksdock.ui` entry point when it is installed, and serves only the API otherwise.

Run: `racksdock-marketplace serve` (SQLite by default, `RACKSDOCK_DB_URL` for Postgres). Dev: `RACKSDOCK_TRUST_MODE=dev RACKSDOCK_DEV_CA_DIR=... RACKSDOCK_CHAIN=mock RACKSDOCK_CHAIN_FILE=...`.

What it never does: hold tenant private keys, open an SSH path into a VM, or forward workload telemetry. Every host-agent event and validator record is a signed envelope stored in the evidence table; the public verification pages are rendered from those.

Layout
- `models.py` SQLAlchemy models (users, keys, templates, nodes, leases with a partial unique index, instances, evidence, strikes, incidents, billing, settings)
- `services/hosts.py` WebSocket registry of connected host-agents, command dispatch, event handling
- `services/orchestrator.py` instance state machine, boot-evidence verification, reachability probe, timeouts
- `services/allocation.py`, `services/billing.py`, `services/catalog.py`, `services/auth.py`, `services/records.py`
- `routers/` public catalog, instances, sshkeys, templates, billing, validators, miners, admin
