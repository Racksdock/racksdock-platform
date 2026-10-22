"""Validator-facing endpoints (docs/03 §10): verify instances, leases, records, challenges, strikes, incidents."""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.schemas import Challenge, CreateInstanceRequest, VmMode
from sqlalchemy import select

from ..models import Instance, Lease, Node, Strike, User
from ..services.auth import new_api_key, require_role
from ..services.connectivity import outages_for
from ..services.records import ingest_canary, ingest_inrental, ingest_qualification, store_envelope
from .deps import catalog, chain, db, orch, settings
from .views import instance_info

router = APIRouter(prefix="/v1/validators", tags=["validators"])
require_validator = require_role("validator")


class Enroll(BaseModel):
    envelope: SignedEnvelope  # kind "validator_enroll", payload {hotkey}


@router.post("/enroll", status_code=201)
def enroll(body: Enroll, _db=Depends(db), st=Depends(settings), ch=Depends(chain)):
    env = body.envelope
    if not env.verify() or env.kind != "validator_enroll":
        raise HTTPException(400, "bad envelope")
    hotkey = env.payload.get("hotkey")
    if not hotkey:
        raise HTTPException(400, "hotkey required")
    if not st.open_validator_enroll:
        ok = any(n.hotkey == hotkey and n.is_validator and n.stake >= st.min_validator_stake for n in ch.neurons())
        if not ok:
            raise HTTPException(403, "hotkey is not a validator with enough stake")
    with _db.session() as s:
        u = s.scalar(select(User).where(User.validator_key == env.signer))
        if u is None:
            u = User(email=f"validator-{env.signer[:12]}@racksdock.local", role="validator", validator_key=env.signer, hotkey=hotkey)
            s.add(u)
            s.flush()
        key = new_api_key(s, u, name="validator")
        return {"user_id": u.id, "api_key": key, "validator_key": env.signer}


@router.get("/nodes")
def nodes(_v: User = Depends(require_validator), _db=Depends(db)):
    with _db.session() as s:
        out = []
        for n in s.scalars(select(Node)).all():
            live = s.scalar(select(Lease).where(Lease.node_id == n.node_id, Lease.released_at.is_(None)))
            out.append({
                "node_id": n.node_id, "miner_hotkey": n.miner_hotkey, "miner_service_key": n.miner_service_key, "status": n.status, "region": n.region,
                "registration": n.registration, "registration_hash": n.registration_hash, "registered_block": n.registered_block,
                "qualified_at": n.qualified_at, "qualification": n.qualification, "perf": n.perf, "host_connected": n.host_connected, "last_heartbeat_at": n.last_heartbeat_at,
                "needs_readiness": n.needs_readiness, "critical_fault": n.critical_fault, "maintenance_until": n.maintenance_until, "funded_slot": n.funded_slot,
                "offline_since": n.offline_since, "outages": outages_for(s, n.node_id, _dt.datetime.now(_dt.UTC) - _dt.timedelta(hours=48)),
                "live_lease": ({"instance_id": live.instance_id, "mode": live.mode, "is_validator": live.is_validator, "created_at": live.created_at, "active_at": live.active_at} if live else None),
            })
        return {"nodes": out}


@router.post("/instances", status_code=201)
async def create_verify_instance(req: CreateInstanceRequest, v: User = Depends(require_validator), _db=Depends(db), o=Depends(orch)):
    """A validator rents the node through the same path a tenant uses, with the verify image and its own SSH key."""
    if req.node_id == "auto":
        raise HTTPException(400, "validators must name a node_id")
    pubkeys = req.model_extra.get("ssh_public_keys") if req.model_extra else None
    try:
        inst = o.create_instance(req, user_id=v.id, validator_key=v.validator_key, ssh_public_keys=pubkeys or [], mode=VmMode.verify)
    except ValueError as e:
        raise HTTPException({"node_busy": 409, "node_not_found": 404}.get(str(e), 400), str(e))
    await o.launch(inst.id)
    return {"id": inst.id}


class VerifyInstanceRequest(BaseModel):
    node_id: str
    name: str = "verify"
    ssh_public_keys: list[str] = []
    tenant_nonce: str | None = None
    os: str = "verify"
    deployment: str | None = None


@router.post("/verify-instances", status_code=201)
async def create_verify_instance2(body: VerifyInstanceRequest, v: User = Depends(require_validator), o=Depends(orch)):
    req = CreateInstanceRequest(name=body.name, node_id=body.node_id, os=body.os, tenant_nonce=body.tenant_nonce, deployment=getattr(body, "deployment", None))
    try:
        inst = o.create_instance(req, user_id=v.id, validator_key=v.validator_key, ssh_public_keys=body.ssh_public_keys, mode=VmMode.verify)
    except ValueError as e:
        raise HTTPException({"node_busy": 409, "node_not_found": 404}.get(str(e), 400), str(e))
    await o.launch(inst.id)
    return {"id": inst.id}


@router.get("/instances/{instance_id}")
def verify_instance_info(instance_id: str, v: User = Depends(require_validator), _db=Depends(db), cat=Depends(catalog)):
    with _db.session() as s:
        inst = s.get(Instance, instance_id)
        if inst is None:
            raise HTTPException(404, "not found")
        if inst.validator_key != v.validator_key and v.role != "admin":
            # other tenants' instances: validators see only what they need for monitoring
            node = s.get(Node, inst.node_id)
            return {"id": inst.id, "node_id": inst.node_id, "status": inst.status, "ip": inst.ip, "ssh_port": inst.ssh_port, "mode": inst.mode,
                    "tenant_bound": inst.mode == "tenant" and inst.validator_key is None, "active_at": inst.active_at,
                    "attestation_gpu_ids": (inst.attestation or {}).get("gpu_ids"), "node_agent_pubkey": inst.node_agent_pubkey,
                    "registration_gpu_ids": (node.registration or {}).get("gpu_ids") if node else None}
        return instance_info(inst, s.get(Node, inst.node_id), cat).model_dump()


@router.post("/instances/{instance_id}/delete")
async def delete_verify_instance(instance_id: str, v: User = Depends(require_validator), _db=Depends(db), o=Depends(orch)):
    with _db.session() as s:
        inst = s.get(Instance, instance_id)
        if inst is None or inst.validator_key != v.validator_key:
            raise HTTPException(404, "not found")
    await o.delete(instance_id, reason="validator delete")
    return {"id": instance_id, "status": "deleting"}


@router.get("/leases")
def leases(_v: User = Depends(require_validator), _db=Depends(db), live_only: bool = True, since: str | None = None):
    with _db.session() as s:
        q = select(Lease)
        if live_only:
            q = q.where(Lease.released_at.is_(None))
        if since:
            q = q.where(Lease.created_at >= since)
        rows = s.scalars(q.order_by(Lease.created_at)).all()
        out = []
        for lease in rows:
            inst = s.get(Instance, lease.instance_id)
            out.append({"lease_id": lease.lease_id, "instance_id": lease.instance_id, "node_id": lease.node_id, "mode": lease.mode, "is_validator": lease.is_validator,
                        "created_at": lease.created_at, "active_at": lease.active_at, "released_at": lease.released_at,
                        "status": inst.status if inst else None, "ip": inst.ip if inst else None, "ssh_port": inst.ssh_port if inst else None,
                        "node_agent_pubkey": inst.node_agent_pubkey if inst else None})
        return {"leases": out}


class RecordBody(BaseModel):
    envelope: SignedEnvelope


@router.post("/records", status_code=201)
def publish_record(body: RecordBody, v: User = Depends(require_validator), _db=Depends(db)):
    env = body.envelope
    if env.signer != v.validator_key:
        raise HTTPException(403, "record must be signed by your validator key")
    with _db.session() as s:
        try:
            if env.kind == "qualification":
                ingest_qualification(s, env)
            elif env.kind == "canary":
                ingest_canary(s, env)
            elif env.kind == "inrental":
                ingest_inrental(s, env)
            elif env.kind in ("probe", "scoring", "validator_note"):
                store_envelope(s, env, env.kind, node_id=env.payload.get("node_id"), instance_id=env.payload.get("instance_id"))
            else:
                raise HTTPException(400, f"unknown record kind {env.kind}")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"stored": True, "content_hash": env.content_hash, "kind": env.kind}


class ChallengeBody(BaseModel):
    node_id: str
    envelope: SignedEnvelope  # kind "challenge"


@router.post("/challenge")
async def relay_challenge(body: ChallengeBody, v: User = Depends(require_validator), _db=Depends(db), o=Depends(orch)):
    env = body.envelope
    if not env.verify(expected_signer=v.validator_key) or env.kind != "challenge":
        raise HTTPException(400, "challenge must be signed by your validator key")
    ch = Challenge.model_validate(env.payload)
    with _db.session() as s:
        inst = s.get(Instance, ch.instance_id)
        if inst is None or inst.node_id != body.node_id:
            raise HTTPException(404, "instance not on that node")
    try:
        ev = await o.challenge(body.node_id, ch.instance_id, env)
    except ConnectionError:
        raise HTTPException(503, "host not connected")
    except TimeoutError:
        raise HTTPException(504, "no response from node")
    return {"response": ev.payload}


class StrikeBody(BaseModel):
    node_id: str
    reason: str
    weight: float = 1.0


@router.post("/strikes", status_code=201)
def add_strike(body: StrikeBody, v: User = Depends(require_validator), _db=Depends(db)):
    with _db.session() as s:
        if s.get(Node, body.node_id) is None:
            raise HTTPException(404, "node not found")
        s.add(Strike(node_id=body.node_id, reason=body.reason, weight=body.weight, source=v.validator_key[:12]))
    return {"stored": True}


class IncidentBody(BaseModel):
    instance_id: str
    klass: str
    details: str = ""
    strike: bool = False


@router.post("/incidents", status_code=201)
def report_incident(body: IncidentBody, v: User = Depends(require_validator), o=Depends(orch)):
    o.degrade(body.instance_id, body.klass, body.details, source=v.validator_key[:12], strike=body.strike)
    return {"stored": True}


@router.post("/incidents/{instance_id}/recover")
def recover(instance_id: str, v: User = Depends(require_validator), o=Depends(orch)):
    o.recover(instance_id, source=v.validator_key[:12])
    return {"recovered": True}


class ProgressBody(BaseModel):
    node_id: str
    session_kind: str
    session_id: str
    validator_key: str | None = None
    at: str | None = None
    plan: list[dict] | None = None
    step_id: str | None = None
    step_name: str | None = None
    state: str | None = None  # running | pass | fail | skipped
    detail: str | None = None
    duration_s: float | None = None
    outcome: str | None = None


@router.post("/progress")
def progress(body: ProgressBody, v: User = Depends(require_validator), _db=Depends(db)):
    """Verification step progress (docs/03 §5.1): a plan opens a session, each step is reported running then pass/fail/skipped,
    an outcome closes it. The console shows it; the signed qualification record stays the source of truth."""
    from ..models import VerificationProgress, now_iso

    with _db.session() as s:
        row = s.get(VerificationProgress, body.node_id)
        if row is None or row.session_id != body.session_id:
            if row is None:
                row = VerificationProgress(node_id=body.node_id, session_id=body.session_id, session_kind=body.session_kind, validator_key=v.validator_key, steps=[])
                s.add(row)
            else:
                row.session_id, row.session_kind, row.validator_key, row.steps, row.outcome = body.session_id, body.session_kind, v.validator_key, [], None
            row.started_at = body.at or now_iso()
        steps = [dict(st) for st in (row.steps or [])]  # copies: in-place edits of the stored dicts would not be flushed
        if body.plan:
            known = {st["id"] for st in steps}
            steps += [{"id": pl["id"], "name": pl.get("name", pl["id"]), "state": "pending"} for pl in body.plan if pl.get("id") and pl["id"] not in known]
        if body.step_id:
            entry = next((st for st in steps if st["id"] == body.step_id), None)
            if entry is None:
                entry = {"id": body.step_id, "name": body.step_name or body.step_id}
                steps.append(entry)
            entry.update({"name": body.step_name or entry.get("name", body.step_id), "state": body.state or entry.get("state", "running"), "at": body.at or now_iso()})
            if body.detail is not None:
                entry["detail"] = body.detail
            if body.duration_s is not None:
                entry["duration_s"] = body.duration_s
        if body.outcome:
            row.outcome = body.outcome
            for st in steps:
                if st.get("state") in ("pending", "running"):
                    st["state"] = "skipped"
        row.steps = steps
        row.updated_at = now_iso()
        return {"node_id": row.node_id, "session_id": row.session_id, "steps": len(steps), "outcome": row.outcome}


@router.get("/settings")
def validator_settings(_v: User = Depends(require_validator), _db=Depends(db), st=Depends(settings)):
    from ..models import Setting

    with _db.session() as s:
        fs = s.get(Setting, "funded_slots")
        return {"funded_slots": int(fs.value) if fs else st.default_funded_slots, "catalog_release": None}
