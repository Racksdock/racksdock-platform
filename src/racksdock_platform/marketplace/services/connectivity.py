"""Host connectivity: outages, tenant alerts and recovery (docs/02 §8).

A node is offered for rent only while its host agent is connected. When the agent drops (socket closed, or no heartbeat for
host_offline_after_s) the node is hidden from the listing at once; after the grace period the outage is recorded, every tenant
instance running on the node goes `degraded` with a clear "host offline" reason (billing pauses, the instance stays until the
host returns or the tenant deletes it), and validators see the outage so offline time never earns idle pay. When the host is
back, an instance it still runs returns to `active`; an idle node that was gone long enough gets a readiness canary first.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..models import HostOutage, Incident, Instance, Lease, Node, now_iso

log = logging.getLogger("racksdock.marketplace.connectivity")


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def open_outage(s, node_id: str) -> HostOutage | None:
    return s.scalar(select(HostOutage).where(HostOutage.node_id == node_id, HostOutage.ended_at.is_(None)))


def tenant_rented(s, node_id: str) -> bool:
    return s.scalar(select(Lease).where(Lease.node_id == node_id, Lease.released_at.is_(None), Lease.is_validator.is_(False))) is not None


def declare_offline(s, node: Node, reason: str = "") -> HostOutage:
    """Record the outage (idempotent) and alert tenants: active instances on the node become degraded with the reason."""
    node.host_connected = False
    started = node.offline_since or now_iso()
    node.offline_since = started
    existing = open_outage(s, node.node_id)
    if existing is not None:
        return existing
    affected = []
    for inst in s.scalars(select(Instance).where(Instance.node_id == node.node_id, Instance.mode == "tenant", Instance.status == "active")).all():
        inst.status = "degraded"
        inst.status_details = (f"host offline since {started}: the server's host agent is unreachable. Billing is paused; the instance "
                               "comes back by itself when the host returns, or you can delete it.")
        s.add(Incident(instance_id=inst.id, node_id=node.node_id, klass="host_offline", details=f"host agent unreachable ({reason})", source="platform"))
        affected.append(inst.id)
    outage = HostOutage(node_id=node.node_id, started_at=started, rented=bool(affected), affected=affected, reason=reason)
    s.add(outage)
    log.warning("node %s offline since %s (%s); %d tenant instance(s) degraded", node.node_id[:16], started, reason, len(affected))
    return outage


def declare_online(s, node: Node, settings) -> None:
    """The host is talking to us again: close the outage; an idle node that was away long enough must pass a readiness canary."""
    now = now_iso()
    node.host_connected = True
    node.last_heartbeat_at = now
    outage = open_outage(s, node.node_id)
    if outage is not None:
        outage.ended_at = now
        gone_s = (_ts(now) - _ts(outage.started_at)).total_seconds()
        if gone_s >= settings.offline_readiness_s and not tenant_rented(s, node.node_id):
            node.needs_readiness = True
        log.info("node %s back online after %ds", node.node_id[:16], int(gone_s))
    node.offline_since = None


def recover_or_lose(s, node: Node, instance_id: str | None, vm_state: str | None, release_lease) -> None:
    """With the host back, a degraded (host offline) tenant instance either still runs on it -> active again, or is gone -> error."""
    for inst in s.scalars(select(Instance).where(Instance.node_id == node.node_id, Instance.status == "degraded")).all():
        inc = s.scalar(select(Incident).where(Incident.instance_id == inst.id, Incident.klass == "host_offline", Incident.closed_at.is_(None)))
        if inc is None:
            continue
        if instance_id == inst.id and vm_state == "running":
            inst.status = "active"
            inst.status_details = None
            inc.closed_at = now_iso()
            log.info("instance %s recovered: host back online", inst.id)
        elif instance_id != inst.id and vm_state in (None, "shut off", "none"):
            inst.status = "error"
            inst.status_details = f"the host came back without this instance (offline since {inc.opened_at}); nothing billed since then"
            inc.closed_at = now_iso()
            release_lease(s, inst.id)
            node.needs_readiness = True
            log.warning("instance %s lost: host returned without it", inst.id)


def sweep(s, settings, now: dt.datetime | None = None) -> list[str]:
    """Periodic: stale heartbeats or a dropped socket older than host_offline_after_s become outages. Returns node ids declared."""
    now = now or dt.datetime.now(dt.UTC)
    declared = []
    for node in s.scalars(select(Node)).all():
        stale = bool(node.last_heartbeat_at) and (now - _ts(node.last_heartbeat_at)).total_seconds() > settings.host_offline_after_s
        if node.host_connected and stale:
            node.host_connected = False
            node.offline_since = node.offline_since or node.last_heartbeat_at
        elif not node.host_connected and not node.offline_since and stale:
            node.offline_since = node.last_heartbeat_at  # dropped before this platform version tracked it: date it from the last heartbeat
        gone = not node.host_connected and node.offline_since and (now - _ts(node.offline_since)).total_seconds() > settings.host_offline_after_s
        if gone and open_outage(s, node.node_id) is None:
            declare_offline(s, node, reason="stale heartbeats" if stale else "connection dropped")
            declared.append(node.node_id)
    return declared


def outages_for(s, node_id: str, since: dt.datetime) -> list[dict]:
    rows = s.scalars(select(HostOutage).where(HostOutage.node_id == node_id).order_by(HostOutage.id.desc()).limit(200)).all()
    out = []
    for o in rows:
        if o.ended_at is not None and _ts(o.ended_at) < since:
            continue
        out.append({"started_at": o.started_at, "ended_at": o.ended_at, "rented": o.rented, "reason": o.reason})
    return out
