from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ..models import Deposit, Node, Setting, User
from ..services.auth import new_api_key, require_admin
from ..services.payments import Payments
from ..services.payments.tao import LAST_BLOCK_KEY
from .deps import db

router = APIRouter(prefix="/v1/admin", tags=["admin"], dependencies=[Depends(require_admin)])


class NodeUpdate(BaseModel):
    provider_name: str | None = None
    hourly_price_cents: int | None = None
    operator_owned: bool | None = None
    status: str | None = None
    clear_critical_fault: bool = False


@router.post("/nodes/{node_id}")
def update_node(node_id: str, body: NodeUpdate, _db=Depends(db)):
    with _db.session() as s:
        n = s.get(Node, node_id)
        if n is None:
            raise HTTPException(404, "node not found")
        for f in ("provider_name", "hourly_price_cents", "operator_owned", "status"):
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


# ---- payments -------------------------------------------------------------------------------------------------------------
@router.get("/deposits")
def list_deposits(_db=Depends(db), status: str | None = None, limit: int = 200):
    """All deposits, newest first; ?status=unmatched lists TAO transfers from wallets nobody has linked."""
    with _db.session() as s:
        q = select(Deposit).order_by(Deposit.created_at.desc(), Deposit.id.desc()).limit(limit)
        if status:
            q = q.where(Deposit.status == status)
        return {"deposits": [{**d.as_dict(), "user_id": d.user_id} for d in s.scalars(q).all()]}


class AssignBody(BaseModel):
    user_id: str


@router.post("/deposits/{deposit_id}/assign")
def assign_deposit(deposit_id: str, body: AssignBody, request: Request, _db=Depends(db)):
    """Give an unmatched TAO transfer to the account that sent it (after checking with them out of band)."""
    pay: Payments = request.app.state.payments
    with _db.session() as s:
        d = s.get(Deposit, deposit_id)
        if d is None:
            raise HTTPException(404, "deposit not found")
        if d.status != "unmatched" or d.method != "tao" or pay.tao is None:
            raise HTTPException(409, f"deposit is {d.status}")
        if s.get(User, body.user_id) is None:
            raise HTTPException(404, "user not found")
        d.user_id, d.status = body.user_id, "confirming"
        d.note = f"{d.amount} τ received in block {d.external_ref.split(':')[0]} (assigned by the operator)"
        pay.tao.settle(s, d)
        return {**d.as_dict(), "user_id": d.user_id}


@router.get("/payments")
def payments_status(request: Request, _db=Depends(db)):
    pay: Payments = request.app.state.payments
    with _db.session() as s:
        last = s.get(Setting, LAST_BLOCK_KEY)
    return {**pay.methods(), "tao_last_block": int(last.value) if last else None, "processor_sandbox": pay.settings.nowpayments_sandbox}
