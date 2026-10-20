from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..models import Node, Setting, User
from ..services.auth import new_api_key, require_admin
from .deps import db

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class NodeUpdate(BaseModel):
    provider_name: str | None = None
    hourly_price_cents: int | None = None
    operator_owned: bool | None = None
    funded_slot: bool | None = None
    status: str | None = None
    clear_critical_fault: bool = False


@router.post("/nodes/{node_id}")
def update_node(node_id: str, body: NodeUpdate, _db=Depends(db)):
    with _db.session() as s:
        n = s.get(Node, node_id)
        if n is None:
            raise HTTPException(404, "node not found")
        for f in ("provider_name", "hourly_price_cents", "operator_owned", "funded_slot", "status"):
            v = getattr(body, f)
            if v is not None:
                setattr(n, f, v)
        if body.clear_critical_fault:
            n.critical_fault = None
    return {"node_id": node_id, "updated": True}


class SettingBody(BaseModel):
    value: str


@router.post("/settings/{key}")
def set_setting(key: str, body: SettingBody, _db=Depends(db)):
    with _db.session() as s:
        st = s.get(Setting, key)
        if st is None:
            s.add(Setting(key=key, value=body.value))
        else:
            st.value = body.value
    return {key: body.value}


@router.get("/settings")
def get_settings(_db=Depends(db)):
    with _db.session() as s:
        return {r.key: r.value for r in s.scalars(select(Setting)).all()}


class CreditBody(BaseModel):
    cents: int


@router.post("/users/{user_id}/credits")
def add_credits(user_id: str, body: CreditBody, _db=Depends(db)):
    with _db.session() as s:
        u = s.get(User, user_id)
        if u is None:
            raise HTTPException(404, "user not found")
        u.credits_cents += body.cents
        return {"credits_cents": u.credits_cents}


@router.post("/users/{user_id}/apikey")
def mint_key(user_id: str, _db=Depends(db)):
    with _db.session() as s:
        u = s.get(User, user_id)
        if u is None:
            raise HTTPException(404, "user not found")
        return {"api_key": new_api_key(s, u, name="admin-minted")}
