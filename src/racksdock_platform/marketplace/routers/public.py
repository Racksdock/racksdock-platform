from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from racksdock_subnet.core.profile import current as current_profile
from racksdock_subnet.core.profile import deployment_modes
from racksdock_subnet.core.profile import get as profile_get
from sqlalchemy import select

from ..models import EvidenceRecord, Lease, Node, Strike
from ..services.catalog import availability, progress_view
from ..services.connectivity import outages_for
from ..services.records import active_strikes
from .deps import catalog, db, settings

router = APIRouter(tags=["public"])


@router.get("/v1/platform/key")
def platform_key(request: Request):
    """The platform's signing key (hex). Validators pin it and verify every scoring snapshot against it."""
    return {"platform_key": request.app.state.platform_key.public.hex}


@router.get("/v1/scoring/snapshot")
def scoring_snapshot(request: Request, _db=Depends(db), st=Depends(settings), tempo_s: float = 300.0):
    """Platform-signed inputs for one scoring tempo (docs/04 §6): what the verifier observed, per node, so a validator can run
    the deterministic scorer and set weights without touching any node."""
    from ..services.scoring import build_snapshot

    def verification_of(node_id: str) -> dict:
        try:
            return node_verification(node_id, _db=_db)
        except HTTPException:
            return {}

    env = build_snapshot(_db, verification_of, request.app.state.platform_key, max(60.0, min(tempo_s, 7 * 24 * 3600.0)))
    return {"envelope": env.model_dump(), "platform_key": request.app.state.platform_key.public.hex}


@router.get("/v1/scoring/params")
def scoring_params_route():
    """Every scorer parameter in force (weights, floors, strikes), so anyone can reproduce the weights from public data."""
    from ..services.scoring import scoring_params

    return scoring_params()


@router.get("/v1/scoring/rounds")
def scoring_rounds(limit: int = 50, _db=Depends(db)):
    """The platform's own scoring per tempo-aligned window (scored set, hotkey weights, burn): what every validator should match."""
    from ..services.scoring import rounds

    with _db.session() as s:
        return {"rounds": rounds(s, min(limit, 500))}


@router.get("/v1/leases")
def leases_public(live_only: bool = False, since: str | None = None, limit: int = 2000, _db=Depends(db)):
    """The lease ledger (docs/02 §5): which node was leased when, by a tenant or the verifier. No tenant data, no addresses."""
    q = select(Lease).order_by(Lease.created_at)
    if live_only:
        q = q.where(Lease.released_at.is_(None))
    if since:
        q = q.where((Lease.released_at.is_(None)) | (Lease.released_at >= since))
    with _db.session() as s:
        rows = s.scalars(q.limit(min(limit, 5000))).all()
        return {"leases": [{"lease_id": x.lease_id, "instance_id": x.instance_id, "node_id": x.node_id, "mode": x.mode, "is_validator": x.is_validator,
                            "created_at": x.created_at, "active_at": x.active_at, "released_at": x.released_at} for x in rows]}


@router.get("/v1/catalog")
def catalog_info(cat=Depends(catalog), st=Depends(settings)):
    """The image catalog: release, where hosts fetch it, and the templates with their metadata and built kinds."""
    return {"release": cat.release, "catalog_url": f"{st.base_url}/catalog/current", "default_os": cat.default_os,
            "templates": [t for t in cat.templates if not t["validator_only"]]}


@router.get("/v1/instances/types")
def instance_types(request: Request, _db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    with _db.session() as s:
        avail = availability(s, cat, st.ui_url or st.base_url)
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
def nodes(include_offline: bool = False, _db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    """Every node with its status. Offline nodes nobody rents are left out unless include_offline=true (operators, miners)."""
    with _db.session() as s:
        return {"nodes": [a.model_dump(exclude={"hourly_price_cents"}) for a in availability(s, cat, st.ui_url or st.base_url, include_unqualified=True, include_offline=include_offline)]}


@router.get("/v1/sitemap.xml", include_in_schema=False)
def sitemap(_db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    """The console's sitemap: its public pages plus one page per listed server. Served at the console origin by a rewrite."""
    base = st.ui_url or st.base_url
    with _db.session() as s:
        listed = [a for a in availability(s, cat, base, include_offline=True) if a.status == "qualified"]  # a listed server keeps its page while its host is briefly away
    urls = [f"{base}/", f"{base}/miner"] + [a.verification_url for a in listed]
    body = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    body += "".join(f"  <url><loc>{u}</loc></url>\n" for u in urls) + "</urlset>\n"
    return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=600"})


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
            "node_id": node_id, "status": node.status, "provider": node.provider_name, "region": node.region, "region_source": node.region_source or "claimed",
            "location": node.location, "perf": node.perf,
            "miner_hotkey": node.miner_hotkey, "registered_block": node.registered_block, "maintenance_until": node.maintenance_until,
            "profile": node.profile, "instance_type": profile_get(node.profile).instance_type, "deployment": node.deployment,
            "registration": {"gpu_ids": reg.get("gpu_ids"), "claimed_spec": reg.get("claimed_spec"), "host_image_version": reg.get("host_image_version"),
                             "registered_block": node.registered_block, "registration_hash": node.registration_hash},
            "qualification": node.qualification, "qualified_at": node.qualified_at,
            "qualification_history": [{"hash": q.content_hash, "finished_at": q.envelope["payload"].get("finished_at"), "outcome": q.envelope["payload"].get("outcome"),
                                       "kind": q.envelope["payload"].get("kind"), "perf": (q.envelope["payload"].get("performance") or {}).get("perf"),
                                       "validator": q.signer} for q in quals],
            "canaries": {"total": len(canaries), "passed": passed, "pass_rate": (passed / len(canaries)) if canaries else None},
            "strikes": [{"reason": x.reason, "at": x.at, "weight": x.weight} for x in strikes], "active_strike_weight": round(active_strikes(s, node_id), 3),
            "progress": progress_view(s, node_id), "host_connected": bool(node.host_connected), "offline_since": node.offline_since,
            "outages": outages_for(s, node_id, _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=7)),
            "critical_fault": node.critical_fault, "operator_owned": node.operator_owned,
            "evidence_url": f"/v1/evidence?node_id={node_id}",
        }


@router.get("/v1/evidence")
def evidence(_db=Depends(db), node_id: str | None = None, instance_id: str | None = None, kind: str | None = None, since_id: int = 0, limit: int = 200,
             desc: bool | None = None, since: str | None = None):
    """Signed records, newest first by default (what a reader expects; filter with node_id, instance_id, kind). Paginate oldest
    first with since_id (ascending unless desc=true is given). `since` (ISO time) keeps only records created at or after it, so a
    scoring window is bounded by time, not by record count."""
    if desc is None:
        desc = since_id == 0
    with _db.session() as s:
        q = select(EvidenceRecord).where(EvidenceRecord.id > since_id).order_by(EvidenceRecord.id.desc() if desc else EvidenceRecord.id).limit(min(limit, 1000))
        if since:
            q = q.where(EvidenceRecord.created_at >= since)
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
