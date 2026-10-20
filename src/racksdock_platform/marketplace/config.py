from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from racksdock_subnet.verifier.config import TrustConfig


@dataclass
class Settings:
    db_url: str = "sqlite:///./racksdock-marketplace.db"
    base_url: str = "http://127.0.0.1:8000"
    chain_kind: str = "mock"
    chain_file: Path = Path("./chain.json")
    chain_network: str = "finney"
    netuid: int = 1
    catalog_path: Path | None = None
    default_price_cents: int = 5000
    default_funded_slots: int = 64
    pending_provider_limit_s: int = 600
    pending_limit_s: int = 600
    heartbeat_lost_degraded_s: int = 900
    probe_timeout_s: float = 3.0
    challenge_timeout_s: float = 60.0
    admin_token: str = "dev-admin-token"
    secret: str = "dev-secret-change-me"
    open_validator_enroll: bool = True
    min_validator_stake: float = 0.0
    trust: TrustConfig = field(default_factory=TrustConfig.from_env)

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        return cls(
            db_url=e("RACKSDOCK_DB_URL", "sqlite:///./racksdock-marketplace.db"),
            base_url=e("RACKSDOCK_BASE_URL", "http://127.0.0.1:8000"),
            chain_kind=e("RACKSDOCK_CHAIN", "mock"),
            chain_file=Path(e("RACKSDOCK_CHAIN_FILE", "./chain.json")),
            chain_network=e("RACKSDOCK_CHAIN_NETWORK", "finney"),
            netuid=int(e("RACKSDOCK_NETUID", "1")),
            catalog_path=Path(e("RACKSDOCK_CATALOG_MANIFEST")) if e("RACKSDOCK_CATALOG_MANIFEST") else None,
            default_price_cents=int(e("RACKSDOCK_PRICE_CENTS", "5000")),
            default_funded_slots=int(e("RACKSDOCK_FUNDED_SLOTS", "64")),
            admin_token=e("RACKSDOCK_ADMIN_TOKEN", "dev-admin-token"),
            secret=e("RACKSDOCK_SECRET", "dev-secret-change-me"),
            open_validator_enroll=e("RACKSDOCK_OPEN_VALIDATOR_ENROLL", "1") == "1",
            min_validator_stake=float(e("RACKSDOCK_MIN_VALIDATOR_STAKE", "0")),
            trust=TrustConfig.from_env(),
        )
