"""Buying credits: TAO transfers to the treasury (linked wallets, the block watcher), NOWPayments checkouts (webhook + API
confirmation), free credits off mainnet, and the admin's view of unmatched transfers."""
import json
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from racksdock_subnet.verifier.config import TrustConfig

from racksdock_platform.marketplace.app import create_app
from racksdock_platform.marketplace.config import Settings
from racksdock_platform.marketplace.services.payments import nowpayments
from racksdock_platform.marketplace.services.payments.rates import Rates

sp_core = pytest.importorskip("bittensor.sp_core", reason="TAO payments need the bittensor SDK (pip install 'racksdock-platform[tao]')")
TREASURY = "5FYPgyWUYd5ucaaYYaeFUKtKc7oHanSBXC59mAGQoaM9Rdbv"


@pytest.fixture
def pay_app(tmp_path, ca):
    st = Settings(db_url=f"sqlite:///{tmp_path}/p.db", base_url="http://test", chain_kind="mock", chain_file=tmp_path / "chain.json", platform_key_path=tmp_path / "platform.key", trust=TrustConfig(mode="dev", dev_ca_dir=ca.dir),
                  tao_treasury=TREASURY, tao_usd=250.0, nowpayments_api_key="np-key", nowpayments_ipn_secret="ipn-secret", free_credits=False, admin_token="adm")
    return create_app(st, background=False)


@pytest.fixture
def client(pay_app):
    return TestClient(pay_app)


def signup(client, email="a@b.c"):
    r = client.post("/v1/account/signup", json={"email": email, "password": "password1"})
    return {"X-API-KEY": r.json()["api_key"]}, r.json()["user_id"]


def link_wallet(client, h, kp, wrap=False, label="main"):
    ch = client.post("/v1/billing/wallets/challenge", headers=h).json()
    msg = ch["message"].encode()
    sig = kp.sign(b"<Bytes>" + msg + b"</Bytes>" if wrap else msg)
    return client.post("/v1/billing/wallets/link", json={"token": ch["token"], "address": kp.ss58_address, "signature": "0x" + sig.hex(), "label": label}, headers=h)


def keypair():
    return sp_core.Keypair.create_from_mnemonic(sp_core.Keypair.generate_mnemonic())


# ---- methods and the free stub -------------------------------------------------------------------------------------------
def test_methods_reflect_the_configuration(client, app):
    m = client.get("/v1/billing/methods").json()
    assert m["tao"] == {"enabled": True, "treasury": TREASURY, "network": "finney", "rate_usd": "250.0"} and m["crypto"]["enabled"] and not m["free"]["enabled"]
    assert m["min_cents"] == 1000
    plain = TestClient(app).get("/v1/billing/methods").json()  # the default test app: no treasury, no processor keys, mock chain => free credits
    assert not plain["tao"]["enabled"] and not plain["crypto"]["enabled"] and plain["free"]["enabled"]


def test_free_credits_only_where_enabled(client, app):
    h, _ = signup(client)
    assert client.post("/v1/billing/credits/purchase", json={"cents": 5000}, headers=h).status_code == 403
    assert client.post("/v1/billing/deposits", json={"method": "free", "cents": 5000}, headers=h).status_code == 403
    c2 = TestClient(app)
    h2, _ = signup(c2)
    d = c2.post("/v1/billing/deposits", json={"method": "free", "cents": 5000}, headers=h2).json()
    assert d["method"] == "free" and d["status"] == "credited" and d["cents"] == 5000
    assert c2.get("/v1/account", headers=h2).json()["credits_cents"] == 5000
    assert c2.get("/v1/billing/usage", headers=h2).json()["events"][0]["kind"] == "purchase"


def test_free_credits_default_is_off_on_mainnet_only():
    assert Settings(chain_kind="mock", chain_network="finney").free_credits_enabled
    assert Settings(chain_kind="subtensor", chain_network="test").free_credits_enabled
    assert not Settings(chain_kind="subtensor", chain_network="finney").free_credits_enabled
    assert Settings(chain_kind="subtensor", chain_network="finney", free_credits=True).free_credits_enabled


# ---- linking wallets -----------------------------------------------------------------------------------------------------------
def test_link_wallet_with_a_signed_challenge(client):
    h, _ = signup(client)
    kp = keypair()
    r = link_wallet(client, h, kp)
    assert r.status_code == 201 and r.json()["address"] == kp.ss58_address and r.json()["claimed"] == []
    assert client.get("/v1/billing/wallets", headers=h).json()["wallets"][0]["label"] == "main"
    kp2 = keypair()
    assert link_wallet(client, h, kp2, wrap=True).status_code == 201  # a browser wallet's signRaw wraps the bytes
    # the wrong key, a stale token and junk are refused; another account cannot take a linked wallet
    ch = client.post("/v1/billing/wallets/challenge", headers=h).json()
    bad = client.post("/v1/billing/wallets/link", json={"token": ch["token"], "address": kp.ss58_address, "signature": "0x" + keypair().sign(ch["message"].encode()).hex()}, headers=h)
    assert bad.status_code == 400 and "does not match" in bad.json()["detail"]
    assert client.post("/v1/billing/wallets/link", json={"token": "link:x:1.sig", "address": kp.ss58_address, "signature": "00"}, headers=h).status_code == 400
    assert client.post("/v1/billing/wallets/link", json={"token": ch["token"], "address": "not-an-address", "signature": "00" * 64}, headers=h).status_code == 400
    h2, _ = signup(client, "o@b.c")
    assert link_wallet(client, h2, kp).status_code == 409
    assert client.post(f"/v1/billing/wallets/{kp.ss58_address}/unlink", headers=h).json()["unlinked"] == kp.ss58_address
    assert link_wallet(client, h2, kp).status_code == 201


# ---- TAO transfers -----------------------------------------------------------------------------------------------------------
def test_tao_intent_then_transfer_credits_at_the_rate(client, pay_app):
    h, uid = signup(client)
    kp = keypair()
    link_wallet(client, h, kp)
    r = client.post("/v1/billing/deposits", json={"method": "tao", "cents": 50000}, headers=h)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["status"] == "pending" and d["address"] == TREASURY and d["amount"] == "2.0000" and d["rate"] == "250.0"  # $500 at $250
    assert client.post("/v1/billing/deposits", json={"method": "tao", "cents": 500}, headers=h).status_code == 400  # below the minimum
    tao = pay_app.state.payments.tao
    tao.chain.add_transfer(100, kp.ss58_address, "5SomeoneElse", 10**9)  # not to the treasury
    tao.chain.add_transfer(101, kp.ss58_address, TREASURY, 2_000_000_000)  # 2 τ
    assert tao.scan_once() == 1  # a fresh database starts at the finalized head: block 101
    got = client.get(f"/v1/billing/deposits/{d['id']}", headers=h).json()
    assert got["status"] == "credited" and got["cents"] == 50000 and got["amount"] == "2" and got["external_ref"] == "101:0" and got["from_address"] == kp.ss58_address
    assert client.get("/v1/account", headers=h).json()["credits_cents"] == 50000
    ev = client.get("/v1/billing/usage", headers=h).json()["events"][0]
    assert ev["kind"] == "purchase" and ev["cents"] == 50000 and "2 τ at $250.0" in ev["note"]
    assert tao.scan_once() == 0 and client.get("/v1/account", headers=h).json()["credits_cents"] == 50000  # nothing new, nothing double
    # a second transfer without an intent is a deposit of its own, floored to the cent
    tao.chain.add_transfer(102, kp.ss58_address, TREASURY, 123_456_789)
    tao.scan_once()
    deps = client.get("/v1/billing/deposits", headers=h).json()["deposits"]
    second = next(x for x in deps if x["external_ref"] == "102:0")
    assert [x["status"] for x in deps] == ["credited", "credited"] and second["cents"] == 3086 and second["amount"] == "0.123456789"


def test_tao_from_an_unlinked_wallet_waits_and_is_claimed_on_link(client, pay_app):
    h, uid = signup(client)
    kp = keypair()
    tao = pay_app.state.payments.tao
    tao.chain.add_transfer(200, kp.ss58_address, TREASURY, 4 * 10**9)
    tao.scan_once()
    assert client.get("/v1/billing/deposits", headers=h).json()["deposits"] == []
    adm = {"X-Admin-Token": "adm"}
    un = client.get("/v1/admin/deposits?status=unmatched", headers=adm).json()["deposits"]
    assert len(un) == 1 and un[0]["user_id"] is None and un[0]["amount"] == "4"
    r = link_wallet(client, h, kp)
    assert r.json()["claimed"][0]["status"] == "credited" and r.json()["claimed"][0]["cents"] == 100000
    assert client.get("/v1/account", headers=h).json()["credits_cents"] == 100000
    # the operator can also assign one by hand
    kp2 = keypair()
    tao.chain.add_transfer(201, kp2.ss58_address, TREASURY, 10**9)
    tao.scan_once()
    dep_id = client.get("/v1/admin/deposits?status=unmatched", headers=adm).json()["deposits"][0]["id"]
    assert client.post(f"/v1/admin/deposits/{dep_id}/assign", json={"user_id": "nope"}, headers=adm).status_code == 404
    assert client.post(f"/v1/admin/deposits/{dep_id}/assign", json={"user_id": uid}, headers=adm).json()["status"] == "credited"
    assert client.get("/v1/account", headers=h).json()["credits_cents"] == 125000
    st = client.get("/v1/admin/payments", headers=adm).json()
    assert st["tao_last_block"] == 201 and st["tao"]["enabled"]


def test_tao_waits_for_a_price_and_scans_from_now_on_a_fresh_database(client, pay_app):
    h, _ = signup(client)
    kp = keypair()
    link_wallet(client, h, kp)
    tao = pay_app.state.payments.tao
    quotes = {"n": 0}
    tao.rates = Rates(fetch=lambda url: (_ for _ in ()).throw(RuntimeError("down")) if quotes["n"] == 0 else {"bittensor": {"usd": 300}} if "coingecko" in url
                      else {"result": {"TAOUSD": {"c": ["301.5", "1"]}}} if "kraken" in url else {"price": "600"})  # binance is an outlier
    tao.chain.add_transfer(300, kp.ss58_address, TREASURY, 10**9)  # nobody scanned yet and the head moves on: a fresh database starts at the head, so this is history
    tao.chain.add_transfer(301, kp.ss58_address, TREASURY, 10**9)
    assert tao.scan_once() == 1
    d = client.get("/v1/billing/deposits", headers=h).json()["deposits"][0]
    assert d["status"] == "confirming" and d["cents"] == 0  # no price source answered
    quotes["n"] = 1
    tao.scan_once()
    d = client.get("/v1/billing/deposits", headers=h).json()["deposits"][0]
    assert d["status"] == "credited" and d["rate"] == "300.75" and d["cents"] == 30075  # median of the two close quotes; the outlier dropped


def test_cancel_and_expire_tao_intents(client, pay_app):
    h, _ = signup(client)
    d = client.post("/v1/billing/deposits", json={"method": "tao", "cents": 1000}, headers=h).json()
    assert client.post(f"/v1/billing/deposits/{d['id']}/cancel", headers=h).json()["status"] == "expired"
    assert client.post(f"/v1/billing/deposits/{d['id']}/cancel", headers=h).status_code == 409
    d2 = client.post("/v1/billing/deposits", json={"method": "tao", "cents": 1000}, headers=h).json()
    from racksdock_platform.marketplace.models import Deposit
    with pay_app.state.db.session() as s:
        s.get(Deposit, d2["id"]).created_at = "2020-01-01T00:00:00Z"
    assert [x["status"] for x in client.get("/v1/billing/deposits", headers=h).json()["deposits"]] == ["expired", "expired"]
    h2, _ = signup(client, "o@b.c")
    assert client.get(f"/v1/billing/deposits/{d['id']}", headers=h2).status_code == 404


# ---- NOWPayments --------------------------------------------------------------------------------------------------------------
class FakeNow:
    """Stands in for NOWPayments' API: records the invoice, serves the payment record the test sets."""

    def __init__(self):
        self.invoices, self.payments = [], {}

    def __call__(self, method, path, body=None):
        if method == "POST" and path == "/invoice":
            self.invoices.append(body)
            n = len(self.invoices)  # every invoice gets its own id, as at NOWPayments
            return {"id": f"inv-{n}", "invoice_url": f"https://nowpayments.io/payment/?iid=inv-{n}"}
        if method == "GET" and path.startswith("/payment/"):
            return self.payments[path.split("/")[-1]]
        raise AssertionError((method, path))


def ipn(client, body):
    raw = json.dumps(body)
    return client.post("/v1/billing/webhooks/nowpayments", content=raw, headers={"Content-Type": "application/json", nowpayments.SIG_HEADER: nowpayments.sign("ipn-secret", body)})


def test_crypto_checkout_and_webhook(client, pay_app):
    fake = FakeNow()
    pay_app.state.payments.processor._request = fake
    h, _ = signup(client)
    r = client.post("/v1/billing/deposits", json={"method": "crypto", "cents": 10000}, headers=h)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["checkout_url"].startswith("https://nowpayments.io/") and d["status"] == "pending"
    inv = fake.invoices[0]
    assert inv["price_amount"] == "100.00" and inv["order_id"] == d["id"] and inv["ipn_callback_url"] == "http://test/v1/billing/webhooks/nowpayments"
    assert inv["success_url"] == f"http://test/billing?deposit={d['id']}"
    # with the console on its own origin the customer returns there, and the webhook can go through an https proxy of the API
    pay_app.state.settings.ui_url, pay_app.state.settings.nowpayments_ipn_url = "https://console.example.com", "https://console.example.com/v1/billing/webhooks/nowpayments"
    d_console = client.post("/v1/billing/deposits", json={"method": "crypto", "cents": 1500}, headers=h).json()
    inv2 = fake.invoices[1]
    assert inv2["success_url"] == f"https://console.example.com/billing?deposit={d_console['id']}" and inv2["cancel_url"] == "https://console.example.com/billing"
    assert inv2["ipn_callback_url"] == "https://console.example.com/v1/billing/webhooks/nowpayments"
    pay_app.state.settings.ui_url, pay_app.state.settings.nowpayments_ipn_url = "", None
    # a webhook needs a valid signature and a payment the API confirms for this order
    base = {"payment_id": 777, "invoice_id": "inv-1", "payment_status": "waiting", "pay_address": "bc1q...", "price_amount": 100, "price_currency": "usd",
            "pay_amount": 0.0015, "actually_paid": 0, "pay_currency": "btc", "order_id": d["id"], "order_description": "x", "outcome_amount": 98.5, "outcome_currency": "usdt"}
    r = client.post("/v1/billing/webhooks/nowpayments", content=json.dumps(base), headers={"Content-Type": "application/json", nowpayments.SIG_HEADER: "00"})
    assert r.status_code == 400
    assert ipn(client, {**base, "order_id": "dep_missing"}).status_code == 404
    fake.payments["777"] = {**base, "order_id": "dep_other"}
    assert ipn(client, base).status_code == 400  # the API says this payment is someone else's
    fake.payments["777"] = base
    assert ipn(client, base).json()["status"] == "confirming"
    assert client.get("/v1/account", headers=h).json()["credits_cents"] == 0
    fake.payments["777"] = {**base, "payment_status": "partially_paid", "actually_paid": 0.0012}
    assert ipn(client, {**base, "payment_status": "partially_paid"}).json()["status"] == "underpaid"
    assert client.get("/v1/account", headers=h).json()["credits_cents"] == 8000  # 0.0012 of 0.0015 => 80% of $100
    fake.payments["777"] = {**base, "payment_status": "finished", "actually_paid": 0.0015}
    assert ipn(client, {**base, "payment_status": "finished"}).json()["status"] == "credited"
    assert ipn(client, {**base, "payment_status": "finished"}).json()["status"] == "credited"  # replayed: no double credit
    got = client.get(f"/v1/billing/deposits/{d['id']}", headers=h).json()
    assert got["cents"] == 10000 and got["asset"] == "BTC" and got["amount"] == "0.0015" and got["external_ref"] == "pay:777"
    assert client.get("/v1/account", headers=h).json()["credits_cents"] == 10000
    assert [e["cents"] for e in client.get("/v1/billing/usage", headers=h).json()["events"]] == [2000, 8000]
    # an expired checkout never credits, and a processor outage is a clean error
    d2 = client.post("/v1/billing/deposits", json={"method": "crypto", "cents": 2000}, headers=h).json()
    fake.payments["778"] = {**base, "payment_id": 778, "order_id": d2["id"], "payment_status": "expired", "price_amount": 20}
    assert ipn(client, {**base, "payment_id": 778, "order_id": d2["id"], "payment_status": "expired"}).json()["status"] == "expired"
    pay_app.state.payments.processor._request = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("503"))
    assert client.post("/v1/billing/deposits", json={"method": "crypto", "cents": 2000}, headers=h).status_code == 502
    assert len(client.get("/v1/billing/deposits", headers=h).json()["deposits"]) == 3  # d, d_console, d2: the failed checkout left no row


def test_signature_canonical_form_matches_javascript():
    body = {"price_amount": 10.0, "pay_amount": 1e-08, "actually_paid": 0.00012, "order_id": "dep_1", "n": None, "big": 1e21, "s": "a/b é", "ok": True, "list": [1.5, 2.0]}
    assert nowpayments.canonical(body) == '{"actually_paid":0.00012,"big":1e+21,"list":[1.5,2],"n":null,"ok":true,"order_id":"dep_1","pay_amount":1e-8,"price_amount":10,"s":"a/b é"}'
    assert nowpayments.js_number(0.1 + 0.2) == "0.30000000000000004" and nowpayments.js_number(1e-7) == "0.0000001" and nowpayments.js_number(123456789012345680000) == "123456789012345680000"
    np = nowpayments.NowPayments("k", "secret")
    assert np.verify(body, nowpayments.sign("secret", body)) and not np.verify(body, nowpayments.sign("other", body)) and not np.verify(body, None)


def test_outcomes():
    out = nowpayments.NowPayments.outcome
    assert out({"payment_status": "finished", "price_amount": "49.99", "pay_currency": "eth", "actually_paid": "0.0171", "pay_amount": "0.0171"})[:4] == ("credited", 4999, "ETH", "0.0171")
    assert out({"payment_status": "partially_paid", "price_amount": 100, "pay_currency": "usdttrc20", "actually_paid": 25, "pay_amount": 100})[:2] == ("underpaid", 2500)
    assert out({"payment_status": "refunded", "price_amount": 100})[:2] == ("failed", 0) and out({"payment_status": "sending"})[0] == "confirming"
    assert Decimal("1") == 1


def test_challenge_message_is_readable(client):
    h, _ = signup(client)
    ch = client.post("/v1/billing/wallets/challenge", headers=h).json()
    assert ch["message"].startswith("RacksDock: link this wallet to a@b.c (valid until 20") and ch["message"].endswith(" UTC)")
