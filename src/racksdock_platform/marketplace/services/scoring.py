"""The platform-signed scoring snapshot (docs/04 §6): everything a validator needs to compute this tempo's weights with the
deterministic scorer. Built from the platform's own tables with the same `build_inputs` function the verifier uses, so every
validator reaches exactly the weights the evidence implies, without touching any node."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict

from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.keys import SigningKey
from racksdock_subnet.validator.snapshot import build_inputs
from sqlalchemy import select

from ..models import EvidenceRecord, Instance, Lease, Node, ScoringRound, Setting
from .connectivity import outages_for

SNAPSHOT_KIND = "scoring_snapshot"
EVIDENCE_KINDS = ("canary", "inrental", "probe")


def node_rows(s) -> list[dict]:
    out = []
    for n in s.scalars(select(Node)).all():
        live = s.scalar(select(Lease).where(Lease.node_id == n.node_id, Lease.released_at.is_(None)))
        out.append({
            "node_id": n.node_id, "miner_hotkey": n.miner_hotkey, "miner_service_key": n.miner_service_key, "status": n.status, "region": n.region,
            "registration": n.registration, "registration_hash": n.registration_hash, "registered_block": n.registered_block,
            "qualified_at": n.qualified_at, "qualification": n.qualification, "perf": n.perf, "host_connected": n.host_connected, "last_heartbeat_at": n.last_heartbeat_at,
            "offline_since": n.offline_since, "outages": outages_for(s, n.node_id, dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)),
            "needs_readiness": n.needs_readiness, "critical_fault": n.critical_fault, "maintenance_until": n.maintenance_until, "funded_slot": n.funded_slot,
            "live_lease": ({"instance_id": live.instance_id, "mode": live.mode, "is_validator": live.is_validator, "created_at": live.created_at, "active_at": live.active_at} if live else None),
        })
    return out


def lease_rows(s, live_only: bool = False) -> list[dict]:
    q = select(Lease)
    if live_only:
        q = q.where(Lease.released_at.is_(None))
    out = []
    for lease in s.scalars(q.order_by(Lease.created_at)).all():
        inst = s.get(Instance, lease.instance_id)
        out.append({"lease_id": lease.lease_id, "instance_id": lease.instance_id, "node_id": lease.node_id, "mode": lease.mode, "is_validator": lease.is_validator,
                    "created_at": lease.created_at, "active_at": lease.active_at, "released_at": lease.released_at,
                    "status": inst.status if inst else None, "ip": inst.ip if inst else None, "ssh_port": inst.ssh_port if inst else None,
                    "node_agent_pubkey": inst.node_agent_pubkey if inst else None})
    return out


def evidence_rows(s, window_start: dt.datetime, limit: int = 1000) -> list[dict]:
    """The records a scoring window needs: per kind, everything since the shared cut-off (racksdock_subnet evidence_window), newest
    `limit` per kind. Exactly what an all-in-one verifier fetches from /v1/evidence?kind=&since=, so both see the same data."""
    from racksdock_subnet.validator.snapshot import evidence_window

    out = []
    for kind, since in evidence_window(window_start).items():
        rows = s.scalars(select(EvidenceRecord).where(EvidenceRecord.kind == kind, EvidenceRecord.created_at >= since).order_by(EvidenceRecord.id.desc()).limit(limit)).all()
        out += [{"id": r.id, "kind": r.kind, "node_id": r.node_id, "instance_id": r.instance_id, "signer": r.signer, "content_hash": r.content_hash,
                 "envelope": r.envelope, "created_at": r.created_at} for r in rows]
    out.sort(key=lambda e: e["id"])
    return out


def last_funded(s) -> list[str]:
    """The funded set of the most recent published scoring record (hysteresis input shared by every validator)."""
    r = s.scalar(select(EvidenceRecord).where(EvidenceRecord.kind == "scoring").order_by(EvidenceRecord.id.desc()).limit(1))
    if r is None:
        return []
    return list((r.envelope.get("payload") or {}).get("funded") or [])


def funded_slots(s, default: int) -> int:
    fs = s.get(Setting, "funded_slots")
    return int(fs.value) if fs else default


def aligned_window(now: dt.datetime, tempo_s: float) -> tuple[dt.datetime, dt.datetime]:
    """Windows are aligned to multiples of tempo_s on the UTC epoch, so every validator asking during one tempo gets the same
    window, hence the same snapshot and the same weights, however far apart their requests are."""
    end_ts = (int(now.timestamp()) // int(tempo_s)) * int(tempo_s)
    end = dt.datetime.fromtimestamp(end_ts, dt.UTC)
    return end - dt.timedelta(seconds=tempo_s), end


def previous_funded(s, window_end: dt.datetime) -> list[str]:
    """The funded set of the latest platform round before this window (hysteresis input); legacy scoring records as fallback."""
    r = s.scalar(select(ScoringRound).where(ScoringRound.window_end < _iso(window_end)).order_by(ScoringRound.window_end.desc()).limit(1))
    if r is not None:
        return list(r.funded or [])
    return last_funded(s)


def scoring_params(s, default_funded_slots: int) -> dict:
    """Every scorer parameter a validator applies, so an auditor can reproduce the weights from the public inputs."""
    from racksdock_subnet.validator.scorer import Params

    p = Params(funded_slots=funded_slots(s, default_funded_slots))
    return asdict(p)


def _iso(t: dt.datetime) -> str:
    return t.isoformat()


def build_snapshot(db, verification_of, platform_key: SigningKey, default_funded_slots: int, tempo_s: float, now: dt.datetime | None = None) -> SignedEnvelope:
    """verification_of(node_id) -> the public verification dict (strikes etc.). The window is tempo-aligned; the platform records
    its own scoring of the window the first time it is built (ScoringRound), which feeds the next window's previously_funded."""
    from racksdock_subnet.validator.scorer import Params, score_tempo

    now = now or dt.datetime.now(dt.UTC)
    window_start, window_end = aligned_window(now, tempo_s)
    with db.session() as s:
        nodes = node_rows(s)
        leases = lease_rows(s)
        evidence = evidence_rows(s, window_start)
        prev = set(previous_funded(s, window_end))
        slots = funded_slots(s, default_funded_slots)
    verification = {n["node_id"]: verification_of(n["node_id"]) for n in nodes}
    inputs = build_inputs(nodes, leases, evidence, verification, window_start, window_end, "", prev)  # "" = pool every validator's checks
    rented = sum(i.minutes_rented for i in inputs)
    avail = sum(i.minutes_available for i in inputs)
    util = (rented / (rented + avail)) if (rented + avail) > 0 else 0.0
    payload = {
        "schema": "racksdock.scoring-snapshot.v1", "window_start": _iso(window_start), "window_end": _iso(window_end), "tempo_s": tempo_s,
        "funded_slots": slots, "utilization_7d": util, "previously_funded": sorted(prev),
        "inputs": [asdict(i) for i in inputs],
    }
    json.dumps(payload)  # must be JSON-serializable (dataclasses hold plain values)
    with db.session() as s:
        if s.get(ScoringRound, _iso(window_end)) is None:
            res = score_tempo(inputs, window_end, tempo_s / 60, Params(funded_slots=slots), utilization_7d=util)
            s.add(ScoringRound(window_end=_iso(window_end), window_start=_iso(window_start), tempo_s=tempo_s, funded=res.funded,
                               hotkey_weights=res.hotkey_weights, burn=res.burn))
    return SignedEnvelope.create(SNAPSHOT_KIND, payload, platform_key)


def rounds(s, limit: int = 50) -> list[dict]:
    rows = s.scalars(select(ScoringRound).order_by(ScoringRound.window_end.desc()).limit(limit)).all()
    return [{"window_start": r.window_start, "window_end": r.window_end, "tempo_s": r.tempo_s, "funded": r.funded, "hotkey_weights": r.hotkey_weights,
             "burn": r.burn, "computed_at": r.computed_at} for r in rows]
