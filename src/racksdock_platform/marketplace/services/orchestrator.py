"""Instance state machine (docs/02 §2–§5): create -> lease -> launch -> attest -> active -> delete -> relist."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.nonce import compose_boot_nonce, new_nonce_hex, ssh_fingerprint, ssh_host_key_bytes_from_line
from racksdock_subnet.core.profile import current as current_profile
from racksdock_subnet.core.profile import deployment_modes
from racksdock_subnet.core.profile import get as profile_get
from racksdock_subnet.core.schemas import (
    BootEvidence,
    CreateInstanceRequest,
    HostEvent,
    HostHeartbeat,
    InstanceStatus,
    LaunchCommand,
    StatusUpdate,
    UserDataInput,
    VmMode,
)
from racksdock_subnet.core.userdata import render, user_data_hash
from racksdock_subnet.verifier.verifiers import build_verifier
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..models import CommandLog, Incident, Instance, Lease, Node, SshKey, Strike, now_iso
from .allocation import pick_node
from .catalog import Catalog, node_available
from .connectivity import declare_online, recover_or_lose, sweep
from .hosts import HostRegistry, HostSession
from .records import store_envelope

log = logging.getLogger("racksdock.marketplace.orchestrator")


class Orchestrator:
    def __init__(self, db, settings, catalog: Catalog, hosts: HostRegistry):
        self.db = db
        self.settings = settings
        self.catalog = catalog
        self.hosts = hosts
        self.verifier = build_verifier(settings.trust)
        self._verifiers: dict[int, object] = {}
        hosts.on_event = self.on_host_event
        hosts.on_connect = self.on_host_connect
        hosts.on_disconnect = self.on_host_disconnect
        self._probe_tasks: set[asyncio.Task] = set()
        self.command_notes: dict[str, list[dict]] = {}  # host log/error notes per command id (bootstrap progress for miners)

    def verifier_for_profile(self, profile_name: str | None) -> object:
        """Evidence verifier expecting that profile's GPU count (one platform serves several hardware profiles)."""
        n = profile_get(profile_name).gpu_count
        if n == current_profile().gpu_count:
            return self.verifier
        if n not in self._verifiers:
            self._verifiers[n] = build_verifier(self.settings.trust, expected_devices=n)
        return self._verifiers[n]

    def verifier_for(self, node) -> object:
        return self.verifier_for_profile(getattr(node, "profile", None) if node is not None else None)

    # ------------------------------------------------------------------ create
    def create_instance(self, req: CreateInstanceRequest, *, user_id: str | None, validator_key: str | None = None,
                        ssh_public_keys: list[str] | None = None, mode: VmMode = VmMode.tenant) -> Instance:
        os_name = req.os or self.catalog.default_os
        if os_name not in self.catalog.os_options(validator=validator_key is not None):
            raise ValueError("invalid_os")
        image_version = self.catalog.version_for(os_name)
        with self.db.session() as s:
            if ssh_public_keys is None:
                keys = s.scalars(select(SshKey).where(SshKey.user_id == user_id, SshKey.id.in_(req.ssh_key_ids))).all() if req.ssh_key_ids else []
                ssh_public_keys = [k.public_key for k in keys]
            ud_input = UserDataInput(ssh_public_keys=ssh_public_keys, launch_configuration=req.launch_configuration, envs=req.envs,
                                     networking=req.networking, enable_metrics=req.enable_metrics)
            user_data = render(ud_input)
            inst = Instance(user_id=user_id, validator_key=validator_key, name=req.name, node_id=None, mode=mode.value, os=os_name,
                            image_version=image_version, tenant_nonce=req.tenant_nonce or new_nonce_hex(), user_data=user_data,
                            user_data_hash=user_data_hash(user_data), request=req.model_dump(mode="json"), tags=req.tags,
                            alert=req.alert.model_dump() if req.alert else None, auto_delete=req.auto_delete.model_dump() if req.auto_delete else None)
            s.add(inst)
            s.flush()
            node = pick_node(s, req.region, os_name, inst.id, instance_type=req.instance_type, deployment=req.deployment) if req.node_id == "auto" else s.get(Node, req.node_id)
            if node is None:
                inst.status = InstanceStatus.error.value
                inst.status_details = "no_capacity" if req.node_id == "auto" else f"node_not_found: {req.node_id[:24]}"
                raise ValueError("no_capacity" if req.node_id == "auto" else "node_not_found")
            if req.node_id != "auto" and not node_available(s, node) and validator_key is None:
                inst.status = InstanceStatus.error.value
                inst.status_details = "node_busy"
                inst.node_id = node.node_id
                raise ValueError("node_busy")
            inst.node_id = node.node_id
            # the tier this rental runs in: a single-tier node has one answer; a dual node takes the customer's pick (VM by default)
            offered = deployment_modes(node.deployment or "vm")
            if req.deployment and req.deployment not in offered:
                inst.status = InstanceStatus.error.value
                inst.status_details = f"mode_not_offered: this node offers {'/'.join(offered)}"
                raise ValueError("mode_not_offered")
            inst.deployment = req.deployment or ("vm" if "vm" in offered else offered[0])
            if node.images is not None and f"{image_version}:{inst.deployment}" not in node.images and validator_key is None:
                inst.status = InstanceStatus.error.value
                inst.status_details = f"image_not_on_node: {os_name} ({inst.deployment}) is not on this node yet"
                raise ValueError("image_not_on_node")
            inst.hourly_price_cents = 0 if validator_key else node.hourly_price_cents
            lease = Lease(instance_id=inst.id, node_id=node.node_id, tenant_nonce=inst.tenant_nonce, user_data_hash=inst.user_data_hash,
                          mode=mode.value, is_validator=validator_key is not None)
            s.add(lease)
            try:
                s.flush()
            except IntegrityError:
                s.rollback()
                with self.db.session() as s2:
                    i2 = s2.get(Instance, inst.id)
                    if i2 is not None:
                        i2.status = InstanceStatus.error.value
                        i2.status_details = "node_busy"
                        i2.node_id = node.node_id
                raise ValueError("node_busy")
            return inst

    async def launch(self, instance_id: str) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.status != InstanceStatus.creating.value:
                return
            node_row = s.get(Node, inst.node_id)
            node_dep = node_row.deployment if node_row is not None else "vm"
            lc = LaunchCommand(instance_id=inst.id, node_id=inst.node_id, mode=VmMode(inst.mode), image=inst.os, image_version=inst.image_version, deployment=inst.deployment if node_dep == "dual" else None,
                               user_data=inst.user_data, tenant_nonce=inst.tenant_nonce)
            node_id = inst.node_id
        try:
            cid = await self.hosts.send(node_id, "launch", lc.model_dump(mode="json"), instance_id=instance_id)
            with self.db.session() as s:
                s.add(CommandLog(command_id=cid, node_id=node_id, kind="launch", instance_id=instance_id, payload={"image": lc.image}))
        except ConnectionError:
            await self._fail(instance_id, "provider_unreachable", release=True)

    # ------------------------------------------------------------------ delete / restart
    async def delete(self, instance_id: str, reason: str = "user") -> bool:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.status in (InstanceStatus.deleted.value, InstanceStatus.deleting.value):
                return False
            was_error = inst.status == InstanceStatus.error.value
            inst.status = InstanceStatus.deleting.value
            inst.status_details = reason
            inst.deleting_at = now_iso()
            node_id = inst.node_id
        if was_error or not node_id:
            self._mark_deleted(instance_id, "no VM")
            return True
        try:
            cid = await self.hosts.send(node_id, "teardown", {}, instance_id=instance_id)
            with self.db.session() as s:
                s.add(CommandLog(command_id=cid, node_id=node_id, kind="teardown", instance_id=instance_id))
        except ConnectionError:
            # host offline: lease stays until the host reconnects and confirms teardown; billing already stopped
            with self.db.session() as s:
                inst = s.get(Instance, instance_id)
                inst.status_details = "teardown pending: host offline"
        return True

    async def restart(self, instance_id: str, hard: bool = False) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.status not in ("active", "degraded"):
                raise ValueError("instance not running")
            node_id = inst.node_id
        await self.hosts.send(node_id, "restart", {"hard": hard}, instance_id=instance_id, wait=True, timeout=120)

    async def challenge(self, node_id: str, instance_id: str, envelope: SignedEnvelope) -> HostEvent:
        return await self.hosts.send(node_id, "challenge", envelope.model_dump(), instance_id=instance_id, wait=True, timeout=self.settings.challenge_timeout_s)

    # ------------------------------------------------------------------ host events
    async def on_host_connect(self, sess: HostSession) -> None:
        with self.db.session() as s:
            if sess.node_id:
                node = s.get(Node, sess.node_id)
                if node is not None:
                    if node.miner_service_key != sess.host_pubkey:
                        log.warning("host key mismatch for node %s", sess.node_id[:16])
                        return
                    declare_online(s, node, self.settings)
            else:
                # pre-registration host: bind by service key if a registration already names it
                node = s.scalar(select(Node).where(Node.miner_service_key == sess.host_pubkey))
                if node is not None:
                    self.hosts.bind_node(sess, node.node_id)
                    declare_online(s, node, self.settings)

    async def on_host_disconnect(self, sess: HostSession) -> None:
        if not sess.node_id:
            return
        with self.db.session() as s:
            node = s.get(Node, sess.node_id)
            if node is not None:
                # hidden from the listing at once; the outage (tenant alerts, scoring) is declared by the sweep after the grace period
                node.host_connected = False
                node.offline_since = node.offline_since or now_iso()

    def _note(self, host_pubkey: str, command_id: str, kind: str, message: str) -> None:
        """Keep a host's latest log/error lines per command (bounded, in memory) so a miner polling for its bootstrap sees what the
        host is doing or why it failed. ':launch' sub-commands are filed under their parent command."""
        base = command_id.split(":", 1)[0]
        self.command_notes.setdefault(base, []).append({"host": host_pubkey, "kind": kind, "message": message[:600], "at": now_iso()})
        self.command_notes[base] = self.command_notes[base][-20:]
        while len(self.command_notes) > 200:
            self.command_notes.pop(next(iter(self.command_notes)))

    async def on_host_event(self, sess: HostSession, ev: HostEvent) -> None:
        node_id = sess.node_id or ev.node_id
        if ev.kind == "hello" and sess.node_id and ev.payload.get("images") is not None:
            with self.db.session() as s:
                node = s.get(Node, sess.node_id)
                if node is not None:
                    node.images = sorted(ev.payload["images"])
        if ev.kind == "heartbeat":
            await self._on_heartbeat(node_id, ev)
        elif ev.kind == "status" and ev.instance_id:
            await self._on_status(node_id, ev.instance_id, StatusUpdate.model_validate(ev.payload))
        elif ev.kind == "evidence" and ev.instance_id:
            await self._on_evidence(node_id, ev.instance_id, SignedEnvelope.model_validate(ev.payload))
        elif ev.kind == "identity":
            errors: list[str] = []
            reg = ev.payload.get("registration") or {}
            if ev.payload.get("reused") and not ev.payload.get("evidence"):
                # no fresh evidence: accepted only as an update of a node this same host key already registered with the same
                # GPU identities (a changed tier/profile/spec); the admission test that follows re-verifies the hardware
                with self.db.session() as s:
                    node = s.get(Node, reg.get("node_id") or "")
                    if node is None:
                        errors.append("identity reuse for an unregistered node: bootstrap without reuse")
                    elif node.miner_service_key != sess.host_pubkey:
                        errors.append("identity reuse from a different host key")
                    elif set(node.registration.get("gpu_ids", [])) != set(reg.get("gpu_ids", [])):
                        errors.append("identity reuse with different gpu_ids: bootstrap without reuse")
            else:
                try:
                    boot = BootEvidence.model_validate(ev.payload.get("evidence") or {})
                    draft_profile = reg.get("profile")  # the host declares its hardware class in the draft
                    res = self.verifier_for_profile(draft_profile).verify(boot.bundle, boot.boot_nonce)
                    if not res.ok:
                        errors.extend(res.errors or ["attestation failed"])
                    if set(res.gpu_ids) != set(reg.get("gpu_ids", [])):
                        errors.append("draft gpu_ids do not match the evidence")
                except Exception as e:
                    errors.append(f"cannot verify draft evidence: {e}")
            if errors:
                log.warning("identity draft from host %s rejected: %s", sess.host_pubkey[:12], "; ".join(errors)[:300])
            with self.db.session() as s:
                payload = {**ev.payload, "command_id": ev.command_id, "verified": not errors, "errors": errors}
                env = SignedEnvelope(kind="identity_draft", payload=payload, signer=sess.host_pubkey, signature="")
                store_envelope(s, env, "identity_draft" if not errors else "identity_draft_rejected", node_id=ev.node_id)
            if ev.node_id and not sess.node_id and not errors:
                self.hosts.bind_node(sess, ev.node_id)
        elif ev.kind == "log":
            if ev.command_id:
                self._note(sess.host_pubkey, ev.command_id, "log", str(ev.payload.get("message") or ""))
        elif ev.kind in ("ack", "error", "challenge_response"):
            if ev.kind == "error" and ev.command_id:
                self._note(sess.host_pubkey, ev.command_id, "error", str(ev.payload.get("error") or ""))
            if ev.command_id:
                with self.db.session() as s:
                    cl = s.get(CommandLog, ev.command_id)
                    if cl is not None:
                        cl.acked_at = now_iso()
                        cl.result = {"kind": ev.kind, **({"error": ev.payload.get("error")} if ev.kind == "error" else {})}
            if ev.kind == "error" and ev.instance_id:
                with self.db.session() as s:
                    inst = s.get(Instance, ev.instance_id)
                    if inst is not None and inst.status in ("creating", "pending_provider", "pending"):
                        inst.status = InstanceStatus.error.value
                        inst.status_details = f"host error: {ev.payload.get('error')}"
                        self._release_lease(s, inst.id)

    async def _on_heartbeat(self, node_id: str | None, ev: HostEvent) -> None:
        if not node_id:
            return
        hb = HostHeartbeat.model_validate(ev.payload)
        with self.db.session() as s:
            node = s.get(Node, node_id)
            if node is None:
                return
            if not node.host_connected or node.offline_since:
                declare_online(s, node, self.settings)
            # an instance degraded by a host outage recovers on the first heartbeat that shows it running, whichever way the host
            # came back (reconnect or heartbeat); a no-op without an open host_offline incident
            recover_or_lose(s, node, hb.instance_id, hb.vm_state, self._release_lease)
            node.last_heartbeat_at = now_iso()
            node.host_connected = True
            node.maintenance_until = hb.maintenance_until
            if hb.images is not None:
                node.images = sorted(hb.images)
            # the host reports no VM: reconcile instances that still think they have one (host-agent restarted mid-flight)
            if hb.vm_state in (None, "shut off", "none"):
                now = dt.datetime.now(dt.UTC)

                def age(ts: str | None) -> float:
                    return (now - dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds() if ts else 0.0

                live = s.scalars(select(Instance).where(Instance.node_id == node_id, Instance.status.in_(["active", "deleting", "pending_provider", "pending"]))).all()
                for inst in live:
                    if inst.status == "active" and age(inst.active_at) > 120:
                        self._degrade(s, inst, "vm_missing", "host reports no VM while instance active")
                    elif inst.status == "deleting" and age(inst.deleting_at or inst.created_at) > 60:
                        # teardown was requested and the VM is gone (a restarted host-agent destroys leftovers on start): close it
                        inst.status = InstanceStatus.deleted.value
                        inst.deleted_at = now_iso()
                        inst.status_details = "VM gone; teardown completed without a host acknowledgement"
                        self._release_lease(s, inst.id)
                        node.needs_readiness = node.needs_readiness or inst.mode == "tenant"
                        log.warning("instance %s closed from heartbeat: host reports no VM while deleting", inst.id)
                    elif inst.status in ("pending_provider", "pending") and age(inst.provider_at or inst.created_at) > 600:
                        # a launch takes minutes on real hardware; after 10 min with no VM the launch is lost
                        inst.status = InstanceStatus.error.value
                        inst.status_details = "host reports no VM while launching"
                        self._release_lease(s, inst.id)
                        log.warning("instance %s failed from heartbeat: host reports no VM while %s", inst.id, inst.status)

    async def _on_status(self, node_id: str | None, instance_id: str, upd: StatusUpdate) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None:
                return
            if upd.ip:
                inst.ip = upd.ip
            if upd.ssh_port:
                inst.ssh_port = upd.ssh_port
            if upd.ssh_host_key_line and not inst.host_key_line:
                inst.host_key_line = upd.ssh_host_key_line
                inst.host_key_fp = ssh_fingerprint(upd.ssh_host_key_line)
            if upd.launch_status:
                inst.launch_status = upd.launch_status
            st = upd.status
            if inst.status in ("deleted", "error") and st not in (InstanceStatus.deleted,):
                return
            if st == InstanceStatus.pending_provider and inst.status == "creating":
                inst.status = st.value
                inst.provider_at = now_iso()
                inst.status_details = upd.status_details
            elif st == InstanceStatus.pending and inst.status in ("creating", "pending_provider"):
                inst.status = st.value
                inst.pending_at = now_iso()
                inst.status_details = upd.status_details
            elif st == InstanceStatus.active and inst.status in ("creating", "pending_provider", "pending"):
                if inst.attestation.get("status") == "verified":
                    self._schedule_activation(instance_id)
                else:
                    inst.active_pending_attestation = True
                    inst.status_details = "waiting for attestation"
            elif st == InstanceStatus.active and inst.status in ("active", "degraded"):
                # restart completed; keep state
                inst.status_details = upd.status_details
            elif st == InstanceStatus.error:
                inst.status = st.value
                inst.status_details = upd.status_details
                self._release_lease(s, inst.id)
            elif st == InstanceStatus.deleted:
                if inst.status == "error" and inst.status_details:
                    # keep the failure reason visible after the cleanup of a failed launch
                    inst.status_details = f"{inst.status_details} (cleaned up: {upd.status_details})" if upd.status_details else inst.status_details
                else:
                    inst.status_details = upd.status_details
                inst.status = st.value
                inst.deleted_at = now_iso()
                self._release_lease(s, inst.id)
                node = s.get(Node, inst.node_id)
                if node is not None:
                    node.needs_readiness = inst.mode == "tenant"  # a validator's own instance needs no readiness canary afterwards

    async def _on_evidence(self, node_id: str | None, instance_id: str, env: SignedEnvelope) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None:
                return
            errors: list[str] = []
            if not env.verify() or env.kind != "boot_evidence":
                errors.append("boot evidence envelope signature invalid")
                ev = None
            else:
                ev = BootEvidence.model_validate(env.payload)
            node = s.get(Node, inst.node_id)
            store_envelope(s, env, "boot_evidence", node_id=inst.node_id, instance_id=instance_id)
            if ev is not None:
                if env.signer != ev.node_agent_pubkey:
                    errors.append("evidence signer != node_agent_pubkey")
                if ev.tenant_nonce != inst.tenant_nonce:
                    errors.append("tenant nonce mismatch")
                if ev.user_data_hash != inst.user_data_hash:
                    errors.append("user-data hash mismatch (VM booted with different user-data)")
                if ev.image_version != inst.image_version:
                    errors.append(f"image version {ev.image_version} != requested {inst.image_version}")
                expected_nonce = compose_boot_nonce(inst.tenant_nonce, ssh_host_key_bytes_from_line(ev.ssh_host_key_line), inst.user_data.encode())
                if ev.boot_nonce != expected_nonce:
                    errors.append("boot nonce does not bind tenant nonce, host key and user-data")
                res = self.verifier_for(node).verify(ev.bundle, expected_nonce)
                if not res.ok:
                    errors.extend(res.errors or ["attestation verification failed"])
                reg_ids = set((node.registration or {}).get("gpu_ids", [])) if node else set()
                ids_match = set(res.gpu_ids) == reg_ids and len(res.gpu_ids) == profile_get(node.profile if node else None).gpu_count
                if not ids_match:
                    errors.append("GPU identities do not match the node registration")
                inst.host_key_line = ev.ssh_host_key_line
                inst.host_key_fp = ssh_fingerprint(ev.ssh_host_key_line)
                inst.node_agent_pubkey = ev.node_agent_pubkey
                inst.attestation = {
                    "status": "verified" if not errors else "failed", "gpu_ids_match": ids_match, "image_version": ev.image_version,
                    "user_data_hash": ev.user_data_hash, "boot_nonce": ev.boot_nonce, "verified_at": now_iso(), "errors": errors,
                    "evidence_url": f"{self.settings.base_url}/v1/evidence/{env.content_hash}", "measurements_hash": res.measurements_hash,
                    "gpu_ids": res.gpu_ids,
                }
            else:
                inst.attestation = {"status": "failed", "errors": errors, "verified_at": now_iso()}
            if errors:
                inst.status = InstanceStatus.error.value
                inst.status_details = "attestation_failed: " + "; ".join(errors)[:300]
                if node is not None and ev is not None and inst.attestation.get("gpu_ids_match") is False:
                    node.critical_fault = "boot evidence identities mismatch"
                    s.add(Strike(node_id=node.node_id, reason="boot_attestation_failed", source="platform"))
                self._release_lease(s, inst.id)
                node_id_ = inst.node_id
                asyncio.create_task(self._teardown_quiet(node_id_, instance_id))
                return
            if inst.active_pending_attestation:
                inst.active_pending_attestation = False
                self._schedule_activation(instance_id)

    async def _teardown_quiet(self, node_id: str, instance_id: str) -> None:
        try:
            await self.hosts.send(node_id, "teardown", {}, instance_id=instance_id)
        except ConnectionError:
            pass

    # ------------------------------------------------------------------ activation (reachability probe)
    def _schedule_activation(self, instance_id: str) -> None:
        t = asyncio.create_task(self._activate(instance_id))
        self._probe_tasks.add(t)
        t.add_done_callback(self._probe_tasks.discard)

    async def probe(self, ip: str, port: int) -> bool:
        try:
            r, w = await asyncio.wait_for(asyncio.open_connection(ip, port), self.settings.probe_timeout_s)
            try:
                banner = await asyncio.wait_for(r.readline(), self.settings.probe_timeout_s)
            finally:
                w.close()
            return banner.startswith(b"SSH-")
        except Exception:
            return False

    async def _activate(self, instance_id: str) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.status not in ("creating", "pending_provider", "pending"):
                return
            ip, port = inst.ip, inst.ssh_port
        ok = False
        for _ in range(20):
            if ip and await self.probe(ip, port):
                ok = True
                break
            await asyncio.sleep(0.5 if self.settings.probe_timeout_s < 5 else 3)
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.status not in ("creating", "pending_provider", "pending"):
                return
            if not ok:
                inst.status_details = "VM reported active but SSH port unreachable; retrying"
                return
            inst.status = InstanceStatus.active.value
            inst.status_details = None
            inst.active_at = now_iso()
            inst.last_metered_at = inst.active_at
            lease = s.scalar(select(Lease).where(Lease.instance_id == instance_id, Lease.released_at.is_(None)))
            if lease is not None:
                lease.active_at = inst.active_at

    # ------------------------------------------------------------------ helpers
    def _release_lease(self, s, instance_id: str) -> None:
        lease = s.scalar(select(Lease).where(Lease.instance_id == instance_id, Lease.released_at.is_(None)))
        if lease is not None:
            lease.released_at = now_iso()

    def _mark_deleted(self, instance_id: str, details: str) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None:
                return
            inst.status = InstanceStatus.deleted.value
            inst.deleted_at = now_iso()
            inst.status_details = details
            self._release_lease(s, instance_id)

    async def _fail(self, instance_id: str, details: str, release: bool) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None:
                return
            inst.status = InstanceStatus.error.value
            inst.status_details = details
            if release:
                self._release_lease(s, instance_id)

    def _degrade(self, s, inst: Instance, klass: str, details: str, source: str = "platform") -> None:
        if inst.status == "active":
            inst.status = InstanceStatus.degraded.value
            inst.status_details = f"degraded: {klass}"
        s.add(Incident(instance_id=inst.id, node_id=inst.node_id, klass=klass, details=details, source=source))

    def degrade(self, instance_id: str, klass: str, details: str, source: str, strike: bool) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.status not in ("active", "degraded"):
                return
            self._degrade(s, inst, klass, details, source)
            if strike:
                s.add(Strike(node_id=inst.node_id, reason=klass, source=source))

    def recover(self, instance_id: str, source: str) -> None:
        with self.db.session() as s:
            inst = s.get(Instance, instance_id)
            if inst is None or inst.status != "degraded":
                return
            inst.status = InstanceStatus.active.value
            inst.status_details = None
            for inc in s.scalars(select(Incident).where(Incident.instance_id == instance_id, Incident.closed_at.is_(None))).all():
                inc.closed_at = now_iso()

    # ------------------------------------------------------------------ periodic
    async def tick_timeouts(self) -> None:
        now = dt.datetime.now(dt.UTC)
        to_teardown: list[tuple[str, str]] = []
        with self.db.session() as s:
            for inst in s.scalars(select(Instance).where(Instance.status.in_(["creating", "pending_provider", "pending"]))).all():
                started = dt.datetime.fromisoformat((inst.pending_at or inst.provider_at or inst.created_at).replace("Z", "+00:00"))
                limit = self.settings.pending_limit_s if inst.status == "pending" else self.settings.pending_provider_limit_s
                if (now - started).total_seconds() > limit:
                    inst.status = InstanceStatus.error.value
                    inst.status_details = f"timeout in {inst.status}"
                    self._release_lease(s, inst.id)
                    to_teardown.append((inst.node_id, inst.id))
            # host connectivity: a dropped socket or stale heartbeats beyond the grace period is an outage (tenants alerted,
            # instances degraded with billing paused, validators told)
            sweep(s, self.settings, now)
        for node_id, iid in to_teardown:
            await self._teardown_quiet(node_id, iid)
