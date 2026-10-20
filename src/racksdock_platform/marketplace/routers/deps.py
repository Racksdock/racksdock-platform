from __future__ import annotations

from fastapi import Request


def db(request: Request):
    return request.app.state.db


def orch(request: Request):
    return request.app.state.orchestrator


def settings(request: Request):
    return request.app.state.settings


def catalog(request: Request):
    return request.app.state.catalog


def chain(request: Request):
    return request.app.state.chain
