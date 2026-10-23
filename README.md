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

## Payments

Credits are US dollar cents on the account; running instances are metered per minute against them (`services/billing.py`).
Three ways to add credits (`services/payments/`, routes under `/v1/billing`, all written to the `deposits` table with the
native amount, the rate used and the chain or processor reference):

- **TAO**, sent straight to a treasury coldkey. Install the SDK with `pip install "racksdock-platform[tao]"` and set `RACKSDOCK_TAO_TREASURY=<ss58>`: a wallet you keep cold; the platform
  only reads the chain and never holds a key that can spend. A user links the coldkey they pay from by signing a challenge
  (a browser wallet extension or `btcli wallet sign`), then sends any amount. A watcher thread reads finalized blocks on
  `RACKSDOCK_CHAIN_NETWORK` and credits each transfer at the TAO price when it is final: the median of CoinGecko, Kraken and
  Binance, or `RACKSDOCK_TAO_USD=<price>` to pin it on a test network. A transfer from a wallet nobody linked is kept as
  `unmatched` and credited the moment that wallet is linked, or assigned by the operator with
  `POST /v1/admin/deposits/{id}/assign`. A fresh database starts at the current block; `RACKSDOCK_TAO_START_BLOCK` scans
  history instead.
- **Other coins** through NOWPayments' hosted checkout. Set `RACKSDOCK_NOWPAYMENTS_API_KEY` and
  `RACKSDOCK_NOWPAYMENTS_IPN_SECRET` from its dashboard (Settings > Payments > IPN), and `RACKSDOCK_NOWPAYMENTS_SANDBOX=1`
  to use its sandbox. Its IPN calls `{RACKSDOCK_BASE_URL}/v1/billing/webhooks/nowpayments`; the signature is checked and the
  payment is re-read from the NOWPayments API before anything is credited. A partial payment credits the share that arrived.
- **Free credits** (`POST /v1/billing/credits/purchase`, or method `free`): on everywhere except mainnet;
  `RACKSDOCK_FREE_CREDITS=0|1` overrides.

`RACKSDOCK_MIN_DEPOSIT_CENTS` (default 1000) is the minimum for TAO and crypto. `GET /v1/admin/deposits?status=unmatched`
lists transfers waiting for an owner and `GET /v1/admin/payments` shows what is enabled and the watcher's last block.
