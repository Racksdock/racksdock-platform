from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from racksdock_subnet.core.envelope import SignedEnvelope
from sqlalchemy import select

from ..models import EvidenceRecord, Node
from ..services.records import ingest_registration
from .deps import chain, db, orch, settings

router = APIRouter(prefix="/v1/miners", tags=["miners"])


class RegisterBody(BaseModel):
    envelope: SignedEnvelope  # kind "registration" signed by miner_service_key


@router.post("/register", status_code=201)
async def register(body: RegisterBody, _db=Depends(db), ch=Depends(chain), st=Depends(settings), o=Depends(orch)):
    with _db.session() as s:
        try:
            node = ingest_registration(s, body.envelope, ch, st.default_price_cents)
        except ValueError as e:
            raise HTTPException(400, str(e))
        node_id = node.node_id
        key = node.miner_service_key
    # if the host is already connected under this service key, bind it now
    sess = o.hosts.by_key.get(key)
    if sess is not None:
        o.hosts.bind_node(sess, node_id)
        with _db.session() as s:
            n = s.get(Node, node_id)
            n.host_connected = True
        await o.locate(node_id, sess.peer_ip)
    return {"node_id": node_id, "status": "registered"}


@router.get("/identity-drafts/{host_pubkey}")
def identity_drafts(host_pubkey: str, _db=Depends(db), o=Depends(orch)):
    """Registration drafts produced by a host-agent's bootstrap_identity, for the miner neuron to sign and commit, plus the host's
    latest log/error notes per bootstrap command (what it waits for, why a launch failed)."""
    notes = [{"command_id": cid, **n} for cid, ns in o.command_notes.items() for n in ns if n["host"] == host_pubkey]
    with _db.session() as s:
        rows = s.scalars(select(EvidenceRecord).where(EvidenceRecord.kind.in_(["identity_draft", "identity_draft_rejected"]), EvidenceRecord.signer == host_pubkey)
                         .order_by(EvidenceRecord.id.desc()).limit(10)).all()
        return {"drafts": [{**r.envelope["payload"], "created_at": r.created_at} for r in rows], "notes": notes[-40:]}


class MaintenanceBody(BaseModel):
    node_id: str
    starts_at: str
    ends_at: str
    envelope: SignedEnvelope | None = None


@router.post("/maintenance")
async def maintenance(body: MaintenanceBody, _db=Depends(db), o=Depends(orch)):
    with _db.session() as s:
        n = s.get(Node, body.node_id)
        if n is None:
            raise HTTPException(404, "node not found")
        if body.envelope is None or not body.envelope.verify(expected_signer=n.miner_service_key):
            raise HTTPException(403, "must be signed by the node's service key")
        n.maintenance_until = body.ends_at
    try:
        await o.hosts.send(body.node_id, "announce_maintenance", {"starts_at": body.starts_at, "ends_at": body.ends_at})
    except ConnectionError:
        pass
    return {"node_id": body.node_id, "maintenance_until": body.ends_at}


@router.post("/bootstrap/{host_pubkey}")
async def bootstrap(host_pubkey: str, o=Depends(orch), image_version: str | None = None, reuse: bool = False):
    """Ask a connected host to boot a verify VM and produce its identity draft. reuse=true: re-sign the identity it already proved
    with its current tier/profile/spec (no VM: a registered node changes its offer, even while a rental runs)."""
    try:
        cid = await o.hosts.send_to_key(host_pubkey, "bootstrap_identity", {"image": "verify", "image_version": image_version or o.catalog.version_for("verify"), "reuse": reuse})
    except ConnectionError:
        raise HTTPException(404, "host not connected: start racksdock-host-agent on the node and point RACKSDOCK_PLATFORM_WS at this platform")
    return {"command_id": cid}
