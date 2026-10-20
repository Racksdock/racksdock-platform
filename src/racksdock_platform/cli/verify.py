"""Client-side verification used by `racksdock ssh` and `racksdock nodes verify`."""
from __future__ import annotations

from dataclasses import dataclass, field

from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.nonce import compose_boot_nonce, ssh_fingerprint, ssh_host_key_bytes_from_line
from racksdock_subnet.core.schemas import BootEvidence, QualificationRecord, UserDataInput
from racksdock_subnet.core.userdata import render, user_data_hash
from racksdock_subnet.verifier.config import TrustConfig
from racksdock_subnet.verifier.verifiers import build_verifier


@dataclass
class Verdict:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    host_key_line: str | None = None
    fingerprint: str | None = None

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))
        if not ok:
            self.ok = False


def verify_instance(info: dict, boot_evidence_envelope: dict, registration_gpu_ids: list[str], local: dict | None, trust: TrustConfig | None = None) -> Verdict:
    """local: what this client stored at create time: {tenant_nonce, user_data_input, ssh_public_keys}."""
    v = Verdict(ok=True)
    env = SignedEnvelope.model_validate(boot_evidence_envelope)
    v.add("evidence envelope signature", env.verify() and env.kind == "boot_evidence")
    ev = BootEvidence.model_validate(env.payload)
    v.add("envelope signed by the node-agent key it names", env.signer == ev.node_agent_pubkey)
    v.add("evidence is for this instance", ev.instance_id == info["id"], ev.instance_id)
    # tenant nonce: ours if we have it
    if local and local.get("tenant_nonce"):
        v.add("tenant nonce is the one this client generated", ev.tenant_nonce == local["tenant_nonce"])
    else:
        v.add("tenant nonce matches the API (no local copy: created in the console or elsewhere)", ev.tenant_nonce == info["tenant_nonce"])
    # user-data: re-render from what we submitted if we have it, else use the API copy
    if local and local.get("user_data_input"):
        ud = render(UserDataInput.model_validate(local["user_data_input"]))
        v.add("user-data hash matches what this client submitted", user_data_hash(ud) == ev.user_data_hash)
    else:
        ud = info.get("user_data") or ""
        v.add("user-data hash matches the API copy", user_data_hash(ud) == ev.user_data_hash)
    expected_nonce = compose_boot_nonce(ev.tenant_nonce, ssh_host_key_bytes_from_line(ev.ssh_host_key_line), ud.encode())
    v.add("boot nonce binds tenant nonce, host key and user-data", expected_nonce == ev.boot_nonce)
    verifier = build_verifier(trust or TrustConfig.from_env())
    res = verifier.verify(ev.bundle, ev.boot_nonce)
    v.add(f"NVIDIA evidence verifies ({verifier.name})", res.ok, "; ".join(res.errors)[:200])
    v.add("eight GPU identities equal the node registration", set(res.gpu_ids) == set(registration_gpu_ids) and len(res.gpu_ids) == 8)
    if info.get("host_key_line"):
        v.add("host key presented by the API equals the attested host key", info["host_key_line"].split()[:2] == ev.ssh_host_key_line.split()[:2])
    v.host_key_line = ev.ssh_host_key_line
    v.fingerprint = ssh_fingerprint(ev.ssh_host_key_line)
    return v


def verify_qualification(envelope: dict, registration_gpu_ids: list[str]) -> Verdict:
    v = Verdict(ok=True)
    env = SignedEnvelope.model_validate(envelope)
    v.add("qualification envelope signature", env.verify() and env.kind == "qualification")
    q = QualificationRecord.model_validate(env.payload)
    v.add("signed by the validator key named in the record", env.signer == q.validator_key)
    v.add("attested GPU identities equal the registration", set(q.attestation.gpu_ids) == set(registration_gpu_ids))
    v.add("outcome is pass", q.outcome == "pass", q.outcome)
    v.add("Q1 attestation check passed", any(c.id == "Q1" and c.passed for c in q.checks) if q.checks else True)
    return v
