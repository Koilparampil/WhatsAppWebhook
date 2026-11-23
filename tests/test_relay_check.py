"""``whatsapp-webhook relay-check`` and ``relay-parse`` (spec section 7)."""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
import respx
from app import cli
from app.relay.check import PHONE_FIELDS, run_check, run_parse
from app.whatsapp.admin import appsecret_proof

from tests.conftest import make_settings

TOKEN_URL = "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
BOX = "https://graph.microsoft.com/v1.0/users/whatsapp@example.com"
INBOX = f"{BOX}/mailFolders/inbox/messages"
FOLDERS = f"{BOX}/mailFolders"
FOLDER_MESSAGES = f"{BOX}/mailFolders/FOLDER1/messages"
WABA = "102290129340398"
PHONE_ID = "106540352242922"
TEMPLATES = f"https://graph.facebook.com/v27.0/{WABA}/message_templates"
PHONE = f"https://graph.facebook.com/v27.0/{PHONE_ID}"
BODY = (
    "Hello {{1}}, VShip has an update:\n{{2}}\nPlease either reply to this message or send us an email at"
    " Elizabeth@vship2000.com if you have any Questions"
)
CHECK_ON: dict[str, Any] = {
    "mail_transport": "graph",
    "graph_sender": "whatsapp@example.com",
    "oauth_tenant_id": "tenant-id",
    "oauth_client_id": "client-id",
    "oauth_client_secret": "client-secret",
    "wa_phone_number_id": PHONE_ID,
    "wa_waba_id": WABA,
    "relay_enabled": True,
    "relay_allowed_senders": "chris@example.com",
}
PHONE_OK = {
    "display_phone_number": "+1 555-078-3881",
    "verified_name": "VShip",
    "name_status": "APPROVED",
    "quality_rating": "GREEN",
    "whatsapp_business_manager_messaging_limit": "TIER_250",
    "id": PHONE_ID,
}


def check_settings(tmp_path, **overrides: Any):
    return make_settings(tmp_path, **{**CHECK_ON, **overrides})


def template(name: str = "vship_update", *, status: str = "APPROVED", category: str = "UTILITY") -> dict:
    return {
        "name": name,
        "language": "en_US",
        "status": status,
        "category": category,
        "components": [{"type": "BODY", "text": BODY}],
        "id": "1387372356726668",
    }


def listed(message_id: str, received: str, subject: str, sender: str = "Chris@Example.com") -> dict:
    return {
        "id": message_id,
        "receivedDateTime": received,
        "subject": subject,
        "from": {"emailAddress": {"name": "Chris", "address": sender}},
    }


def full_message(
    *,
    auth_as: list[str] | None = None,
    sender: str = "chris@example.com",
    html: str = (
        "<html><body><p>Name:Maria</p><p>Message:</p><p>Your container arrived.</p><p>##</p>"
        "<p>Chris</p></body></html>"
    ),
    delivered_subject: str | None = "WA | 650 555 1234 | BK1",
) -> dict:
    """A command email as GraphMailbox.get_message returns it (``delivered_subject``: its Subject line)."""
    headers = [{"name": "From", "value": "Chris <chris@example.com>"}]
    if delivered_subject is not None:
        headers.append({"name": "Subject", "value": delivered_subject})
    headers += [{"name": "X-MS-Exchange-Organization-AuthAs", "value": value} for value in (auth_as or [])]
    return {
        "id": "MSG1",
        "subject": "WA | 650 555 1234 | BK1",
        "from": {"emailAddress": {"name": "Chris", "address": "chris@example.com"}},
        "sender": {"emailAddress": {"name": "Chris", "address": sender}},
        "internetMessageId": "<cmd1@example.com>",
        "receivedDateTime": "2026-11-18T15:00:00Z",
        "body": {"contentType": "html", "content": html},
        "internetMessageHeaders": headers,
        "singleValueExtendedProperties": [{"id": "String 0x1A", "value": "IPM.Note"}],
    }


def mock_everything(inbox: list[dict] | None = None, folder: list[dict] | None = None) -> dict[str, Any]:
    """Graph and Meta answering as for a mailbox and templates that are ready."""
    return {
        "token": respx.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        ),
        "inbox": respx.get(INBOX).mock(return_value=httpx.Response(200, json={"value": inbox or []})),
        "folders": respx.get(FOLDERS).mock(
            return_value=httpx.Response(
                200, json={"value": [{"id": "FOLDER1", "displayName": "WhatsApp updates - handled"}]}
            )
        ),
        "folder": respx.get(FOLDER_MESSAGES).mock(
            return_value=httpx.Response(200, json={"value": folder or []})
        ),
        "templates": respx.get(TEMPLATES).mock(return_value=httpx.Response(200, json={"data": [template()]})),
        "phone": respx.get(PHONE).mock(return_value=httpx.Response(200, json=PHONE_OK)),
    }


async def check(settings, **kwargs: Any) -> tuple[int, list[str]]:
    lines: list[str] = []
    async with httpx.AsyncClient() as http:
        code = await run_check(settings, http, out=lines.append, **kwargs)
    return code, lines


def failures(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("FAIL")]


def only_reads() -> bool:
    """Apart from asking Microsoft for a token, every request was a GET: nothing sent, moved or changed."""
    return all(call.request.method == "GET" for call in respx.calls if str(call.request.url) != TOKEN_URL)


# ----------------------------------------------------------------------------- relay-check
@respx.mock
async def test_everything_ready(tmp_path):
    routes = mock_everything()
    code, lines = await check(check_settings(tmp_path))
    assert code == 0 and failures(lines) == []
    assert all(line[:6] in ("ok    ", "WARN  ") for line in lines)
    assert "ok    RELAY_ENABLED is true: the service handles WhatsApp update emails" in lines
    assert "ok    allowed senders (1): chris@example.com" in lines
    assert "ok    mailbox whatsapp@example.com can be read (0 email(s) in its Inbox)" in lines
    assert (
        'ok    folder "WhatsApp updates - handled" exists in whatsapp@example.com (created if it was missing)'
        in lines
    )
    assert (
        "ok    template vship_update (en_US), the update template: APPROVED, UTILITY, 2 variable(s) filled"
        " with name, message" in lines
    )
    assert any(line.startswith("ok    no booking template") for line in lines)
    assert any(line.startswith("ok    no document template") for line in lines)
    assert "ok    sending number +1 555-078-3881 (VShip) can be reached" in lines
    assert "ok    display name status: APPROVED" in lines and "ok    quality rating: GREEN" in lines
    assert (
        "WARN  messaging limit: TIER_250: updates can reach at most 250 different customers per 24 hours"
        " (customers who wrote to VShip in the last 24 hours do not count)" in lines
    )
    phone = routes["phone"].calls.last.request
    assert phone.url.params["fields"] == PHONE_FIELDS
    assert phone.url.params["appsecret_proof"] == appsecret_proof("wa-token", "app-secret-app-secret")
    assert phone.headers["Authorization"] == "Bearer wa-token"
    assert only_reads()


@respx.mock
async def test_failed_checks_are_fail_lines_and_exit_code_2(tmp_path):
    routes = mock_everything()
    routes["inbox"].mock(
        return_value=httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "Denied."}})
    )
    routes["templates"].mock(return_value=httpx.Response(200, json={"data": [template(status="REJECTED")]}))
    routes["phone"].mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "Unsupported get request.", "code": 100, "error_subcode": 33}}
        )
    )
    code, lines = await check(check_settings(tmp_path))
    assert code == 2
    mailbox, templ, phone = failures(lines)
    assert mailbox.startswith(
        "FAIL  mailbox whatsapp@example.com cannot be read: Graph list inbox returned HTTP 403"
    )
    assert "ErrorAccessDenied" in mailbox
    assert templ.startswith("FAIL  template vship_update (en_US), the update template:")
    assert "REJECTED" in templ
    assert phone == (
        f"FAIL  WhatsApp refused to show the sending number {PHONE_ID} (HTTP 400, code 100): Unsupported get"
        " request. Check the token and the phone number id with `whatsapp-webhook meta-check`"
    )
    assert not routes["folders"].called  # not looked for when the mailbox cannot be read
    assert only_reads()


@respx.mock
async def test_every_configured_template_is_checked(tmp_path):
    routes = mock_everything()

    def answer(request: httpx.Request) -> httpx.Response:
        name = request.url.params["name"]
        status = "PENDING" if name == "vship_booking_update" else "APPROVED"
        category = "MARKETING" if name == "vship_document" else "UTILITY"
        return httpx.Response(200, json={"data": [template(name, status=status, category=category)]})

    routes["templates"].mock(side_effect=answer)
    settings = check_settings(
        tmp_path,
        relay_booking_template_name="vship_booking_update",
        relay_booking_template_params="name,booking,message",
        relay_document_template_name="vship_document",
        relay_document_template_params="name,filename",
    )
    code, lines = await check(settings)
    assert code == 2
    booking, document = failures(lines)
    assert booking.startswith("FAIL  template vship_booking_update (en_US), the booking template")
    assert "PENDING" in booking
    assert document.startswith("FAIL  template vship_document (en_US), the document template")
    assert "MARKETING" in document and "document header" in document
    assert any(line.startswith("ok    template vship_update (en_US)") for line in lines)
    assert routes["templates"].call_count == 3


@respx.mock
async def test_meta_unreachable_for_the_template_check_is_a_failure(tmp_path):
    routes = mock_everything()
    routes["templates"].mock(return_value=httpx.Response(503, text="Service Unavailable"))
    code, lines = await check(check_settings(tmp_path))
    assert code == 2
    (line,) = failures(lines)
    assert line.startswith("FAIL  template vship_update (en_US), the update template: could not be checked:")


@respx.mock
async def test_runs_with_the_feature_off_and_says_so(tmp_path):
    mock_everything()
    code, lines = await check(check_settings(tmp_path, relay_enabled=False))
    assert code == 1 and failures(lines) == []
    assert "WARN  RELAY_ENABLED is false: the feature is off (checking everything anyway)" in lines
    assert "ok    mailbox whatsapp@example.com can be read (0 email(s) in its Inbox)" in lines


@respx.mock
async def test_an_unreadable_relay_enabled_line_is_not_called_false(tmp_path):
    mock_everything()
    code, lines = await check(check_settings(tmp_path, relay_enabled="flase"))
    assert code == 1
    assert "WARN  RELAY_ENABLED is not true: the feature is off (checking everything anyway)" in lines
    assert failures(lines) == ["FAIL  setting: RELAY_ENABLED must be true or false, not 'flase'"]


@respx.mock
async def test_settings_that_would_stop_the_feature_are_shown_while_it_is_off(tmp_path):
    mock_everything()
    code, lines = await check(check_settings(tmp_path, relay_enabled=False, relay_allowed_senders=""))
    assert code == 1
    assert failures(lines) == [
        "FAIL  setting: RELAY_ALLOWED_SENDERS must list at least one address when RELAY_ENABLED=true"
    ]


@respx.mock
async def test_missing_settings_skip_the_checks_that_need_them(tmp_path):
    # SMTP transport, no WABA, no business number: nothing can be asked (respx fails any request)
    code, lines = await check(make_settings(tmp_path))
    assert code == 1
    assert (
        "FAIL  mailbox not checked: MAIL_TRANSPORT must be graph (the Inbox is read through Graph)" in lines
    )
    assert "FAIL  templates not checked: WA_ACCESS_TOKEN and WA_WABA_ID are needed" in lines
    assert (
        "FAIL  sending number not checked: RELAY_PHONE_NUMBER_ID (or WA_PHONE_NUMBER_ID) is not set" in lines
    )
    assert any("RELAY_ENABLED=true needs MAIL_TRANSPORT=graph" in line for line in lines)
    assert not respx.calls


@respx.mock
async def test_sample_shows_the_internal_mark_the_sender_and_how_the_text_is_read(tmp_path):
    inbox = [
        listed("MSG0", "2026-11-18T16:00:00Z", "RE: WA | 650 555 1234 | BK1"),  # a reply: no command
        listed("MSG2", "2026-11-18T17:00:00Z", "WA | 650 555 1234", sender="stranger@example.com"),
        listed("MSG1", "2026-11-18T15:00:00Z", "WA | 650 555 1234 | BK1"),
    ]
    routes = mock_everything(inbox=inbox, folder=[listed("OLD", "2026-11-17T09:00:00Z", "WA | 650 555 1234")])
    message = respx.get(f"{BOX}/messages/MSG1").mock(
        return_value=httpx.Response(200, json=full_message(auth_as=["Internal"]))
    )
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 0 and failures(lines) == []
    start = lines.index(
        "ok    sample: the newest command email from an allowed sender, received 2026-11-18T15:00:00Z (Inbox)"
    )
    assert lines[start + 1 :] == [
        "ok    sample: From chris@example.com is on RELAY_ALLOWED_SENDERS",
        "ok    sample: From and Sender are the same person (not sent on behalf of someone else)",
        "ok    sample: Exchange's internal-sender mark is visible:"
        " X-MS-Exchange-Organization-AuthAs: Internal",
        "ok    sample: its subject is the one it was delivered with",
        "ok    sample: it was delivered to the mailbox (not a draft written in it)",
        "ok    sample: an ordinary email, not automatic mail",
        "ok    phone number: +16*****1234",
        "ok    booking number: BK1",
        "ok    name: Maria",
        "ok    message (23 characters): Your container arrived.",
        "ok    template vship_update (en_US): {{1}} = Maria, {{2}} = Your container arrived.",
    ]
    assert message.called and routes["folder"].called
    assert only_reads()  # nothing was moved or marked as read


@respx.mock
async def test_sample_without_the_internal_mark_or_sent_on_behalf_fails(tmp_path):
    mock_everything(inbox=[listed("MSG1", "2026-11-18T15:00:00Z", "WA | 650 555 1234 | BK1")])
    message = respx.get(f"{BOX}/messages/MSG1").mock(
        return_value=httpx.Response(200, json=full_message(auth_as=[], sender="assistant@example.com"))
    )
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 2
    behalf, mark = failures(lines)
    assert behalf.startswith("FAIL  sample: sent on behalf of someone else (Sender assistant@example.com")
    assert mark.startswith("FAIL  sample: Graph does not show Exchange's internal-sender mark")
    assert "EVERY command would be ignored" in mark

    message.mock(return_value=httpx.Response(200, json=full_message(auth_as=["Internal", "Anonymous"])))
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 2
    (mark,) = failures(lines)
    assert mark == (
        "FAIL  sample: Exchange's internal-sender mark says 'internal', 'anonymous', not Internal: such an"
        " email is ignored"
    )


@respx.mock
async def test_sample_shows_whether_the_subject_is_the_one_it_was_delivered_with(tmp_path):
    """The check against emails edited in the mailbox, tried on real Exchange data during the dry run."""
    mock_everything(inbox=[listed("MSG1", "2026-11-18T15:00:00Z", "WA | 650 555 1234 | BK1")])
    message = respx.get(f"{BOX}/messages/MSG1").mock(
        return_value=httpx.Response(
            200, json=full_message(auth_as=["Internal"], delivered_subject="Lunch on Friday?")
        )
    )
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 2
    assert failures(lines) == [
        "FAIL  sample: its subject is not the Subject line it was delivered with (edited in the mailbox after"
        " it arrived): such an email is ignored"
    ]
    # Graph shows no Subject line: commands still work, but an edited subject would go unnoticed
    message.mock(
        return_value=httpx.Response(200, json=full_message(auth_as=["Internal"], delivered_subject=None))
    )
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 0 and failures(lines) == []
    assert (
        "WARN  sample: Graph does not show the Subject line it was delivered with, so a subject changed in"
        " the mailbox after an email arrived cannot be noticed" in lines
    )
    # encoded and folded the way Exchange keeps a long or non-ASCII Subject line: the same subject
    message.mock(
        return_value=httpx.Response(
            200,
            json=full_message(
                auth_as=["Internal"], delivered_subject="=?utf-8?Q?WA_|_650_555_1234?=\r\n =?utf-8?Q?_|_BK1?="
            ),
        )
    )
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 0 and "ok    sample: its subject is the one it was delivered with" in lines


@respx.mock
async def test_sample_fails_for_a_draft_written_in_the_mailbox(tmp_path):
    mock_everything(inbox=[listed("MSG1", "2026-11-18T15:00:00Z", "WA | 650 555 1234 | BK1")])
    respx.get(f"{BOX}/messages/MSG1").mock(
        return_value=httpx.Response(200, json={**full_message(auth_as=["Internal"]), "isDraft": True})
    )
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 2
    assert failures(lines) == [
        "FAIL  sample: it is a draft, written in the mailbox rather than sent to it: such an email is ignored"
    ]


@respx.mock
async def test_sample_says_when_there_is_nothing_to_look_at(tmp_path):
    mock_everything(inbox=[listed("X", "2026-11-18T15:00:00Z", "Hello")])
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 0
    assert any(line.startswith("WARN  --sample: no command email from an allowed sender") for line in lines)


@respx.mock
async def test_sample_reports_a_refused_text(tmp_path):
    mock_everything(inbox=[listed("MSG1", "2026-11-18T15:00:00Z", "WA | 650 555 1234 | BK1")])
    respx.get(f"{BOX}/messages/MSG1").mock(
        return_value=httpx.Response(
            200, json=full_message(auth_as=["Internal"], html="<p>Name:Maria</p><p>Message: hi</p>")
        )
    )
    code, lines = await check(check_settings(tmp_path), sample=True)
    assert code == 2
    (line,) = failures(lines)
    assert line.startswith("FAIL  NOT sent (end_missing):")


# ----------------------------------------------------------------------------- relay-parse
def parse(settings, subject: str, body: str, **kwargs: Any) -> tuple[int, list[str]]:
    lines: list[str] = []
    return run_parse(settings, subject, body, out=lines.append, **kwargs), lines


def test_parse_shows_what_the_customer_would_get(tmp_path):
    body = "Hi team\nName:Maria\nMessage:\nIt arrived\nat the port.\n##\nChris\nVShip"
    code, lines = parse(make_settings(tmp_path), "WA | 650 555 1234 | BK1", body)
    assert code == 0
    assert lines == [
        "ok    phone number: +1 650-555-1234",
        "ok    booking number: BK1",
        "ok    name: Maria",
        "ok    message (23 characters): It arrived at the port.",
        "WARN  the message had line breaks: they are joined with spaces (the template allows none)",
        "ok    template vship_update (en_US): {{1}} = Maria, {{2}} = It arrived at the port.",
    ]


def test_parse_shows_the_fallback_name_and_the_booking_template(tmp_path):
    settings = make_settings(
        tmp_path,
        relay_booking_template_name="vship_booking_update",
        relay_booking_template_params="name,booking,message",
        relay_test_numbers="+44 20 7946 0958",
    )
    code, lines = parse(settings, "wa|+1 (650) 555-1234|BK7", "Name:\nMessage: Ready for pickup.\n##")
    assert code == 0
    assert 'WARN  the Name: line is empty: the greeting uses "there"' in lines
    assert "WARN  test mode is on and this is not a test number: the update would be refused" in lines
    assert lines[-1] == (
        "ok    template vship_booking_update (en_US): {{1}} = there, {{2}} = BK7, {{3}} = Ready for pickup."
    )
    code, lines = parse(settings, "WA | 650 555 1234", "Name:Maria\nMessage: Ready.\n##")
    assert lines[-1] == "ok    template vship_update (en_US): {{1}} = Maria, {{2}} = Ready."


@pytest.mark.parametrize(
    ("subject", "body", "code"),
    [
        ("WA | 650 555 1234 | BK1 | extra", "Name:M\nMessage: x\n##", "subject_fields"),
        ("WA |  | BK1", "Name:M\nMessage: x\n##", "phone_missing"),
        ("WA | 12345", "Name:M\nMessage: x\n##", "phone_invalid"),
        ("WA | 0044 20 7946 0958", "Name:M\nMessage: x\n##", "phone_prefix"),
        ("WA | 650 555 1234", "Message: x\n##", "name_missing"),
        ("WA | 650 555 1234", "Name:" + "M" * 61 + "\nMessage: x\n##", "name_too_long"),
        ("WA | 650 555 1234", "Name:M\nText: x\n##", "message_missing"),
        ("WA | 650 555 1234", "Name:M\nMessage: x", "end_missing"),
        ("WA | 650 555 1234", "Name:M\nMessage:\n##", "message_empty"),
        ("WA | 650 555 1234", "Name:M\nMessage: " + "x" * 1001 + "\n##", "message_too_long"),
    ],
)
def test_parse_shows_why_an_email_would_be_refused(tmp_path, subject, body, code):
    result, lines = parse(make_settings(tmp_path), subject, body)
    assert result == 2
    (line,) = failures(lines)
    assert line.startswith(f"FAIL  NOT sent ({code}): ")


def test_parse_of_a_reply_or_forward_is_no_command(tmp_path):
    for subject in ("RE: WA | 650 555 1234", "FW: WA | 650 555 1234", "Update for 650 555 1234"):
        code, lines = parse(make_settings(tmp_path), subject, "Name:M\nMessage: x\n##")
        assert code == 2 and lines[0].startswith("FAIL  not a command: the subject must start with WA |")


def test_parse_of_a_template_value_that_would_be_empty(tmp_path):
    settings = make_settings(tmp_path, relay_template_params="name,booking,message")
    code, lines = parse(settings, "WA | 650 555 1234", "Name:M\nMessage: x\n##")
    assert code == 2
    assert failures(lines)[0].startswith("FAIL  NOT sent: The WhatsApp template vship_update needs a booking")


def test_parse_reads_html_like_the_service(tmp_path):
    html = "<html><body><div>Name: Maria</div><div>Message:</div><div>Ready.</div><div>##</div></body></html>"
    code, lines = parse(make_settings(tmp_path), "WA | 650 555 1234", html, html=True)
    assert code == 0 and "ok    name: Maria" in lines and "ok    message (6 characters): Ready." in lines
    code, lines = parse(make_settings(tmp_path), "WA | 650 555 1234", html)
    assert code == 2
    assert lines[0].startswith("WARN  the text looks like HTML: add --html")


# ----------------------------------------------------------------------------- through the command line
@pytest.fixture
def cli_logging():
    """cli.main configures logging for the command line; put the level back afterwards."""
    root = logging.getLogger()
    level = root.level
    yield
    root.setLevel(level)


def test_relay_parse_command(tmp_path, monkeypatch, capsys, cli_logging):
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    body = tmp_path / "body.txt"
    body.write_text(chr(0xFEFF) + "Name:Maria\r\nMessage:\r\nReady.\r\n##\r\n", encoding="utf-8")  # BOM
    assert cli.main(["relay-parse", "--subject", "WA | 650 555 1234", "--body-file", str(body)]) == 0
    out = capsys.readouterr().out
    assert "ok    name: Maria" in out and "ok    message (6 characters): Ready." in out
    page = tmp_path / "body.html"
    page.write_text("<p>Name:Maria</p><p>Message:</p><p>Ready.</p><p>##</p>", encoding="utf-8")
    argv = ["relay-parse", "--subject", "WA | 650 555 1234", "--body-file", str(page), "--html"]
    assert cli.main(argv) == 0
    missing = ["relay-parse", "--subject", "WA | 650 555 1234", "--body-file", str(tmp_path / "nope.txt")]
    assert cli.main(missing) == 1
    assert "cannot read" in capsys.readouterr().err


@respx.mock
def test_relay_check_command(tmp_path, monkeypatch, capsys, cli_logging):
    settings = check_settings(tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    mock_everything(inbox=[listed("X", "2026-11-18T15:00:00Z", "Hello")])
    assert cli.main(["relay-check"]) == 0
    assert "ok    mailbox whatsapp@example.com can be read" in capsys.readouterr().out
    assert cli.main(["relay-check", "--sample"]) == 0
    assert "WARN  --sample: no command email" in capsys.readouterr().out


def test_relay_check_with_a_configuration_error_exits_78(tmp_path, monkeypatch, cli_logging):
    # a core setting the whole service needs (MAIL_TO) is wrong: nothing can be loaded
    monkeypatch.setattr(cli, "load_settings", lambda: make_settings(tmp_path, relay_enabled=True, mail_to=""))
    assert cli.main(["relay-check"]) == cli.EX_CONFIG == 78


def test_relay_check_lists_relay_setting_problems_and_fails(tmp_path, monkeypatch, capsys):
    # RELAY_ENABLED=true with nothing else set: the service still starts (forwarder only); the check says why
    monkeypatch.setattr(cli, "load_settings", lambda: make_settings(tmp_path, relay_enabled=True))
    assert cli.main(["relay-check"]) == 2
    out = capsys.readouterr().out
    assert "FAIL  RELAY_ENABLED is true, but the service does NOT handle WhatsApp update emails" in out
    assert "FAIL  setting: RELAY_ALLOWED_SENDERS must list at least one address" in out


def test_check_config_passes_but_warns_about_relay_setting_problems(tmp_path, monkeypatch, capsys):
    # install.sh restarts the service only when check-config passes: relay problems must not block it
    monkeypatch.setattr(cli, "load_settings", lambda: make_settings(tmp_path, relay_enabled=True))
    assert cli.main(["check-config"]) == 0
    err = capsys.readouterr().err
    assert "WARNING: WhatsApp updates from email will NOT start until this is fixed:" in err
    assert "RELAY_ALLOWED_SENDERS must list at least one address" in err
