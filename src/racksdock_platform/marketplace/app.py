from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from racksdock_subnet.core.chainclient import build_chain

from .config import Settings
from .db import Database
from .routers import account, admin, billing, dev, hosts_ws, instances, miners, public, session, validators
from .services.auth import SessionSigner
from .services.billing import meter
from .services.catalog import Catalog
from .services.hosts import HostRegistry
from .services.orchestrator import Orchestrator
from .services.payments import Payments

log = logging.getLogger("racksdock.marketplace")


def create_app(settings: Settings | None = None, background: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks = []
        if background:
            tasks.append(asyncio.create_task(_loop(app, 60.0, _billing_tick)))
            tasks.append(asyncio.create_task(_loop(app, 15.0, _timeouts_tick)))
            app.state.payments.start()  # the TAO watcher thread (only when a treasury is configured)
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            app.state.payments.stop()

    app = FastAPI(title="racksdock marketplace", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = Database(settings.db_url)
    app.state.catalog = Catalog(settings.catalog_path)
    app.state.chain = build_chain(settings.chain_kind, path=settings.chain_file, netuid=settings.netuid, network=settings.chain_network)
    app.state.hosts = HostRegistry()
    if settings.trust.dev and settings.trust.dev_ca_dir is not None and not (settings.trust.dev_ca_dir / "root.pem").exists():
        from racksdock_subnet.verifier.devca import DevCA

        DevCA.create(settings.trust.dev_ca_dir)
        log.warning("dev trust mode: created a simulated device CA in %s (the platform plays NVIDIA's role; never use in production)", settings.trust.dev_ca_dir)
    app.state.orchestrator = Orchestrator(app.state.db, settings, app.state.catalog, app.state.hosts)
    app.state.signer = SessionSigner(settings.secret)
    app.state.payments = Payments(settings, app.state.db)
    from racksdock_subnet.core.keys import SigningKey

    app.state.platform_key = SigningKey.load_or_create(settings.platform_key_path)
    for r in (public.router, instances.router, account.router, billing.router, session.router, validators.router, miners.router, admin.router, hosts_ws.router):
        app.include_router(r)
    if settings.trust.dev:
        app.include_router(dev.router)
    if settings.cors_origins or settings.cors_origin_regex:
        # a console served from another origin (static hosting) calls the API with its session cookie
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_origin_regex=settings.cors_origin_regex, allow_credentials=True,
                           allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    if settings.catalog_store.is_dir():
        # the image store: /catalog/<release>/{manifest.signed.json, catalog.pub, images}; /catalog/current -> the published release
        app.mount("/catalog", StaticFiles(directory=str(settings.catalog_store), follow_symlink=True), name="catalog")
    return app


async def _loop(app: FastAPI, interval: float, fn) -> None:
    while True:
        try:
            await fn(app)
        except Exception as e:
            log.exception("background task failed: %s", e)
        await asyncio.sleep(interval)


async def _billing_tick(app: FastAPI) -> None:
    with app.state.db.session() as s:
        actions = meter(s, cutoff_cents=app.state.settings.credit_cutoff_cents)
    for target, action in actions:
        if action == "auto_delete":
            await app.state.orchestrator.delete(target, reason="auto_delete threshold")
        elif action == "credit_cutoff":
            await app.state.orchestrator.delete(target, reason="credit balance below the cutoff")
        elif action == "alert":
            log.info("alert threshold reached for %s (email hook goes here)", target)
        elif action == "credit_alert":
            log.info("credit balance of user %s is negative (email hook goes here)", target)


async def _timeouts_tick(app: FastAPI) -> None:
    await app.state.orchestrator.tick_timeouts()


app = None  # populated by `racksdock-marketplace serve`
