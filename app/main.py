"""FastAPI application: webhook endpoints plus lifespan-managed background tasks."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from app.config import Settings, load_settings
from app.crm.client import VShipClient
from app.crm.directory import Directory
from app.mail.transports import build_transport
from app.security import SIGNATURE_HEADER, verify_signature, verify_token_matches
from app.store import KIND_MESSAGE, Store
from app.whatsapp.parser import parse_webhook
from app.worker import Worker

log = logging.getLogger("app")


def configure_logging(level: str) -> None:
    numeric = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,  # the CLI configures logging first; the service's LOG_LEVEL must win
    )
    logging.getLogger().setLevel(numeric)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_directory(settings: Settings, http: httpx.AsyncClient) -> Directory:
    client = None
    if settings.crm_enabled:
        client = VShipClient(
            http,
            base_url=settings.crm_base_url,
            username=settings.crm_username,
            password=settings.crm_password,
            timeout=settings.crm_timeout_seconds,
        )
    return Directory(
        client,
        entities=settings.crm_entity_list,
        default_region=settings.crm_default_region,
        include_inactive=settings.crm_include_inactive,
        reply_to_all=settings.crm_reply_to_all_emails,
        refresh_seconds=settings.crm_refresh_seconds,
        miss_refresh_min_age_seconds=settings.crm_miss_refresh_min_age_seconds,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = httpx.AsyncClient(follow_redirects=False, timeout=60)
        store = Store(settings.store_path)
        directory = build_directory(settings, http)
        transport = build_transport(settings, http)
        worker = Worker(settings, store, directory, transport, http)
        stop = asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        app.state.settings = settings
        app.state.store = store
        app.state.directory = directory
        app.state.worker = worker
        app.state.started_at = time.time()
        app.state.worker_task = None
        if settings.wa_allow_unsigned:
            log.error(
                "WA_ALLOW_UNSIGNED=true: webhook signature checks are OFF. Never run production like this."
            )
        if settings.worker_enabled:
            app.state.worker_task = asyncio.create_task(worker.run(stop), name="worker")
            tasks.append(app.state.worker_task)
            tasks.append(asyncio.create_task(_maintenance_loop(worker, stop), name="maintenance"))
        if directory.enabled:
            tasks.append(asyncio.create_task(_warm_directory(directory), name="crm-warmup"))
            tasks.append(asyncio.create_task(directory.run_refresher(stop), name="crm-refresher"))
        log.info(
            "startup complete (worker=%s, crm=%s, transport=%s)",
            settings.worker_enabled,
            directory.enabled,
            transport.name,
        )
        try:
            yield
        finally:
            stop.set()
            worker.wakeup.set()
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=settings.worker_shutdown_grace_seconds)
                for task in pending:
                    log.warning("task %s did not stop in time; cancelling", task.get_name())
                    task.cancel()
                for task in done:
                    if not task.cancelled() and task.exception():
                        log.error("task %s crashed: %r", task.get_name(), task.exception())
            await http.aclose()
            store.close()
            log.info("shutdown complete")

    app = FastAPI(
        title="WhatsApp email forwarder", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan
    )

    @app.get("/webhook")
    async def verify(request: Request) -> Response:
        params = request.query_params
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if (
            mode == "subscribe"
            and challenge is not None
            and verify_token_matches(settings.wa_verify_token, token)
        ):
            log.info("webhook verification handshake succeeded")
            return PlainTextResponse(challenge)
        log.warning("webhook verification failed (mode=%s)", mode)
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhook")
    async def receive(request: Request) -> Response:
        body = await request.body()
        signature = request.headers.get(SIGNATURE_HEADER)
        if not verify_signature(settings.wa_app_secret, body, signature):
            if settings.wa_allow_unsigned:
                log.warning("accepting unsigned/invalid-signature request because WA_ALLOW_UNSIGNED=true")
            else:
                log.warning("rejected webhook POST with missing/invalid signature")
                return PlainTextResponse("invalid signature", status_code=403)
        try:
            payload = json.loads(body)
        except ValueError:
            return PlainTextResponse("invalid json", status_code=400)
        parsed = parse_webhook(payload, settings.wa_phone_number_id or None)
        store: Store = request.app.state.store
        worker: Worker = request.app.state.worker
        now = time.time()
        accepted = duplicates = 0
        for msg in parsed.messages:
            if store.enqueue(KIND_MESSAGE, msg.wamid, {"envelope": _envelope_for(payload, msg.wamid)}, now):
                accepted += 1
            else:
                duplicates += 1
        if accepted:
            worker.wakeup.set()
        if parsed.object_type != "whatsapp_business_account":
            log.info("ignored webhook for object %r", parsed.object_type)
        log.info(
            "webhook: %d message(s) queued, %d duplicate(s), %d status update(s), other fields=%s",
            accepted,
            duplicates,
            parsed.statuses,
            sorted(set(parsed.other_fields)),
        )
        return JSONResponse({"status": "ok", "queued": accepted, "duplicates": duplicates})

    @app.get("/healthz")
    async def healthz(request: Request) -> Response:
        store: Store = request.app.state.store
        worker: Worker = request.app.state.worker
        directory: Directory = request.app.state.directory
        stats = store.stats()
        task: asyncio.Task[Any] | None = request.app.state.worker_task
        worker_alive = (not settings.worker_enabled) or (task is not None and not task.done())
        body = {
            "ok": worker_alive,
            "uptime_seconds": round(time.time() - request.app.state.started_at, 1),
            "queue": stats,
            "worker": {**worker.status(), "alive": worker_alive},
            "crm": directory.status(),
        }
        return JSONResponse(body, status_code=200 if worker_alive else 503)

    return app


def _envelope_for(payload: dict[str, Any], wamid: str) -> dict[str, Any]:
    """Keep only the change that carries ``wamid`` so each queue row is self-contained and small."""
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            messages = [
                m for m in (value.get("messages") or []) if isinstance(m, dict) and m.get("id") == wamid
            ]
            if messages:
                trimmed_value = {k: v for k, v in value.items() if k not in ("messages", "statuses")}
                trimmed_value["messages"] = messages
                return {
                    "object": payload.get("object"),
                    "entry": [
                        {
                            "id": entry.get("id"),
                            "changes": [{"field": change.get("field"), "value": trimmed_value}],
                        }
                    ],
                }
    return payload


async def _warm_directory(directory: Directory) -> None:
    try:
        await directory.refresh()
    except Exception as exc:
        log.warning("initial CRM index load failed (will retry on schedule / on demand): %s", exc)


async def _maintenance_loop(worker: Worker, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
            return
        except TimeoutError:
            pass
        try:
            purged = worker.purge_old()
            if purged:
                log.info("purged %d finished queue item(s)", purged)
        except Exception as exc:
            log.warning("maintenance failed: %s", exc)


app = None  # populated lazily by ``get_app`` so importing this module needs no environment


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
