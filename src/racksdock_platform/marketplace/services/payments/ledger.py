"""Deposits and crediting: every way credits are bought ends here, in one Deposit row and one BillingEvent."""
from __future__ import annotations

import datetime as dt
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select

from ...models import BillingEvent, Deposit, User, now_iso

RAO = Decimal(10**9)
INTENT_TTL = dt.timedelta(hours=2)  # a TAO "I will send" intent the user never completed is hidden after this


def tao_str(rao: int) -> str:
    """Whole TAO with up to 9 decimals, no trailing zeros: 1500000000 -> '1.5'."""
    return format((Decimal(rao) / RAO).normalize(), "f") if rao else "0"


def cents_for_rao(rao: int, rate_usd: Decimal) -> int:
    return int((Decimal(rao) * rate_usd * 100 / RAO).to_integral_value(ROUND_DOWN))


def credit(s, dep: Deposit, cents: int, note: str, status: str = "credited") -> None:
    """Add `cents` to the deposit's owner (on top of what it credited before). Caller sets amount/asset/rate first."""
    if dep.user_id is None:
        return
    dep.status, dep.note, dep.updated_at = status, note, now_iso()
    if cents <= 0:
        return
    user = s.get(User, dep.user_id)
    dep.cents += cents
    dep.credited_at = dep.updated_at
    user.credits_cents += cents
    s.add(BillingEvent(user_id=user.id, kind="purchase", cents=cents, note=note))


def free_credits(s, user: User, cents: int) -> Deposit:
    dep = Deposit(user_id=user.id, method="free", cents_requested=cents, asset="USD", amount=f"{cents / 100:.2f}")
    s.add(dep)
    s.flush()
    credit(s, dep, cents, "credits added (test network, nothing charged)")
    return dep


def user_deposits(s, user_id: str, limit: int = 50) -> list[Deposit]:
    rows = s.scalars(select(Deposit).where(Deposit.user_id == user_id).order_by(Deposit.updated_at.desc(), Deposit.created_at.desc()).limit(limit)).all()
    return rows


def expire_intents(s, now: dt.datetime | None = None) -> int:
    """Pending TAO intents older than INTENT_TTL become `expired` (a transfer that arrives later still credits as a new deposit)."""
    now = now or dt.datetime.now(dt.UTC)
    cutoff = (now - INTENT_TTL).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    n = 0
    for d in s.scalars(select(Deposit).where(Deposit.method == "tao", Deposit.status == "pending", Deposit.created_at < cutoff)).all():
        d.status, d.updated_at = "expired", now_iso()
        n += 1
    return n
