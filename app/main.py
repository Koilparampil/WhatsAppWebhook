"""FastAPI application: webhook endpoints plus lifespan-managed background tasks.

With RELAY_ENABLED (and the worker running) the service also reads the relay mailbox for "WhatsApp updates
from email" (``app.relay``): a poller task queues command emails, the worker handles them, and the webhook
queues WhatsApp's "failed" delivery reports, so that the employee is told when a message that feature sent
could not be delivered. When the feature is off none of this exists and the forwarder behaves exactly as
without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from app.config import Settings, load_settings
from app.crm.client import VShipClient
from app.crm.directory import Directory
from app.mail.transports import OAuthTokenProvider, build_transport
from app.onboard import ONBOARD_HEADERS, render_onboard_page
from app.relay.mailbox import GraphMailbox
from app.relay.poller import RelayPoller
from app.relay.templates import TemplateGuard
from app.security import SIGNATURE_HEADER, verify_signature, verify_token_matches
from app.store import KIND_MESSAGE, KIND_RELAY_STATUS, Store
from app.whatsapp.parser import StatusEvent, parse_webhook
from app.whatsapp.send import WhatsAppClient
from app.worker import Worker

if TYPE_CHECKING:
    from app.relay.processor import RelayProcessor

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


def build_relay(
    settings: Settings,
    store: Store,
    directory: Directory,
    http: httpx.AsyncClient,
    *,
    wakeup: asyncio.Event | None = None,
) -> tuple[RelayProcessor, RelayPoller] | None:
    """The parts of "WhatsApp updates from email"; None unless RELAY_ENABLED and WORKER_ENABLED are both on.

    The processor module is imported here rather than at the top, so the forwarder itself never depends on
    it while the feature is off. ``wakeup`` is the worker's event, set after anything is queued.
    """
    if not (settings.relay_enabled and settings.worker_enabled):
        return None
    from app.relay.processor import RelayProcessor

    mailbox = GraphMailbox(http, settings, OAuthTokenProvider(http, settings), settings.relay_mailbox_address)
    relay = RelayProcessor(
        settings,
        store,
        mailbox,
        WhatsAppClient(http, settings),
        TemplateGuard(http, settings),
        directory,
    )
    relay.wakeup = wakeup
    poller = RelayPoller(settings, store, mailbox, wakeup=wakeup, on_round=relay.flush_digests)
    return relay, poller


def _start_relay(
    settings: Settings, store: Store, directory: Directory, http: httpx.AsyncClient, worker: Worker
) -> tuple[RelayProcessor | None, RelayPoller | None, str | None]:
    """(processor, poller, why it is not running) for the lifespan; never raises.

    A relay that cannot be built must not take the forwarder down with it: the service then runs without
    it (relay items wait in the queue, nothing is sent) and says so in the log and in /healthz.
    """
    if not settings.relay_enabled:
        # Off, but a RELAY_* value could not be read at all (RELAY_ENABLED=ture keeps the feature off this
        # way): said in the log and in /healthz, so that nobody believes it is on.
        problems = settings.relay_problems()
        if problems:
            log.warning(
                "WhatsApp updates from email are off, and a RELAY_* setting cannot be read (fix it and"
                " restart; the forwarder runs as before): %s",
                "; ".join(problems),
            )
            return None, None, "not started, settings: " + "; ".join(problems)
        return None, None, None
    if not settings.worker_enabled:
        log.warning(
            "RELAY_ENABLED=true but WORKER_ENABLED=false: WhatsApp updates from email are not handled"
        )
        return None, None, None
    problems = settings.relay_problems()
    if problems:
        # A mistake in the relay's settings leaves only the relay off; the forwarder runs as before.
        log.error(
            "WhatsApp updates from email are NOT started because of their settings (the forwarder runs as"
            " before); fix them and restart: %s",
            "; ".join(problems),
        )
        return None, None, "not started, settings: " + "; ".join(problems)
    try:
        parts = build_relay(settings, store, directory, http, wakeup=worker.wakeup)
    except Exception as exc:
        log.exception("WhatsApp updates from email could not be started; the forwarder runs without them")
        return None, None, f"could not be started: {exc!r}"
    if parts is None:
        return None, None, None
    relay, poller = parts
    relay.wakeup = worker.wakeup
    poller.wakeup = worker.wakeup
    worker.relay = relay
    return relay, poller, None


def _relay_mode(settings: Settings, relay: object | None, error: str | None) -> str:
    """The relay's state for the startup log line."""
    if relay is None:
        if not error:
            return "off"
        # switched off by a RELAY_* value that cannot be read (see _start_relay), or not started though on
        return "failed to start" if settings.relay_enabled else "off (a RELAY_* setting cannot be read)"
    modes = ["dry run" if settings.relay_dry_run else "live"]
    if settings.relay_test_number_list:
        modes.append("test mode")
    return f"on ({', '.join(modes)}, mailbox {settings.relay_mailbox_address})"


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
        relay, poller, relay_error = _start_relay(settings, store, directory, http, worker)
        stop = asyncio.Event()
        tasks: list[asyncio.Task[Any]] = []
        app.state.settings = settings
        app.state.store = store
        app.state.directory = directory
        app.state.worker = worker
        app.state.relay = relay
        app.state.relay_poller = poller
        app.state.relay_error = relay_error
        app.state.started_at = time.time()
        app.state.worker_task = None
        app.state.relay_task = None
        if settings.wa_allow_unsigned:
            log.error(
                "WA_ALLOW_UNSIGNED=true: webhook signature checks are OFF. Never run production like this."
            )
        if settings.worker_enabled:
            app.state.worker_task = asyncio.create_task(worker.run(stop), name="worker")
            tasks.append(app.state.worker_task)
            tasks.append(asyncio.create_task(_maintenance_loop(worker, stop), name="maintenance"))
            if poller is not None:
                app.state.relay_task = asyncio.create_task(poller.run(stop), name="relay-poller")
                tasks.append(app.state.relay_task)
        if directory.enabled:
            tasks.append(asyncio.create_task(_warm_directory(directory), name="crm-warmup"))
            tasks.append(asyncio.create_task(directory.run_refresher(stop), name="crm-refresher"))
        log.info(
            "startup complete (worker=%s, crm=%s, transport=%s, relay=%s)",
            settings.worker_enabled,
            directory.enabled,
            transport.name,
            _relay_mode(settings, relay, relay_error),
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
                if pending:
                    # let the cancelled worker hand its item back to the queue before the store closes
                    await asyncio.wait(pending, timeout=5)
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
        if getattr(request.app.state, "relay", None) is not None and parsed.status_events:
            # A "failed" report for a WhatsApp update the relay sent: its sender is told. Never a reason
            # to refuse Meta's delivery, which also carries customers' messages.
            try:
                failures = _queue_relay_failures(store, parsed.status_events, now)
            except Exception:
                log.exception("could not queue the delivery failures reported for WhatsApp updates")
            else:
                if failures:
                    worker.wakeup.set()
                    log.info("webhook: %d failed delivery report(s) queued for WhatsApp updates", failures)
        if parsed.object_type != "whatsapp_business_account":
            log.info("ignored webhook for object %r", parsed.object_type)
        if parsed.skipped_messages:
            log.info(
                "skipped %d message(s) addressed to other business numbers %s (WA_PHONE_NUMBER_ID=%s)",
                parsed.skipped_messages,
                sorted(parsed.skipped_phone_number_ids),
                settings.wa_phone_number_id,
            )
        log.info(
            "webhook: %d message(s) queued, %d duplicate(s), %d status update(s), other fields=%s",
            accepted,
            duplicates,
            parsed.statuses,
            sorted(set(parsed.other_fields)),
        )
        return JSONResponse({"status": "ok", "queued": accepted, "duplicates": duplicates})

    @app.get("/onboard")
    async def onboard(request: Request) -> Response:
        # Same bare 404 whether the page is disabled or the key is wrong, so the path reveals nothing.
        if not settings.onboard_enabled or not verify_token_matches(
            settings.onboard_key, request.query_params.get("key")
        ):
            return PlainTextResponse("not found", status_code=404)
        log.info("served the WhatsApp Business app onboarding page")
        page = render_onboard_page(
            settings.wa_app_id, settings.onboard_config_id, settings.wa_graph_api_version
        )
        return HTMLResponse(page, headers=ONBOARD_HEADERS)

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
            "relay": _relay_health(settings, request.app.state),
        }
        # the relay never changes the status code: only a dead worker makes the service unhealthy
        return JSONResponse(body, status_code=200 if worker_alive else 503)

    return app


def _queue_relay_failures(store: Store, events: Iterable[StatusEvent], now: float) -> int:
    """Queue each "failed" report that may be about a WhatsApp message the relay sent; returns how many
    were new.

    A report is queued even when its message id is not recorded yet: Meta's webhook can overtake its own
    answer to the send, and the relay records the id only once that answer is in, so such a report would
    otherwise be lost (Meta never sends it again). The worker handles one item at a time, and the update
    being sent is that item: the report is looked at only after its message id was recorded. The processor
    ignores a report about a message the relay did not send. Keyed by the message id, so Meta repeating a
    report queues nothing more; a failure the employee was already told about is not queued again either.
    """
    queued = 0
    for event in events:
        if event.status != "failed":
            continue
        sent = store.get_relay_sent(event.wamid)
        if sent is not None and sent.get("failure_notified"):
            continue
        payload = {
            "wamid": event.wamid,
            "status": event.status,
            "timestamp": event.timestamp,
            "recipient_id": event.recipient_id,
            "errors": [
                {"code": err.code, "title": err.title, "message": err.message, "details": err.details}
                for err in event.errors
            ],
        }
        if store.enqueue(KIND_RELAY_STATUS, f"relay_status:{event.wamid}", payload, now):
            queued += 1
    return queued


def _relay_health(settings: Settings, state: Any) -> dict[str, Any]:
    """The /healthz "relay" block; while the relay is not running only ``enabled`` (and why it failed)."""
    relay = getattr(state, "relay", None)
    poller = getattr(state, "relay_poller", None)
    if relay is None or poller is None:
        error = getattr(state, "relay_error", None)
        return {"enabled": False, "error": error} if error else {"enabled": False}
    task: asyncio.Task[Any] | None = getattr(state, "relay_task", None)
    polling = _status_of(poller)
    return {
        "enabled": True,
        "dry_run": settings.relay_dry_run,
        "test_mode": bool(settings.relay_test_number_list),
        "last_poll_ok_at": polling.get("last_ok_at"),
        "last_error": polling.get("last_error"),
        "failing_since": polling.get("failing_since"),
        "poller_alive": task is not None and not task.done(),
        "poller": polling,
        "processor": _status_of(relay),
    }


def _status_of(part: Any) -> dict[str, Any]:
    """A relay part's status() as plain JSON values; a failure is reported instead of breaking /healthz."""
    try:
        status = part.status()
        return json.loads(json.dumps(status if isinstance(status, dict) else {"status": status}, default=str))
    except Exception as exc:
        return {"error": f"status not available: {exc!r}"}


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
