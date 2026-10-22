"""Server-rendered console (docs/07 §2). Thin: pages fetch JSON from the API with the session cookie."""
from __future__ import annotations

from importlib import resources

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..models import Instance, Node, User
from ..services.auth import check_password, hash_password, new_api_key
from ..services.catalog import availability
from .deps import catalog, db, settings

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(resources.files("racksdock_platform.marketplace").joinpath("templates")))


def _user(request: Request) -> User | None:
    uid = request.app.state.signer.verify(request.cookies.get("racksdock_session"))
    if not uid:
        return None
    with request.app.state.db.session() as s:
        return s.get(User, uid)


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    user = _user(request)
    return templates.TemplateResponse(request, name, {"user": user, **ctx})


@router.get("/", response_class=HTMLResponse)
def home(request: Request, _db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    with _db.session() as s:
        nodes = availability(s, cat, st.base_url, include_unqualified=True)  # every node with its status; only qualified ones can be launched
        shown = {n.node_id for n in nodes}
        offline_nodes = [n for n in availability(s, cat, st.base_url, include_unqualified=True, include_offline=True) if n.node_id not in shown]
    return _render(request, "home.html", nodes=nodes, offline_nodes=offline_nodes)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return _render(request, "login.html", error=None)


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), signup: str = Form(""), _db=Depends(db)):
    with _db.session() as s:
        u = s.scalar(select(User).where(User.email == email))
        if signup:
            if u is not None:
                return _render(request, "login.html", error="email already registered")
            u = User(email=email, password_hash=hash_password(password))
            s.add(u)
            s.flush()
            new_api_key(s, u)
        elif u is None or not check_password(password, u.password_hash):
            return _render(request, "login.html", error="bad credentials")
        token = request.app.state.signer.sign(u.id)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie("racksdock_session", token, httponly=True, samesite="lax")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("racksdock_session")
    return resp


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    if _user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "dashboard.html")


@router.get("/launch", response_class=HTMLResponse)
def launch(request: Request, _db=Depends(db), cat=Depends(catalog), st=Depends(settings)):
    if _user(request) is None:
        return RedirectResponse("/login", status_code=303)
    with _db.session() as s:
        nodes = availability(s, cat, st.base_url)
    return _render(request, "launch.html", nodes=nodes, os_options=cat.os_options(), default_os=cat.default_os,
                   templates=[tpl for tpl in cat.templates if not tpl["validator_only"]])


@router.get("/instances/{instance_id}", response_class=HTMLResponse)
def instance_page(request: Request, instance_id: str, _db=Depends(db)):
    user = _user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    with _db.session() as s:
        inst = s.get(Instance, instance_id)
        if inst is None or inst.user_id != user.id:
            raise HTTPException(404)
    return _render(request, "instance.html", instance_id=instance_id)


@router.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request):
    if _user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "keys.html")


@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    if _user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return _render(request, "billing.html")


@router.get("/nodes/{node_id}", response_class=HTMLResponse)
def node_page(request: Request, node_id: str, _db=Depends(db)):
    with _db.session() as s:
        if s.get(Node, node_id) is None:
            raise HTTPException(404)
    return _render(request, "node.html", node_id=node_id)
