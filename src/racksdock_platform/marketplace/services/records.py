"""Evidence store and ingestion of signed records (registration, qualification, canary, in-rental, probes, strikes)."""
from __future__ import annotations

import logging
from typing import Any

from racksdock_subnet.core.canonical import hash_obj
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.profile import available as available_profiles
from racksdock_subnet.core.profile import get as profile_get
from racksdock_subnet.core.schemas import CanaryResult, InRentalCheckResult, QualificationRecord, RegistrationRecord
from sqlalchemy import select

from ..models import EvidenceRecord, Node, Strike

log = logging.getLogger("racksdock.marketplace.records")


def store_envelope(session, env: SignedEnvelope, kind: str, node_id: str | None = None, instance_id: str | None = None) -> bool:
    """Keep one copy of a signed envelope; returns False when this exact envelope is already on file.

    Only the duplicate is tolerated. Any other integrity failure (for example a row the flush cannot insert) propagates so the
    request fails loudly instead of answering 201 for a write the database rolled back.
    """
    if session.scalar(select(EvidenceRecord.id).where(EvidenceRecord.content_hash == env.content_hash)) is not None:
        return False
    rec = EvidenceRecord(kind=kind, node_id=node_id, instance_id=instance_id, signer=env.signer, content_hash=env.content_hash, envelope=env.model_dump())
    session.add(rec)
    session.flush()
    return True


def ingest_registration(session, env: SignedEnvelope, chain, default_price_cents: int) -> Node:
    if not env.verify() or env.kind != "registration":
        raise ValueError("invalid registration envelope")
    rec = RegistrationRecord.model_validate(env.payload)
    if env.signer != rec.miner_service_key:
        raise ValueError("registration must be signed by miner_service_key")
    if rec.profile not in available_profiles():
        raise ValueError(f"unknown profile {rec.profile!r}; this platform knows {', '.join(available_profiles())}")
    prof = profile_get(rec.profile)
    if len(rec.gpu_ids) != prof.gpu_count:
        raise ValueError(f"registration has {len(rec.gpu_ids)} gpu ids but profile {prof.name} expects {prof.gpu_count}")
    rhash = hash_obj(rec.model_dump(by_alias=True))
    # on-chain commitment check: the committing hotkey must be the record's hotkey
    block = None
    for c in chain.commitments():
        if c.hotkey == rec.miner_hotkey and c.data == rhash:
            block = c.block
            break
    if block is None:
        raise ValueError("no on-chain commitment matching this registration record")
    # gpu_id ownership: first commitment wins (docs/03 §2.4)
    for other in session.scalars(select(Node).where(Node.node_id != rec.node_id)).all():
        overlap = set(other.registration.get("gpu_ids", [])) & set(rec.gpu_ids)
        if overlap and other.registered_block <= block and other.status != "retired":
            raise ValueError(f"gpu identities already registered under node {other.node_id[:16]}")
    node = session.get(Node, rec.node_id)
    if node is None:
        node = Node(node_id=rec.node_id, miner_hotkey=rec.miner_hotkey, miner_service_key=rec.miner_service_key,
                    provider_name=f"miner-{rec.miner_hotkey[:6]}", region=rec.claimed_spec.region, registration=rec.model_dump(by_alias=True),
                    registration_hash=rhash, registered_block=block, hourly_price_cents=default_price_cents, profile=rec.profile, deployment=rec.deployment)
        session.add(node)
    else:
        node.registration = rec.model_dump(by_alias=True)
        node.miner_hotkey = rec.miner_hotkey  # a re-registration may move the node to another hotkey (its on-chain commitment proves the owner)
        node.profile = rec.profile
        node.deployment = rec.deployment
        node.registration_hash = rhash
        node.registered_block = block
        node.miner_service_key = rec.miner_service_key
        node.region = rec.claimed_spec.region
        node.status = "registered"
        node.needs_readiness = True
    store_envelope(session, env, "registration", node_id=rec.node_id)
    return node


def ingest_qualification(session, env: SignedEnvelope) -> QualificationRecord:
    if not env.verify() or env.kind != "qualification":
        raise ValueError("invalid qualification envelope")
    q = QualificationRecord.model_validate(env.payload)
    if env.signer != q.validator_key:
        raise ValueError("qualification must be signed by validator_key")
    node = session.get(Node, q.node_id)
    if node is None:
        raise ValueError("unknown node")
    if set(q.attestation.gpu_ids) != set(node.registration.get("gpu_ids", [])):
        raise ValueError("qualification gpu_ids do not match registration")
    store_envelope(session, env, "qualification", node_id=q.node_id, instance_id=q.instance_id)
    summary: dict[str, Any] = {
        "kind": q.kind, "outcome": q.outcome, "finished_at": q.finished_at, "validator_key": q.validator_key,
        "attestation_ok": True, "perf": q.performance.perf, "spec_measured": q.spec_measured.model_dump(),
        "failing_checks": q.failing_checks, "reference_set": q.reference_set, "verify_image": q.verify_image,
    }
    if q.outcome == "pass":
        if q.kind == "full":
            node.status = "qualified"
            node.qualified_at = q.finished_at
            node.qualification = {**summary, "admission": (node.qualification or {}).get("admission")}
            node.perf = q.performance.perf
            node.critical_fault = None
        elif q.kind == "admission":
            # admitted, not yet listed: the full qualification (measured spec, perf) follows; a previously measured spec is kept
            # on the node so the console keeps showing real figures while it is re-verified
            if node.status != "qualified":
                node.status = "admitted"
            node.qualification = {**(node.qualification or {}), "admission": summary}
            node.critical_fault = None
        node.needs_readiness = False
    else:
        if q.kind in ("full", "admission"):
            node.status = "quarantined"
            node.qualification = {**(node.qualification or {}), "last_failed": summary}
        else:
            node.needs_readiness = True
    return q


def ingest_canary(session, env: SignedEnvelope) -> CanaryResult:
    if not env.verify() or env.kind != "canary":
        raise ValueError("invalid canary envelope")
    c = CanaryResult.model_validate(env.payload)
    if env.signer != c.validator_key:
        raise ValueError("canary must be signed by validator_key")
    store_envelope(session, env, "canary", node_id=c.node_id, instance_id=c.instance_id)
    node = session.get(Node, c.node_id)
    if node is not None:
        if c.outcome == "pass":
            node.needs_readiness = False
        elif c.outcome in ("fail", "refused"):
            session.add(Strike(node_id=c.node_id, reason=f"canary_{c.outcome}", source=c.validator_key[:12]))
            if c.gpu_ids_match is False:
                node.critical_fault = "gpu set changed"
    return c


def ingest_inrental(session, env: SignedEnvelope) -> InRentalCheckResult:
    if not env.verify() or env.kind != "inrental":
        raise ValueError("invalid in-rental envelope")
    r = InRentalCheckResult.model_validate(env.payload)
    if env.signer != r.validator_key:
        raise ValueError("in-rental check must be signed by validator_key")
    store_envelope(session, env, "inrental", node_id=r.node_id, instance_id=r.instance_id)
    if r.attestation_ok and not r.gpu_ids_match:
        node = session.get(Node, r.node_id)
        if node is not None:
            node.critical_fault = "gpu set changed during rental"
            session.add(Strike(node_id=r.node_id, reason="inrental_identity_change", source=r.validator_key[:12]))
    return r


def active_strikes(session, node_id: str, days: int = 30) -> float:
    """Strikes decay linearly over `days` (docs/04 §7)."""
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    total = 0.0
    for s in session.scalars(select(Strike).where(Strike.node_id == node_id)).all():
        at = dt.datetime.fromisoformat(s.at.replace("Z", "+00:00"))
        age = (now - at).total_seconds() / 86400
        if age < days:
            total += s.weight * (1 - age / days)
    return total
