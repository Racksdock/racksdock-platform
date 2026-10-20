from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from ..models import BillingEvent, SshKey, Template, User
from ..services.auth import check_password, hash_password, new_api_key, require_user
from .deps import db

router = APIRouter(tags=["account"])


class SignupRequest(BaseModel):
    email: str
    password: str


@router.post("/v1/account/signup", status_code=201)
def signup(body: SignupRequest, _db=Depends(db)):
    with _db.session() as s:
        if s.scalar(select(User).where(User.email == body.email)):
            raise HTTPException(409, "email exists")
        u = User(email=body.email, password_hash=hash_password(body.password))
        s.add(u)
        s.flush()
        key = new_api_key(s, u)
        return {"user_id": u.id, "api_key": key}


@router.post("/v1/account/login")
def login(body: SignupRequest, _db=Depends(db)):
    with _db.session() as s:
        u = s.scalar(select(User).where(User.email == body.email))
        if u is None or not check_password(body.password, u.password_hash):
            raise HTTPException(401, "bad credentials")
        return {"user_id": u.id, "api_key": new_api_key(s, u, name="login")}


@router.get("/v1/account")
def me(user: User = Depends(require_user)):
    return {"user_id": user.id, "email": user.email, "role": user.role, "credits_cents": user.credits_cents, "default_ssh_key_id": user.default_ssh_key_id}


# ---- SSH keys ---------------------------------------------------------------
class AddKey(BaseModel):
    name: str
    public_key: str


def _validate_pubkey(pk: str) -> str:
    parts = pk.strip().split()
    if len(parts) < 2 or parts[0] not in ("ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "sk-ssh-ed25519@openssh.com"):
        raise HTTPException(400, "unsupported public key format")
    import base64

    try:
        base64.b64decode(parts[1], validate=True)
    except Exception:
        raise HTTPException(400, "public key is not valid base64")
    return " ".join(parts[:3])


@router.post("/v1/sshkeys/add", status_code=201)
def add_key(body: AddKey, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        k = SshKey(user_id=user.id, name=body.name, public_key=_validate_pubkey(body.public_key))
        s.add(k)
        s.flush()
        u = s.get(User, user.id)
        if u.default_ssh_key_id is None:
            u.default_ssh_key_id = k.id
        return {"id": k.id, "name": k.name, "public_key": k.public_key, "is_default": u.default_ssh_key_id == k.id}


@router.get("/v1/sshkeys")
def list_keys(user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        keys = s.scalars(select(SshKey).where(SshKey.user_id == user.id)).all()
        return {"ssh_keys": [{"id": k.id, "name": k.name, "public_key": k.public_key, "is_default": k.id == user.default_ssh_key_id, "created_at": k.created_at} for k in keys]}


@router.get("/v1/sshkeys/{key_id}/info")
def key_info(key_id: str, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        k = s.get(SshKey, key_id)
        if k is None or k.user_id != user.id:
            raise HTTPException(404, "key not found")
        return {"id": k.id, "name": k.name, "public_key": k.public_key, "is_default": k.id == user.default_ssh_key_id}


@router.post("/v1/sshkeys/{key_id}/setdefault")
def set_default(key_id: str, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        k = s.get(SshKey, key_id)
        if k is None or k.user_id != user.id:
            raise HTTPException(404, "key not found")
        s.get(User, user.id).default_ssh_key_id = key_id
    return {"id": key_id, "is_default": True}


@router.post("/v1/sshkeys/{key_id}/delete")
def delete_key(key_id: str, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        k = s.get(SshKey, key_id)
        if k is None or k.user_id != user.id:
            raise HTTPException(404, "key not found")
        s.delete(k)
        u = s.get(User, user.id)
        if u.default_ssh_key_id == key_id:
            u.default_ssh_key_id = None
    return {"deleted": key_id}


# ---- templates --------------------------------------------------------------
class SaveTemplate(BaseModel):
    name: str
    body: dict


@router.post("/v1/templates/save", status_code=201)
def save_template(body: SaveTemplate, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        t = Template(user_id=user.id, name=body.name, body=body.body)
        s.add(t)
        s.flush()
        return {"id": t.id}


@router.get("/v1/templates")
def list_templates(user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        rows = s.scalars(select(Template).where(Template.user_id == user.id)).all()
        return {"templates": [{"id": t.id, "name": t.name, "body": t.body, "created_at": t.created_at} for t in rows]}


@router.get("/v1/templates/featured")
def featured(_db=Depends(db)):
    with _db.session() as s:
        rows = s.scalars(select(Template).where(Template.featured.is_(True))).all()
        return {"templates": [{"id": t.id, "name": t.name, "body": t.body} for t in rows]}


@router.get("/v1/templates/{template_id}/info")
def template_info(template_id: str, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        t = s.get(Template, template_id)
        if t is None or (t.user_id != user.id and not t.featured):
            raise HTTPException(404, "template not found")
        return {"id": t.id, "name": t.name, "body": t.body}


@router.post("/v1/templates/{template_id}/update")
def template_update(template_id: str, body: SaveTemplate, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        t = s.get(Template, template_id)
        if t is None or t.user_id != user.id:
            raise HTTPException(404, "template not found")
        t.name, t.body = body.name, body.body
    return {"id": template_id, "updated": True}


@router.post("/v1/templates/{template_id}/delete")
def template_delete(template_id: str, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        t = s.get(Template, template_id)
        if t is None or t.user_id != user.id:
            raise HTTPException(404, "template not found")
        s.delete(t)
    return {"deleted": template_id}


# ---- billing ----------------------------------------------------------------
class Purchase(BaseModel):
    cents: int


@router.post("/v1/billing/credits/purchase")
def purchase(body: Purchase, user: User = Depends(require_user), _db=Depends(db)):
    """Dev stub: credits are added directly. Production wires a payment processor webhook here."""
    if body.cents <= 0:
        raise HTTPException(400, "cents must be positive")
    with _db.session() as s:
        u = s.get(User, user.id)
        u.credits_cents += body.cents
        s.add(BillingEvent(user_id=user.id, kind="purchase", cents=body.cents, note="credits purchase (dev)"))
        return {"credits_cents": u.credits_cents}


@router.get("/v1/billing/usage")
def usage(user: User = Depends(require_user), _db=Depends(db), limit: int = 500):
    with _db.session() as s:
        rows = s.scalars(select(BillingEvent).where(BillingEvent.user_id == user.id).order_by(BillingEvent.id.desc()).limit(limit)).all()
        return {"events": [{"kind": r.kind, "cents": r.cents, "instance_id": r.instance_id, "note": r.note, "at": r.at} for r in rows]}
