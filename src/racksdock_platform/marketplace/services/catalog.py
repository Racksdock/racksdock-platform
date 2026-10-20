from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from racksdock_subnet.core.schemas import NodeAvailability, SpecSummary
from sqlalchemy import select

from ..models import Lease, Node


class Catalog:
    def __init__(self, path: Path | None):
        if path:
            self.data = json.loads(Path(path).read_text())
        else:
            self.data = json.loads(resources.files("racksdock_platform.marketplace").joinpath("catalog.json").read_text())

    @property
    def release(self) -> str:
        return self.data["release"]

    def os_options(self, validator: bool = False) -> list[str]:
        return [i["os"] for i in self.data["images"] if validator or not i.get("validator_only")]

    def version_for(self, os: str) -> str | None:
        for i in self.data["images"]:
            if i["os"] == os:
                return i["version"]
        return None

    @property
    def default_os(self) -> str:
        return self.data.get("default_os", "ubuntu24.04")


def node_badges(n: Node) -> list[str]:
    b = []
    q = n.qualification or {}
    if q.get("attestation_ok"):
        b.append("hardware_verified")
    if n.status == "qualified" and n.perf >= 0.90:
        b.append("performance_qualified")
    b += ["dedicated", "not_confidential"]
    if n.operator_owned:
        b.append("operator_owned")
    return b


def node_spec_summary(n: Node) -> SpecSummary:
    q = (n.qualification or {}).get("spec_measured") or {}
    reg = (n.registration or {}).get("claimed_spec") or {}
    cpu = q.get("cpu") or {}
    nvme = q.get("nvme") or {}
    gpu = q.get("gpu") or {}
    net = q.get("network") or {}
    return SpecSummary(
        vcpus=int(cpu.get("vcpus") or reg.get("vcpus") or 0),
        cpu_model=str(cpu.get("model") or reg.get("cpu_model") or ""),
        memory_in_gb=int(q.get("memory_gb") or reg.get("memory_gb") or 0),
        storage_in_gb=int(float(nvme.get("capacity_tb") or reg.get("nvme_tb") or 0) * 1000),
        vram_per_gpu_in_gb=float(gpu.get("vram_gb_usable_min") or 0),
        network_gbps=float(net.get("down_gbps") or reg.get("network_gbps") or 0),
    )


def node_available(session, n: Node) -> bool:
    if n.status != "qualified" or not n.host_connected or n.critical_fault or n.needs_readiness:
        return False
    if n.maintenance_until:
        return False
    live = session.scalar(select(Lease).where(Lease.node_id == n.node_id, Lease.released_at.is_(None)))
    return live is None


def availability(session, catalog: Catalog, base_url: str, include_unqualified: bool = False) -> list[NodeAvailability]:
    out = []
    for n in session.scalars(select(Node)).all():
        if n.status not in ("qualified",) and not include_unqualified:
            continue
        out.append(NodeAvailability(
            node_id=n.node_id, provider=n.provider_name or f"miner-{n.miner_hotkey[:6]}", region=n.region,
            available=node_available(session, n), hourly_price_cents=n.hourly_price_cents, os_options=catalog.os_options(),
            spec=node_spec_summary(n), badges=node_badges(n), perf=round(n.perf, 3), uptime_30d=round(n.uptime_30d, 4),
            verification_url=f"{base_url}/nodes/{n.node_id}", funded_slot=n.funded_slot, operator_owned=n.operator_owned,
        ))
    return out
