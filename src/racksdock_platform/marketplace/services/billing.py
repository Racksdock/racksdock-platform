"""Per-minute metering, degraded credits, per-instance alerts and auto-delete (docs/02 §9), and the account balance guard:
below zero the account is alerted once; at -credit_cutoff_cents every running server of the account is deleted."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..models import BillingEvent, Instance, User, now_iso

log = logging.getLogger("racksdock.marketplace.billing")


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def meter(session, now: dt.datetime | None = None, cutoff_cents: int = 500) -> list[tuple[str, str]]:
    """Advance metering for all billable instances. Returns (id, action) pairs for the app: (instance_id, "auto_delete" | "credit_cutoff")
    and (user_id, "credit_alert")."""
    now = now or dt.datetime.now(dt.UTC)
    actions: list[tuple[str, str]] = []
    running = session.scalars(select(Instance).where(Instance.status.in_(["active", "degraded"]))).all()
    for inst in running:
        last = _parse(inst.last_metered_at or inst.active_at or inst.created_at)
        minutes = int((now - last).total_seconds() // 60)
        if minutes <= 0:
            continue
        inst.last_metered_at = (last + dt.timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if inst.status == "degraded":
            inst.degraded_minutes += minutes
            if inst.user_id:
                session.add(BillingEvent(user_id=inst.user_id, instance_id=inst.id, kind="credit", cents=0, note=f"{minutes} degraded min credited"))
            continue
        inst.billed_minutes += minutes
        cents = round(inst.hourly_price_cents * minutes / 60)
        inst.cost_cents += cents
        if inst.user_id and cents:
            user = session.get(User, inst.user_id)
            if user is not None:
                user.credits_cents -= cents
            session.add(BillingEvent(user_id=inst.user_id, instance_id=inst.id, kind="charge", cents=cents, note=f"{minutes} min"))
        # thresholds
        for which, cfg in (("alert", inst.alert), ("auto_delete", inst.auto_delete)):
            if not cfg:
                continue
            hit = False
            if cfg.get("spend_threshold") and inst.cost_cents >= int(float(cfg["spend_threshold"]) * 100):
                hit = True
            if cfg.get("date_threshold") and now >= _parse(cfg["date_threshold"]):
                hit = True
            if hit:
                if which == "alert" and not inst.alert_sent:
                    inst.alert_sent = True
                    actions.append((inst.id, "alert"))
                if which == "auto_delete":
                    actions.append((inst.id, "auto_delete"))
    actions += balance_guard(session, running, cutoff_cents)
    return actions


def usd(cents: int) -> str:
    return f"{'-' if cents < 0 else ''}${abs(cents) / 100:.2f}"


def balance_guard(session, running: list[Instance], cutoff_cents: int) -> list[tuple[str, str]]:
    """Accounts whose balance went negative get one alert (a billing event the console shows) until it is positive again; at
    -cutoff_cents every running server of the account is deleted."""
    actions: list[tuple[str, str]] = []
    by_user: dict[str, list[Instance]] = {}
    for inst in running:
        if inst.user_id:
            by_user.setdefault(inst.user_id, []).append(inst)
    for uid, insts in by_user.items():
        user = session.get(User, uid)
        if user is None:
            continue
        if user.credits_cents >= 0:
            user.credit_alert_at = None
            continue
        if not user.credit_alert_at:
            user.credit_alert_at = now_iso()
            session.add(BillingEvent(user_id=uid, kind="alert", cents=0,
                                     note=f"Credit balance is {usd(user.credits_cents)}. Add credits: at {usd(-cutoff_cents)} every running server is deleted."))
            actions.append((uid, "credit_alert"))
        if user.credits_cents <= -cutoff_cents:
            session.add(BillingEvent(user_id=uid, kind="cutoff", cents=0,
                                     note=f"{len(insts)} server{'s' if len(insts) != 1 else ''} deleted: credit balance reached {usd(user.credits_cents)}."))
            actions += [(inst.id, "credit_cutoff") for inst in insts]
    return actions


def cost_estimate(inst: Instance) -> str:
    return f"{inst.cost_cents / 100:.2f}"
