"""End-to-end: marketplace (uvicorn) + real host-agent (sim backend) + node-agent process + validator/miner/user flows over HTTP."""
import asyncio
import datetime as dt
import socket

import httpx
import pytest
import uvicorn
from racksdock_node.host_agent.agent import HostAgent
from racksdock_node.host_agent.config import HostAgentConfig
from racksdock_subnet.core.canonical import hash_obj
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.core.nonce import new_nonce_hex
from racksdock_subnet.core.schemas import (
    AttestationSummary,
    CanaryResult,
    Challenge,
    ChallengeResponse,
    PerformanceSummary,
    QualificationRecord,
    RegistrationRecord,
    SpecMeasured,
    VmResources,
)
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.verifiers import SimVerifier

from racksdock_platform.marketplace.models import Instance, Lease, Node
from racksdock_platform.marketplace.services.billing import meter


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def wait_for(fn, timeout=60, interval=0.3, desc="condition"):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        v = fn()
        if asyncio.iscoroutine(v):
            v = await v
        if v:
            return v
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timeout waiting for {desc}")
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_full_marketplace_flow(app, settings, ca, tmp_path):
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    srv_task = asyncio.create_task(server.serve())
    await wait_for(lambda: server.started, desc="server start")
    base = f"http://127.0.0.1:{port}"
    chain = app.state.chain

    node_dir = ca.issue_node(tmp_path / "node")
    cfg = HostAgentConfig(mode="sim", platform_ws=f"ws://127.0.0.1:{port}/v1/hosts/ws", state_dir=tmp_path / "hstate", key_path=tmp_path / "hstate" / "host.seed",
                          miner_hotkey="5Fminer", host_image_version="host_vtest", region="sim-1", image_dir=tmp_path / "img", vm_dir=tmp_path / "vms",
                          agent_transport="tcp", agent_port=0, heartbeat_interval_s=2.0, sim_node_dir=node_dir,
                          resources=VmResources(gpu_pci=[f"0000:{17 + i:02x}:00.0" for i in range(8)], nvme_pci=["0000:a0:00.0"], vcpus=8, memory_gb=16),
                          trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir))
    host = HostAgent(cfg)
    host_task = asyncio.create_task(host.run())
    try:
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            await wait_for(lambda: host.key.public.hex in app.state.hosts.by_key, desc="host connected")

            # ---- miner: bootstrap identity, commit on chain, register
            r = await c.post(f"/v1/miners/bootstrap/{host.key.public.hex}")
            assert r.status_code == 200
            async def drafts_ready():
                d = (await c.get(f"/v1/miners/identity-drafts/{host.key.public.hex}")).json()
                return d["drafts"] or None

            drafts = await wait_for(drafts_ready, timeout=90, desc="identity draft")
            draft = drafts[0]
            rec = RegistrationRecord.model_validate(draft["registration"])
            env = SignedEnvelope.model_validate(draft["registration_envelope"])
            assert env.verify(expected_signer=host.key.public.hex)
            chain.register("5Fminer")
            chain.commit("5Fminer", hash_obj(rec.model_dump(by_alias=True)))
            r = await c.post("/v1/miners/register", json={"envelope": env.model_dump()})
            assert r.status_code == 201, r.text
            await wait_for(lambda: rec.node_id in app.state.hosts.by_node, desc="host bound to node")
            await wait_for(lambda: host.current is None, desc="bootstrap VM torn down")

            # ---- validator: enroll, verify instance, qualify
            vkey = SigningKey.generate()
            r = await c.post("/v1/validators/enroll", json={"envelope": SignedEnvelope.create("validator_enroll", {"hotkey": "5Gval"}, vkey).model_dump()})
            vh = {"X-API-KEY": r.json()["api_key"]}
            r = await c.post("/v1/validators/verify-instances", json={"node_id": rec.node_id, "ssh_public_keys": [vkey.public.ssh_public_line("validator")]}, headers=vh)
            assert r.status_code == 201, r.text
            vid = r.json()["id"]

            async def vstatus():
                i = (await c.get(f"/v1/validators/instances/{vid}", headers=vh)).json()
                return i if i["status"] in ("active", "error") else None

            vi = await wait_for(vstatus, timeout=90, desc="verify instance active")
            assert vi["status"] == "active", vi
            assert vi["attestation"]["status"] == "verified" and vi["attestation"]["gpu_ids_match"] is True
            assert vi["configuration"]["os"] == "verify"
            # publish qualification
            q = QualificationRecord(node_id=rec.node_id, validator_hotkey="5Gval", validator_key=vkey.public.hex, instance_id=vid, verify_image=vi["attestation"]["image_version"],
                                    started_at=vi["created_at"], finished_at=vi["active_at"], outcome="pass",
                                    attestation=AttestationSummary(gpu_ids=rec.gpu_ids, measurements_hash="sha256:m"), spec_measured=SpecMeasured(memory_gb=16),
                                    performance=PerformanceSummary(ratios={"nvlink.allreduce_1gb_busbw_gbps": 0.98}, perf=0.98), reference_set="ref_dev_v0")
            r = await c.post("/v1/validators/records", json={"envelope": SignedEnvelope.create("qualification", q.model_dump(by_alias=True, mode="json"), vkey).model_dump()}, headers=vh)
            assert r.status_code == 201, r.text
            r = await c.post(f"/v1/validators/instances/{vid}/delete", headers=vh)
            assert r.status_code == 200
            async def vdeleted():
                return (await c.get(f"/v1/validators/instances/{vid}", headers=vh)).json()["status"] == "deleted"

            await wait_for(vdeleted, timeout=60, desc="verify instance deleted")
            types = (await c.get("/v1/instances/types")).json()["instance_types"][0]
            assert types["availability"][0]["available"] is True and types["availability"][0]["node_id"] == rec.node_id

            # ---- user: signup, key, credits, create instance
            r = await c.post("/v1/account/signup", json={"email": "u@x.y", "password": "password1"})
            uh = {"X-API-KEY": r.json()["api_key"]}
            ukey = SigningKey.generate()
            r = await c.post("/v1/sshkeys/add", json={"name": "laptop", "public_key": ukey.public.ssh_public_line("laptop")}, headers=uh)
            await c.post("/v1/billing/credits/purchase", json={"cents": 50000}, headers=uh)
            tn = new_nonce_hex()
            body = {"name": "train-1", "node_id": rec.node_id, "os": "ubuntu24.04_cuda13.x", "tenant_nonce": tn, "envs": [{"name": "WANDB_MODE", "value": "offline"}],
                    "launch_configuration": {"type": "docker", "docker_configuration": {"image": "vllm/vllm-openai:v0.1", "port_mappings": [{"container_port": 8000, "host_port": 8000}]}},
                    "networking": {"ufw_rules": [{"rule": "allow", "port": "8000", "proto": "tcp"}]}, "tags": ["team-a"], "enable_metrics": True}
            r = await c.post("/v1/instances/create", json=body, headers=uh)
            assert r.status_code == 201, r.text
            iid = r.json()["id"]
            # the node is now leased: a second create must fail with node_busy
            r2 = await c.post("/v1/instances/create", json={**body, "name": "train-2", "tenant_nonce": new_nonce_hex()}, headers=uh)
            assert r2.status_code == 409, r2.text

            async def ustatus():
                i = (await c.get(f"/v1/instances/{iid}/info", headers=uh)).json()
                return i if i["status"] in ("active", "error") else None

            info = await wait_for(ustatus, timeout=90, desc="tenant instance active")
            assert info["status"] == "active", info
            att = info["attestation"]
            assert att["status"] == "verified" and att["gpu_ids_match"] is True and att["user_data_hash"] == info["user_data_hash"]
            assert info["tenant_nonce"] == tn and info["host_key_fingerprint"].startswith("SHA256:") and info["ip"] == "127.0.0.1"
            assert "WANDB_MODE" in info["user_data"] and "launch.json" in info["user_data"] and "ufw allow" in info["user_data"]
            assert info["launch_configuration"] == {"type": "docker", "container_status": "started"}
            assert info["configuration"]["gpu_type"] == "B300" and info["configuration"]["num_gpus"] == 8
            with app.state.db.session() as s:
                assert s.query(Lease).filter(Lease.released_at.is_(None)).count() == 1

            # ---- validator: in-rental challenge relayed through platform and host to the node-agent
            nonce = new_nonce_hex()
            ch = Challenge(instance_id=iid, nonce=nonce, fields=["attestation", "health"], validator_key=vkey.public.hex)
            r = await c.post("/v1/validators/challenge", json={"node_id": rec.node_id, "envelope": SignedEnvelope.create("challenge", ch.model_dump(), vkey).model_dump()}, headers=vh)
            assert r.status_code == 200, r.text
            renv = SignedEnvelope.model_validate(r.json()["response"])
            with app.state.db.session() as s:
                inst = s.get(Instance, iid)
                assert renv.verify(expected_signer=inst.node_agent_pubkey)
            resp = ChallengeResponse.model_validate(renv.payload)
            vres = SimVerifier(ca).verify(resp.bundle, nonce)
            assert vres.ok and sorted(vres.gpu_ids) == sorted(rec.gpu_ids)
            assert resp.health.mode.value == "tenant" and len(resp.health.gpus) == 8
            # validators see a monitoring view of tenant instances, never the user-data
            mon = (await c.get(f"/v1/validators/instances/{iid}", headers=vh)).json()
            assert mon["tenant_bound"] is True and "user_data" not in mon
            # validator publishes an in-rental record
            from racksdock_subnet.core.schemas import InRentalCheckResult

            irc = InRentalCheckResult(node_id=rec.node_id, instance_id=iid, validator_key=vkey.public.hex, checked_at="now", attestation_ok=True, gpu_ids_match=True, health=resp.health, reachable=True)
            r = await c.post("/v1/validators/records", json={"envelope": SignedEnvelope.create("inrental", irc.model_dump(by_alias=True, mode="json"), vkey).model_dump()}, headers=vh)
            assert r.status_code == 201

            # ---- billing: two minutes later
            with app.state.db.session() as s:
                inst = s.get(Instance, iid)
                t0 = dt.datetime.fromisoformat(inst.active_at.replace("Z", "+00:00"))
                meter(s, now=t0 + dt.timedelta(minutes=2, seconds=5))
                inst = s.get(Instance, iid)
                assert inst.billed_minutes == 2 and inst.cost_cents == round(inst.hourly_price_cents * 2 / 60)
            info = (await c.get(f"/v1/instances/{iid}/info", headers=uh)).json()
            assert float(info["cost_estimate"]) > 0

            # ---- restart keeps the instance active
            r = await c.post(f"/v1/instances/{iid}/restart", headers=uh)
            assert r.status_code == 200, r.text
            await asyncio.sleep(1.0)
            assert (await c.get(f"/v1/instances/{iid}/info", headers=uh)).json()["status"] == "active"

            # ---- degrade + recover via validator incident
            r = await c.post("/v1/validators/incidents", json={"instance_id": iid, "klass": "gpu_fault", "details": "xid 79", "strike": True}, headers=vh)
            assert (await c.get(f"/v1/instances/{iid}/info", headers=uh)).json()["status"] == "degraded"
            await c.post(f"/v1/validators/incidents/{iid}/recover", headers=vh)
            assert (await c.get(f"/v1/instances/{iid}/info", headers=uh)).json()["status"] == "active"

            # ---- delete
            r = await c.post(f"/v1/instances/{iid}/delete", headers=uh)
            assert r.json()["status"] == "deleting"
            async def udeleted():
                return (await c.get(f"/v1/instances/{iid}/info", headers=uh)).json()["status"] == "deleted"

            await wait_for(udeleted, timeout=60, desc="deleted")
            with app.state.db.session() as s:
                assert s.query(Lease).filter(Lease.released_at.is_(None)).count() == 0
                node = s.get(Node, rec.node_id)
                assert node.needs_readiness is True
            assert (await c.get("/v1/instances/types")).json()["instance_types"][0]["availability"][0]["available"] is False

            # ---- readiness canary by the validator relists the node
            r = await c.post("/v1/validators/verify-instances", json={"node_id": rec.node_id, "name": "canary", "ssh_public_keys": [vkey.public.ssh_public_line("v")]}, headers=vh)
            cid = r.json()["id"]
            async def cstatus():
                i = (await c.get(f"/v1/validators/instances/{cid}", headers=vh)).json()
                return i if i["status"] in ("active", "error") else None

            ci = await wait_for(cstatus, timeout=90, desc="canary active")
            assert ci["status"] == "active" and ci["attestation"]["gpu_ids_match"] is True
            await c.post(f"/v1/validators/instances/{cid}/delete", headers=vh)
            async def cdeleted():
                return (await c.get(f"/v1/validators/instances/{cid}", headers=vh)).json()["status"] == "deleted"

            await wait_for(cdeleted, timeout=60, desc="canary deleted")
            can = CanaryResult(node_id=rec.node_id, validator_key=vkey.public.hex, instance_id=cid, started_at=ci["created_at"], finished_at=ci["active_at"], outcome="pass", gpu_ids_match=True, time_to_active_s=3.0)
            r = await c.post("/v1/validators/records", json={"envelope": SignedEnvelope.create("canary", can.model_dump(by_alias=True, mode="json"), vkey).model_dump()}, headers=vh)
            assert r.status_code == 201
            assert (await c.get("/v1/instances/types")).json()["instance_types"][0]["availability"][0]["available"] is True
            ver = (await c.get(f"/v1/nodes/{rec.node_id}/verification")).json()
            assert ver["canaries"]["passed"] == 1 and ver["strikes"][0]["reason"] == "gpu_fault"
            ev = (await c.get("/v1/evidence", params={"node_id": rec.node_id})).json()["records"]
            kinds = {e["kind"] for e in ev}
            assert {"registration", "boot_evidence", "qualification", "inrental", "canary"} <= kinds
            # console pages render
            assert (await c.get("/")).status_code == 200
            assert (await c.get(f"/nodes/{rec.node_id}")).status_code == 200
    finally:
        host.stop()
        host_task.cancel()
        server.should_exit = True
        await srv_task
        for t in (host_task,):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
