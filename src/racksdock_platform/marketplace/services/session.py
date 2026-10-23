"""Console sessions: the cookie the browser carries, password sign-in and sign-up, and where the console lives.

The console (racksdock-ui) is a static site on its own origin; this platform is its API. Sign-in sets an HMAC-signed cookie
(user_id.sig, services.auth.SessionSigner) that require_user accepts next to API keys. RACKSDOCK_UI_URL is the console's
origin: page requests are redirected there and sign-out and Google sign-in return there. RACKSDOCK_COOKIE_SAMESITE=none makes
the cookie usable when console and API are not the same site (it then has to be Secure, i.e. https)."""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..models import User
from .auth import check_password, hash_password, new_api_key

COOKIE = "racksdock_session"
MIN_PASSWORD = 8

# error codes the console shows (?error=<code> after a redirect, or the JSON detail of a failed sign-in)
ERRORS = {
    "invalid_login": "Email or password is incorrect.",
    "google_only": "This account signs in with Google. Use Continue with Google.",
    "email_taken": "An account with this email already exists.",
    "weak_password": f"Use a password of at least {MIN_PASSWORD} characters.",
    "google_cancelled": "Google sign-in was cancelled.",
    "google_expired": "The Google sign-in took too long or was started in another tab. Please try again.",
    "google_failed": "Google sign-in failed. Please try again.",
    "google_unverified": "Your Google account's email address is not verified, so it cannot be used to sign in.",
}


def current_user(request: Request) -> User | None:
    uid = request.app.state.signer.verify(request.cookies.get(COOKIE))
    if not uid:
        return None
    with request.app.state.db.session() as s:
        return s.get(User, uid)


def sign_in(s, email: str, password: str, signup: bool) -> tuple[User | None, str | None]:
    """The user, or an error code from ERRORS. Sign-up creates the account with a CLI API key."""
    email = email.strip()
    u = s.scalar(select(User).where(func.lower(User.email) == email.lower()))
    if signup:
        if u is not None:
            return None, "email_taken"
        if len(password) < MIN_PASSWORD:
            return None, "weak_password"
        u = User(email=email, password_hash=hash_password(password))
        s.add(u)
        s.flush()
        new_api_key(s, u)
        return u, None
    if u is not None and not u.password_hash:
        return None, "google_only"
    if u is None or not check_password(password, u.password_hash):
        return None, "invalid_login"
    return u, None


def set_cookie(resp, request: Request, user_id: str) -> None:
    st = request.app.state.settings
    samesite = st.cookie_samesite.lower()
    resp.set_cookie(COOKIE, request.app.state.signer.sign(user_id), httponly=True, samesite=samesite, secure=request.url.scheme == "https" or samesite == "none")


def console_url(request: Request, path: str) -> str:
    """A path on the console: absolute when RACKSDOCK_UI_URL is set, otherwise on this server (nothing serves it then)."""
    return request.app.state.settings.ui_url.rstrip("/") + path


def to_console(request: Request, path: str, status: int = 303) -> RedirectResponse:
    return RedirectResponse(console_url(request, path), status_code=status)
