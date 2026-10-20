"""Dev-trust-mode only: the platform plays NVIDIA's role for simulated attestation.

GET  /v1/dev/devca        -> public root certificate, device CA certificate and reference measurements (what validators need)
POST /v1/dev/issue-node   -> simulated GPU device keys/certificates for one node (what a node-agent needs on hardware without attestation)
These routes are mounted only when RACKSDOCK_TRUST_MODE=dev. In production there is no dev CA: evidence verifies against NVIDIA's root.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from racksdock_subnet.core.profile import current as current_profile
from racksdock_subnet.verifier.devca import DevCA

from .deps import settings

router = APIRouter(prefix="/v1/dev", tags=["dev (simulated attestation)"])


def _ca(st) -> DevCA:
    if not st.trust.dev or st.trust.dev_ca_dir is None:
        raise HTTPException(404, "not in dev trust mode")
    return DevCA(st.trust.dev_ca_dir)


@router.get("/devca")
def devca_public(st=Depends(settings)):
    ca = _ca(st)
    return {"root_pem": ca.root_pem.decode(), "device_ca_pem": ca.device_ca_pem.decode(), "rim": ca.rim(), "profile": current_profile().name}


class IssueNode(BaseModel):
    count: int | None = None
    pci_id: str | None = None
    memory_mib: int | None = None
    board_part: str | None = None


@router.post("/issue-node")
def issue_node(body: IssueNode, st=Depends(settings)):
    ca = _ca(st)
    prof = current_profile()
    with tempfile.TemporaryDirectory() as td:
        kw = {"count": body.count or prof.gpu_count, "pci_id": body.pci_id or prof.sim_pci_id}
        if body.memory_mib:
            kw["memory_mib"] = body.memory_mib
        if body.board_part:
            kw["board_part"] = body.board_part
        nd = ca.issue_node(Path(td) / "node", **kw)
        devices = []
        for i in range(kw["count"]):
            devices.append({"index": i, "key_pem": (nd / "devices" / f"{i}.key").read_text(), "cert_pem": (nd / "devices" / f"{i}.pem").read_text()})
        inventory = json.loads((nd / "inventory.json").read_text())
    return {"profile": prof.name, "devices": devices, "inventory": inventory}
