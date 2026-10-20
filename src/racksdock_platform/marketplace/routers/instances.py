from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from racksdock_subnet.core.schemas import CreateInstanceRequest, Threshold, VmMode
from sqlalchemy import select

from ..models import Instance, Node, Template, User
from ..services.auth import require_user
from .deps import catalog, db, orch
from .views import instance_info

router = APIRouter(prefix="/v1/instances", tags=["instances"])


def _apply_template(s, req: CreateInstanceRequest, user: User) -> CreateInstanceRequest:
    if not req.template_id:
        return req
    tpl = s.get(Template, req.template_id)
    if tpl is None or (tpl.user_id != user.id and not tpl.featured):
        raise HTTPException(404, "template not found")
    merged = {**tpl.body, **{k: v for k, v in req.model_dump(exclude_none=True, exclude_defaults=True).items()}}
    merged.pop("template_id", None)
    return CreateInstanceRequest.model_validate(merged)


@router.post("/create", status_code=201)
async def create(req: CreateInstanceRequest, user: User = Depends(require_user), _db=Depends(db), o=Depends(orch)):
    with _db.session() as s:
        req = _apply_template(s, req, user)
        if not req.ssh_key_ids and user.default_ssh_key_id:
            req.ssh_key_ids = [user.default_ssh_key_id]
        if not req.ssh_key_ids:
            raise HTTPException(400, "no ssh key: pass ssh_key_ids or set a default key")
        if user.credits_cents <= 0 and user.role == "user":
            raise HTTPException(402, "payment_required: add credits")
    try:
        inst = o.create_instance(req, user_id=user.id, mode=VmMode.tenant)
    except ValueError as e:
        code = {"invalid_os": 400, "node_busy": 409, "node_not_found": 404, "no_capacity": 409}.get(str(e), 400)
        raise HTTPException(code, str(e))
    await o.launch(inst.id)
    return {"id": inst.id}


@router.get("")
def list_instances(user: User = Depends(require_user), _db=Depends(db), cat=Depends(catalog), include_deleted: bool = False):
    with _db.session() as s:
        q = select(Instance).where(Instance.user_id == user.id).order_by(Instance.created_at.desc())
        rows = s.scalars(q).all()
        out = []
        for inst in rows:
            if not include_deleted and inst.status == "deleted":
                continue
            out.append(instance_info(inst, s.get(Node, inst.node_id) if inst.node_id else None, cat, include_user_data=False).model_dump())
        return {"instances": out}


def _get_owned(s, instance_id: str, user: User) -> Instance:
    inst = s.get(Instance, instance_id)
    if inst is None or (inst.user_id != user.id and user.role != "admin"):
        raise HTTPException(404, "instance not found")
    return inst


@router.get("/{instance_id}/info")
def info(instance_id: str, user: User = Depends(require_user), _db=Depends(db), cat=Depends(catalog)):
    with _db.session() as s:
        inst = _get_owned(s, instance_id, user)
        return instance_info(inst, s.get(Node, inst.node_id) if inst.node_id else None, cat).model_dump()


@router.post("/{instance_id}/delete")
async def delete(instance_id: str, user: User = Depends(require_user), _db=Depends(db), o=Depends(orch)):
    with _db.session() as s:
        _get_owned(s, instance_id, user)
    await o.delete(instance_id, reason="user delete")
    return {"id": instance_id, "status": "deleting"}


@router.post("/{instance_id}/restart")
async def restart(instance_id: str, user: User = Depends(require_user), _db=Depends(db), o=Depends(orch), hard: bool = False):
    with _db.session() as s:
        _get_owned(s, instance_id, user)
    try:
        await o.restart(instance_id, hard=hard)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except (ConnectionError, TimeoutError) as e:
        raise HTTPException(503, f"host unreachable: {e}")
    return {"id": instance_id, "restarted": True}


class UpdateRequest(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    alert: Threshold | None = None
    auto_delete: Threshold | None = None


@router.post("/{instance_id}/update")
def update(instance_id: str, body: UpdateRequest, user: User = Depends(require_user), _db=Depends(db)):
    with _db.session() as s:
        inst = _get_owned(s, instance_id, user)
        if body.name is not None:
            inst.name = body.name
        if body.tags is not None:
            inst.tags = body.tags
        if body.alert is not None:
            inst.alert = body.alert.model_dump()
            inst.alert_sent = False
        if body.auto_delete is not None:
            inst.auto_delete = body.auto_delete.model_dump()
    return {"id": instance_id, "updated": True}
