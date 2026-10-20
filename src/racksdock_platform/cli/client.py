from __future__ import annotations

import httpx
import typer

from . import config


def make_client(auth: bool = True) -> httpx.Client:
    cfg = config.load()
    headers = {}
    if auth:
        if not cfg.get("api_key"):
            raise typer.BadParameter("not logged in: run `racksdock auth login`")
        headers["X-API-KEY"] = cfg["api_key"]
    return httpx.Client(base_url=cfg["api_url"].rstrip("/"), headers=headers, timeout=60)


def call(c: httpx.Client, method: str, path: str, **kw):
    r = c.request(method, path, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        typer.echo(f"error {r.status_code}: {detail}", err=True)
        raise typer.Exit(1)
    return r.json() if r.content else {}
