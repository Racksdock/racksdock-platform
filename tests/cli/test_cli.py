import asyncio
import json
import socket
import threading

import pytest
import uvicorn
from racksdock_node.host_agent.agent import HostAgent
from racksdock_node.host_agent.config import HostAgentConfig
from racksdock_subnet.core.schemas import VmResources
from racksdock_subnet.miner.neuron import MinerConfig, MinerNeuron
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.devca import DevCA
from typer.testing import CliRunner

from racksdock_platform.marketplace.app import create_app
from racksdock_platform.marketplace.config import Settings

runner = CliRunner()


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Stack:
    """Marketplace + sim host-agent + miner registration running in a background event loop thread (the CLI is synchronous)."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.ca = DevCA.create(tmp_path / "ca")
        self.trust = TrustConfig(mode="dev", dev_ca_dir=self.ca.dir)
        self.settings = Settings(db_url=f"sqlite:///{tmp_path}/m.db", chain_kind="mock", chain_file=tmp_path / "chain.json", platform_key_path=tmp_path / "platform.key", probe_timeout_s=1.0, trust=self.trust)
        self.app = create_app(self.settings, background=False)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.node_id = None

    def run(self, coro, timeout=120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def start(self):
        async def boot():
            self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="warning"))
            self.srv_task = asyncio.create_task(self.server.serve())
            while not self.server.started:
                await asyncio.sleep(0.05)
            node_dir = self.ca.issue_node(self.tmp / "node")
            hcfg = HostAgentConfig(mode="sim", platform_ws=f"ws://127.0.0.1:{self.port}/v1/hosts/ws", state_dir=self.tmp / "hstate", key_path=self.tmp / "hstate" / "host.seed",
                                   miner_hotkey="5Fminer", region="sim-1", vm_dir=self.tmp / "vms", agent_transport="tcp", agent_port=0, sim_node_dir=node_dir,
                                   resources=VmResources(gpu_pci=[f"0000:{17 + i:02x}:00.0" for i in range(8)], nvme_pci=["0000:a0:00.0"]), trust=self.trust)
            self.host = HostAgent(hcfg)
            self.host_task = asyncio.create_task(self.host.run())
            while self.host.key.public.hex not in self.app.state.hosts.by_key:
                await asyncio.sleep(0.1)
            miner = MinerNeuron(MinerConfig(platform_url=self.base, hotkey="5Fminer", chain_kind="mock", chain_file=self.tmp / "chain.json", host_seed_path=hcfg.key_path, state_dir=self.tmp / "mstate", poll_s=0.3))
            rec = await miner.bootstrap(timeout_s=90)
            await miner.close()
            while self.host.current is not None:
                await asyncio.sleep(0.1)
            return rec.node_id

        self.node_id = self.run(boot(), timeout=180)

    def qualify(self):
        """Mark the node qualified via a signed validator record (what racksdock-validator does)."""
        import httpx
        from racksdock_subnet.core.envelope import SignedEnvelope
        from racksdock_subnet.core.keys import SigningKey
        from racksdock_subnet.core.schemas import AttestationSummary, PerformanceSummary, QualificationRecord

        vkey = SigningKey.generate()
        with httpx.Client(base_url=self.base, timeout=30) as c:
            r = c.post("/v1/validators/enroll", json={"envelope": SignedEnvelope.create("validator_enroll", {"hotkey": "5Gval"}, vkey).model_dump()})
            vh = {"X-API-KEY": r.json()["api_key"]}
            nodes = c.get("/v1/validators/nodes", headers=vh).json()["nodes"]
            gpu_ids = nodes[0]["registration"]["gpu_ids"]
            q = QualificationRecord(node_id=self.node_id, validator_hotkey="5Gval", validator_key=vkey.public.hex, instance_id="x", verify_image="verify", started_at="t", finished_at="t",
                                    outcome="pass", attestation=AttestationSummary(gpu_ids=gpu_ids), performance=PerformanceSummary(perf=0.97))
            r = c.post("/v1/validators/records", json={"envelope": SignedEnvelope.create("qualification", q.model_dump(by_alias=True, mode="json"), vkey).model_dump()}, headers=vh)
            assert r.status_code == 201, r.text

    def stop(self):
        async def down():
            self.host.stop()
            self.host_task.cancel()
            self.server.should_exit = True
            await self.srv_task

        try:
            self.run(down(), timeout=30)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    monkeypatch.setenv("RACKSDOCK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RACKSDOCK_TRUST_MODE", "dev")
    import racksdock_platform.cli.config as cfgmod

    monkeypatch.setattr(cfgmod, "HOME", tmp_path / "home")
    s = Stack(tmp_path)
    s.start()
    monkeypatch.setenv("RACKSDOCK_DEV_CA_DIR", str(s.ca.dir))
    yield s
    s.stop()


def test_cli_full_user_flow(stack, tmp_path):
    from racksdock_platform.cli.main import app

    r = runner.invoke(app, ["auth", "signup", "cli@x.y", "password1", "--api-url", stack.base])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["ssh-keys", "generate", "--name", "lap", "--out", str(tmp_path / "idkey")])
    assert r.exit_code == 0, r.output and (tmp_path / "idkey.pub").exists()
    r = runner.invoke(app, ["nodes", "list"])
    assert r.exit_code == 0 and "(no nodes)" in r.output  # not qualified yet
    stack.qualify()
    r = runner.invoke(app, ["nodes", "list", "--available"])
    assert r.exit_code == 0 and stack.node_id[:24] in r.output
    r = runner.invoke(app, ["nodes", "verify", stack.node_id])
    assert r.exit_code == 0 and "VERIFIED" in r.output
    # credits via the dev purchase endpoint
    import httpx

    cfg = json.loads((tmp_path / "home" / "config.json").read_text())
    httpx.post(f"{stack.base}/v1/billing/credits/purchase", json={"cents": 20000}, headers={"X-API-KEY": cfg["api_key"]})
    r = runner.invoke(app, ["instances", "create", "--name", "cli-1", "--node", stack.node_id, "--os", "ubuntu24.04", "--env", "FOO=bar", "--docker", "vllm/vllm-openai:v0.1", "--port", "8000:8000", "--tag", "t"])
    assert r.exit_code == 0, r.output
    iid = r.output.strip().splitlines()[-1]
    assert (tmp_path / "home" / "instances" / f"{iid}.json").exists()
    # wait for active via the API
    import time

    for _ in range(200):
        info = httpx.get(f"{stack.base}/v1/instances/{iid}/info", headers={"X-API-KEY": cfg["api_key"]}).json()
        if info["status"] in ("active", "error"):
            break
        time.sleep(0.3)
    assert info["status"] == "active", info
    r = runner.invoke(app, ["instances", "list"])
    assert r.exit_code == 0 and iid in r.output and "active" in r.output
    r = runner.invoke(app, ["ssh", iid, "--check-only"])
    assert r.exit_code == 0, r.output
    assert "attestation: VERIFIED" in r.output and "[ok] tenant nonce is the one this client generated" in r.output
    assert "[ok] user-data hash matches what this client submitted" in r.output
    # tamper the local record: a different tenant nonce must fail closed
    p = tmp_path / "home" / "instances" / f"{iid}.json"
    rec = json.loads(p.read_text())
    rec["tenant_nonce"] = "00" * 32
    p.write_text(json.dumps(rec))
    r = runner.invoke(app, ["ssh", iid, "--check-only"])
    assert r.exit_code == 1 and "FAIL] tenant nonce" in r.output
    r = runner.invoke(app, ["instances", "delete", iid])
    assert r.exit_code == 0
    r = runner.invoke(app, ["instances", "create", "--schema"])
    assert r.exit_code == 0 and "tenant_nonce" in r.output


def test_auth_login_uses_the_api_url_given_on_the_command_line(tmp_path, monkeypatch):
    """`auth login --api-url` must connect to that URL, not to the one left in an older config file (seen on the 8x4090 box)."""
    import json

    import httpx
    from typer.testing import CliRunner

    from racksdock_platform.cli import config, main

    monkeypatch.setattr(config, "HOME", tmp_path)
    config.save({"api_url": "http://127.0.0.1:1", "api_key": "old"})
    seen = {}

    class FakeClient:
        def __init__(self, base_url="", headers=None, timeout=None):
            seen["base_url"] = base_url

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def request(self, method, path, **kw):
            return httpx.Response(200, json={"api_key": "new"}, request=httpx.Request(method, "http://x" + path))

    from racksdock_platform.cli import client as cli_client

    monkeypatch.setattr(cli_client.httpx, "Client", FakeClient)
    r = CliRunner().invoke(main.app, ["auth", "login", "--email", "a@b.c", "--password", "pw", "--api-url", "http://127.0.0.1:8000"])
    assert r.exit_code == 0, r.output
    assert seen["base_url"] == "http://127.0.0.1:8000"
    assert json.loads((tmp_path / "config.json").read_text()) == {"api_url": "http://127.0.0.1:8000", "api_key": "new"}
