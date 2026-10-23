"""Billing: usage, deposits (TAO, other coins through NOWPayments, free credits off mainnet) and linked wallets."""
from __future__ import annotations

import json
from decimal import ROUND_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from ..models import BillingEvent, Deposit, LinkedWallet, User, now_iso
from ..services.auth import require_user
from ..services.payments import Payments, ledger, wallets
from ..services.payments.nowpayments import SIG_HEADER, NowPayments
from .deps import db

router = APIRouter(prefix="/v1/billing", tags=["billing"])


def payments(request: Request) -> Payments:
    return request.app.state.payments


@router.get("/methods")
def methods(pay: Payments = Depends(payments)):
    return pay.methods()


@router.get("/usage")
def usage(user: User = Depends(require_user), _db=Depends(db), limit: int = 500):
    with _db.session() as s:
        rows = s.scalars(select(BillingEvent).where(BillingEvent.user_id == user.id).order_by(BillingEvent.id.desc()).limit(limit)).all()
        return {"events": [{"kind": r.kind, "cents": r.cents, "instance_id": r.instance_id, "note": r.note, "at": r.at} for r in rows]}


# ---- deposits -----------------------------------------------------------------------------------------------------------
class NewDeposit(BaseModel):
    method: str  # tao | crypto | free
    cents: int


class Purchase(BaseModel):
    cents: int


def _free(s, pay: Payments, user: User, cents: int) -> Deposit:
    if not pay.settings.free_credits_enabled:
        raise HTTPException(403, "free credits are not available on this network")
    return ledger.free_credits(s, s.get(User, user.id), cents)


@router.post("/credits/purchase")
def purchase(body: Purchase, user: User = Depends(require_user), _db=Depends(db), pay: Payments = Depends(payments)):
    """Free, instant credits on test networks (the development stub kept for the CLI and older clients)."""
    if body.cents <= 0:
        raise HTTPException(400, "cents must be positive")
    with _db.session() as s:
        _free(s, pay, user, body.cents)
        return {"credits_cents": s.get(User, user.id).credits_cents}


@router.post("/deposits", status_code=201)
def create_deposit(body: NewDeposit, request: Request, user: User = Depends(require_user), _db=Depends(db), pay: Payments = Depends(payments)):
    """Start a deposit. tao: the amount to send and where; crypto: a checkout URL; free: credited at once."""
    if body.method not in ("tao", "crypto", "free"):
        raise HTTPException(400, "method must be tao, crypto or free")
    if body.cents < pay.settings.min_deposit_cents and body.method != "free":
        raise HTTPException(400, f"the minimum deposit is ${pay.settings.min_deposit_cents / 100:.2f}")
    if body.cents <= 0:
        raise HTTPException(400, "cents must be positive")
    with _db.session() as s:
        if body.method == "free":
            return _free(s, pay, user, body.cents).as_dict()
        if body.method == "tao":
            if pay.tao is None:
                raise HTTPException(403, "TAO payments are not enabled")
            rate = pay.rates.tao_usd()
            if rate is None:
                raise HTTPException(503, "no TAO price is available right now; try again in a minute")
            amount = (Decimal(body.cents) / 100 / rate).quantize(Decimal("0.0001"), ROUND_UP)
            dep = Deposit(user_id=user.id, method="tao", cents_requested=body.cents, asset="TAO", amount=format(amount, "f"), rate=str(rate),
                          note=f"send {format(amount, 'f')} τ to the treasury")
            s.add(dep)
            s.flush()
            return {**dep.as_dict(), "address": pay.settings.tao_treasury, "network": pay.settings.chain_network}
        if not pay.processor.enabled:
            raise HTTPException(403, "card and crypto checkout are not enabled")
        dep = Deposit(user_id=user.id, method="crypto", cents_requested=body.cents, note="checkout opened")
        s.add(dep)
        s.flush()
        base = pay.settings.base_url.rstrip("/")
        try:
            inv = pay.processor.create_invoice(dep.id, body.cents, f"RacksDock credits ${body.cents / 100:.2f} ({user.email})",
                                               f"{base}/v1/billing/webhooks/nowpayments", f"{base}/billing?deposit={dep.id}", f"{base}/billing")
        except Exception as e:
            s.delete(dep)
            raise HTTPException(502, f"the payment processor did not accept the checkout: {e}")
        dep.checkout_url, dep.external_ref = str(inv.get("invoice_url", "")), f"inv:{inv.get('id', '')}"
        return dep.as_dict()


@router.get("/deposits")
def list_deposits(user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        ledger.expire_intents(s)
        return {"deposits": [d.as_dict() for d in ledger.user_deposits(s, user.id)]}


@router.get("/deposits/{deposit_id}")
def get_deposit(deposit_id: str, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        d = s.get(Deposit, deposit_id)
        if d is None or d.user_id != user.id:
            raise HTTPException(404, "deposit not found")
        return d.as_dict()


@router.post("/deposits/{deposit_id}/cancel")
def cancel_deposit(deposit_id: str, user: User = Depends(require_user), _db=Depends(db)):
    """Drop a TAO intent the user is not going to send (a transfer that still arrives credits as a new deposit)."""
    with _db.session() as s:
        d = s.get(Deposit, deposit_id)
        if d is None or d.user_id != user.id:
            raise HTTPException(404, "deposit not found")
        if d.status != "pending":
            raise HTTPException(409, f"deposit is {d.status}")
        d.status, d.updated_at = "expired", now_iso()
        return d.as_dict()


# ---- linked wallets -------------------------------------------------------------------------------------------------------
class LinkBody(BaseModel):
    token: str
    address: str
    signature: str
    label: str = ""


@router.post("/wallets/challenge")
def wallet_challenge(request: Request, user: User = Depends(require_user)):
    """The message to sign with the coldkey (browser wallet `signRaw`, or `btcli wallet sign --message '<message>'`)."""
    return wallets.challenge(request.app.state.signer, user)


@router.post("/wallets/link", status_code=201)
def wallet_link(body: LinkBody, request: Request, user: User = Depends(require_user), _db=Depends(db), pay: Payments = Depends(payments)):
    try:
        addr = wallets.verify_link(request.app.state.signer, user, body.token, body.address, body.signature)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with _db.session() as s:
        w = s.get(LinkedWallet, addr)
        if w is not None and w.user_id != user.id:
            raise HTTPException(409, "this wallet is linked to another account; unlink it there first")
        if w is None:
            s.add(LinkedWallet(address=addr, user_id=user.id, label=body.label.strip()[:64]))
        claimed = pay.tao.claim(s, user.id, addr) if pay.tao else []
        return {"address": addr, "label": body.label.strip()[:64], "claimed": [d.as_dict() for d in claimed]}


@router.get("/wallets")
def wallet_list(user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        rows = s.scalars(select(LinkedWallet).where(LinkedWallet.user_id == user.id).order_by(LinkedWallet.created_at)).all()
        return {"wallets": [{"address": w.address, "label": w.label, "created_at": w.created_at} for w in rows]}


@router.post("/wallets/{address}/unlink")
def wallet_unlink(address: str, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        w = s.get(LinkedWallet, address)
        if w is None or w.user_id != user.id:
            raise HTTPException(404, "wallet not linked")
        s.delete(w)
        return {"unlinked": address}


# ---- processor webhook --------------------------------------------------------------------------------------------------
def apply_payment(s, dep: Deposit, payment: dict, payment_id: str) -> None:
    """Move a crypto deposit to the state NOWPayments reports; credits are added once, and topped up if a partial payment completes."""
    status, cents, asset, amount, note = NowPayments.outcome(payment)
    ref = f"pay:{payment_id}"
    other = s.scalar(select(Deposit).where(Deposit.method == "crypto", Deposit.external_ref == ref, Deposit.id != dep.id))
    if other is not None:
        raise HTTPException(409, "this payment belongs to another deposit")
    dep.external_ref, dep.updated_at = ref, now_iso()
    if asset:
        dep.asset, dep.amount = asset, amount
    if status in ("credited", "underpaid"):
        ledger.credit(s, dep, cents - dep.cents, note, status=status)
    elif dep.cents == 0:  # never downgrade a credited deposit
        dep.status, dep.note = status, note


@router.post("/webhooks/nowpayments", include_in_schema=False)
async def nowpayments_webhook(request: Request, _db=Depends(db), pay: Payments = Depends(payments)):
    if not pay.processor.enabled:
        raise HTTPException(404)
    raw = await request.body()
    try:
        body = json.loads(raw)
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        raise HTTPException(400, "body must be a JSON object")
    if not pay.processor.verify(body, request.headers.get(SIG_HEADER)):
        raise HTTPException(400, "bad signature")
    order_id, payment_id = str(body.get("order_id") or ""), str(body.get("payment_id") or "")
    with _db.session() as s:
        dep = s.get(Deposit, order_id)
        if dep is None or dep.method != "crypto":
            raise HTTPException(404, "unknown order")
        try:
            record = pay.processor.payment(payment_id)  # the API is the source of truth, not the webhook body
        except Exception as e:
            raise HTTPException(502, f"could not confirm the payment with NOWPayments: {e}")
        if str(record.get("order_id") or "") != dep.id:
            raise HTTPException(400, "payment does not belong to this order")
        apply_payment(s, dep, record, payment_id)
        return {"ok": True, "status": dep.status}
