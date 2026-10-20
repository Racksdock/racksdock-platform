"""Registry of connected host-agents and command dispatch."""
from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import WebSocket
from racksdock_subnet.core.envelope import SignedEnvelope
from racksdock_subnet.core.schemas import HostCommand, HostEvent

log = logging.getLogger("racksdock.marketplace.hosts")


class HostSession:
    def __init__(self, ws: WebSocket, host_pubkey: str, node_id: str | None, hello: dict):
        self.ws = ws
        self.host_pubkey = host_pubkey
        self.node_id = node_id
        self.hello = hello
        self.connected_at = asyncio.get_event_loop().time()

    async def send(self, cmd: HostCommand) -> None:
        await self.ws.send_text(cmd.model_dump_json())


class HostRegistry:
    def __init__(self):
        self.by_node: dict[str, HostSession] = {}
        self.by_key: dict[str, HostSession] = {}
        self.waiters: dict[str, asyncio.Future] = {}
        self.on_event: Callable[[HostSession, HostEvent], Awaitable[None]] | None = None
        self.on_connect: Callable[[HostSession], Awaitable[None]] | None = None
        self.on_disconnect: Callable[[HostSession], Awaitable[None]] | None = None

    def session_for(self, node_id: str) -> HostSession | None:
        return self.by_node.get(node_id)

    def bind_node(self, sess: HostSession, node_id: str) -> None:
        sess.node_id = node_id
        self.by_node[node_id] = sess

    async def send(self, node_id: str, kind: str, payload: dict[str, Any] | None = None, instance_id: str | None = None,
                   wait: bool = False, timeout: float = 60.0) -> HostEvent | str:
        sess = self.by_node.get(node_id)
        if sess is None:
            raise ConnectionError(f"host for node {node_id} not connected")
        command_id = f"cmd_{secrets.token_hex(8)}"
        cmd = HostCommand(kind=kind, command_id=command_id, instance_id=instance_id, payload=payload or {})
        fut: asyncio.Future | None = None
        if wait:
            fut = asyncio.get_running_loop().create_future()
            self.waiters[command_id] = fut
        await sess.send(cmd)
        if not wait:
            return command_id
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.waiters.pop(command_id, None)

    async def send_to_key(self, host_pubkey: str, kind: str, payload: dict | None = None, instance_id: str | None = None) -> str:
        sess = self.by_key.get(host_pubkey)
        if sess is None:
            raise ConnectionError("host not connected")
        command_id = f"cmd_{secrets.token_hex(8)}"
        await sess.send(HostCommand(kind=kind, command_id=command_id, instance_id=instance_id, payload=payload or {}))
        return command_id

    def resolve(self, ev: HostEvent) -> None:
        if ev.command_id and ev.command_id in self.waiters:
            fut = self.waiters[ev.command_id]
            if not fut.done():
                fut.set_result(ev)

    async def serve(self, ws: WebSocket) -> None:
        await ws.accept()
        sess: HostSession | None = None
        try:
            first = await ws.receive_text()
            env = SignedEnvelope.model_validate_json(first)
            if not env.verify() or env.kind != "host_event":
                await ws.close(code=4001, reason="bad hello signature")
                return
            hello = HostEvent.model_validate(env.payload)
            if hello.kind != "hello" or hello.payload.get("host_pubkey") != env.signer:
                await ws.close(code=4002, reason="expected hello")
                return
            sess = HostSession(ws, env.signer, hello.node_id, hello.payload)
            self.by_key[env.signer] = sess
            if hello.node_id:
                self.by_node[hello.node_id] = sess
            if self.on_connect:
                await self.on_connect(sess)
            if self.on_event:
                await self.on_event(sess, hello)
            while True:
                raw = await ws.receive_text()
                try:
                    env = SignedEnvelope.model_validate_json(raw)
                except ValueError:
                    continue
                if not env.verify(expected_signer=sess.host_pubkey) or env.kind != "host_event":
                    log.warning("dropping event with bad signature from %s", sess.host_pubkey[:8])
                    continue
                ev = HostEvent.model_validate(env.payload)
                self.resolve(ev)
                if self.on_event:
                    try:
                        await self.on_event(sess, ev)
                    except Exception as e:
                        log.exception("event handling failed: %s", e)
        except Exception as e:
            log.info("host session closed: %s", type(e).__name__)
        finally:
            if sess is not None:
                if sess.node_id and self.by_node.get(sess.node_id) is sess:
                    del self.by_node[sess.node_id]
                if self.by_key.get(sess.host_pubkey) is sess:
                    del self.by_key[sess.host_pubkey]
                if self.on_disconnect:
                    try:
                        await self.on_disconnect(sess)
                    except Exception:
                        pass
