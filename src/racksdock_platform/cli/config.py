from __future__ import annotations

import json
import os
from pathlib import Path

HOME = Path(os.environ.get("RACKSDOCK_HOME", "~/.racksdock")).expanduser()


def load() -> dict:
    p = HOME / "config.json"
    cfg = json.loads(p.read_text()) if p.exists() else {}
    if os.environ.get("RACKSDOCK_API_URL"):
        cfg["api_url"] = os.environ["RACKSDOCK_API_URL"]
    if os.environ.get("RACKSDOCK_API_KEY"):
        cfg["api_key"] = os.environ["RACKSDOCK_API_KEY"]
    cfg.setdefault("api_url", "http://127.0.0.1:8000")
    return cfg


def save(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "config.json").write_text(json.dumps(cfg, indent=1))
    (HOME / "config.json").chmod(0o600)


def instance_record_path(instance_id: str) -> Path:
    d = HOME / "instances"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{instance_id}.json"


def keys_registry_path() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    return HOME / "keys.json"


def remember_private_key(public_line: str, private_path: str) -> None:
    p = keys_registry_path()
    reg = json.loads(p.read_text()) if p.exists() else {}
    reg[" ".join(public_line.split()[:2])] = private_path
    p.write_text(json.dumps(reg, indent=1))


def private_key_for(public_lines: list[str]) -> str | None:
    p = keys_registry_path()
    if not p.exists():
        return None
    reg = json.loads(p.read_text())
    for line in public_lines:
        path = reg.get(" ".join(line.split()[:2]))
        if path and Path(path).exists():
            return path
    return None
