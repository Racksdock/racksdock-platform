"""Dev trust mode: the platform creates and publishes the simulated CA; nodes and validators fetch from it over HTTP."""

from fastapi.testclient import TestClient
from racksdock_subnet.core.nonce import new_nonce_hex
from racksdock_subnet.verifier.collectors import SimulatedCollector
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.devca import DevCA
from racksdock_subnet.verifier.verifiers import SimVerifier

from racksdock_platform.marketplace.app import create_app
from racksdock_platform.marketplace.config import Settings


def test_platform_creates_and_serves_dev_ca_and_issues_nodes(tmp_path, monkeypatch):
    ca_dir = tmp_path / "platform-devca"
    settings = Settings(db_url=f"sqlite:///{tmp_path}/m.db", chain_kind="mock", chain_file=tmp_path / "chain.json", trust=TrustConfig(mode="dev", dev_ca_dir=ca_dir))
    app = create_app(settings, background=False)
    assert (ca_dir / "root.pem").exists()  # created at startup
    c = TestClient(app)
    pub = c.get("/v1/dev/devca").json()
    assert pub["root_pem"].startswith("-----BEGIN CERTIFICATE-----") and "measurements" in pub["rim"]
    # a validator-side copy made only from the public files verifies evidence from a platform-issued node
    vdir = tmp_path / "validator-devca"
    vdir.mkdir()
    (vdir / "root.pem").write_text(pub["root_pem"])
    (vdir / "device_ca.pem").write_text(pub["device_ca_pem"])
    import json

    (vdir / "rim.json").write_text(json.dumps(pub["rim"]))
    issued = c.post("/v1/dev/issue-node", json={"count": 8, "memory_mib": 81920}).json()
    assert len(issued["devices"]) == 8 and issued["inventory"]["gpus"][0]["memory_total_mib"] == 81920
    node_dir = tmp_path / "node"
    (node_dir / "devices").mkdir(parents=True)
    for d in issued["devices"]:
        (node_dir / "devices" / f"{d['index']}.key").write_text(d["key_pem"])
        (node_dir / "devices" / f"{d['index']}.pem").write_text(d["cert_pem"])
    (node_dir / "inventory.json").write_text(json.dumps(issued["inventory"]))
    n = new_nonce_hex()
    bundle = SimulatedCollector(DevCA(ca_dir), node_dir).collect(n)
    assert SimVerifier(DevCA(vdir)).verify(bundle, n).ok
    # the public copy holds no private keys
    assert not (vdir / "root.key").exists() and "PRIVATE" not in pub["root_pem"]
    # dev routes are absent in prod trust mode
    prod = create_app(Settings(db_url=f"sqlite:///{tmp_path}/p.db", chain_kind="mock", chain_file=tmp_path / "chain2.json", trust=TrustConfig(mode="prod", dev_ca_dir=None)), background=False)
    assert TestClient(prod).get("/v1/dev/devca").status_code == 404
