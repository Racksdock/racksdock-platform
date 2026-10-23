"""The console's server side: its config endpoint, password sign-in and sign-up with the session cookie, sign-out, and the
redirect of page paths to the console's own origin."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from racksdock_platform.marketplace.models import User
from racksdock_platform.marketplace.services.session import ERRORS


def test_config_sign_up_log_in_and_out(app):
    c = TestClient(app)
    cfg = c.get("/ui/config").json()
    assert cfg["user"] is None and cfg["network_label"] == "Local chain" and cfg["google_enabled"] is False and cfg["platform_url"] == "http://test"
    assert cfg["google_errors"]["google_failed"] and cfg["google_errors"]["invalid_login"]
    assert c.post("/auth/signup", json={"email": "a@b.c", "password": "short"}).json()["detail"] == ERRORS["weak_password"]
    r = c.post("/auth/signup", json={"email": "a@b.c", "password": "password1"})
    assert r.status_code == 200 and r.json() == {"redirect": "/keys"} and "racksdock_session" in r.cookies  # a new account adds a key first
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" not in cookie  # http test server, same site
    with app.state.db.session() as s:
        assert s.scalar(select(User).where(User.email == "a@b.c")) is not None
    assert c.get("/ui/config").json()["user"] == {"email": "a@b.c", "credits_cents": 0}
    assert c.get("/v1/account").json()["email"] == "a@b.c"  # the API accepts the cookie
    assert c.post("/auth/signup", json={"email": "A@B.C", "password": "password1"}).json()["detail"] == ERRORS["email_taken"]
    assert c.post("/auth/login", json={"email": "a@b.c", "password": "wrong"}).json()["detail"] == ERRORS["invalid_login"]
    r = c.get("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/ui/config").json()["user"] is None and c.get("/v1/account").status_code == 401
    r = c.post("/auth/login", json={"email": "A@b.c", "password": "password1"})
    assert r.json() == {"redirect": "/dashboard"} and c.get("/v1/account").status_code == 200


def test_this_server_is_only_the_api(app):
    c = TestClient(app)
    assert c.get("/").json() == {"service": "racksdock-platform", "api": "/v1", "docs": "/docs", "console": None}
    assert c.get("/dashboard").status_code == 404 and c.get("/nodes/sha256:ab").status_code == 404  # console paths are not served here
    app.state.settings.ui_url = "https://console.example.com"
    try:
        assert c.get("/").json()["console"] == "https://console.example.com"
        c.post("/auth/signup", json={"email": "o@b.c", "password": "password1"})
        assert c.get("/logout", follow_redirects=False).headers["location"] == "https://console.example.com/"  # sign-out returns to the console
        assert c.get("/v1/nodes").status_code == 200
    finally:
        app.state.settings.ui_url = ""


def test_cross_site_cookie(app):
    """Console and API on different sites: the cookie must be SameSite=None, which forces Secure."""
    app.state.settings.cookie_samesite = "none"
    try:
        cookie = TestClient(app).post("/auth/signup", json={"email": "x@b.c", "password": "password1"}).headers["set-cookie"].lower()
        assert "samesite=none" in cookie and "secure" in cookie and "httponly" in cookie
    finally:
        app.state.settings.cookie_samesite = "lax"
