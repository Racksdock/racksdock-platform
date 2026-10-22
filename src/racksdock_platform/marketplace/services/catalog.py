from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from racksdock_subnet.core.manifest import template_meta
from racksdock_subnet.core.profile import deployment_modes
from racksdock_subnet.core.profile import get as profile_get
from racksdock_subnet.core.schemas import NodeAvailability, SpecSummary
from sqlalchemy import select

from ..models import Lease, Node


class Catalog:
    """The image catalog the marketplace offers. Source: a signed release manifest (manifest.signed.json with catalog.pub next to
    it, as published in the image store) or the legacy catalog.json. Templates = one entry per os/version with the kinds built."""

    def __init__(self, path: Path | None):
        self.templates: list[dict] = []
        if path and Path(path).name == "manifest.signed.json":
            self.data = self._from_signed(Path(path))
        elif path:
            self.data = json.loads(Path(path).read_text())
        else:
            self.data = json.loads(resources.files("racksdock_platform.marketplace").joinpath("catalog.json").read_text())
        if not self.templates:
            self.templates = [{"os": i["os"], "version": i["version"], "label": i.get("label") or template_meta(i["os"])["label"], "kinds": ["vm", "container"],
                               "validator_only": bool(i.get("validator_only")), **{k: template_meta(i["os"])[k] for k in ("base_os", "cuda", "pytorch")}, "size_bytes": {}}
                              for i in self.data["images"]]

    def _from_signed(self, path: Path) -> dict:
        from racksdock_subnet.core.manifest import load_signed_manifest

        pub = (path.parent / "catalog.pub").read_text().strip()
        m = load_signed_manifest(path, pub)
        by_version: dict[str, dict] = {}
        for e in m.images:
            t = by_version.setdefault(e.version, {"os": e.os, "version": e.version, "label": e.label or template_meta(e.os)["label"], "kinds": [],
                                                  "validator_only": e.validator_only, "base_os": e.base_os or template_meta(e.os)["base_os"],
                                                  "cuda": e.cuda or template_meta(e.os)["cuda"], "pytorch": e.pytorch or template_meta(e.os)["pytorch"], "size_bytes": {}})
            t["kinds"].append(e.kind)
            t["size_bytes"][e.kind] = e.size_bytes
        self.templates = sorted(by_version.values(), key=lambda t: (t["validator_only"], t["os"]))
        return {"schema": "racksdock.catalog.v1", "release": m.release, "default_os": "ubuntu24.04",
                "images": [{"os": t["os"], "version": t["version"], "label": t["label"], **({"validator_only": True} if t["validator_only"] else {})} for t in self.templates]}

    def kinds_for(self, os: str) -> list[str]:
        for t in self.templates:
            if t["os"] == os:
                return list(t["kinds"])
        return ["vm", "container"]

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


def progress_view(session, node_id: str) -> dict | None:
    """The node's current/last verification session as the validator reported it, or None."""
    from ..models import VerificationProgress

    row = session.get(VerificationProgress, node_id)
    if row is None:
        return None
    steps = list(row.steps or [])
    done = sum(1 for st in steps if st.get("state") in ("pass", "fail", "skipped"))
    current = next((st for st in steps if st.get("state") == "running"), None)
    return {"session_kind": row.session_kind, "session_id": row.session_id, "validator_key": row.validator_key, "started_at": row.started_at, "updated_at": row.updated_at,
            "outcome": row.outcome, "steps": steps, "done": done, "total": len(steps), "current": current["id"] if current else None,
            "current_name": current.get("name") if current else None, "failed": [st["id"] for st in steps if st.get("state") == "fail"]}


def progress_summary(session, node_id: str) -> dict | None:
    v = progress_view(session, node_id)
    if v is None:
        return None
    return {k: v[k] for k in ("session_kind", "outcome", "done", "total", "current", "current_name", "failed", "updated_at")}


def node_badges(n: Node) -> list[str]:
    b = []
    q = n.qualification or {}
    if q.get("attestation_ok"):
        b.append("hardware_verified")
    if n.status == "qualified" and n.perf >= 0.90:
        b.append("performance_qualified")
    elif n.status == "admitted":
        b.append("verifying")  # admission passed; the full qualification (measured spec, perf) is in progress
    b += ["dedicated", "not_confidential"]
    b += {"vm": ["vm_isolated"], "container": ["container"], "static": ["static_lab"], "dual": ["vm_isolated", "container"]}.get(n.deployment, [n.deployment])
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
        vram_per_gpu_in_gb=float(gpu.get("vram_gb_usable_min") or 0), vram_per_gpu_total_gb=float(gpu.get("vram_gb_total") or 0),
        network_gbps=float(net.get("down_gbps") or reg.get("network_gbps") or 0),
    )


def node_available(session, n: Node) -> bool:
    if n.status != "qualified" or not n.host_connected or n.critical_fault or n.needs_readiness:
        return False
    if n.maintenance_until:
        return False
    live = session.scalar(select(Lease).where(Lease.node_id == n.node_id, Lease.released_at.is_(None)))
    return live is None


def images_by_mode(n: Node, catalog: Catalog) -> dict[str, list[str]]:
    """Catalog templates (os names, tenants' only) present on the node per tier; {} when the host does not report its images."""
    if n.images is None:
        return {}
    have = set(n.images)
    out: dict[str, list[str]] = {}
    for mode in deployment_modes(n.deployment or "vm"):
        out[mode] = [t["os"] for t in catalog.templates if not t["validator_only"] and f"{t['version']}:{mode}" in have]
    return out


def node_os_options(n: Node, catalog: Catalog) -> list[str]:
    """Templates a tenant can launch on this node (present in at least one of its tiers); the whole catalog when unknown."""
    by_mode = images_by_mode(n, catalog)
    if not by_mode:
        return catalog.os_options()
    seen: list[str] = []
    for t in catalog.templates:
        if not t["validator_only"] and any(t["os"] in v for v in by_mode.values()):
            seen.append(t["os"])
    return seen


def offline_unrented(session, n: Node) -> bool:
    """An offline node nobody rents is not listed (docs/07 §2.7): it comes back by itself when its host agent returns."""
    if n.host_connected:
        return False
    rented = session.scalar(select(Lease).where(Lease.node_id == n.node_id, Lease.released_at.is_(None), Lease.is_validator.is_(False)))
    return rented is None


def availability(session, catalog: Catalog, base_url: str, include_unqualified: bool = False, include_offline: bool = False) -> list[NodeAvailability]:
    out = []
    for n in session.scalars(select(Node)).all():
        if n.status not in ("qualified",) and not include_unqualified:
            continue
        if not include_offline and offline_unrented(session, n):
            continue
        out.append(NodeAvailability(
            node_id=n.node_id, provider=n.provider_name or f"miner-{n.miner_hotkey[:6]}", region=n.region,
            available=node_available(session, n), hourly_price_cents=n.hourly_price_cents, os_options=node_os_options(n, catalog), images_by_mode=images_by_mode(n, catalog), verification=progress_summary(session, n.node_id),
            spec=node_spec_summary(n), badges=node_badges(n), perf=round(n.perf, 3), uptime_30d=round(n.uptime_30d, 4),
            verification_url=f"{base_url}/nodes/{n.node_id}", funded_slot=n.funded_slot, operator_owned=n.operator_owned,
            profile=n.profile, instance_type=profile_get(n.profile).instance_type, deployment=n.deployment, status=n.status,
            host_connected=bool(n.host_connected), offline_since=n.offline_since,
        ))
    return out
