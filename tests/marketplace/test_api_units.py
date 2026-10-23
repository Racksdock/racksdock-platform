
import pytest
from fastapi.testclient import TestClient
from racksdock_subnet.core.canonical import hash_obj
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.identity import node_id_from_gpu_ids
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.schemas import (
    AttestationSummary,
    CanaryResult,
    ClaimedSpec,
    HostEvent,
    PerformanceSummary,
    QualificationRecord,
    RegistrationRecord,
    SpecMeasured,
)
from sqlalchemy import select

from racksdock_platform.marketplace.models import Instance, Lease, Node
from racksdock_platform.marketplace.services.billing import meter


def register_node(client, chain, host_key: SigningKey, gpu_ids, hotkey="5Fminer", region="sim-1"):
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(gpu_ids), gpu_ids=gpu_ids, miner_hotkey=hotkey, miner_service_key=host_key.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=120, memory_gb=1850, nvme_tb=28, network_gbps=25, region=region), host_image_version="host_v1")
    env = SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host_key)
    chain.register(hotkey)
    chain.commit(hotkey, hash_obj(rec.model_dump(by_alias=True)))
    r = client.post("/v1/miners/register", json={"envelope": env.model_dump()})
    return rec, r


def enroll_validator(client, vkey: SigningKey, hotkey="5Gval"):
    env = SignedEnvelope.create("validator_enroll", {"hotkey": hotkey}, vkey)
    r = client.post("/v1/validators/enroll", json={"envelope": env.model_dump()})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def qualify(client, vkey, api_key, rec, perf=0.97, outcome="pass", kind="full"):
    q = QualificationRecord(kind=kind, node_id=rec.node_id, validator_hotkey="5Gval", validator_key=vkey.public.hex, instance_id="inst_q", verify_image="verify_v1",
                            started_at="2026-10-20T00:00:00Z", finished_at="2026-10-20T02:00:00Z", outcome=outcome,
                            attestation=AttestationSummary(gpu_ids=rec.gpu_ids, measurements_hash="sha256:m"),
                            spec_measured=SpecMeasured(memory_gb=1850), performance=PerformanceSummary(ratios={"x": perf}, perf=perf), reference_set="ref_dev_v0")
    env = SignedEnvelope.create("qualification", q.model_dump(by_alias=True, mode="json"), vkey)
    return client.post("/v1/validators/records", json={"envelope": env.model_dump()}, headers={"X-API-KEY": api_key})


def test_signup_keys_templates_billing(app):
    c = TestClient(app)
    r = c.post("/v1/account/signup", json={"email": "a@b.c", "password": "password1"})
    assert r.status_code == 201
    key = r.json()["api_key"]
    h = {"X-API-KEY": key}
    assert c.get("/v1/account", headers=h).json()["email"] == "a@b.c"
    assert c.post("/v1/sshkeys/add", json={"name": "bad", "public_key": "not a key"}, headers=h).status_code == 400
    r = c.post("/v1/sshkeys/add", json={"name": "k1", "public_key": SigningKey.generate().public.ssh_public_line("c")}, headers=h)
    assert r.status_code == 201 and r.json()["is_default"]
    kid = r.json()["id"]
    assert c.get("/v1/sshkeys", headers=h).json()["ssh_keys"][0]["id"] == kid
    r = c.post("/v1/templates/save", json={"name": "t", "body": {"os": "ubuntu22.04", "tags": ["x"]}}, headers=h)
    assert r.status_code == 201
    assert c.get("/v1/templates", headers=h).json()["templates"][0]["body"]["os"] == "ubuntu22.04"
    assert c.post("/v1/billing/credits/purchase", json={"cents": 10000}, headers=h).json()["credits_cents"] == 10000
    assert c.get("/v1/instances", headers=h).json()["instances"] == []
    assert c.get("/v1/instances/types").json()["instance_types"][0]["availability"] == []
    assert c.get("/v1/account").status_code == 401


def test_registration_requires_chain_commitment_and_unique_gpu_ids(app):
    c = TestClient(app)
    chain = app.state.chain
    host = SigningKey.generate()
    gpu_ids = [f"sha256:{i:064x}" for i in range(8)]
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(gpu_ids), gpu_ids=gpu_ids, miner_hotkey="5Fm", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=1, memory_gb=1, nvme_tb=1, network_gbps=1, region="r"), host_image_version="h")
    env = SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host)
    r = c.post("/v1/miners/register", json={"envelope": env.model_dump()})
    assert r.status_code == 400 and "commitment" in r.text
    chain.register("5Fm")
    chain.commit("5Fm", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": env.model_dump()}).status_code == 201
    # another hotkey claims overlapping gpu ids later -> rejected
    host2 = SigningKey.generate()
    ids2 = gpu_ids[:4] + [f"sha256:{i:064x}" for i in range(100, 104)]
    rec2 = RegistrationRecord(node_id=node_id_from_gpu_ids(ids2), gpu_ids=ids2, miner_hotkey="5Fother", miner_service_key=host2.public.hex,
                              claimed_spec=ClaimedSpec(vcpus=1, memory_gb=1, nvme_tb=1, network_gbps=1, region="r"), host_image_version="h")
    chain.register("5Fother")
    chain.commit("5Fother", hash_obj(rec2.model_dump(by_alias=True)))
    r = c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec2.model_dump(by_alias=True), host2).model_dump()})
    assert r.status_code == 400 and "already registered" in r.text
    # wrong signer
    bad = SignedEnvelope.create("registration", rec.model_dump(by_alias=True), SigningKey.generate())
    assert c.post("/v1/miners/register", json={"envelope": bad.model_dump()}).status_code == 400


def test_qualification_makes_node_listed_and_lease_is_exclusive(app):
    c = TestClient(app)
    host = SigningKey.generate()
    gpu_ids = [f"sha256:{i:064x}" for i in range(8)]
    rec, r = register_node(c, app.state.chain, host, gpu_ids)
    assert r.status_code == 201
    vkey = SigningKey.generate()
    vk = enroll_validator(c, vkey)
    # wrong gpu ids in qualification are rejected
    bad = rec.model_copy(update={"gpu_ids": [f"sha256:{i:064x}" for i in range(50, 58)]})
    assert qualify(c, vkey, vk, bad).status_code == 400
    assert qualify(c, vkey, vk, rec).status_code == 201
    with app.state.db.session() as s:
        n = s.get(Node, rec.node_id)
        assert n.status == "qualified" and n.perf == 0.97 and n.needs_readiness is False
        n.host_connected = True  # pretend the host is online
    types = c.get("/v1/instances/types").json()["instance_types"][0]
    assert len(types["availability"]) == 1 and types["availability"][0]["available"] is True
    assert "hardware_verified" in types["availability"][0]["badges"] and "not_confidential" in types["availability"][0]["badges"]
    ver = c.get(f"/v1/nodes/{rec.node_id}/verification").json()
    assert ver["status"] == "qualified" and ver["registration"]["gpu_ids"] == gpu_ids and "miner_hotkey" not in ver["registration"]
    # user creates instance; no host connected -> provider_unreachable, lease released
    r = c.post("/v1/account/signup", json={"email": "u@x.y", "password": "password1"})
    h = {"X-API-KEY": r.json()["api_key"]}
    c.post("/v1/sshkeys/add", json={"name": "k", "public_key": SigningKey.generate().public.ssh_public_line("c")}, headers=h)
    assert c.post("/v1/instances/create", json={"name": "i1", "node_id": rec.node_id}, headers=h).status_code == 402  # no credits
    c.post("/v1/billing/credits/purchase", json={"cents": 5000}, headers=h)
    r = c.post("/v1/instances/create", json={"name": "i1", "node_id": rec.node_id}, headers=h)
    assert r.status_code == 201
    iid = r.json()["id"]
    info = c.get(f"/v1/instances/{iid}/info", headers=h).json()
    assert info["status"] == "error" and info["status_details"] == "provider_unreachable"
    with app.state.db.session() as s:
        assert s.query(Lease).filter(Lease.released_at.is_(None)).count() == 0
    # quarantine on a failed re-qualification
    assert qualify(c, vkey, vk, rec, perf=0.5, outcome="quarantined", kind="full").status_code == 201
    with app.state.db.session() as s:
        assert s.get(Node, rec.node_id).status == "quarantined"
    assert c.get("/v1/instances/types").json()["instance_types"][0]["availability"] == []
    # canary fail adds a strike
    can = CanaryResult(node_id=rec.node_id, validator_key=vkey.public.hex, started_at="t", finished_at="t", outcome="fail", gpu_ids_match=True, reason="timeout")
    r = c.post("/v1/validators/records", json={"envelope": SignedEnvelope.create("canary", can.model_dump(by_alias=True, mode="json"), vkey).model_dump()}, headers={"X-API-KEY": vk})
    assert r.status_code == 201
    assert c.get(f"/v1/nodes/{rec.node_id}/verification").json()["strikes"][0]["reason"] == "canary_fail"


def test_metering_credits_degraded_minutes(app):
    import datetime as dt

    with app.state.db.session() as s:
        from racksdock_platform.marketplace.models import User

        u = User(email="m@x.y", credits_cents=10000)
        s.add(u)
        s.flush()
        inst = Instance(user_id=u.id, name="i", node_id=None, os="ubuntu24.04", image_version="v", status="active", tenant_nonce="00" * 32, user_data="", user_data_hash="h",
                        request={}, hourly_price_cents=6000, active_at="2026-10-20T00:00:00Z", last_metered_at="2026-10-20T00:00:00Z",
                        auto_delete={"spend_threshold": "1.50"})
        s.add(inst)
        s.flush()
        iid, uid = inst.id, u.id
    with app.state.db.session() as s:
        actions = meter(s, now=dt.datetime(2026, 10, 20, 0, 1, 30, tzinfo=dt.UTC))
        inst = s.get(Instance, iid)
        assert inst.billed_minutes == 1 and inst.cost_cents == 100 and actions == []
        inst.status = "degraded"
    with app.state.db.session() as s:
        meter(s, now=dt.datetime(2026, 10, 20, 0, 3, 0, tzinfo=dt.UTC))
        inst = s.get(Instance, iid)
        assert inst.degraded_minutes == 2 and inst.cost_cents == 100  # credited
        inst.status = "active"
    with app.state.db.session() as s:
        actions = meter(s, now=dt.datetime(2026, 10, 20, 0, 4, 0, tzinfo=dt.UTC))
        inst = s.get(Instance, iid)
        assert inst.cost_cents == 200 and actions == [(iid, "auto_delete")]
        from racksdock_platform.marketplace.models import User

        assert s.get(User, uid).credits_cents == 10000 - 200


async def test_a_delete_that_found_the_host_offline_is_sent_when_the_host_is_back(app):
    """delete() while the host-agent is disconnected leaves the instance `deleting` with a pending marker; the host still runs the
    server until it hears the teardown, so the timeout tick sends it as soon as the host reconnects (seen after a credit cutoff
    landed during a marketplace restart)."""
    from racksdock_platform.marketplace.models import CommandLog
    from racksdock_platform.marketplace.services.orchestrator import TEARDOWN_PENDING

    c = TestClient(app)
    host = SigningKey.generate()
    rec, r = register_node(c, app.state.chain, host, [f"sha256:{i:064x}" for i in range(8)])
    assert r.status_code == 201
    with app.state.db.session() as s:
        s.add(Instance(id="inst_cut", user_id=None, name="cut", node_id=rec.node_id, mode="tenant", os="ubuntu24.04", image_version="ubuntu24.04_v1", tenant_nonce="00" * 32,
                       user_data="", user_data_hash="sha256:x", request={}, status="deleting", status_details=TEARDOWN_PENDING, deleting_at="2026-10-23T12:56:24Z"))
    orch = app.state.orchestrator
    await orch.tick_timeouts()  # host still offline: nothing to send
    with app.state.db.session() as s:
        assert s.get(Instance, "inst_cut").status_details == TEARDOWN_PENDING

    class Sess:  # the host is back
        node_id, host_pubkey, sent = rec.node_id, host.public.hex, []

        async def send(self, cmd):
            self.sent.append(cmd)

    sess = Sess()
    app.state.hosts.by_node[rec.node_id] = sess
    await orch.tick_timeouts()
    await orch.tick_timeouts()
    assert [(cmd.kind, cmd.instance_id) for cmd in sess.sent] == [("teardown", "inst_cut")]  # once, not every tick
    with app.state.db.session() as s:
        assert s.get(Instance, "inst_cut").status_details == "teardown sent after the host reconnected"
        assert s.scalar(select(CommandLog).where(CommandLog.instance_id == "inst_cut")).kind == "teardown"
    app.state.hosts.by_node.pop(rec.node_id)


def test_cors_for_a_console_on_another_origin(tmp_path, ca):
    """A static console (Vercel) calls the API from its own origin with the session cookie: the platform answers with CORS headers
    for the configured origins only, and only when origins are configured."""
    from racksdock_subnet.verifier.config import TrustConfig

    from racksdock_platform.marketplace.app import create_app
    from racksdock_platform.marketplace.config import Settings

    st = Settings(db_url=f"sqlite:///{tmp_path}/c.db", chain_kind="mock", chain_file=tmp_path / "chain.json", platform_key_path=tmp_path / "platform.key",
                  trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir), cors_origins=["https://console.example.com"], cors_origin_regex=r"https://.*\.vercel\.app")
    c = TestClient(create_app(st, background=False))
    for origin in ("https://console.example.com", "https://racksdock-abc.vercel.app"):
        r = c.options("/v1/nodes", headers={"Origin": origin, "Access-Control-Request-Method": "GET"})
        assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin and r.headers["access-control-allow-credentials"] == "true"
    r = c.get("/v1/nodes", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    plain = TestClient(create_app(Settings(db_url=f"sqlite:///{tmp_path}/p.db", chain_kind="mock", chain_file=tmp_path / "chain2.json", platform_key_path=tmp_path / "platform.key",
                                           trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir)), background=False))
    assert "access-control-allow-origin" not in plain.get("/v1/nodes", headers={"Origin": "https://console.example.com"}).headers


async def test_heartbeat_without_vm_reconciles_stuck_instances(app):
    """A host-agent restarted mid-flight destroys leftover VMs and reports vm_state none. The platform must then close an instance
    stuck in `deleting` (releasing the node's lease) and fail a launch that never produced a VM, instead of leaving the node
    unrentable (seen after the 8x4090 test run exited during a teardown)."""
    import datetime as dt

    from racksdock_subnet.core.schemas import HostEvent, HostHeartbeat

    c = TestClient(app)
    host = SigningKey.generate()
    gpu_ids = [f"sha256:{i:064x}" for i in range(8)]
    rec, r = register_node(c, app.state.chain, host, gpu_ids)
    assert r.status_code == 201
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    r = c.post("/v1/account/signup", json={"email": "hb@x.y", "password": "password1"})
    uid = c.get("/v1/account", headers={"X-API-KEY": r.json()["api_key"]}).json()["user_id"]
    with app.state.db.session() as s:
        s.add(Instance(id="inst_stuck", user_id=uid, name="stuck", node_id=rec.node_id, mode="tenant", os="ubuntu24.04", image_version="ubuntu24.04_v1",
                       tenant_nonce="00" * 32, user_data="#cloud-config\n{}\n", user_data_hash="sha256:x", request={}, status="deleting", deleting_at=old, created_at=old))
        s.add(Lease(instance_id="inst_stuck", node_id=rec.node_id, tenant_nonce="00" * 32, user_data_hash="sha256:x"))
        s.add(Instance(id="inst_lost", user_id=uid, name="lost", node_id=rec.node_id, mode="verify", os="verify", image_version="verify_v1",
                       tenant_nonce="11" * 32, user_data="#cloud-config\n{}\n", user_data_hash="sha256:y", request={}, status="pending_provider", provider_at=old, created_at=old))
        s.add(Instance(id="inst_fresh", user_id=uid, name="fresh", node_id=rec.node_id, mode="verify", os="verify", image_version="verify_v1",
                       tenant_nonce="22" * 32, user_data="#cloud-config\n{}\n", user_data_hash="sha256:z", request={}, status="pending_provider"))
    hb = HostHeartbeat(node_id=rec.node_id, host_pubkey=host.public.hex, host_image_version="host_v1", vm_state=None, instance_id=None)
    await app.state.orchestrator._on_heartbeat(rec.node_id, HostEvent(kind="heartbeat", node_id=rec.node_id, payload=hb.model_dump()))
    with app.state.db.session() as s:
        assert s.get(Instance, "inst_stuck").status == "deleted"
        assert s.query(Lease).filter(Lease.released_at.is_(None)).count() == 0
        assert s.get(Instance, "inst_lost").status == "error"
        assert s.get(Instance, "inst_fresh").status == "pending_provider"  # a launch in progress is left alone
        assert s.get(Node, rec.node_id).needs_readiness is True


def test_one_platform_serves_several_hardware_profiles(app):
    """Nodes of different profiles (8xB300 default, 1xA100 lab, 8x4090 lab) register with one platform; the listing shows one
    instance type per profile, each node carries its deployment tier, and 'auto' allocation can be narrowed by type and tier."""
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, RegistrationRecord

    from racksdock_platform.marketplace.services.allocation import pick_node

    c = TestClient(app)
    vkey = SigningKey.generate()
    vk = enroll_validator(c, vkey)

    def reg(prefix, count, profile, deployment, hotkey):
        ids = [f"sha256:{prefix}{i:063x}" for i in range(count)]
        host = SigningKey.generate()
        rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=count), gpu_ids=ids, profile=profile, deployment=deployment, miner_hotkey=hotkey,
                                 miner_service_key=host.public.hex, claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
        env = SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host)
        app.state.chain.register(hotkey)
        app.state.chain.commit(hotkey, hash_obj(rec.model_dump(by_alias=True)))
        r = c.post("/v1/miners/register", json={"envelope": env.model_dump()})
        assert r.status_code == 201, r.text
        return rec

    b300 = reg("a", 8, "b300x8", "vm", "5Fb300")
    a100 = reg("b", 1, "a100-dev", "static", "5Fa100")
    r4090 = reg("c", 8, "rtx4090x8-dev", "container", "5F4090")
    # wrong gpu count for the declared profile is refused
    ids = [f"sha256:d{i:063x}" for i in range(2)]
    host = SigningKey.generate()
    bad = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=2), gpu_ids=ids, profile="a100-dev", miner_hotkey="5Fbad", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Fbad")
    app.state.chain.commit("5Fbad", hash_obj(bad.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", bad.model_dump(by_alias=True), host).model_dump()}).status_code == 400
    for rec in (b300, a100, r4090):
        assert qualify(c, vkey, vk, rec).status_code == 201, rec.profile
    with app.state.db.session() as s:
        for rec in (b300, a100, r4090):
            s.get(Node, rec.node_id).host_connected = True
    types = {t["profile"]: t for t in c.get("/v1/instances/types").json()["instance_types"]}
    assert set(types) == {"b300x8", "a100-dev", "rtx4090x8-dev"}
    assert types["b300x8"]["num_gpus"] == 8 and types["a100-dev"]["num_gpus"] == 1 and types["a100-dev"]["deployments"] == ["static"]
    assert types["rtx4090x8-dev"]["availability"][0]["deployment"] == "container" and "container" in types["rtx4090x8-dev"]["availability"][0]["badges"]
    assert "vm_isolated" in types["b300x8"]["availability"][0]["badges"]
    ver = c.get(f"/v1/nodes/{a100.node_id}/verification").json()
    assert ver["profile"] == "a100-dev" and ver["instance_type"] == "A100x1-DEV" and ver["deployment"] == "static"
    with app.state.db.session() as s:
        assert pick_node(s, None, "ubuntu24.04", "i1", instance_type="A100x1-DEV").node_id == a100.node_id
        assert pick_node(s, None, "ubuntu24.04", "i2", instance_type="rtx4090x8-dev").node_id == r4090.node_id
        assert pick_node(s, None, "ubuntu24.04", "i3", deployment="vm").node_id == b300.node_id
        assert pick_node(s, None, "ubuntu24.04", "i4", instance_type="B300x8", deployment="container") is None


def test_dual_node_offers_both_modes_and_the_launch_carries_the_choice(app):
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, CreateInstanceRequest, RegistrationRecord

    from racksdock_platform.marketplace.services.allocation import pick_node

    c = TestClient(app)
    vkey = SigningKey.generate()
    vk = enroll_validator(c, vkey)
    ids = [f"sha256:e{i:063x}" for i in range(8)]
    host = SigningKey.generate()
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="dual", miner_hotkey="5Fdual", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Fdual")
    app.state.chain.commit("5Fdual", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    assert qualify(c, vkey, vk, rec).status_code == 201
    with app.state.db.session() as s:
        s.get(Node, rec.node_id).host_connected = True
    t = c.get("/v1/instances/types").json()["instance_types"][0]
    assert t["deployments"] == ["container", "vm"] and {"vm_isolated", "container"} <= set(t["availability"][0]["badges"])
    with app.state.db.session() as s:
        assert pick_node(s, None, "ubuntu24.04", "i1", deployment="container").node_id == rec.node_id
        assert pick_node(s, None, "ubuntu24.04", "i2", deployment="vm").node_id == rec.node_id
    # the chosen mode is stored on the instance (the host is not connected here, so the launch itself errors out)
    r = c.post("/v1/account/signup", json={"email": "d@x.y", "password": "password1"})
    h = {"X-API-KEY": r.json()["api_key"]}
    c.post("/v1/sshkeys/add", json={"name": "k", "public_key": SigningKey.generate().public.ssh_public_line("c")}, headers=h)
    c.post("/v1/billing/credits/purchase", json={"cents": 5000}, headers=h)
    orch = app.state.orchestrator
    inst = orch.create_instance(CreateInstanceRequest(name="dock", node_id=rec.node_id, deployment="container", ssh_key_ids=[]), user_id=r.json()["user_id"], ssh_public_keys=["ssh-ed25519 AAAA x"])
    with app.state.db.session() as s:
        assert s.get(Instance, inst.id).deployment == "container"
    import asyncio

    asyncio.run(orch._fail(inst.id, "test_done", release=True))  # free the node (one rental at a time)
    inst2 = orch.create_instance(CreateInstanceRequest(name="vm", node_id=rec.node_id, ssh_key_ids=[]), user_id=r.json()["user_id"], ssh_public_keys=["ssh-ed25519 AAAA x"])
    with app.state.db.session() as s:
        assert s.get(Instance, inst2.id).deployment == "vm"  # default for a dual node
    import pytest as _pt

    with _pt.raises(ValueError):
        orch.create_instance(CreateInstanceRequest(name="bad", node_id=rec.node_id, deployment="static", ssh_key_ids=[]), user_id=r.json()["user_id"], ssh_public_keys=["k"])


@pytest.mark.asyncio
async def test_identity_reuse_updates_a_registered_node_but_never_creates_one(app):
    """A host re-signing its proven identity (reuse, no evidence) is accepted as a draft only for the node it already registered
    with the same host key and the same gpu_ids."""
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, HostEvent, RegistrationRecord

    from racksdock_platform.marketplace.models import EvidenceRecord
    from racksdock_platform.marketplace.services.hosts import HostSession

    c = TestClient(app)
    ids = [f"sha256:f{i:063x}" for i in range(8)]
    host = SigningKey.generate()
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="vm", miner_hotkey="5Freuse", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Freuse")
    app.state.chain.commit("5Freuse", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    orch = app.state.orchestrator
    sess = HostSession(ws=None, host_pubkey=host.public.hex, node_id=rec.node_id, hello={})

    def drafts(signer=None):
        with app.state.db.session() as s:
            rows = s.query(EvidenceRecord).filter(EvidenceRecord.signer == (signer or host.public.hex), EvidenceRecord.kind.like("identity_draft%")).order_by(EvidenceRecord.id).all()
            return [(r.kind, r.envelope["payload"].get("errors"), r.envelope["payload"].get("command_id")) for r in rows]

    dual = rec.model_copy(update={"deployment": "dual"})
    await orch.on_host_event(sess, HostEvent(kind="identity", command_id="c1", node_id=rec.node_id, payload={"registration": dual.model_dump(by_alias=True), "evidence": None, "reused": True}))
    assert drafts()[-1] == ("identity_draft", [], "c1")
    # different gpu_ids: rejected
    other = rec.model_copy(update={"gpu_ids": [*ids[:7], "sha256:" + "e" * 64]})
    await orch.on_host_event(sess, HostEvent(kind="identity", command_id="c2", node_id=rec.node_id, payload={"registration": other.model_dump(by_alias=True), "evidence": None, "reused": True}))
    assert drafts()[-1][0] == "identity_draft_rejected" and "different gpu_ids" in drafts()[-1][1][0]
    # another host key: rejected
    sess2 = HostSession(ws=None, host_pubkey=SigningKey.generate().public.hex, node_id=None, hello={})
    await orch.on_host_event(sess2, HostEvent(kind="identity", command_id="c3", node_id=rec.node_id, payload={"registration": dual.model_dump(by_alias=True), "evidence": None, "reused": True}))
    rej = [d for d in drafts(sess2.host_pubkey) if d[2] == "c3"]
    assert rej and rej[-1][0] == "identity_draft_rejected" and "different host key" in rej[-1][1][0]
    assert [d for d in drafts() if d[2] == "c1"][-1][0] == "identity_draft"  # the accepted draft under our key is untouched
    # unknown node: rejected
    unknown = rec.model_copy(update={"node_id": "sha256:" + "d" * 64, "gpu_ids": [f"sha256:d{i:063x}" for i in range(8)]})
    await orch.on_host_event(sess, HostEvent(kind="identity", command_id="c4", node_id=unknown.node_id, payload={"registration": unknown.model_dump(by_alias=True), "evidence": None, "reused": True}))
    assert drafts()[-1][0] == "identity_draft_rejected" and "unregistered" in drafts()[-1][1][0]


def test_admission_pass_admits_but_does_not_list_and_keeps_the_measured_spec(app):
    """An admission record alone never lists a node (status admitted, badge verifying); a re-registered node keeps the spec its
    last full qualification measured; the next full pass lists it again."""
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, RegistrationRecord

    from racksdock_platform.marketplace.services.catalog import node_badges, node_spec_summary

    c = TestClient(app)
    vkey = SigningKey.generate()
    vk = enroll_validator(c, vkey)
    ids = [f"sha256:a{i:063x}" for i in range(8)]
    host = SigningKey.generate()
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="vm", miner_hotkey="5Fadmit", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Fadmit")
    app.state.chain.commit("5Fadmit", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    assert qualify(c, vkey, vk, rec, kind="admission").status_code == 201
    with app.state.db.session() as s:
        n = s.get(Node, rec.node_id)
        assert n.status == "admitted" and "verifying" in node_badges(n) and "performance_qualified" not in node_badges(n)
        assert n.qualification["admission"]["outcome"] == "pass" and n.qualification.get("spec_measured") is None
    assert all(a["node_id"] != rec.node_id for t in c.get("/v1/instances/types").json()["instance_types"] for a in t["availability"])
    assert qualify(c, vkey, vk, rec).status_code == 201  # full
    with app.state.db.session() as s:
        n = s.get(Node, rec.node_id)
        assert n.status == "qualified" and n.qualification["kind"] == "full" and n.qualification["admission"]["outcome"] == "pass"
        measured = node_spec_summary(n)
    # re-registration (e.g. switching to dual): back to registered, admission passes again -> admitted, spec still the measured one
    dual = rec.model_copy(update={"deployment": "dual"})
    app.state.chain.commit("5Fadmit", hash_obj(dual.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", dual.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    assert qualify(c, vkey, vk, dual, kind="admission").status_code == 201
    with app.state.db.session() as s:
        n = s.get(Node, rec.node_id)
        assert n.status == "admitted" and node_spec_summary(n) == measured and n.qualification["kind"] == "full"


@pytest.mark.asyncio
async def test_failed_launch_reason_survives_the_cleanup(app):
    """A host's 'deleted' status for an errored instance keeps the launch error in status_details."""
    from racksdock_subnet.core.schemas import HostEvent, InstanceStatus, StatusUpdate

    from racksdock_platform.marketplace.services.hosts import HostSession

    with app.state.db.session() as s:
        inst = Instance(name="x", node_id=None, mode="tenant", os="ubuntu24.04", image_version="v", tenant_nonce="ab" * 32, user_data="#cloud-config\n",
                        user_data_hash="sha256:" + "0" * 64, request={}, status="error", status_details="launch failed: docker run -d failed (127): boom")
        s.add(inst)
        s.flush()
        iid = inst.id
    sess = HostSession(ws=None, host_pubkey="aa" * 32, node_id=None, hello={})
    upd = StatusUpdate(status=InstanceStatus.deleted, status_details="no VM present")
    await app.state.orchestrator.on_host_event(sess, HostEvent(kind="status", instance_id=iid, payload=upd.model_dump(mode="json")))
    with app.state.db.session() as s:
        i = s.get(Instance, iid)
        assert i.status == "deleted" and i.status_details.startswith("launch failed: docker run -d failed (127): boom") and "no VM present" in i.status_details


@pytest.mark.asyncio
async def test_catalog_templates_from_a_signed_release_and_per_node_availability(app, tmp_path):
    """Templates come from the signed release manifest; a node offers only the templates it reports holding, per tier, and a launch
    of a template the node lacks is refused."""
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.manifest import CatalogManifest, ImageEntry, sign_manifest, template_meta
    from racksdock_subnet.core.schemas import ClaimedSpec, CreateInstanceRequest, HostEvent, HostHeartbeat, RegistrationRecord

    from racksdock_platform.marketplace.services.catalog import Catalog, images_by_mode, node_os_options
    from racksdock_platform.marketplace.services.hosts import HostSession

    key = SigningKey.generate()
    rel = "v2026.10.9"
    entries = []
    for os_name, kinds in (("ubuntu24.04", ("vm", "container")), ("pytorch-2.9-cuda13.0", ("vm", "container")), ("verify", ("vm", "container"))):
        for k in kinds:
            entries.append(ImageEntry(os=os_name, version=f"{os_name}_{rel}", file=f"{os_name}_{rel}.qcow2" if k == "vm" else f"racksdock/{os_name}:{rel}", sha256="0" * 64,
                                      size_bytes=10, kind=k, validator_only=(os_name == "verify"), **template_meta(os_name)))
    env = sign_manifest(CatalogManifest(release=rel, images=entries), key)
    d = tmp_path / "rel"
    d.mkdir()
    (d / "manifest.signed.json").write_text(env.model_dump_json())
    (d / "catalog.pub").write_text(key.public.hex + "\n")
    cat = Catalog(d / "manifest.signed.json")
    assert cat.release == rel and [t["os"] for t in cat.templates] == ["pytorch-2.9-cuda13.0", "ubuntu24.04", "verify"]
    assert cat.templates[0]["label"] == "PyTorch 2.9, CUDA 13.0, Ubuntu 24.04" and cat.templates[0]["kinds"] == ["vm", "container"]
    assert cat.os_options() == ["pytorch-2.9-cuda13.0", "ubuntu24.04"] and cat.version_for("verify") == f"verify_{rel}"
    # a dual node that holds the ubuntu template in both tiers and pytorch only as a container
    n = Node(node_id="n1", miner_hotkey="h", miner_service_key="k", provider_name="p", region="lab", registration={}, registration_hash="x", registered_block=1,
             deployment="dual", images=[f"ubuntu24.04_{rel}:vm", f"ubuntu24.04_{rel}:container", f"pytorch-2.9-cuda13.0_{rel}:container", f"verify_{rel}:vm"])
    assert images_by_mode(n, cat) == {"container": ["pytorch-2.9-cuda13.0", "ubuntu24.04"], "vm": ["ubuntu24.04"]}
    assert node_os_options(n, cat) == ["pytorch-2.9-cuda13.0", "ubuntu24.04"]
    n.images = None
    assert images_by_mode(n, cat) == {} and node_os_options(n, cat) == cat.os_options()  # unknown inventory: whole catalog
    # live: a node's heartbeat records its images; launching a template it lacks in the chosen tier fails with image_not_on_node
    app.state.catalog = cat
    app.state.orchestrator.catalog = cat
    c = TestClient(app)
    vkey = SigningKey.generate()
    vk = enroll_validator(c, vkey)
    ids = [f"sha256:c{i:063x}" for i in range(8)]
    host = SigningKey.generate()
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="dual", miner_hotkey="5Fcat", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Fcat")
    app.state.chain.commit("5Fcat", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    assert qualify(c, vkey, vk, rec).status_code == 201
    sess = HostSession(ws=None, host_pubkey=host.public.hex, node_id=rec.node_id, hello={})
    hb = HostHeartbeat(node_id=rec.node_id, host_pubkey=host.public.hex, host_image_version="h", images=[f"ubuntu24.04_{rel}:vm", f"pytorch-2.9-cuda13.0_{rel}:container"])
    await app.state.orchestrator.on_host_event(sess, HostEvent(kind="heartbeat", node_id=rec.node_id, payload=hb.model_dump(mode="json")))
    with app.state.db.session() as s:
        assert s.get(Node, rec.node_id).images == sorted(hb.images)
        s.get(Node, rec.node_id).host_connected = True
    t = c.get("/v1/instances/types").json()["instance_types"][0]
    assert t["availability"][0]["images_by_mode"] == {"container": ["pytorch-2.9-cuda13.0"], "vm": ["ubuntu24.04"]} and t["availability"][0]["os_options"] == ["pytorch-2.9-cuda13.0", "ubuntu24.04"]
    assert [x["os"] for x in c.get("/v1/catalog").json()["templates"]] == ["pytorch-2.9-cuda13.0", "ubuntu24.04"]
    r = c.post("/v1/account/signup", json={"email": "cat@x.y", "password": "password1"})
    uid, h = r.json()["user_id"], {"X-API-KEY": r.json()["api_key"]}
    c.post("/v1/billing/credits/purchase", json={"cents": 5000}, headers=h)
    orch = app.state.orchestrator
    with pytest.raises(ValueError, match="image_not_on_node"):
        orch.create_instance(CreateInstanceRequest(name="pt-vm", node_id=rec.node_id, os="pytorch-2.9-cuda13.0", deployment="vm", ssh_key_ids=[]), user_id=uid, ssh_public_keys=["k"])
    inst = orch.create_instance(CreateInstanceRequest(name="pt-docker", node_id=rec.node_id, os="pytorch-2.9-cuda13.0", deployment="container", ssh_key_ids=[]), user_id=uid, ssh_public_keys=["k"])
    with app.state.db.session() as s:
        assert s.get(Instance, inst.id).deployment == "container"


def test_scoring_snapshot_is_signed_and_scores_like_the_verifying_validator(app):
    """A validator gets a platform-signed snapshot with the same inputs the verifier builds, and the shared
    scorer yields weights for the qualified miner; the hysteresis set comes from the last published scoring record."""
    from racksdock_subnet.core.envelope import SignedEnvelope as Env
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, RegistrationRecord
    from racksdock_subnet.validator.validator import score_snapshot

    c = TestClient(app)
    vkey = SigningKey.generate()
    vk = enroll_validator(c, vkey)
    ids = [f"sha256:b{i:063x}" for i in range(8)]
    host = SigningKey.generate()
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="vm", miner_hotkey="5Flight", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Flight")
    app.state.chain.commit("5Flight", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": Env.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    assert qualify(c, vkey, vk, rec).status_code == 201
    # a passed canary makes the node "available" for the window
    from racksdock_subnet.core.envelope import utcnow_iso

    canary = {"schema": "racksdock.canary.v1", "node_id": rec.node_id, "validator_key": vkey.public.hex, "started_at": utcnow_iso(), "finished_at": utcnow_iso(),
              "outcome": "pass", "gpu_ids_match": True, "time_to_active_s": 30.0, "instance_id": "inst_c"}
    r = c.post("/v1/validators/records", json={"envelope": Env.create("canary", canary, vkey).model_dump()}, headers={"X-API-KEY": vk})
    assert r.status_code in (200, 201), r.text
    pub = c.get("/v1/platform/key").json()["platform_key"]
    snap = c.get("/v1/scoring/snapshot", params={"tempo_s": 300}).json()
    env = Env.model_validate(snap["envelope"])
    assert env.kind == "scoring_snapshot" and env.verify(expected_signer=pub) and snap["platform_key"] == pub
    p = env.payload
    assert p["tempo_s"] == 300 and "funded_slots" not in p and "previously_funded" not in p
    node = next(i for i in p["inputs"] if i["node_id"] == rec.node_id)
    assert node["hotkey"] == "5Flight" and node["qualification_outcome"] == "pass" and node["last_attestation_ok"] is True and node["deployment"] == "vm"
    res = score_snapshot(p)
    assert res.hotkey_weights.get("5Flight", 0) > 0 and rec.node_id in res.scored
    # a verifier publishes its scoring record; the snapshot it gets next is unaffected by it
    scoring = {"window_start": p["window_start"], "window_end": p["window_end"], "scores": res.scores, "scored": res.scored, "hotkey_weights": res.hotkey_weights, "burn": res.burn, "details": {}, "weights": {}}
    r = c.post("/v1/validators/records", json={"envelope": Env.create("scoring", scoring, vkey).model_dump()}, headers={"X-API-KEY": vk})
    assert r.status_code in (200, 201), r.text
    p2 = Env.model_validate(c.get("/v1/scoring/snapshot").json()["envelope"]).payload
    assert p2["window_end"] == p["window_end"] and "previously_funded" not in p2
    # a bad signature is refused by the validator's check
    tampered = Env.model_validate(snap["envelope"])
    tampered.payload["utilization_7d"] = 0.99
    assert not tampered.verify(expected_signer=pub)


def test_evidence_endpoint_can_return_the_newest_records(app):
    """Scoring windows need the newest records; the ascending default is for pagination from since_id."""
    from racksdock_subnet.core.schemas import HostEvent

    from racksdock_platform.marketplace.services.records import store_envelope

    with app.state.db.session() as s:
        for i in range(5):
            store_envelope(s, SignedEnvelope.create("validator_note", {"i": i}, SigningKey.generate()), "validator_note")
    c = TestClient(app)
    asc = [r["envelope"]["payload"]["i"] for r in c.get("/v1/evidence", params={"kind": "validator_note", "desc": "false", "limit": 3}).json()["records"]]
    desc = [r["envelope"]["payload"]["i"] for r in c.get("/v1/evidence", params={"kind": "validator_note", "limit": 3, "desc": "true"}).json()["records"]]
    assert asc == [0, 1, 2] and desc == [4, 3, 2]
    assert HostEvent  # imported for symmetry with the other tests


def test_reregistration_can_move_a_node_to_a_new_hotkey(app):
    """The scoring snapshot and weight mapping use Node.miner_hotkey, so a re-registration under a new hotkey must update it."""
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, RegistrationRecord

    c = TestClient(app)
    ids = [f"sha256:9{i:063x}" for i in range(8)]
    host = SigningKey.generate()
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="vm", miner_hotkey="5Fold", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    for hk in ("5Fold", "5Fnew"):
        app.state.chain.register(hk)
    app.state.chain.commit("5Fold", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    moved = rec.model_copy(update={"miner_hotkey": "5Fnew"})
    app.state.chain.commit("5Fnew", hash_obj(moved.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", moved.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    with app.state.db.session() as s:
        assert s.get(Node, rec.node_id).miner_hotkey == "5Fnew"


def test_verification_progress_is_recorded_per_session_and_shown(app):
    """Plan opens a session with pending steps; running/pass/fail updates them; an outcome closes it (pending -> skipped);
    a new session id resets; the public verification view and the node listing carry it."""
    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, RegistrationRecord

    c = TestClient(app)
    vkey = SigningKey.generate()
    vk = enroll_validator(c, vkey)
    h = {"X-API-KEY": vk}
    ids = [f"sha256:7{i:063x}" for i in range(8)]
    host = SigningKey.generate()
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="vm", miner_hotkey="5Fprog", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Fprog")
    app.state.chain.commit("5Fprog", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    nid = rec.node_id
    base = {"node_id": nid, "session_kind": "admission", "session_id": "v1:s1"}
    assert c.post("/v1/validators/progress", json={**base, "plan": [{"id": "B0", "name": "Boot"}, {"id": "A1", "name": "Provision"}, {"id": "A2", "name": "GPUs"}]}, headers=h).status_code == 200
    c.post("/v1/validators/progress", json={**base, "step_id": "B0", "step_name": "Boot", "state": "running"}, headers=h)
    c.post("/v1/validators/progress", json={**base, "step_id": "B0", "step_name": "Boot", "state": "pass", "duration_s": 42.0}, headers=h)
    c.post("/v1/validators/progress", json={**base, "step_id": "A1", "step_name": "Provision", "state": "running"}, headers=h)
    v = c.get(f"/v1/nodes/{nid}/verification").json()["progress"]
    assert v["session_kind"] == "admission" and v["done"] == 1 and v["total"] == 3 and v["current"] == "A1" and v["outcome"] is None
    assert [st["state"] for st in v["steps"]] == ["pass", "running", "pending"]
    c.post("/v1/validators/progress", json={**base, "step_id": "A1", "step_name": "Provision", "state": "fail", "detail": "ssh refused"}, headers=h)
    c.post("/v1/validators/progress", json={**base, "outcome": "quarantined"}, headers=h)
    v = c.get(f"/v1/nodes/{nid}/verification").json()["progress"]
    assert v["outcome"] == "quarantined" and v["failed"] == ["A1"] and [st["state"] for st in v["steps"]] == ["pass", "fail", "skipped"]
    # a new session replaces the old steps; the listing carries the summary
    c.post("/v1/validators/progress", json={"node_id": nid, "session_kind": "full", "session_id": "v1:s2", "plan": [{"id": "B0", "name": "Boot"}, {"id": "Q1", "name": "Identity"}]}, headers=h)
    c.post("/v1/validators/progress", json={"node_id": nid, "session_kind": "full", "session_id": "v1:s2", "step_id": "B0", "state": "running"}, headers=h)
    v = c.get(f"/v1/nodes/{nid}/verification").json()["progress"]
    assert v["session_kind"] == "full" and v["total"] == 2 and v["done"] == 0 and v["current"] == "B0"
    listing = [n for n in c.get("/v1/nodes?include_offline=true").json()["nodes"] if n["node_id"] == nid][0]  # its host never connected
    assert listing["verification"]["current"] == "B0" and listing["verification"]["total"] == 2


def test_host_offline_alerts_tenants_hides_idle_nodes_and_recovers(app):
    """Host agent unreachable: after the grace period the outage is recorded; a rented node stays listed and its tenant instance
    is degraded with a clear reason (billing paused); an idle node disappears from the list (include_offline shows it); validators
    see the outage; when the host returns the instance is active again (or lost -> error), and a long-gone idle node needs a
    readiness canary before it is listed again."""
    import datetime as dt

    from sqlalchemy import select

    from racksdock_platform.marketplace.models import HostOutage, Incident, Instance, Lease, Node, User
    from racksdock_platform.marketplace.services.connectivity import declare_online, recover_or_lose, sweep

    c = TestClient(app)
    settings, orch = app.state.settings, app.state.orchestrator
    host = SigningKey.generate()
    ids = [f"sha256:9{i:063x}" for i in range(8)]
    rec, r = register_node(c, app.state.chain, host, ids, hotkey="5Foffline")
    assert r.status_code == 201, r.text
    nid = rec.node_id
    now = dt.datetime(2026, 10, 22, 4, 30, tzinfo=dt.UTC)
    iso = lambda t: t.replace(microsecond=0).isoformat().replace("+00:00", "Z")  # noqa: E731

    def rent(s, name):
        u = s.scalar(select(User).where(User.email == "t@x.y")) or User(email="t@x.y", credits_cents=10000)
        s.add(u)
        s.flush()
        inst = Instance(user_id=u.id, name=name, node_id=nid, mode="tenant", os="ubuntu24.04", image_version="v", status="active", tenant_nonce="00" * 32,
                        user_data="", user_data_hash="h", request={}, hourly_price_cents=6000, active_at=iso(now - dt.timedelta(minutes=30)))
        s.add(inst)
        s.flush()
        s.add(Lease(instance_id=inst.id, node_id=nid, tenant_nonce="00" * 32, user_data_hash="h", mode="tenant", is_validator=False, active_at=inst.active_at))
        return inst.id

    with app.state.db.session() as s:
        node = s.get(Node, nid)
        node.status, node.host_connected, node.needs_readiness, node.perf, node.qualification = "qualified", True, False, 1.0, {"outcome": "pass"}
        node.last_heartbeat_at = iso(now - dt.timedelta(seconds=10))
        iid = rent(s, "rented-while-offline")
    listed = {n["node_id"]: n for n in c.get("/v1/nodes").json()["nodes"]}
    assert listed[nid]["available"] is False and listed[nid]["host_connected"] is True
    # heartbeats stop; within the grace period nothing is declared
    with app.state.db.session() as s:
        s.get(Node, nid).last_heartbeat_at = iso(now - dt.timedelta(seconds=60))
        assert sweep(s, settings, now) == []
    # ... after it, the outage is declared and the tenant is alerted through the instance status
    with app.state.db.session() as s:
        s.get(Node, nid).last_heartbeat_at = iso(now - dt.timedelta(seconds=200))
        assert sweep(s, settings, now) == [nid]
        inst, node = s.get(Instance, iid), s.get(Node, nid)
        assert inst.status == "degraded" and inst.status_details.startswith("host offline since") and "Billing is paused" in inst.status_details
        assert node.host_connected is False and node.offline_since == iso(now - dt.timedelta(seconds=200))
        inc = s.scalar(select(Incident).where(Incident.instance_id == iid, Incident.klass == "host_offline"))
        assert inc is not None and inc.closed_at is None
        assert sweep(s, settings, now) == []  # idempotent
    # rented + offline: still listed, flagged; validators see the outage
    row = {n["node_id"]: n for n in c.get("/v1/nodes").json()["nodes"]}[nid]
    assert row["host_connected"] is False and row["offline_since"] and row["available"] is False
    vk = enroll_validator(c, SigningKey.generate(), hotkey="5Gvo")
    vrow = {n["node_id"]: n for n in c.get("/v1/validators/nodes", headers={"X-API-KEY": vk}).json()["nodes"]}[nid]
    assert len(vrow["outages"]) == 1 and vrow["outages"][0]["rented"] is True and vrow["outages"][0]["ended_at"] is None and vrow["offline_since"]
    # the host is back and still runs the instance: active again, outage and incident closed
    with app.state.db.session() as s:
        node = s.get(Node, nid)
        declare_online(s, node, settings)
        recover_or_lose(s, node, iid, "running", orch._release_lease)
        inst = s.get(Instance, iid)
        assert inst.status == "active" and inst.status_details is None and node.host_connected is True and node.offline_since is None
        assert s.scalar(select(Incident).where(Incident.instance_id == iid)).closed_at is not None
        assert s.scalar(select(HostOutage).where(HostOutage.node_id == nid)).ended_at is not None
        assert node.needs_readiness is False  # rented: the readiness canary comes after the rental as usual
    # the host drops again and comes back WITHOUT the instance: error, lease released, readiness required
    with app.state.db.session() as s:
        node = s.get(Node, nid)
        node.last_heartbeat_at = iso(now - dt.timedelta(seconds=500))
        assert sweep(s, settings, now) == [nid]
        declare_online(s, node, settings)
        recover_or_lose(s, node, None, None, orch._release_lease)
        inst = s.get(Instance, iid)
        assert inst.status == "error" and "came back without this instance" in inst.status_details
        assert s.scalar(select(Lease).where(Lease.instance_id == iid)).released_at is not None and node.needs_readiness is True
    # idle node offline: hidden from the list (shown with include_offline and on the home page's offline line)
    with app.state.db.session() as s:
        node = s.get(Node, nid)
        node.needs_readiness = False
        node.host_connected, node.offline_since = False, iso(now - dt.timedelta(seconds=300))
        assert sweep(s, settings, now) == [nid]
    assert nid not in {n["node_id"] for n in c.get("/v1/nodes").json()["nodes"]}
    assert nid in {n["node_id"] for n in c.get("/v1/nodes?include_offline=true").json()["nodes"]}
    v = c.get(f"/v1/nodes/{nid}/verification").json()
    assert v["host_connected"] is False and v["offline_since"] and len(v["outages"]) == 3
    # back after a long outage: listed only after a readiness canary
    with app.state.db.session() as s:
        node = s.get(Node, nid)
        declare_online(s, node, settings)
        assert node.host_connected is True and node.needs_readiness is True
    row = {n["node_id"]: n for n in c.get("/v1/nodes").json()["nodes"]}[nid]
    assert row["host_connected"] is True and row["available"] is False


@pytest.mark.asyncio
async def test_bootstrap_host_notes_reach_the_miner(app):
    """A host's log/error events for a bootstrap command (and its ':launch' sub-command) are returned with the identity drafts,
    so the miner CLI can show what the host waits for or why it failed instead of staying silent."""
    from racksdock_platform.marketplace.services.hosts import HostSession

    host = SigningKey.generate()
    sess = HostSession(ws=None, host_pubkey=host.public.hex, node_id=None, hello={})
    orch = app.state.orchestrator
    await orch.on_host_event(sess, HostEvent(kind="log", command_id="cmd_b1", payload={"message": "waiting for catalog image verify_v2 (download in progress, 0s so far)"}))
    await orch.on_host_event(sess, HostEvent(kind="error", command_id="cmd_b1:launch", payload={"error": "launch failed: container image racksdock/verify:v2 not present on this host"}))
    c = TestClient(app)
    r = c.get(f"/v1/miners/identity-drafts/{host.public.hex}").json()
    assert r["drafts"] == [] and [n["kind"] for n in r["notes"]] == ["log", "error"] and all(n["command_id"] == "cmd_b1" for n in r["notes"])
    assert "not present on this host" in r["notes"][1]["message"]
    assert c.get(f"/v1/miners/identity-drafts/{SigningKey.generate().public.hex}").json()["notes"] == []


def test_validator_enrolment_can_be_closed(app):
    """RACKSDOCK_VALIDATOR_ENROLL=0: no new validator keys; keys issued earlier keep working."""
    c = TestClient(app)
    vk = enroll_validator(c, SigningKey.generate(), hotkey="5Gearly")
    app.state.settings.validator_enroll_enabled = False
    try:
        env = SignedEnvelope.create("validator_enroll", {"hotkey": "5Glate"}, SigningKey.generate())
        assert c.post("/v1/validators/enroll", json={"envelope": env.model_dump()}).status_code == 403
        assert c.get("/v1/validators/nodes", headers={"X-API-KEY": vk}).status_code == 200
    finally:
        app.state.settings.validator_enroll_enabled = True


def test_snapshot_is_reproducible_from_public_endpoints_and_windows_are_aligned(app):
    """Anyone can rebuild the signed snapshot's inputs from the public endpoints alone and get the same values; two requests in
    the same tempo get the identical window, and the platform records its own round for comparison."""
    import datetime as dt

    from racksdock_subnet.core.identity import node_id_from_gpu_ids
    from racksdock_subnet.core.schemas import ClaimedSpec, RegistrationRecord
    from racksdock_subnet.validator.validator import compare_inputs, inputs_from_snapshot, rebuild_inputs, score_snapshot

    from racksdock_platform.marketplace.models import Node

    c = TestClient(app)
    host = SigningKey.generate()
    ids = [f"sha256:b{i:063x}" for i in range(8)]
    rec = RegistrationRecord(node_id=node_id_from_gpu_ids(ids, expected=8), gpu_ids=ids, profile="b300x8", deployment="vm", miner_hotkey="5Faudit", miner_service_key=host.public.hex,
                             claimed_spec=ClaimedSpec(vcpus=8, memory_gb=64, nvme_tb=1, network_gbps=10, region="lab"), host_image_version="h")
    app.state.chain.register("5Faudit")
    app.state.chain.commit("5Faudit", hash_obj(rec.model_dump(by_alias=True)))
    assert c.post("/v1/miners/register", json={"envelope": SignedEnvelope.create("registration", rec.model_dump(by_alias=True), host).model_dump()}).status_code == 201
    with app.state.db.session() as s:
        n = s.get(Node, rec.node_id)
        n.status, n.host_connected, n.needs_readiness, n.perf, n.qualification = "qualified", True, False, 1.0, {"outcome": "pass"}
        n.qualified_at = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    snap1 = c.get("/v1/scoring/snapshot", params={"tempo_s": 1260}).json()["envelope"]["payload"]
    snap2 = c.get("/v1/scoring/snapshot", params={"tempo_s": 1260}).json()["envelope"]["payload"]
    assert snap1["window_end"] == snap2["window_end"] and snap1["inputs"] == snap2["inputs"]
    end = dt.datetime.fromisoformat(snap1["window_end"])
    assert int(end.timestamp()) % 1260 == 0 and (end - dt.datetime.fromisoformat(snap1["window_start"])).total_seconds() == 1260

    def fetch_json(path, params):
        r = c.get(path, params=params)
        assert r.status_code == 200, (path, r.text)
        return r.json()

    rebuilt = rebuild_inputs(fetch_json, snap1)
    assert compare_inputs(inputs_from_snapshot(snap1), rebuilt) == []
    mine = next(i for i in rebuilt if i.node_id == rec.node_id)
    assert mine.hotkey == "5Faudit" and mine.minutes_available == 21.0  # freshly qualified counts as available
    # the platform recorded its round for this window; it equals what a validator computes from the snapshot
    rounds = c.get("/v1/scoring/rounds").json()["rounds"]
    mine_round = next(r for r in rounds if r["window_end"] == snap1["window_end"])
    res = score_snapshot(snap1)
    assert mine_round["scored"] == res.scored and mine_round["hotkey_weights"] == res.hotkey_weights
    params = c.get("/v1/scoring/params").json()
    assert "funded_slots" not in params and "qualification_validity_s" not in params and params["perf_floor"] == 0.9
    assert "leases" in c.get("/v1/leases").json()


@pytest.mark.asyncio
async def test_degraded_tenant_recovers_when_the_host_reconnects_then_heartbeats(app):
    """Outage on a rented node, host comes back through a reconnect (on_host_connect) and only then heartbeats: the instance
    must return to active on that heartbeat (regression: recovery only ran when the heartbeat itself ended the outage)."""
    import datetime as dt

    from sqlalchemy import select

    from racksdock_platform.marketplace.models import Incident, Instance, Lease, Node, User
    from racksdock_platform.marketplace.services.connectivity import sweep
    from racksdock_platform.marketplace.services.hosts import HostSession

    c = TestClient(app)
    host = SigningKey.generate()
    ids = [f"sha256:c{i:063x}" for i in range(8)]
    rec, r = register_node(c, app.state.chain, host, ids, hotkey="5Freconn")
    assert r.status_code == 201
    nid = rec.node_id
    now = dt.datetime.now(dt.UTC)
    iso = lambda t: t.replace(microsecond=0).isoformat().replace("+00:00", "Z")  # noqa: E731
    with app.state.db.session() as s:
        node = s.get(Node, nid)
        node.status, node.host_connected, node.needs_readiness = "qualified", True, False
        node.last_heartbeat_at = iso(now - dt.timedelta(seconds=500))
        u = User(email="r@x.y", credits_cents=1000)
        s.add(u)
        s.flush()
        inst = Instance(user_id=u.id, name="rented", node_id=nid, mode="tenant", os="ubuntu24.04", image_version="v", status="active", tenant_nonce="00" * 32,
                        user_data="", user_data_hash="h", request={}, hourly_price_cents=100, active_at=iso(now - dt.timedelta(hours=1)))
        s.add(inst)
        s.flush()
        s.add(Lease(instance_id=inst.id, node_id=nid, tenant_nonce="00" * 32, user_data_hash="h", mode="tenant", is_validator=False, active_at=inst.active_at))
        iid = inst.id
        assert sweep(s, app.state.settings, now) == [nid]
        assert s.get(Instance, iid).status == "degraded"
    orch = app.state.orchestrator
    sess = HostSession(ws=None, host_pubkey=host.public.hex, node_id=nid, hello={})
    await orch.on_host_connect(sess)  # the reconnect closes the outage ...
    with app.state.db.session() as s:
        assert s.get(Node, nid).host_connected is True and s.get(Instance, iid).status == "degraded"
    await orch.on_host_event(sess, HostEvent(kind="heartbeat", node_id=nid, instance_id=iid, payload={"node_id": nid, "host_pubkey": host.public.hex, "host_image_version": "h", "vm_state": "running", "instance_id": iid}))
    with app.state.db.session() as s:  # ... and the first heartbeat showing the instance running recovers it
        inst = s.get(Instance, iid)
        assert inst.status == "active" and inst.status_details is None
        assert s.scalar(select(Incident).where(Incident.instance_id == iid)).closed_at is not None


def test_stale_heartbeats_with_a_live_link_are_not_an_outage(app):
    """A host whose websocket is up but whose heartbeats run late is not declared offline; once the socket is gone it is."""
    import datetime as dt

    from racksdock_platform.marketplace.models import Node
    from racksdock_platform.marketplace.services.connectivity import sweep

    c = TestClient(app)
    host = SigningKey.generate()
    rec, r = register_node(c, app.state.chain, host, [f"sha256:d{i:063x}" for i in range(8)], hotkey="5Flate")
    assert r.status_code == 201
    now = dt.datetime.now(dt.UTC)
    with app.state.db.session() as s:
        n = s.get(Node, rec.node_id)
        n.status, n.host_connected = "qualified", True
        n.last_heartbeat_at = (now - dt.timedelta(seconds=400)).isoformat().replace("+00:00", "Z")
        assert sweep(s, app.state.settings, now, connected={rec.node_id}) == []
        assert s.get(Node, rec.node_id).host_connected is True
        assert sweep(s, app.state.settings, now, connected=set()) == [rec.node_id]


@pytest.mark.asyncio
async def test_region_is_detected_from_the_host_agents_address(app):
    """A node's region comes from a GeoIP lookup of the address its host agent connects from, not from what the miner typed;
    the listing and the node page show the detected country; a continent prefix matches in auto allocation."""
    from racksdock_platform.marketplace.models import Node
    from racksdock_platform.marketplace.services.geo import Geo, GeoInfo, is_public
    from racksdock_platform.marketplace.services.hosts import HostSession

    assert not is_public("127.0.0.1") and not is_public("10.1.2.3") and is_public("103.196.86.183")
    c = TestClient(app)
    host = SigningKey.generate()
    rec, r = register_node(c, app.state.chain, host, [f"sha256:f{i:063x}" for i in range(8)], hotkey="5Fgeo", region="eu-north-1")
    assert r.status_code == 201
    nid = rec.node_id
    orch = app.state.orchestrator
    orch.geo = Geo(None, False, resolver=lambda ip: GeoInfo(ip=ip, country="Hong Kong", country_code="HK", continent_code="AS", city="Chai Wan", lat=22.27, lon=114.25, source="test") if ip == "103.196.86.183" else None)
    sess = HostSession(ws=None, host_pubkey=host.public.hex, node_id=nid, hello={})
    sess.peer_ip = "103.196.86.183"
    await orch.on_host_connect(sess)
    with app.state.db.session() as s:
        n = s.get(Node, nid)
        assert n.region == "as-hk" and n.region_source == "geoip" and n.location["country"] == "Hong Kong" and n.location["detected_at"]
    row = {x["node_id"]: x for x in c.get("/v1/nodes?include_offline=true").json()["nodes"]}[nid]
    assert row["region"] == "as-hk" and row["region_source"] == "geoip" and row["location"]["city"] == "Chai Wan"
    v = c.get(f"/v1/nodes/{nid}/verification").json()
    assert v["region"] == "as-hk" and v["location"]["country_code"] == "HK"
    # a private address (dev, sim) leaves the claim untouched
    sess.peer_ip = "127.0.0.1"
    orch.geo = Geo(None, False, resolver=lambda ip: None)
    with app.state.db.session() as s:
        s.get(Node, nid).region, s.get(Node, nid).region_source = "eu-north-1", "claimed"
    await orch.on_host_connect(sess)
    with app.state.db.session() as s:
        assert s.get(Node, nid).region == "eu-north-1"


def test_negative_balance_alerts_once_then_the_cutoff_deletes_every_server(app):
    """Below zero the account gets one alert event; at -$5.00 (the default cutoff) every running server is deleted."""
    import datetime as dt

    from sqlalchemy import select

    from racksdock_platform.marketplace.models import BillingEvent, User

    def mk(uid, n, since="2026-10-20T00:00:00Z"):
        return Instance(user_id=uid, name=n, node_id=None, os="ubuntu24.04", image_version="v", status="active", tenant_nonce="00" * 32, user_data="", user_data_hash="h",
                        request={}, hourly_price_cents=6000, active_at=since, last_metered_at=since)  # $1.00 per minute

    with app.state.db.session() as s:
        u = User(email="n@x.y", credits_cents=150)
        s.add(u)
        s.flush()
        a, b = mk(u.id, "a"), mk(u.id, "b")
        s.add_all([a, b])
        s.flush()
        uid, ids = u.id, {a.id, b.id}
    t = dt.datetime(2026, 10, 20, 0, 0, 0, tzinfo=dt.UTC)
    with app.state.db.session() as s:  # minute 1: $1.50 - $2.00 = -$0.50 -> one alert
        actions = meter(s, now=t + dt.timedelta(minutes=1))
        assert actions == [(uid, "credit_alert")] and s.get(User, uid).credits_cents == -50 and s.get(User, uid).credit_alert_at
        ev = s.scalars(select(BillingEvent).where(BillingEvent.user_id == uid, BillingEvent.kind == "alert")).all()
        assert len(ev) == 1 and "-$0.50" in ev[0].note and "-$5.00" in ev[0].note
    with app.state.db.session() as s:  # minute 2: -$2.50, no second alert, no cutoff yet
        assert meter(s, now=t + dt.timedelta(minutes=2)) == []
    with app.state.db.session() as s:  # minute 4: -$6.50 -> both servers go
        actions = meter(s, now=t + dt.timedelta(minutes=4))
        assert {x for x, kind in actions if kind == "credit_cutoff"} == ids and all(kind == "credit_cutoff" for _, kind in actions)
        note = s.scalars(select(BillingEvent).where(BillingEvent.user_id == uid, BillingEvent.kind == "cutoff")).one().note
        assert note.startswith("2 servers deleted") and "-$6.50" in note
        for iid in ids:
            s.get(Instance, iid).status = "deleted"
    with app.state.db.session() as s:  # credits added: the alert arms again
        s.get(User, uid).credits_cents = 100
        s.add(mk(uid, "c", since="2026-10-20T00:05:00Z"))
    with app.state.db.session() as s:
        assert meter(s, now=t + dt.timedelta(minutes=5)) == [] and s.get(User, uid).credit_alert_at is None
    with app.state.db.session() as s:  # custom cutoff
        s.get(User, uid).credits_cents = -120
        assert meter(s, now=t + dt.timedelta(minutes=6), cutoff_cents=100) and s.get(User, uid).credit_alert_at
