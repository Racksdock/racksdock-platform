from __future__ import annotations

from fastapi import APIRouter, WebSocket

router = APIRouter()


@router.websocket("/v1/hosts/ws")
async def hosts_ws(ws: WebSocket):
    await ws.app.state.hosts.serve(ws)
