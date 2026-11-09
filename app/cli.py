"""Operator command line: ``python -m app.cli <command>`` (or ``whatsapp-webhook <command>``)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import UTC, datetime

import httpx

from app.config import load_settings
from app.mail.compose import build_alert_email
from app.mail.transports import build_transport
from app.main import build_directory, configure_logging
from app.store import STATUS_FAILED, STATUS_PENDING, Store


def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat(timespec="seconds") if ts else "-"


async def cmd_check_config(args: argparse.Namespace) -> int:
    settings = load_settings()
    print(json.dumps(settings.redacted(), indent=2, default=str))
    return 0


async def cmd_test_crm(args: argparse.Namespace) -> int:
    settings = load_settings()
    async with httpx.AsyncClient(timeout=60) as http:
        directory = build_directory(settings, http)
        if not directory.enabled:
            print("CRM lookup is disabled (CRM_ENABLED=false or no entities)")
            return 1
        assert directory._client is not None
        await directory._client.login()
        print(f"login ok; JWT found at {directory._client.token_source}")
        counts = await directory.refresh()
        print("indexed records with a phone number:", counts)
        print(json.dumps(directory.status(), indent=2, default=str))
    return 0


async def cmd_lookup(args: argparse.Namespace) -> int:
    settings = load_settings()
    digits = "".join(ch for ch in args.phone if ch.isdigit())
    async with httpx.AsyncClient(timeout=60) as http:
        directory = build_directory(settings, http)
        result = await directory.lookup(digits)
        print(f"status: {result.status.value}")
        if result.reason:
            print(f"reason: {result.reason}")
        for cand in result.candidates:
            print(
                f"- {cand.record.label} via {cand.matched_field}={cand.matched_raw!r}"
                f" exact={cand.exact} emails={cand.record.emails}"
            )
        print(f"reply-to: {result.reply_to}")
    return 0 if result.matched else 2


async def cmd_test_email(args: argparse.Namespace) -> int:
    settings = load_settings()
    async with httpx.AsyncClient(timeout=120) as http:
        transport = build_transport(settings, http)
        message = build_alert_email(
            "test email",
            "This is a test message from the WhatsApp email forwarder.\n"
            f"Sent at {datetime.now(tz=UTC).isoformat(timespec='seconds')}"
            f" via transport '{transport.name}'.\n",
            settings,
        )
        if args.to:
            del message["To"]
            message["To"] = args.to
        result = await transport.send(message)
        print(f"sent via {transport.name}: {result}")
    return 0


def cmd_list_failed(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.store_path)
    items = store.list_by_status(STATUS_FAILED, limit=args.limit)
    if not items:
        print("no failed items")
        return 0
    for item in items:
        print(f"{item.key}  kind={item.kind} attempts={item.attempts} finished={_fmt_ts(item.finished_at)}")
        print(f"    error: {(item.last_error or '').splitlines()[0][:200]}")
    return 0


def cmd_replay_failed(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.store_path)
    n = store.requeue(None if args.key == "all" else args.key, time.time())
    print(f"re-queued {n} item(s); the running service will pick them up")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.store_path)
    print(json.dumps(store.stats(), indent=2))
    pending = store.list_by_status(STATUS_PENDING, limit=5)
    for item in pending:
        print(
            f"pending: {item.key} attempts={item.attempts} next={_fmt_ts(item.next_attempt_at)}"
            f" error={item.last_error!r}"
        )
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = Store(settings.store_path)
    cutoff = time.time() - args.days * 86400
    print(f"purged {store.purge_finished(cutoff)} finished item(s) older than {args.days} day(s)")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    import uvicorn

    settings = load_settings()
    from app.main import create_app

    uvicorn.run(
        create_app(settings),
        host=settings.app_bind_host,
        port=settings.app_bind_port,
        log_level=settings.log_level.lower(),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="whatsapp-webhook", description="WhatsApp → email forwarder operator tools"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check-config", help="print the effective configuration (secrets redacted)")
    sub.add_parser("test-crm", help="log in to VShipCRM and build the customer index once")
    p = sub.add_parser("lookup", help="look a phone number up in the CRM index")
    p.add_argument("phone")
    p = sub.add_parser("test-email", help="send a test email through the configured transport")
    p.add_argument("--to", default="", help="override recipient(s)")
    p = sub.add_parser("list-failed", help="show the dead-letter list")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("replay-failed", help="re-queue a failed item (or 'all')")
    p.add_argument("key")
    sub.add_parser("stats", help="queue statistics")
    p = sub.add_parser("purge", help="delete finished items older than N days")
    p.add_argument("--days", type=int, default=30)
    sub.add_parser("run", help="run the web service with uvicorn (systemd uses this)")
    args = parser.parse_args(argv)

    configure_logging("WARNING")
    handlers = {
        "check-config": cmd_check_config,
        "test-crm": cmd_test_crm,
        "lookup": cmd_lookup,
        "test-email": cmd_test_email,
        "list-failed": cmd_list_failed,
        "replay-failed": cmd_replay_failed,
        "stats": cmd_stats,
        "purge": cmd_purge,
        "run": cmd_run,
    }
    handler = handlers[args.command]
    result = handler(args)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return int(result)


if __name__ == "__main__":
    sys.exit(main())
