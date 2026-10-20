"""Per-minute metering, degraded credits, alerts and auto-delete (docs/02 §9)."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from ..models import BillingEvent, Instance, User

log = logging.getLogger("racksdock.marketplace.billing")


def _parse(ts: str) -> dt.datetime:
    return dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def meter(session, now: dt.datetime | None = None) -> list[tuple[str, str]]:
    """Advance metering for all billable instances. Returns list of (instance_id, action) for the orchestrator (auto_delete)."""
    now = now or dt.datetime.now(dt.UTC)
    actions: list[tuple[str, str]] = []
    for inst in session.scalars(select(Instance).where(Instance.status.in_(["active", "degraded"]))).all():
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
    return actions


def cost_estimate(inst: Instance) -> str:
    return f"{inst.cost_cents / 100:.2f}"
