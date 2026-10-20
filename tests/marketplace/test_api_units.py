from fastapi.testclient import TestClient
from racksdock_subnet.core.canonical import hash_obj
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.identity import node_id_from_gpu_ids
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.schemas import (
    AttestationSummary,
    CanaryResult,
    ClaimedSpec,
    PerformanceSummary,
    QualificationRecord,
    RegistrationRecord,
    SpecMeasured,
)

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
    # quarantine on failed weekly
    assert qualify(c, vkey, vk, rec, perf=0.5, outcome="quarantined", kind="weekly").status_code == 201
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
