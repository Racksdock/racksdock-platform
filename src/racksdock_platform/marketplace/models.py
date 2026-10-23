from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(String, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String, default="")
    role: Mapped[str] = mapped_column(String, default="user")  # user | validator | miner | admin
    credits_cents: Mapped[int] = mapped_column(Integer, default=0)
    default_ssh_key_id: Mapped[str | None] = mapped_column(String, nullable=True)
    validator_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    hotkey: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("key"))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String, unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String, default="default")
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class SshKey(Base):
    __tablename__ = "ssh_keys"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("sshkey"))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    public_key: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class Template(Base):
    __tablename__ = "templates"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("tpl"))
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    body: Mapped[dict] = mapped_column(JSON)
    featured: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class Node(Base):
    __tablename__ = "nodes"
    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    miner_hotkey: Mapped[str] = mapped_column(String, index=True)
    miner_service_key: Mapped[str] = mapped_column(String, index=True)
    provider_name: Mapped[str] = mapped_column(String, default="")
    region: Mapped[str] = mapped_column(String, default="")  # <continent>-<country> detected from the host agent's address; the miner's claim until then
    region_source: Mapped[str] = mapped_column(String, default="claimed")  # claimed | geoip
    location: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # GeoInfo.as_dict() + detected_at
    registration: Mapped[dict] = mapped_column(JSON)
    registration_hash: Mapped[str] = mapped_column(String)
    registered_block: Mapped[int] = mapped_column(Integer, default=0)
    profile: Mapped[str] = mapped_column(String, default="b300x8", index=True)  # hardware class the node registered with
    deployment: Mapped[str] = mapped_column(String, default="vm")  # vm | container | static (docs/01 §4)
    images: Mapped[list | None] = mapped_column(JSON, nullable=True)  # catalog images the host reports ("<version>:<kind>"); None = unknown
    status: Mapped[str] = mapped_column(String, default="registered")  # registered | qualified | quarantined | retired
    qualified_at: Mapped[str | None] = mapped_column(String, nullable=True)
    qualification: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    perf: Mapped[float] = mapped_column(Float, default=0.0)
    hourly_price_cents: Mapped[int] = mapped_column(Integer, default=5000)
    funded_slot: Mapped[bool] = mapped_column(Boolean, default=True)
    operator_owned: Mapped[bool] = mapped_column(Boolean, default=False)
    maintenance_until: Mapped[str | None] = mapped_column(String, nullable=True)
    host_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    last_heartbeat_at: Mapped[str | None] = mapped_column(String, nullable=True)
    offline_since: Mapped[str | None] = mapped_column(String, nullable=True)  # set when the host agent drops; cleared when it is back
    needs_readiness: Mapped[bool] = mapped_column(Boolean, default=True)
    critical_fault: Mapped[str | None] = mapped_column(String, nullable=True)
    uptime_30d: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class Lease(Base):
    __tablename__ = "leases"
    lease_id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("lease"))
    instance_id: Mapped[str] = mapped_column(String, index=True)
    node_id: Mapped[str] = mapped_column(String, ForeignKey("nodes.node_id"), index=True)
    tenant_nonce: Mapped[str] = mapped_column(String)
    user_data_hash: Mapped[str] = mapped_column(String)
    mode: Mapped[str] = mapped_column(String, default="tenant")
    is_validator: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)
    active_at: Mapped[str | None] = mapped_column(String, nullable=True)
    released_at: Mapped[str | None] = mapped_column(String, nullable=True)
    __table_args__ = (
        # exactly one live lease per node: the database enforces atomic reservation
        Index("ux_lease_live_node", "node_id", unique=True, sqlite_where=text("released_at IS NULL"), postgresql_where=text("released_at IS NULL")),
    )


class Instance(Base):
    __tablename__ = "instances"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("inst"))
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)
    validator_key: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String)
    node_id: Mapped[str | None] = mapped_column(String, ForeignKey("nodes.node_id"), nullable=True, index=True)
    mode: Mapped[str] = mapped_column(String, default="tenant")
    deployment: Mapped[str] = mapped_column(String, default="vm")  # the tier this rental runs in: vm | container | static (dual nodes: the customer's pick)
    os: Mapped[str] = mapped_column(String)
    image_version: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="creating", index=True)
    status_details: Mapped[str | None] = mapped_column(String, nullable=True)
    ip: Mapped[str | None] = mapped_column(String, nullable=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    host_key_line: Mapped[str | None] = mapped_column(Text, nullable=True)
    host_key_fp: Mapped[str | None] = mapped_column(String, nullable=True)
    tenant_nonce: Mapped[str] = mapped_column(String)
    user_data: Mapped[str] = mapped_column(Text)
    user_data_hash: Mapped[str] = mapped_column(String)
    request: Mapped[dict] = mapped_column(JSON)
    attestation: Mapped[dict] = mapped_column(JSON, default=dict)
    node_agent_pubkey: Mapped[str | None] = mapped_column(String, nullable=True)
    launch_status: Mapped[str | None] = mapped_column(String, nullable=True)
    active_pending_attestation: Mapped[bool] = mapped_column(Boolean, default=False)
    hourly_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)
    provider_at: Mapped[str | None] = mapped_column(String, nullable=True)
    pending_at: Mapped[str | None] = mapped_column(String, nullable=True)
    active_at: Mapped[str | None] = mapped_column(String, nullable=True)
    deleting_at: Mapped[str | None] = mapped_column(String, nullable=True)
    deleted_at: Mapped[str | None] = mapped_column(String, nullable=True)
    billed_minutes: Mapped[int] = mapped_column(Integer, default=0)
    degraded_minutes: Mapped[int] = mapped_column(Integer, default=0)
    cost_cents: Mapped[int] = mapped_column(Integer, default=0)
    last_metered_at: Mapped[str | None] = mapped_column(String, nullable=True)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    alert: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    auto_delete: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class EvidenceRecord(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    instance_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    signer: Mapped[str] = mapped_column(String, index=True)
    content_hash: Mapped[str] = mapped_column(String, unique=True)
    envelope: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(String, default=now_iso, index=True)


class Strike(Base):
    __tablename__ = "strikes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String, index=True)
    reason: Mapped[str] = mapped_column(String)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String, default="")
    at: Mapped[str] = mapped_column(String, default=now_iso)


class HostOutage(Base):
    """A period during which a node's host agent was unreachable (socket dropped or heartbeats stale for host_offline_after_s).
    Public via the validators' node rows: offline minutes score as failed availability, never as idle pay (docs/04 §3)."""
    __tablename__ = "host_outages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String, index=True)
    started_at: Mapped[str] = mapped_column(String)
    ended_at: Mapped[str | None] = mapped_column(String, nullable=True)
    rented: Mapped[bool] = mapped_column(Boolean, default=False)  # a tenant instance was active when the host dropped
    affected: Mapped[list] = mapped_column(JSON, default=list)  # instance ids degraded by this outage
    reason: Mapped[str] = mapped_column(String, default="")


class ScoringRound(Base):
    """The platform's own scoring of one tempo-aligned window (docs/04 §6): recorded the first time a snapshot for that window is
    built, so every validator asking during the window gets the same `previously_funded`, and anyone can compare their weights
    with the platform's. Validators never read it to score; they recompute from the snapshot."""
    __tablename__ = "scoring_rounds"
    window_end: Mapped[str] = mapped_column(String, primary_key=True)
    window_start: Mapped[str] = mapped_column(String)
    tempo_s: Mapped[float] = mapped_column(Float)
    funded: Mapped[list] = mapped_column(JSON, default=list)
    hotkey_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    burn: Mapped[float] = mapped_column(Float, default=0.0)
    computed_at: Mapped[str] = mapped_column(String, default=now_iso)


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String, index=True)
    node_id: Mapped[str] = mapped_column(String, index=True)
    klass: Mapped[str] = mapped_column(String)
    details: Mapped[str] = mapped_column(Text, default="")
    opened_at: Mapped[str] = mapped_column(String, default=now_iso)
    closed_at: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str] = mapped_column(String, default="")


class BillingEvent(Base):
    __tablename__ = "billing_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    instance_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)  # charge | credit | purchase
    cents: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String, default="")
    at: Mapped[str] = mapped_column(String, default=now_iso)


class CommandLog(Base):
    __tablename__ = "command_log"
    command_id: Mapped[str] = mapped_column(String, primary_key=True)
    node_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String)
    instance_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)
    acked_at: Mapped[str | None] = mapped_column(String, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class AllocationLog(Base):
    __tablename__ = "allocation_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[str] = mapped_column(String, index=True)
    candidates: Mapped[list] = mapped_column(JSON, default=list)
    chosen: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(String, default="")
    at: Mapped[str] = mapped_column(String, default=now_iso)


class VerificationProgress(Base):
    """The current (or last) verification session of a node as the validator reports it step by step (docs/03 §5.1)."""

    __tablename__ = "verification_progress"
    node_id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String)
    session_kind: Mapped[str] = mapped_column(String)  # admission | full | weekly | readiness | idle
    validator_key: Mapped[str] = mapped_column(String)
    started_at: Mapped[str] = mapped_column(String, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, default=now_iso)
    outcome: Mapped[str | None] = mapped_column(String, nullable=True)  # None while running; pass | quarantined | ...
    steps: Mapped[list] = mapped_column(JSON, default=list)  # [{id, name, state: pending|running|pass|fail|skipped, detail, duration_s, at}]


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Deposit(Base):
    """One payment that becomes credits: a TAO transfer, a processor checkout or testnet credits (services/payments)."""

    __tablename__ = "deposits"
    __table_args__ = (Index("ix_deposits_ref", "method", "external_ref", unique=True),)
    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: new_id("dep"))
    user_id: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True, index=True)  # None: a transfer from a wallet nobody linked yet
    method: Mapped[str] = mapped_column(String)  # tao | crypto | free
    status: Mapped[str] = mapped_column(String, default="pending")  # pending | confirming | credited | underpaid | expired | failed | unmatched
    cents_requested: Mapped[int] = mapped_column(Integer, default=0)
    cents: Mapped[int] = mapped_column(Integer, default=0)  # what was credited
    asset: Mapped[str] = mapped_column(String, default="")  # TAO, or the coin the processor reports
    amount: Mapped[str] = mapped_column(String, default="")  # native amount as a decimal string (TAO: whole tao, 9 decimals)
    rate: Mapped[str] = mapped_column(String, default="")  # USD per unit of `asset` used for the credit
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # TAO: "<block>:<event index>"; processor: its payment id
    from_address: Mapped[str] = mapped_column(String, default="")
    checkout_url: Mapped[str] = mapped_column(String, default="")
    note: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, default=now_iso)
    updated_at: Mapped[str] = mapped_column(String, default=now_iso)
    credited_at: Mapped[str | None] = mapped_column(String, nullable=True)

    def as_dict(self) -> dict:
        return {"id": self.id, "method": self.method, "status": self.status, "cents_requested": self.cents_requested, "cents": self.cents, "asset": self.asset,
                "amount": self.amount, "rate": self.rate, "external_ref": self.external_ref, "from_address": self.from_address, "checkout_url": self.checkout_url,
                "note": self.note, "created_at": self.created_at, "updated_at": self.updated_at, "credited_at": self.credited_at}


class LinkedWallet(Base):
    """A coldkey a user proved they own (signed challenge); TAO sent from it to the treasury credits that user."""

    __tablename__ = "linked_wallets"
    address: Mapped[str] = mapped_column(String, primary_key=True)  # SS58 (format 42)
    user_id: Mapped[str] = mapped_column(String, ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String, default="")
    created_at: Mapped[str] = mapped_column(String, default=now_iso)
