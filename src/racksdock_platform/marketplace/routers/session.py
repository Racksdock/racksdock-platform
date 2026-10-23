"""The console's server side: its config, password and Google sign-in, and sign-out."""
from __future__ import annotations

import hmac

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select

from ..models import User
from ..services import google_auth
from ..services.auth import new_api_key
from ..services.session import COOKIE, ERRORS, current_user, set_cookie, sign_in, to_console
from .deps import db, settings

router = APIRouter(include_in_schema=False)

def _network_label(st) -> str:
    return f"Testnet · {st.netuid}" if st.chain_network == "test" else ("Local chain" if st.chain_kind == "mock" else f"Subnet {st.netuid}")


@router.get("/ui/config")
def ui_config(request: Request, st=Depends(settings)):
    """What the console needs before its first render: the network, whether Google sign-in exists, and who is signed in."""
    user = current_user(request)
    return {"network_label": _network_label(st), "google_enabled": google_auth.enabled(st), "platform_url": st.base_url, "netuid": st.netuid,
            "chain_network": st.chain_network, "trust_mode": st.trust.mode, "google_errors": ERRORS,
            "user": {"email": user.email, "credits_cents": user.credits_cents} if user else None}


# ---- password sign-in -----------------------------------------------------------------------------------------------------
class Credentials(BaseModel):
    email: str
    password: str


@router.post("/auth/login")
@router.post("/auth/signup")
def auth_password(request: Request, body: Credentials, _db=Depends(db)):
    signup = request.url.path.endswith("signup")
    with _db.session() as s:
        user, err = sign_in(s, body.email, body.password, signup)
        if user is None:
            return JSONResponse({"detail": ERRORS[err]}, status_code=400)
        resp = JSONResponse({"redirect": "/keys" if signup else "/dashboard"})  # a new account adds an SSH key first
        set_cookie(resp, request, user.id)
        return resp


@router.get("/logout")
def logout(request: Request):
    resp = to_console(request, "/")
    resp.delete_cookie(COOKIE)
    return resp


# ---- Google ----------------------------------------------------------------------------------------------------------------
def _google_fail(request: Request, code: str) -> RedirectResponse:
    resp = to_console(request, f"/login?error={code}")
    resp.delete_cookie(google_auth.FLOW_COOKIE, path=google_auth.FLOW_PATH)
    return resp


@router.get("/auth/google")
def google_start(request: Request, st=Depends(settings)):
    if not google_auth.enabled(st):
        raise HTTPException(404)
    url, flow = google_auth.start_url(request)
    resp = RedirectResponse(url, status_code=303)
    # SameSite=Lax: the cookie comes back on Google's top-level redirect to the callback
    resp.set_cookie(google_auth.FLOW_COOKIE, google_auth.seal(request, flow), max_age=google_auth.FLOW_TTL_S, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path=google_auth.FLOW_PATH)
    return resp


@router.get("/auth/google/callback")
def google_callback(request: Request, st=Depends(settings), _db=Depends(db), code: str = "", state: str = "", error: str = ""):
    if not google_auth.enabled(st):
        raise HTTPException(404)
    if error:
        return _google_fail(request, "google_cancelled" if error == "access_denied" else "google_failed")
    flow = google_auth.unseal(request, request.cookies.get(google_auth.FLOW_COOKIE))
    if flow is None or not state or not hmac.compare_digest(state, flow["state"]):
        return _google_fail(request, "google_expired")
    try:
        tokens = google_auth.exchange_code(st, code, flow["verifier"], google_auth.redirect_uri(request))
        c = google_auth.claims(st, tokens["id_token"], flow["nonce"])
    except (httpx.HTTPError, KeyError, ValueError):
        return _google_fail(request, "google_failed")
    email = str(c.get("email", "")).strip().lower()
    if not email or c.get("email_verified") not in (True, "true"):
        return _google_fail(request, "google_unverified")
    with _db.session() as s:
        user = s.scalar(select(User).where(func.lower(User.email) == email))
        created = user is None
        if created:
            user = User(email=email, password_hash="")  # no password: this account signs in with Google
            s.add(user)
            s.flush()
            new_api_key(s, user)
        uid = user.id
    resp = to_console(request, "/keys" if created else "/dashboard")
    set_cookie(resp, request, uid)
    resp.delete_cookie(google_auth.FLOW_COOKIE, path=google_auth.FLOW_PATH)
    return resp


# ---- this server is the API ---------------------------------------------------------------------------------------------------
@router.get("/")
def root(st=Depends(settings)):
    return {"service": "racksdock-platform", "api": "/v1", "docs": "/docs", "console": st.ui_url or None}
