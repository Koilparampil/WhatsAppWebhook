"""The relay poller: reads the relay mailbox's Inbox and queues command-shaped emails (spec section 13)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
import respx
from app.mail.transports import OAuthTokenProvider, SendError
from app.relay import poller as poller_module
from app.relay.mailbox import GraphMailbox
from app.relay.notices import from_payload
from app.relay.poller import (
    CURSOR_KEY,
    SWITCHED_ON_KEY,
    RelayPoller,
    from_iso,
    oldest_first,
    to_iso,
)
from app.store import KIND_NOTE, KIND_RELAY, STATUS_PENDING, Store

from tests.conftest import FakeClock, make_settings

TOKEN_URL = "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
BOX = "https://graph.microsoft.com/v1.0/users/whatsapp@example.com"
INBOX = f"{BOX}/mailFolders/inbox/messages"
NOW = 1_795_000_000.0  # 2026-11-18T11:06:40Z
NOW_ISO = "2026-11-18T11:06:40Z"


def poller_settings(tmp_path, **overrides: Any):
    """Graph settings for the relay mailbox (the poller itself does not need RELAY_ENABLED)."""
    base = {
        "mail_transport": "graph",
        "graph_sender": "whatsapp@example.com",
        "oauth_tenant_id": "tenant-id",
        "oauth_client_id": "client-id",
        "oauth_client_secret": "client-secret",
    }
    return make_settings(tmp_path, **{**base, **overrides})


def token_route():
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )


def listed(message_id: str, received: str, subject: str | None, sender: str = "chris@example.com") -> dict:
    """One email as Graph lists the Inbox."""
    entry: dict[str, Any] = {
        "id": message_id,
        "receivedDateTime": received,
        "from": {"emailAddress": {"name": "Chris", "address": sender}},
    }
    if subject is not None:
        entry["subject"] = subject
    return entry


def inbox_answer(*entries: dict) -> httpx.Response:
    return httpx.Response(200, json={"value": list(entries)})


def since_of(call) -> str:
    """The time a listing asked for, from its Graph filter."""
    text = call.request.url.params["$filter"]
    assert text.startswith("receivedDateTime ge ")
    return text.removeprefix("receivedDateTime ge ")


class Rig:
    """A poller on a real Store and a real GraphMailbox (Graph mocked with respx)."""

    def __init__(self, tmp_path, http: httpx.AsyncClient, clock: FakeClock, **overrides: Any):
        self.settings = poller_settings(tmp_path, **overrides)
        self.store = Store(self.settings.store_path)
        self.mailbox = GraphMailbox(
            http, self.settings, OAuthTokenProvider(http, self.settings), self.settings.relay_mailbox_address
        )
        self.wakeup = asyncio.Event()
        self.rounds: list[int] = []
        self.poller = RelayPoller(
            self.settings,
            self.store,
            self.mailbox,
            clock=clock,
            wakeup=self.wakeup,
            on_round=lambda: self.rounds.append(1),
        )

    def relay_items(self) -> list[str]:
        items = self.store.list_by_status(STATUS_PENDING, limit=1000)
        return sorted(item.key for item in items if item.kind == KIND_RELAY)

    def notes(self) -> list:
        return [
            item for item in self.store.list_by_status(STATUS_PENDING, limit=1000) if item.kind == KIND_NOTE
        ]

    def worker_order(self, now: float) -> list[str]:
        """The queue keys in the order the worker takes them (it handles one item after the other)."""
        order = []
        while (item := self.store.claim_next(now)) is not None:
            order.append(item.key)
        return order


def test_iso_helpers():
    assert to_iso(NOW) == NOW_ISO and to_iso(NOW + 0.9) == NOW_ISO  # cut to the second
    assert from_iso(NOW_ISO) == NOW and from_iso("2026-11-18T11:06:40+00:00") == NOW
    assert from_iso("2026-11-18T11:06:40") == NOW  # no zone: UTC
    for bad in (None, "", "  ", "yesterday", 17):
        assert from_iso(bad) is None


# ----------------------------------------------------------------------------- the cursor
@respx.mock
async def test_first_round_starts_the_cursor_now_so_older_mail_is_never_handled(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(
        return_value=inbox_answer(
            # already in the Inbox when the feature was first switched on (decision 14)
            listed("OLD1", "2026-11-18T11:06:39Z", "WA | 650 555 1234"),
            listed("OLD2", "2026-11-18T10:00:00Z", "WA | 650 555 1234 | BK1"),
        )
    )
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        assert await rig.poller.poll_once() == 0
        assert since_of(inbox.calls.last) == NOW_ISO  # no overlap before the first switch-on
        assert rig.store.get_meta(CURSOR_KEY) == NOW_ISO
        assert rig.store.get_meta(SWITCHED_ON_KEY) == NOW_ISO
        assert rig.relay_items() == [] and not rig.wakeup.is_set()

        # Later rounds look 15 minutes back, but never to before the first switch-on.
        clock.advance(60)
        assert await rig.poller.poll_once() == 0
        assert since_of(inbox.calls.last) == NOW_ISO
        assert rig.relay_items() == []


@respx.mock
async def test_only_command_shaped_subjects_are_queued_once_with_id_and_time(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        await rig.poller.poll_once()
        clock.advance(30)
        inbox.mock(
            return_value=inbox_answer(
                listed("A", "2026-11-18T11:07:00Z", "WA | 650 555 1234 | BK1"),
                listed("B", "2026-11-18T11:07:01Z", "RE: WA | 650 555 1234"),
                listed("C", "2026-11-18T11:07:02Z", "FW: WA | 650 555 1234"),
                listed("D", "2026-11-18T11:07:03Z", "Automatic reply: WA | 650 555 1234"),
                listed("E", "2026-11-18T11:07:04Z", "Where is my container?"),
                listed("F", "2026-11-18T11:07:05Z", "wa|+44 20 7946 0958"),
                listed("G", "2026-11-18T11:07:06Z", "  Wa  |  6505551234"),
                listed("H", "2026-11-18T11:07:07Z", None),
                listed("I", "2026-11-18T11:07:08Z", "WhatsApp | 650 555 1234"),
                listed("J", "2026-11-18T11:07:09Z", "[EXT] WA | 650 555 1234"),
            )
        )
        assert await rig.poller.poll_once() == 3
        assert rig.relay_items() == ["relay:A", "relay:F", "relay:G"]
        item = rig.store.get("relay:A")
        assert item.kind == KIND_RELAY and item.payload == {"id": "A", "received": "2026-11-18T11:07:00Z"}
        assert rig.wakeup.is_set()  # the worker is woken at once

        # The emails are still in the Inbox while they are handled: listed again, never queued twice.
        rig.wakeup.clear()
        clock.advance(30)
        assert await rig.poller.poll_once() == 0
        assert not rig.wakeup.is_set()
        assert rig.poller.status()["queued"] == 3


@respx.mock
async def test_the_cursor_moves_to_each_round_start_and_rounds_overlap_by_15_minutes(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        await rig.poller.poll_once()
        clock.advance(7200)
        await rig.poller.poll_once()
        assert since_of(inbox.calls.last) == NOW_ISO  # 15 minutes back would be before the switch-on
        assert rig.store.get_meta(CURSOR_KEY) == to_iso(NOW + 7200)
        clock.advance(30)
        await rig.poller.poll_once()
        assert since_of(inbox.calls.last) == to_iso(NOW + 7200 - 900)
        assert rig.store.get_meta(CURSOR_KEY) == to_iso(NOW + 7230)

        # A restart keeps both the cursor and the first switch-on.
        again = RelayPoller(rig.settings, rig.store, rig.mailbox, clock=clock)
        clock.advance(30)
        await again.poll_once()
        assert since_of(inbox.calls.last) == to_iso(NOW + 7230 - 900)
        assert rig.store.get_meta(SWITCHED_ON_KEY) == NOW_ISO

        # The clock going back never moves the cursor back, and the listing starts from the earlier time.
        clock.now = NOW + 3600
        await again.poll_once()
        assert since_of(inbox.calls.last) == to_iso(NOW + 3600 - 900)
        assert rig.store.get_meta(CURSOR_KEY) == to_iso(NOW + 7260)


@respx.mock
async def test_mail_older_than_the_store_keeps_records_is_never_listed(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock, store_retention_days=30)
        await rig.poller.poll_once()
        clock.advance(45 * 86400)  # switched off for 45 days, then on again: the cursor is that old
        await rig.poller.poll_once()
        assert since_of(inbox.calls.last) == to_iso(NOW + 45 * 86400 - (30 * 86400 - 3600))


@respx.mock
async def test_an_unreadable_cursor_looks_again_from_the_first_switch_on(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(
        return_value=inbox_answer(listed("A", "2026-11-18T12:00:00Z", "WA | 650 555 1234"))
    )
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        rig.store.set_meta(SWITCHED_ON_KEY, NOW_ISO)
        rig.store.set_meta(CURSOR_KEY, "garbage")
        clock.advance(86400)
        assert await rig.poller.poll_once() == 1  # nothing skipped; anything queued before is a duplicate
        assert since_of(inbox.calls.last) == NOW_ISO
        assert rig.store.get_meta(CURSOR_KEY) == to_iso(NOW + 86400)


@respx.mock
async def test_a_missing_first_switch_on_is_taken_from_the_cursor(tmp_path, clock):
    clock.now = NOW
    token_route()
    respx.get(INBOX).mock(
        return_value=inbox_answer(listed("EARLY", "2026-11-18T11:00:00Z", "WA | 650 555 1234"))
    )
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        rig.store.set_meta(CURSOR_KEY, NOW_ISO)
        clock.advance(60)
        assert await rig.poller.poll_once() == 0  # received before the cursor's start: never handled
        assert rig.store.get_meta(SWITCHED_ON_KEY) == NOW_ISO


@respx.mock
async def test_a_full_listing_is_listed_once_more_deeply_so_older_commands_are_not_missed(
    tmp_path, clock, monkeypatch
):
    monkeypatch.setattr(poller_module, "LIST_PAGES", 1)
    monkeypatch.setattr(poller_module, "CATCH_UP_PAGES", 3)
    clock.now = NOW
    token_route()

    def page(number: int) -> httpx.Response:
        if number == 3:
            return inbox_answer(listed("OLDCMD", "2026-11-18T12:00:00Z", "WA | 650 555 1234"))
        entries = [listed(f"P{number}-{i}", "2026-11-18T13:00:00Z", f"Newsletter {i}") for i in range(50)]
        body = {"value": entries, "@odata.nextLink": f"{INBOX}?page={number + 1}"}
        return httpx.Response(200, json=body)

    inbox = respx.get(INBOX).mock(side_effect=lambda request: page(int(request.url.params.get("page", "1"))))
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        rig.store.set_meta(SWITCHED_ON_KEY, NOW_ISO)
        rig.store.set_meta(CURSOR_KEY, NOW_ISO)
        clock.advance(86400)  # back after a day-long outage: more mail than one round lists
        assert await rig.poller.poll_once() == 1
        assert rig.relay_items() == ["relay:OLDCMD"]
        assert inbox.call_count == 1 + 3  # the ordinary listing (one page), then three pages deep


# ----------------------------------------------------------------------------- the order of the queue
@respx.mock
async def test_a_rounds_command_emails_are_queued_oldest_first_so_they_go_out_in_order(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        await rig.poller.poll_once()
        clock.advance(30)
        inbox.mock(  # Graph lists the Inbox newest first
            return_value=inbox_answer(
                listed("CORRECTION", "2026-11-18T11:07:05Z", "WA | 650 555 1234"),
                listed("NEWS", "2026-11-18T11:07:01Z", "Port schedule for week 47"),
                listed("UPDATE", "2026-11-18T11:06:45Z", "WA | 650 555 1234"),
                listed("OTHER", "2026-11-18T11:06:41Z", "WA | +44 20 7946 0958"),
            )
        )
        assert await rig.poller.poll_once() == 3
        # An update and the correction written right after it: the customer must get them in that order.
        assert rig.worker_order(clock()) == ["relay:OTHER", "relay:UPDATE", "relay:CORRECTION"]


@respx.mock
async def test_the_catch_up_listing_is_queued_oldest_first_too(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(poller_module, "LIST_PAGES", 1)
    monkeypatch.setattr(poller_module, "CATCH_UP_PAGES", 3)
    clock.now = NOW
    token_route()

    def page(number: int) -> httpx.Response:
        # newest first across the pages: 14:xx on the first, 13:xx on the second, 12:xx on the last
        hour = 15 - number
        entries = [listed(f"CMD{number}", f"2026-11-18T{hour}:30:00Z", "WA | 650 555 1234")]
        if number == 3:
            return inbox_answer(*entries)
        entries += [
            listed(f"P{number}-{i}", f"2026-11-18T{hour}:00:00Z", f"Newsletter {i}") for i in range(49)
        ]
        return httpx.Response(200, json={"value": entries, "@odata.nextLink": f"{INBOX}?page={number + 1}"})

    respx.get(INBOX).mock(side_effect=lambda request: page(int(request.url.params.get("page", "1"))))
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        rig.store.set_meta(SWITCHED_ON_KEY, NOW_ISO)
        rig.store.set_meta(CURSOR_KEY, NOW_ISO)
        clock.advance(86400)
        assert await rig.poller.poll_once() == 3
        assert rig.worker_order(clock()) == ["relay:CMD3", "relay:CMD2", "relay:CMD1"]


def test_oldest_first_orders_by_arrival_time():
    graph_order = [  # newest first, as Graph lists them
        listed("D", "2026-11-18T11:07:09Z", "WA | 1"),
        listed("C2", "2026-11-18T11:07:05Z", "WA | 1"),  # the same second: Graph's order, reversed
        listed("C1", "2026-11-18T11:07:05Z", "WA | 1"),
        {"id": "NO-TIME", "subject": "WA | 1"},  # a time that cannot be read: last
        listed("A", "2026-11-18T11:06:41Z", "WA | 1"),
        listed("B", "2026-11-18T11:06:59Z", "WA | 1"),  # out of place (pages that shifted): sorted in
    ]
    assert [m["id"] for m in oldest_first(graph_order)] == ["A", "B", "C1", "C2", "D", "NO-TIME"]
    assert oldest_first([]) == []


# ----------------------------------------------------------------------------- errors and health
@respx.mock
async def test_a_failed_round_keeps_the_cursor_and_is_reported_in_the_status(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock)
        await rig.poller.poll_once()
        assert rig.poller.status() == {
            "enabled": True,
            "mailbox": "whatsapp@example.com",
            "cursor": NOW_ISO,
            "last_ok_at": NOW,
            "last_error": None,
            "failing_since": None,
            "queued": 0,
        }
        clock.advance(60)
        inbox.mock(return_value=httpx.Response(503, text="Service Unavailable"))
        with pytest.raises(SendError, match="HTTP 503"):
            await rig.poller.poll_once()
        status = rig.poller.status()
        assert "HTTP 503" in status["last_error"]
        assert status["failing_since"] == NOW + 60 and status["last_ok_at"] == NOW
        assert rig.store.get_meta(CURSOR_KEY) == NOW_ISO  # the round did not happen

        clock.advance(30)
        inbox.mock(return_value=inbox_answer(listed("A", "2026-11-18T11:07:30Z", "WA | 650 555 1234")))
        assert await rig.poller.poll_once() == 1
        status = rig.poller.status()
        assert status["last_error"] is None and status["failing_since"] is None
        assert status["last_ok_at"] == NOW + 90 and status["cursor"] == to_iso(NOW + 90)
        assert rig.notes() == []  # 30 seconds of trouble alert nobody


@respx.mock
async def test_the_administrators_hear_once_after_15_minutes_and_once_when_it_works_again(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock, alert_to="admin@example.com")
        await rig.poller.poll_once()
        inbox.mock(
            return_value=httpx.Response(
                403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}
            )
        )
        for _ in range(3):  # failing since NOW+300: 10 minutes at NOW+900
            clock.advance(300)
            with pytest.raises(SendError):
                await rig.poller.poll_once()
        assert rig.notes() == []
        clock.advance(300)  # 15 minutes
        rig.wakeup.clear()
        with pytest.raises(SendError):
            await rig.poller.poll_once()
        (down,) = rig.notes()
        assert down.key == f"note:relay:inbox-down:{int(NOW + 300)}"
        assert rig.wakeup.is_set()
        email = from_payload(down.payload)
        assert email["To"] == "admin@example.com"
        assert email["Subject"] == (
            "[WhatsApp] ALERT: WhatsApp updates: the Inbox of whatsapp@example.com cannot be read"
        )
        text = email.get_body(("plain",)).get_content()
        assert "for 15 minutes" in text and "ErrorAccessDenied" in text and "relay-check" in text
        for _ in range(3):  # still failing: no second alert
            clock.advance(300)
            with pytest.raises(SendError):
                await rig.poller.poll_once()
        assert len(rig.notes()) == 1

        clock.advance(300)
        inbox.mock(return_value=inbox_answer())
        await rig.poller.poll_once()
        notes = rig.notes()
        assert len(notes) == 2
        (up,) = [note for note in notes if note.key != down.key]
        assert up.key == f"note:relay:inbox-up:{int(NOW + 300)}"
        email = from_payload(up.payload)
        assert email["Subject"] == (
            "[WhatsApp] ALERT: WhatsApp updates: the Inbox of whatsapp@example.com can be read again"
        )
        assert "it could not for 35 minutes" in email.get_body(("plain",)).get_content()
        assert rig.poller.status()["failing_since"] is None

        # A later outage is reported again.
        inbox.mock(return_value=httpx.Response(500, text="boom"))
        for _ in range(4):
            clock.advance(300)
            with pytest.raises(SendError):
                await rig.poller.poll_once()
        assert len(rig.notes()) == 3


async def alerted_outage(rig: Rig, inbox, clock: FakeClock):
    """Reading fails from NOW+300 on; at NOW+1200 the administrators are told. Returns that note."""
    await rig.poller.poll_once()
    inbox.mock(return_value=httpx.Response(503, text="Service Unavailable"))
    for _ in range(4):
        clock.advance(300)
        with pytest.raises(SendError):
            await rig.poller.poll_once()
    (down,) = rig.notes()
    assert down.key == f"note:relay:inbox-down:{int(NOW + 300)}"
    return down


def up_alerts(rig: Rig, down) -> list:
    return [note for note in rig.notes() if note.key != down.key]


@respx.mock
async def test_after_a_restart_the_promised_can_be_read_again_alert_still_comes(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock, alert_to="admin@example.com")
        down = await alerted_outage(rig, inbox, clock)

        # The service is restarted (often the fix itself, after rotating OAUTH_CLIENT_SECRET) and the
        # Inbox can be read at once: the email the first alert promised still comes.
        restarted = RelayPoller(rig.settings, rig.store, rig.mailbox, clock=clock)
        clock.advance(60)
        inbox.mock(return_value=inbox_answer())
        await restarted.poll_once()
        (up,) = up_alerts(rig, down)
        assert up.key == f"note:relay:inbox-up:{int(NOW + 300)}"
        email = from_payload(up.payload)
        assert email["Subject"].endswith(": the Inbox of whatsapp@example.com can be read again")
        assert "it could not for 16 minutes" in email.get_body(("plain",)).get_content()

        # That outage is over: later restarts and rounds report nothing more.
        for _ in range(2):
            again = RelayPoller(rig.settings, rig.store, rig.mailbox, clock=clock)
            clock.advance(30)
            await again.poll_once()
        assert len(rig.notes()) == 2


@respx.mock
async def test_an_outage_that_outlasts_a_restart_is_reported_once_and_its_end_once(tmp_path, clock):
    clock.now = NOW
    token_route()
    inbox = respx.get(INBOX).mock(return_value=inbox_answer())
    async with httpx.AsyncClient() as http:
        rig = Rig(tmp_path, http, clock, alert_to="admin@example.com")
        down = await alerted_outage(rig, inbox, clock)

        restarted = RelayPoller(rig.settings, rig.store, rig.mailbox, clock=clock)
        for _ in range(5):  # 25 more minutes without access: the same outage, not a second alert
            clock.advance(300)
            with pytest.raises(SendError):
                await restarted.poll_once()
        assert [note.key for note in rig.notes()] == [down.key]

        clock.advance(300)
        inbox.mock(return_value=inbox_answer())
        await restarted.poll_once()
        (up,) = up_alerts(rig, down)
        assert up.key == f"note:relay:inbox-up:{int(NOW + 300)}"
        text = from_payload(up.payload).get_body(("plain",)).get_content()
        assert "it could not for 45 minutes" in text  # counted from the start of the outage


class ListingMailbox:
    """A stand-in for GraphMailbox for the loop tests (no HTTP)."""

    mailbox = "whatsapp@example.com"

    def __init__(self) -> None:
        self.inbox: list[dict] = []
        self.calls: list[str] = []
        self.errors: list[BaseException] = []
        self.hang: asyncio.Event | None = None

    async def list_inbox(self, since_iso: str, pages: int = 10) -> list[dict]:
        self.calls.append(since_iso)
        if self.hang is not None:
            await self.hang.wait()
        if self.errors:
            raise self.errors.pop(0)
        return list(self.inbox)


def loop_poller(tmp_path, mailbox: ListingMailbox, **kwargs: Any) -> tuple[RelayPoller, Store]:
    settings = poller_settings(tmp_path)
    settings.relay_poll_seconds = 0.01  # validated (>= 10) only with RELAY_ENABLED
    store = Store(settings.store_path)
    return RelayPoller(settings, store, mailbox, **kwargs), store


async def wait_until(condition, within: float = 3.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def test_the_loop_survives_errors_calls_on_round_every_round_and_stops_on_request(tmp_path):
    mailbox = ListingMailbox()
    mailbox.errors = [SendError("Graph list inbox returned HTTP 503"), RuntimeError("odd"), ValueError("x")]
    mailbox.inbox = [listed("A", "2999-01-01T00:00:00Z", "WA | 650 555 1234")]
    rounds: list[int] = []
    wakeup = asyncio.Event()

    def flaky_round() -> None:  # the digest check failing must not stop the loop either
        rounds.append(1)
        if len(rounds) == 1:
            raise RuntimeError("digest broke")

    poller, store = loop_poller(tmp_path, mailbox, wakeup=wakeup, on_round=flaky_round)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: store.get("relay:A") is not None and len(rounds) >= 5)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert len(mailbox.calls) >= 4 and wakeup.is_set()
    assert poller.last_error is None and poller.last_ok_at is not None
    assert store.get_meta(CURSOR_KEY) is not None


async def test_stop_ends_the_loop_at_once_even_during_a_hanging_listing(tmp_path):
    mailbox = ListingMailbox()
    mailbox.hang = asyncio.Event()  # never set: Graph does not answer
    poller, store = loop_poller(tmp_path, mailbox)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: mailbox.calls)
    cursor = store.get_meta(CURSOR_KEY)
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert time.monotonic() - started < 1
    assert store.get_meta(CURSOR_KEY) == cursor  # the dropped round wrote nothing


async def test_the_loop_waits_as_long_as_graph_asks(tmp_path):
    mailbox = ListingMailbox()
    mailbox.errors = [SendError("Graph list inbox returned HTTP 429", status_code=429, retry_after=30)]
    poller, _ = loop_poller(tmp_path, mailbox)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: mailbox.calls)
    await asyncio.sleep(0.3)  # 30 rounds' worth of RELAY_POLL_SECONDS
    assert len(mailbox.calls) == 1
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_the_loop_sets_the_cursor_before_its_first_round(tmp_path):
    mailbox = ListingMailbox()
    mailbox.hang = asyncio.Event()
    clock = FakeClock(NOW)
    poller, store = loop_poller(tmp_path, mailbox, clock=clock)
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))
    await wait_until(lambda: mailbox.calls)
    assert store.get_meta(CURSOR_KEY) == NOW_ISO and store.get_meta(SWITCHED_ON_KEY) == NOW_ISO
    stop.set()
    await asyncio.wait_for(task, timeout=2)
