"""Build API views (InstanceInfo etc.) from ORM rows."""
from __future__ import annotations

from racksdock_subnet.core.profile import get as profile_get
from racksdock_subnet.core.schemas import AttestationView, InstanceConfiguration, InstanceInfo, InstanceStatus

from ..models import Instance, Node
from ..services.billing import cost_estimate
from ..services.catalog import Catalog, node_spec_summary


def instance_info(inst: Instance, node: Node | None, catalog: Catalog, include_user_data: bool = True) -> InstanceInfo:
    spec = node_spec_summary(node) if node else None
    prof = profile_get(node.profile if node else None)
    att = inst.attestation or {}
    return InstanceInfo(
        id=inst.id, name=inst.name, status=InstanceStatus(inst.status), status_details=inst.status_details, node_id=inst.node_id or "",
        provider=(node.provider_name if node else ""), region=(node.region if node else ""), ip=inst.ip, ssh_port=inst.ssh_port,
        host_key_fingerprint=inst.host_key_fp, host_key_line=inst.host_key_line,
        attestation=AttestationView(status=att.get("status", "pending"), gpu_ids_match=att.get("gpu_ids_match"), image_version=att.get("image_version"),
                                    user_data_hash=att.get("user_data_hash"), boot_nonce=att.get("boot_nonce"), verified_at=att.get("verified_at"),
                                    evidence_url=att.get("evidence_url"), errors=att.get("errors", [])),
        configuration=InstanceConfiguration(os=inst.os, os_options=catalog.os_options(), vcpus=spec.vcpus if spec else 0, cpu_model=spec.cpu_model if spec else "",
                                            memory_in_gb=spec.memory_in_gb if spec else 0, storage_in_gb=spec.storage_in_gb if spec else 0,
                                            vram_per_gpu_in_gb=spec.vram_per_gpu_in_gb if spec else 0, vram_per_gpu_total_gb=spec.vram_per_gpu_total_gb if spec else 0, network_gbps=spec.network_gbps if spec else 0,
                                            interconnect=prof.interconnect, gpu_type=prof.gpu_model, num_gpus=prof.gpu_count),
        hourly_price_cents=inst.hourly_price_cents, cost_estimate=cost_estimate(inst), created_at=inst.created_at, active_at=inst.active_at, deployment=inst.deployment or "vm",
        deleted_at=inst.deleted_at, launch_configuration=({"type": inst.request.get("launch_configuration", {}).get("type"), "container_status": inst.launch_status}
                                                          if inst.request.get("launch_configuration") else None),
        degraded_minutes_credited=inst.degraded_minutes, tags=inst.tags or [], tenant_nonce=inst.tenant_nonce,
        user_data=inst.user_data if include_user_data else None, user_data_hash=inst.user_data_hash,
    )
