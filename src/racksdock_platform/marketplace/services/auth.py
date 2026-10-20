from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select

from ..models import ApiKey, User


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100_000).hex()
    return f"{salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, salt), stored)


def new_api_key(session, user: User, name: str = "default") -> str:
    raw = "racksdock_" + secrets.token_urlsafe(32)
    session.add(ApiKey(user_id=user.id, key_hash=hash_key(raw), prefix=raw[:12], name=name))
    return raw


def user_for_key(session, raw: str | None) -> User | None:
    if not raw:
        return None
    k = session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_key(raw)))
    if not k:
        return None
    return session.get(User, k.user_id)


class SessionSigner:
    """HMAC-signed console session cookie: user_id.sig"""

    def __init__(self, secret: str):
        self.secret = secret.encode()

    def sign(self, user_id: str) -> str:
        sig = hmac.new(self.secret, user_id.encode(), hashlib.sha256).hexdigest()
        return f"{user_id}.{sig}"

    def verify(self, token: str | None) -> str | None:
        if not token or "." not in token:
            return None
        uid, sig = token.rsplit(".", 1)
        if hmac.compare_digest(hmac.new(self.secret, uid.encode(), hashlib.sha256).hexdigest(), sig):
            return uid
        return None


def require_user(request: Request, x_api_key: str | None = Header(default=None)) -> User:
    app = request.app
    with app.state.db.session() as s:
        user = user_for_key(s, x_api_key)
        if user is None:
            uid = app.state.signer.verify(request.cookies.get("racksdock_session"))
            user = s.get(User, uid) if uid else None
        if user is None:
            raise HTTPException(401, "invalid or missing API key")
        return user


def require_role(*roles: str):
    def dep(user: User = Depends(require_user)) -> User:
        if user.role not in roles and user.role != "admin":
            raise HTTPException(403, f"requires role {roles}")
        return user

    return dep


def require_admin(request: Request, x_admin_token: str | None = Header(default=None)) -> None:
    if x_admin_token != request.app.state.settings.admin_token:
        raise HTTPException(403, "admin token required")
