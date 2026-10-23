"""Other coins through NOWPayments: a hosted invoice per deposit, and its IPN webhook to learn the outcome.

Two checks before any credit: the webhook's HMAC-SHA512 signature (over the body with keys sorted, as NOWPayments signs it),
and a fresh read of the payment from NOWPayments' API with our key, so a forged or replayed body can never credit on its own.
Settings: RACKSDOCK_NOWPAYMENTS_API_KEY, RACKSDOCK_NOWPAYMENTS_IPN_SECRET (both from the NOWPayments dashboard; the IPN secret
is shown under Settings > Payments > IPN), RACKSDOCK_NOWPAYMENTS_SANDBOX=1 for their sandbox."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
from decimal import Decimal

import httpx

API_URL = "https://api.nowpayments.io/v1"
SANDBOX_URL = "https://api-sandbox.nowpayments.io/v1"
SIG_HEADER = "x-nowpayments-sig"

# NOWPayments payment_status -> our deposit status
STATUS = {"waiting": "confirming", "confirming": "confirming", "confirmed": "confirming", "sending": "confirming", "partially_paid": "underpaid",
          "finished": "credited", "failed": "failed", "refunded": "failed", "expired": "expired"}


def js_number(v) -> str:
    """A number the way JSON.stringify writes it (NOWPayments signs its JS-serialized body): 10.0 -> 10, 1e-08 -> 1e-8."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TypeError(v)
    if isinstance(v, int):
        return str(v)
    if math.isnan(v) or math.isinf(v):
        return "null"
    if v == int(v) and abs(v) < 1e21:
        return str(int(v))
    r = repr(v)  # shortest round-trip digits, Python's exponent style
    if "e" not in r:
        return r
    mant, exp = r.split("e")
    e = int(exp)
    if -7 <= e < 21:  # JavaScript writes these positionally
        d = Decimal(r)
        return format(d, "f")
    return f"{mant}e{'+' if e > 0 else '-'}{abs(e)}"


def canonical(body) -> str:
    """The signed form: compact JSON with keys sorted (top level, as `JSON.stringify(params, Object.keys(params).sort())`)."""
    def enc(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return js_number(v)
        if isinstance(v, str):
            return json.dumps(v, ensure_ascii=False)
        if isinstance(v, list):
            return "[" + ",".join(enc(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + enc(x) for k, x in v.items()) + "}"
        raise TypeError(type(v))

    return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + enc(body[k]) for k in sorted(body)) + "}"


def sign(secret: str, body) -> str:
    return hmac.new(secret.encode(), canonical(body).encode(), hashlib.sha512).hexdigest()


class NowPayments:
    name = "nowpayments"

    def __init__(self, api_key: str, ipn_secret: str, sandbox: bool = False, request=None):
        self.api_key, self.ipn_secret = api_key, ipn_secret
        self.base = SANDBOX_URL if sandbox else API_URL
        self._request = request or self._http  # (method, path, json) -> dict; replaced in tests

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.ipn_secret)

    def _http(self, method: str, path: str, body: dict | None = None) -> dict:
        r = httpx.request(method, self.base + path, json=body, headers={"x-api-key": self.api_key}, timeout=20)
        r.raise_for_status()
        return r.json()

    def create_invoice(self, deposit_id: str, cents: int, description: str, callback_url: str, success_url: str, cancel_url: str) -> dict:
        """The hosted checkout for one deposit: {'id', 'invoice_url'}; the customer picks the coin there."""
        return self._request("POST", "/invoice", {"price_amount": f"{cents / 100:.2f}", "price_currency": "usd", "order_id": deposit_id,
                                                  "order_description": description, "ipn_callback_url": callback_url, "success_url": success_url,
                                                  "cancel_url": cancel_url})

    def payment(self, payment_id: str) -> dict:
        return self._request("GET", f"/payment/{payment_id}")

    def verify(self, body, signature: str | None) -> bool:
        return bool(signature and self.ipn_secret) and hmac.compare_digest(sign(self.ipn_secret, body), signature.strip().lower())

    @staticmethod
    def outcome(payment: dict) -> tuple[str, int, str, str, str]:
        """(deposit status, cents to credit, asset, amount, note) from a payment record (webhook body or API read)."""
        status = STATUS.get(str(payment.get("payment_status", "")), "confirming")
        price = Decimal(str(payment.get("price_amount") or 0))
        asset = str(payment.get("pay_currency") or "").upper()
        paid = Decimal(str(payment.get("actually_paid") or 0))
        due = Decimal(str(payment.get("pay_amount") or 0))
        cents = int(price * 100)
        if status == "underpaid" and due > 0:
            cents = int(price * 100 * paid / due)  # the share that arrived, at the invoice's own rate
        if status not in ("credited", "underpaid"):
            cents = 0
        amount = format(paid.normalize(), "f") if paid else ""
        note = {"credited": f"{amount} {asset} via NOWPayments", "underpaid": f"{amount} {asset} received of {format(due.normalize(), 'f')} due via NOWPayments",
                "expired": "checkout expired before payment", "failed": "payment failed or was refunded"}.get(status, "waiting for the payment to confirm")
        return status, cents, asset, amount, note
