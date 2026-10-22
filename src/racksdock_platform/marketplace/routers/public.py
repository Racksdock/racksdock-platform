from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from racksdock_subnet.core.profile import current as current_profile
from racksdock_subnet.core.profile import deployment_modes
from racksdock_subnet.core.profile import get as profile_get
from sqlalchemy import select

from ..models import EvidenceRecord, Node, Strike
from ..services.catalog import availability, progress_view
from ..services.records import active_strikes
from .deps import catalog, db, settings

router = APIRouter(tags=["public"])


@router.get("/v1/platform/key")
def platform_key(request: Request):
    """The platform's signing key (hex). Light validators pin it and verify every scoring snapshot against it."""
    return {"platform_key": request.app.state.platform_key.public.hex}


@router.get("/v1/scoring/snapshot")
def scoring_snapshot(request: Request, _db=Depends(db), st=Depends(settings), tempo_s: float = 300.0):
    """Platform-signed inputs for one scoring tempo (docs/04 §6): what the verifying validators observed, per node, so a light
    validator can run the deterministic scorer and set weights without touching any node."""
    from ..services.scoring import build_snapshot

    def verification_of(node_id: str) -> dict:
        try:
            return node_verification(node_id, _db=_db)
        except HTTPException:
            return {}

    env = build_snapshot(_db, verification_of, request.app.state.platform_key, st.default_funded_slots, max(60.0, min(tempo_s, 7 * 24 * 3600.0)))
    return {"envelope": env.model_dump(), "platform_key": request.app.state.platform_key.public.hex}


@router.get("/v1/catalog")
def catalog_info(cat=Depends(catalog), st=Depends(settings)):
    """The image catalog: release, where hosts fetch it, and the templates with their metadata and built kinds."""
    return {"release": cat.release, "catalog_url": f"{st.base_url}/catalog/current", "default_os": cat.default_os,
            "templates": [t for t in cat.templates if not t["validator_only"]]}


@router.get("/v1/instances/types")
def instance_types(request: Request, _db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    with _db.session() as s:
        avail = availability(s, cat, st.base_url)
    # one instance type per hardware profile that has a qualified node (the environment's default profile when there is none yet)
    names = sorted({a.profile for a in avail}) or [current_profile().name]
    types = []
    for name in names:
        prof = profile_get(name)
        types.append({
            "shade_instance_type": prof.instance_type, "gpu_type": prof.gpu_model, "num_gpus": prof.gpu_count, "interconnect": prof.interconnect, "profile": prof.name,
            "trust_mode": st.trust.mode, "os_options": cat.os_options(), "templates": [t for t in cat.templates if not t["validator_only"]], "catalog_release": cat.release,
            "deployments": sorted({m for a in avail if a.profile == name for m in deployment_modes(a.deployment)}),
            "availability": [a.model_dump() for a in avail if a.profile == name],
        })
    return {"instance_types": types}


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
            "profile": node.profile, "instance_type": profile_get(node.profile).instance_type, "deployment": node.deployment,
            "registration": {"gpu_ids": reg.get("gpu_ids"), "claimed_spec": reg.get("claimed_spec"), "host_image_version": reg.get("host_image_version"),
                             "registered_block": node.registered_block, "registration_hash": node.registration_hash},
            "qualification": node.qualification, "qualified_at": node.qualified_at,
            "qualification_history": [{"hash": q.content_hash, "finished_at": q.envelope["payload"].get("finished_at"), "outcome": q.envelope["payload"].get("outcome"),
                                       "kind": q.envelope["payload"].get("kind"), "perf": (q.envelope["payload"].get("performance") or {}).get("perf"),
                                       "validator": q.signer} for q in quals],
            "canaries": {"total": len(canaries), "passed": passed, "pass_rate": (passed / len(canaries)) if canaries else None},
            "strikes": [{"reason": x.reason, "at": x.at, "weight": x.weight} for x in strikes], "active_strike_weight": round(active_strikes(s, node_id), 3),
            "progress": progress_view(s, node_id),
            "critical_fault": node.critical_fault, "funded_slot": node.funded_slot, "operator_owned": node.operator_owned,
            "evidence_url": f"/v1/evidence?node_id={node_id}",
        }


@router.get("/v1/evidence")
def evidence(_db=Depends(db), node_id: str | None = None, instance_id: str | None = None, kind: str | None = None, since_id: int = 0, limit: int = 200,
             desc: bool = False):
    """Signed records. Ascending by id from since_id (pagination), or the newest `limit` records with desc=true (scoring windows)."""
    with _db.session() as s:
        q = select(EvidenceRecord).where(EvidenceRecord.id > since_id).order_by(EvidenceRecord.id.desc() if desc else EvidenceRecord.id).limit(min(limit, 1000))
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
