"""Continue with Google: OpenID Connect sign-in for the console (authorization code flow with PKCE, state and nonce).

Settings on the platform server; the console shows the button only when both are set:
  RACKSDOCK_GOOGLE_CLIENT_ID, RACKSDOCK_GOOGLE_CLIENT_SECRET   an OAuth client of type "Web application" in Google Cloud
  RACKSDOCK_GOOGLE_REDIRECT_URI                                 optional; default {RACKSDOCK_BASE_URL}/auth/google/callback
Google accepts only https:// redirect URIs on a real domain (http:// is allowed for localhost only).

A Google account is matched to a console account by its verified email: an existing account signs in, a new email gets a new
account (with a CLI API key, as sign-up does). Accounts created this way have no password and can sign in only with Google.
The ID token comes straight from Google's token endpoint over TLS in exchange for the code and the client secret, so its
issuer is established by TLS (OpenID Connect Core 3.1.3.7); its claims (issuer, audience, expiry, nonce, verified email) are
checked here."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlencode

import httpx
from fastapi import Request

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = ("accounts.google.com", "https://accounts.google.com")
FLOW_COOKIE = "racksdock_oauth"
FLOW_PATH = "/auth/google"
FLOW_TTL_S = 600


def enabled(settings) -> bool:
    return bool(settings.google_client_id and settings.google_client_secret)


def redirect_uri(request: Request) -> str:
    st = request.app.state.settings
    return st.google_redirect_uri or st.base_url.rstrip("/") + "/auth/google/callback"


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64url(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def seal(request: Request, data: dict) -> str:
    """The flow's secrets travel in an HMAC-signed, short-lived cookie (no server-side store)."""
    return request.app.state.signer.sign(_b64url(json.dumps(data, separators=(",", ":")).encode()))


def unseal(request: Request, token: str | None) -> dict | None:
    body = request.app.state.signer.verify(token)
    if not body:
        return None
    try:
        data = json.loads(_unb64url(body))
    except ValueError:
        return None
    return data if data.get("exp", 0) > time.time() else None


def start_url(request: Request) -> tuple[str, dict]:
    """Google's authorization URL and the flow state to seal into the cookie."""
    st = request.app.state.settings
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    url = AUTH_URL + "?" + urlencode({"client_id": st.google_client_id, "redirect_uri": redirect_uri(request), "response_type": "code", "scope": "openid email",
                                      "state": state, "nonce": nonce, "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"})
    return url, {"state": state, "nonce": nonce, "verifier": verifier, "exp": time.time() + FLOW_TTL_S}


def exchange_code(settings, code: str, verifier: str, redirect: str) -> dict:
    """Code -> tokens at Google's token endpoint. Separate so tests can replace it."""
    r = httpx.post(TOKEN_URL, data={"code": code, "client_id": settings.google_client_id, "client_secret": settings.google_client_secret,
                                    "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier}, timeout=15)
    r.raise_for_status()
    return r.json()


def claims(settings, id_token: str, nonce: str) -> dict:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed ID token")
    c = json.loads(_unb64url(parts[1]))
    cid = settings.google_client_id
    if c.get("iss") not in ISSUERS:
        raise ValueError("wrong issuer")
    if c.get("aud") != cid and cid not in (c.get("aud") or []):
        raise ValueError("wrong audience")
    if float(c.get("exp", 0)) < time.time():
        raise ValueError("expired ID token")
    if not hmac.compare_digest(str(c.get("nonce", "")), nonce):
        raise ValueError("nonce mismatch")
    return c
