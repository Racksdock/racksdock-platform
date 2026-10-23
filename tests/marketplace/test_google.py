"""Continue with Google: hidden until configured; the code flow checks state, nonce, audience and a verified email."""
import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from racksdock_platform.marketplace.models import ApiKey, User
from racksdock_platform.marketplace.services import google_auth

CLIENT_ID = "123-test.apps.googleusercontent.com"


def jwt(claims: dict) -> str:  # the token endpoint's ID token; its signature is not what the platform checks
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'RS256'})}.{enc(claims)}.c2ln"


@pytest.fixture
def google(app, monkeypatch):
    app.state.settings.google_client_id, app.state.settings.google_client_secret = CLIENT_ID, "s3cret"
    seen = {}

    def fake_exchange(settings, code, verifier, redirect):
        seen.update(code=code, verifier=verifier, redirect=redirect)
        return {"id_token": jwt(seen["claims"])}

    monkeypatch.setattr(google_auth, "exchange_code", fake_exchange)
    yield seen
    app.state.settings.google_client_id = app.state.settings.google_client_secret = ""


def start(client):
    r = client.get("/auth/google", follow_redirects=False)
    assert r.status_code == 303
    url = urlparse(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "accounts.google.com" and q["client_id"] == CLIENT_ID and q["code_challenge_method"] == "S256" and q["scope"] == "openid email"
    assert q["redirect_uri"] == "http://test/auth/google/callback"
    return q


def claims(q, email="new@gmail.com", **over):
    return {"iss": "https://accounts.google.com", "aud": CLIENT_ID, "exp": time.time() + 300, "nonce": q["nonce"], "email": email, "email_verified": True, **over}


def test_hidden_until_configured(app):
    c = TestClient(app)
    assert c.get("/ui/config").json()["google_enabled"] is False
    assert c.get("/auth/google", follow_redirects=False).status_code == 404


def test_new_google_account_is_created_then_signs_in_again(app, google):
    c = TestClient(app)
    assert c.get("/ui/config").json()["google_enabled"] is True
    q = start(c)
    google["claims"] = claims(q, email="New@Gmail.com")
    r = c.get("/auth/google/callback", params={"code": "c1", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/keys" and "racksdock_session" in r.cookies
    assert google["code"] == "c1" and len(google["verifier"]) > 40 and google["redirect"].endswith("/auth/google/callback")
    with app.state.db.session() as s:
        u = s.scalar(select(User).where(User.email == "new@gmail.com"))
        assert u is not None and u.password_hash == "" and s.scalar(select(ApiKey).where(ApiKey.user_id == u.id)) is not None
    assert c.get("/ui/config").json()["user"]["email"] == "new@gmail.com"  # signed in
    c.get("/logout")
    q = start(c)
    google["claims"] = claims(q, email="new@gmail.com")
    r = c.get("/auth/google/callback", params={"code": "c2", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard"  # an existing account just signs in
    assert c.post("/auth/login", json={"email": "new@gmail.com", "password": "x"}).json()["detail"].startswith("This account signs in with Google")


def test_existing_password_account_signs_in_with_the_same_email(app, google):
    c = TestClient(app)
    c.post("/auth/signup", json={"email": "pw@gmail.com", "password": "password1"})
    c.get("/logout")
    q = start(c)
    google["claims"] = claims(q, email="pw@gmail.com")
    r = c.get("/auth/google/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.headers["location"] == "/dashboard" and c.get("/ui/config").json()["user"]["email"] == "pw@gmail.com"


def test_rejections(app, google):
    c = TestClient(app)
    q = start(c)
    # wrong state (another tab, a forged link) and a missing flow cookie
    assert c.get("/auth/google/callback", params={"code": "c", "state": "nope"}, follow_redirects=False).headers["location"] == "/login?error=google_expired"
    assert c.get("/auth/google/callback", params={"error": "access_denied"}, follow_redirects=False).headers["location"] == "/login?error=google_cancelled"
    for bad in ({"aud": "other-client"}, {"nonce": "x"}, {"exp": time.time() - 1}, {"iss": "https://evil.example"}):
        q = start(c)
        google["claims"] = claims(q, **bad)
        assert c.get("/auth/google/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False).headers["location"] == "/login?error=google_failed", bad
    q = start(c)
    google["claims"] = claims(q, email_verified=False)
    assert c.get("/auth/google/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False).headers["location"] == "/login?error=google_unverified"
    assert c.get("/ui/config").json()["user"] is None
    # with the console on its own origin the browser goes back there
    app.state.settings.ui_url = "https://console.example.com"
    try:
        q = start(c)
        google["claims"] = claims(q, email="back@gmail.com")
        assert c.get("/auth/google/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False).headers["location"] == "https://console.example.com/keys"
    finally:
        app.state.settings.ui_url = ""
