"""Allocation policy for node_id == 'auto' (docs/02 §7)."""
from __future__ import annotations

import itertools

from sqlalchemy import select

from ..models import AllocationLog, Node
from .catalog import node_available

_rr = itertools.count()


def pick_node(session, region: str | None, os_name: str, instance_id: str, instance_type: str | None = None, deployment: str | None = None) -> Node | None:
    """instance_type: a profile name or shade_instance_type (e.g. B300x8); deployment: vm | container. Either narrows 'auto'."""
    from racksdock_subnet.core.profile import available, deployment_modes, load

    wanted = None
    if instance_type:
        wanted = {name for name in available() if name == instance_type or load(name).instance_type.lower() == instance_type.lower()}
    cands = []
    for n in session.scalars(select(Node).where(Node.status == "qualified")).all():
        if region and not (n.region == region or n.region.startswith(region + "-")):
            continue
        if wanted is not None and n.profile not in wanted:
            continue
        if deployment and deployment not in deployment_modes(n.deployment):
            continue
        if not node_available(session, n):
            continue
        cands.append(n)
    if not cands:
        session.add(AllocationLog(instance_id=instance_id, candidates=[], chosen=None, reason="no candidates"))
        return None
    # rank: perf desc, uptime desc; ties broken round-robin (operator-owned nodes get no preference)
    cands.sort(key=lambda n: (-round(n.perf, 2), -round(n.uptime_30d, 3)))
    top_key = (round(cands[0].perf, 2), round(cands[0].uptime_30d, 3))
    ties = [n for n in cands if (round(n.perf, 2), round(n.uptime_30d, 3)) == top_key]
    chosen = ties[next(_rr) % len(ties)]
    session.add(AllocationLog(instance_id=instance_id, candidates=[n.node_id for n in cands], chosen=chosen.node_id,
                              reason=f"perf={chosen.perf:.2f} uptime={chosen.uptime_30d:.3f} ties={len(ties)}"))
    return chosen
