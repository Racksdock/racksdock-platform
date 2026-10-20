"""Allocation policy for node_id == 'auto' (docs/02 §7)."""
from __future__ import annotations

import itertools

from sqlalchemy import select

from ..models import AllocationLog, Node
from .catalog import node_available

_rr = itertools.count()


def pick_node(session, region: str | None, os_name: str, instance_id: str) -> Node | None:
    cands = []
    for n in session.scalars(select(Node).where(Node.status == "qualified")).all():
        if region and n.region != region:
            continue
        if not n.funded_slot:
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
