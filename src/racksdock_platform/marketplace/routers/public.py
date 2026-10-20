from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from racksdock_subnet.core.profile import current as current_profile
from sqlalchemy import select

from ..models import EvidenceRecord, Node, Strike
from ..services.catalog import availability
from ..services.records import active_strikes
from .deps import catalog, db, settings

router = APIRouter(tags=["public"])


@router.get("/v1/instances/types")
def instance_types(request: Request, _db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    with _db.session() as s:
        avail = availability(s, cat, st.base_url)
    prof = current_profile()
    return {"instance_types": [{
        "shade_instance_type": prof.instance_type, "gpu_type": prof.gpu_model, "num_gpus": prof.gpu_count, "interconnect": prof.interconnect, "profile": prof.name,
        "trust_mode": st.trust.mode,
        "os_options": cat.os_options(), "catalog_release": cat.release,
        "availability": [a.model_dump() for a in avail],
    }]}


@router.get("/v1/nodes")
def nodes(_db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    with _db.session() as s:
        return {"nodes": [a.model_dump(exclude={"hourly_price_cents"}) for a in availability(s, cat, st.base_url, include_unqualified=True)]}


@router.get("/v1/nodes/{node_id}/verification")
def node_verification(node_id: str, _db=Depends(db)):
    with _db.session() as s:
        node = s.get(Node, node_id)
        if node is None:
            raise HTTPException(404, "node not found")
        quals = s.scalars(select(EvidenceRecord).where(EvidenceRecord.node_id == node_id, EvidenceRecord.kind == "qualification").order_by(EvidenceRecord.id.desc()).limit(20)).all()
        canaries = s.scalars(select(EvidenceRecord).where(EvidenceRecord.node_id == node_id, EvidenceRecord.kind == "canary").order_by(EvidenceRecord.id.desc()).limit(200)).all()
        strikes = s.scalars(select(Strike).where(Strike.node_id == node_id).order_by(Strike.id.desc()).limit(50)).all()
        reg = dict(node.registration or {})
        reg.pop("miner_hotkey", None)
        passed = sum(1 for c in canaries if c.envelope["payload"].get("outcome") == "pass")
        return {
            "node_id": node_id, "status": node.status, "provider": node.provider_name, "region": node.region, "perf": node.perf,
            "registration": {"gpu_ids": reg.get("gpu_ids"), "claimed_spec": reg.get("claimed_spec"), "host_image_version": reg.get("host_image_version"),
                             "registered_block": node.registered_block, "registration_hash": node.registration_hash},
            "qualification": node.qualification, "qualified_at": node.qualified_at,
            "qualification_history": [{"hash": q.content_hash, "finished_at": q.envelope["payload"].get("finished_at"), "outcome": q.envelope["payload"].get("outcome"),
                                       "kind": q.envelope["payload"].get("kind"), "perf": (q.envelope["payload"].get("performance") or {}).get("perf"),
                                       "validator": q.signer} for q in quals],
            "canaries": {"total": len(canaries), "passed": passed, "pass_rate": (passed / len(canaries)) if canaries else None},
            "strikes": [{"reason": x.reason, "at": x.at, "weight": x.weight} for x in strikes], "active_strike_weight": round(active_strikes(s, node_id), 3),
            "critical_fault": node.critical_fault, "funded_slot": node.funded_slot, "operator_owned": node.operator_owned,
            "evidence_url": f"/v1/evidence?node_id={node_id}",
        }


@router.get("/v1/evidence")
def evidence(_db=Depends(db), node_id: str | None = None, instance_id: str | None = None, kind: str | None = None, since_id: int = 0, limit: int = 200):
    with _db.session() as s:
        q = select(EvidenceRecord).where(EvidenceRecord.id > since_id).order_by(EvidenceRecord.id).limit(min(limit, 1000))
        if node_id:
            q = q.where(EvidenceRecord.node_id == node_id)
        if instance_id:
            q = q.where(EvidenceRecord.instance_id == instance_id)
        if kind:
            q = q.where(EvidenceRecord.kind == kind)
        rows = s.scalars(q).all()
        return {"records": [{"id": r.id, "kind": r.kind, "node_id": r.node_id, "instance_id": r.instance_id, "signer": r.signer,
                             "content_hash": r.content_hash, "envelope": r.envelope, "created_at": r.created_at} for r in rows]}


@router.get("/v1/evidence/{content_hash}")
def evidence_one(content_hash: str, _db=Depends(db)):
    with _db.session() as s:
        r = s.scalar(select(EvidenceRecord).where(EvidenceRecord.content_hash == content_hash))
        if r is None:
            raise HTTPException(404, "not found")
        return {"id": r.id, "kind": r.kind, "node_id": r.node_id, "instance_id": r.instance_id, "signer": r.signer, "envelope": r.envelope, "created_at": r.created_at}
