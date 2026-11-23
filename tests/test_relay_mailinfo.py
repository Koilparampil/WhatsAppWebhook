from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from app.relay import mailbox
from app.relay.mailinfo import (
    AUTH_AS_HEADER,
    MailInfo,
    auth_marks,
    automatic_mail,
    bare_subject,
    internal_auth_ok,
    subject_edited,
    valid_address,
)

# How Exchange names a person when it has no address for them (an internal directory name)
UNREADABLE = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP (FYDIBOHF23SPDLT)/CN=RECIPIENTS/CN=CHRIS"
DELEGATE = "/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN=DELEGATE"
RECEIVED = "2026-11-18T15:00:00Z"


def graph_message(
    *,
    author: str | None = "chris@example.com",
    name: str = "Jordan Rivera",
    sender: str | None = None,  # None: the same as the author
    subject: str = "WA | 650-555-1234 | B-1001",
    headers: list[tuple[str, str]] | None = None,
    auth: str | None = "Internal",
    message_class: str | None = "IPM.Note",
    to: list[str] | None = None,
    body: str = "<div>Name: Sheena Nelson</div>",
    content_type: str = "html",
    received: str | None = RECEIVED,
) -> dict[str, Any]:
    """A command email as Graph's get_message returns it (headers may repeat, as they can in mail)."""
    all_headers = list(headers or [])
    if auth is not None:
        all_headers.append(("X-MS-Exchange-Organization-AuthAs", auth))
    raw: dict[str, Any] = {
        "id": "AAMkADimmutable=",
        "internetMessageId": "<CH3PR01MB1234ABCD@CH3PR01MB1234.namprd01.prod.outlook.com>",
        "subject": subject,
        "from": {"emailAddress": {"address": author, "name": name}},
        "sender": {"emailAddress": {"address": sender or author, "name": name}},
        "toRecipients": [
            {"emailAddress": {"address": a}} for a in (["whatsapp@example.com"] if to is None else to)
        ],
        "ccRecipients": [],
        "receivedDateTime": received,
        "hasAttachments": False,
        "body": {"contentType": content_type, "content": body},
        "internetMessageHeaders": [{"name": k, "value": v} for k, v in all_headers],
    }
    if message_class is not None:
        raw["singleValueExtendedProperties"] = [{"id": "String 0x1a", "value": message_class}]
    return raw


def mail(**changes: Any) -> MailInfo:
    return MailInfo.from_graph(graph_message(**changes))


# ----------------------------------------------------------------------------- reading a Graph message
def test_a_command_email_is_read_as_graph_delivers_it():
    info = mail(
        author="Chris@Example.com",
        name="  Jordan Rivera ",
        headers=[("Received", "from CH3PR01MB1234"), ("received", "from CH3PR01MB5678")],
    )
    assert info.author == "chris@example.com" and info.author_name == "Jordan Rivera"
    assert info.actual_sender == "chris@example.com" and info.on_behalf is False
    assert info.subject == "WA | 650-555-1234 | B-1001"
    assert info.internet_message_id == "<CH3PR01MB1234ABCD@CH3PR01MB1234.namprd01.prod.outlook.com>"
    assert info.recipients == ["whatsapp@example.com"]
    assert info.body == "<div>Name: Sheena Nelson</div>" and info.body_is_html is True
    assert info.message_class == "IPM.Note"
    assert info.received_at == datetime(2026, 11, 18, 15, tzinfo=UTC).timestamp()
    # header names in lower case; every copy of a header is kept, in order
    assert info.headers["received"] == ["from CH3PR01MB1234", "from CH3PR01MB5678"]
    assert info.header("Received") == "from CH3PR01MB1234"
    assert info.header("X-MS-Exchange-Organization-AuthAs") == "Internal"
    assert info.header("In-Reply-To") is None


def test_a_plain_text_email_and_an_email_with_parts_missing():
    plain = mail(body="Name: Sheena\r\nMessage:\r\nHi\r\n##", content_type="text")
    assert plain.body_is_html is False and plain.body.startswith("Name: Sheena\r\n")
    empty = MailInfo.from_graph({})
    assert (empty.author, empty.actual_sender, empty.on_behalf) == (None, None, False)
    assert (empty.subject, empty.internet_message_id, empty.body, empty.body_is_html) == ("", "", "", False)
    assert empty.recipients == [] and empty.headers == {} and empty.message_class is None
    assert empty.received_at is None and empty.addressed() == []
    # whatever odd shape a field has, reading the email does not fail
    odd = MailInfo.from_graph(
        {
            "from": "chris@example.com",
            "sender": {"emailAddress": "chris@example.com"},
            "toRecipients": "whatsapp@example.com",
            "ccRecipients": [None, {"emailAddress": None}, {"emailAddress": {"address": "ops@example.com"}}],
            "body": "Name: x",
            "internetMessageHeaders": [None, "Precedence: bulk", {"value": "no name"}, {"name": "X-Empty"}],
            "singleValueExtendedProperties": [None, {"id": "String 0x0037", "value": "subject"}],
            "receivedDateTime": "yesterday",
        }
    )
    assert odd.author is None and odd.recipients == ["ops@example.com"] and odd.body == ""
    assert odd.headers == {"x-empty": [""]} and odd.message_class is None and odd.received_at is None


@pytest.mark.parametrize("prop_id", ["String 0x1a", "String 0x1A", "String 0x001A", "string 0x001a"])
def test_the_message_class_is_read_whichever_way_graph_writes_its_id(prop_id):
    raw = graph_message(message_class=None)
    raw["singleValueExtendedProperties"] = [
        {"id": "String 0x0037", "value": "not the class"},
        {"id": prop_id, "value": "IPM.Note.Rules.OofTemplate.Microsoft"},
    ]
    assert MailInfo.from_graph(raw).message_class == "IPM.Note.Rules.OofTemplate.Microsoft"
    raw["singleValueExtendedProperties"] = [{"id": prop_id, "value": ""}]
    assert MailInfo.from_graph(raw).message_class is None


@pytest.mark.parametrize(
    ("received", "expected"),
    [
        (RECEIVED, datetime(2026, 11, 18, 15, tzinfo=UTC)),
        ("2026-11-18T10:00:00-05:00", datetime(2026, 11, 18, 15, tzinfo=UTC)),
        ("2026-11-18T15:00:00", datetime(2026, 11, 18, 15, tzinfo=UTC)),  # no zone: Graph means UTC
        ("2026-11-18T15:00:00.25Z", datetime(2026, 11, 18, 15, 0, 0, 250_000, tzinfo=UTC)),
        ("not a date", None),
        (None, None),
    ],
)
def test_when_the_email_arrived(received, expected):
    info = mail(received=received)
    assert info.received_at == (expected.timestamp() if expected else None)


def test_addressed_lists_to_and_cc_from_graph_and_from_the_headers_but_not_display_names():
    raw = graph_message(
        to=["WhatsApp@Example.com", ""],
        headers=[
            ("To", '"whatsapp@example.com" <pat@outside.example>'),  # an address typed into a name
            ("Cc", "Ops <ops@example.com>, whatsapp@example.com"),
        ],
    )
    raw["ccRecipients"] = [{"emailAddress": {"address": "ops@example.com"}}]
    info = MailInfo.from_graph(raw)
    assert info.recipients == ["WhatsApp@Example.com", "ops@example.com"]
    assert info.addressed() == ["whatsapp@example.com", "ops@example.com", "pat@outside.example"]


def test_valid_address_is_defined_once():
    assert valid_address is mailbox.valid_address
    assert valid_address(" O'Brien@Example.com ") == "o'brien@example.com" and valid_address("x@y") is None


@pytest.mark.parametrize(("value", "draft"), [(True, True), (False, False), (None, False), ("true", False)])
def test_a_draft_is_marked_only_when_graph_says_so(value, draft):
    raw = graph_message()
    if value is not None:
        raw["isDraft"] = value
    assert MailInfo.from_graph(raw).is_draft is draft


def test_the_subject_checks_live_next_to_the_other_mail_checks():
    """relay-check --sample and the processor share one subject_edited (and its decoding)."""
    from app.relay import check, processor

    assert check.subject_edited is subject_edited and processor.subject_edited is subject_edited
    assert not subject_edited(mail(headers=[("Subject", "=?utf-8?Q?WA_|_650-555-1234_|_B-1001?=")]))
    assert subject_edited(mail(headers=[("Subject", "Lunch on Friday?")]))


def test_bare_subject():
    assert bare_subject("RE: FW: AW: [WhatsApp] Sheena  Nelson: Hi") == "[whatsapp] sheena nelson: hi"
    assert bare_subject("RE[2]: Sheena Nelson: hi") == "sheena nelson: hi"
    assert bare_subject("SV:  WA | 650-555-1234") == "wa | 650-555-1234"
    assert bare_subject("RE\uff1a WA | 650-555-1234") == "wa | 650-555-1234"  # a full-width colon
    assert bare_subject("WA | 650-555-1234 | B-1001") == "wa | 650-555-1234 | b-1001"


# ----------------------------------------------------------------------------- From must equal Sender
def test_an_email_sent_on_behalf_of_someone_else_is_marked():
    delegated = mail(sender="assistant@example.com")
    assert delegated.on_behalf is True
    assert (delegated.author, delegated.actual_sender) == ("chris@example.com", "assistant@example.com")
    # a sender Exchange names in a form that cannot be read is not taken for the author
    unreadable = mail(sender=DELEGATE)
    assert unreadable.on_behalf is True and unreadable.actual_sender is None
    # ... unless the email's own Sender line names the author
    named = mail(sender=DELEGATE, headers=[("Sender", "Chris <chris@example.com>")])
    assert named.on_behalf is False and named.actual_sender == "chris@example.com"
    # ... and a Sender line that names someone else is someone else
    other = mail(sender=DELEGATE, headers=[("Sender", "Pat <pat@example.com>")])
    assert other.on_behalf is True and other.actual_sender == "pat@example.com"
    # the same address in another letter case is the same person
    assert mail(sender="CHRIS@example.COM").on_behalf is False


def test_an_email_without_a_sender_is_the_authors_own():
    raw = graph_message()
    del raw["sender"]  # RFC 5322: without a Sender, the author sent it
    info = MailInfo.from_graph(raw)
    assert info.on_behalf is False and info.actual_sender is None and info.author == "chris@example.com"


def test_an_author_named_by_an_internal_directory_name_is_read_from_the_from_line():
    # Exchange gives the directory name in both From and Sender; the email's own From line names the person
    info = mail(author=UNREADABLE, headers=[("From", "Jordan Rivera <Chris@Example.com>")])
    assert info.author == "chris@example.com" and info.actual_sender == "chris@example.com"
    assert info.on_behalf is False
    # no From line to read: nobody to answer for (the processor ignores such mail)
    nobody = mail(author=UNREADABLE)
    assert nobody.author is None and nobody.on_behalf is False
    # a From line that names two people names nobody
    two = mail(author=UNREADABLE, headers=[("From", "a@example.com, b@example.com")])
    assert two.author is None


@pytest.mark.parametrize("author", ["not an address", "", None, "chris@", "<chris@example.com>"])
def test_an_author_that_is_not_a_plain_address_is_nobody(author):
    assert mail(author=author).author is None


# ----------------------------------------------------------------------------- automatic mail
@pytest.mark.parametrize(
    "change",
    [
        {"message_class": "IPM.Note.Rules.OofTemplate.Microsoft"},  # out-of-office reply
        {"message_class": "IPM.Note.Rules.ReplyTemplate.Microsoft"},  # a mail rule's reply
        {"message_class": "REPORT.IPM.Note.NDR"},  # bounce
        {"message_class": "REPORT.IPM.Note.DR"},  # delivery receipt
        {"message_class": "REPORT.IPM.Note.IPNRN"},  # read receipt
        {"message_class": "IPM.Schedule.Meeting.Request"},
        {"headers": [("Auto-Submitted", "auto-replied")]},
        {"headers": [("Auto-Submitted", "auto-generated")]},
        {"headers": [("X-Auto-Response-Suppress", "All")]},
        {"headers": [("X-AutoReply", "yes")]},
        {"headers": [("X-Autorespond", "on")]},
        {"headers": [("X-MS-Exchange-Inbox-Rules-Loop", "chris@example.com")]},  # a mail rule sent it on
        {"headers": [("X-MS-Exchange-Generated-Message-Source", "Mailbox Rules Agent")]},
        {"headers": [("X-Failed-Recipients", "nobody@example.com")]},
        {"headers": [("List-Id", "<ops.example.com>")]},
        {"headers": [("List-Unsubscribe", "<mailto:leave@example.com>")]},
        {"headers": [("Precedence", "bulk")]},
        {"headers": [("Precedence", " Junk ")]},
        {"headers": [("Precedence", "list")]},
        {"headers": [("Precedence", "auto_reply")]},
        # a Power Automate flow or a Logic App sent it, not a person
        {
            "headers": [
                ("x-ms-mail-application", "Microsoft Power Automate"),
                ("x-ms-mail-operation-type", "Send"),
            ]
        },
        {"subject": "Automatic reply: WA | 650-555-1234 | B-1001"},
        {"subject": "AutoReply: WA | 650-555-1234"},
        {"subject": "Auto: WA | 650-555-1234"},
        {"subject": "Out of Office: WA | 650-555-1234"},
        {"subject": "Undeliverable: WA | 650-555-1234"},
        {"subject": "Read: WA | 650-555-1234"},
        {"author": "postmaster@example.com"},
        {"author": "MAILER-DAEMON@example.com"},
        {"author": "MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@example.com"},
    ],
)
def test_automatic_mail_is_recognised(change):
    assert automatic_mail(mail(**change)) is not None


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"message_class": None},  # Graph would not say: the headers decide
        {"message_class": "IPM.Note.SMIME"},
        {"message_class": "IPM.Note.SMIME.MultipartSigned"},
        {"headers": [("Auto-Submitted", "no")]},
        {"headers": [("Auto-Submitted", " No ")]},
        {"headers": [("Precedence", "normal")]},
        {"subject": "WA | 650-555-1234 | Automatic reply: B-1001"},
        {"author": "post.master@example.com"},
    ],
)
def test_a_persons_email_is_not_automatic(change):
    assert automatic_mail(mail(**change)) is None


def test_the_reason_says_what_was_found():
    oof = mail(message_class="IPM.Note.Rules.OofTemplate.Microsoft")
    assert automatic_mail(oof) == "not an ordinary email (IPM.Note.Rules.OofTemplate.Microsoft)"
    assert automatic_mail(mail(headers=[("Auto-Submitted", "auto-replied")])) == (
        "an automatic message (Auto-Submitted)"
    )
    assert automatic_mail(mail(headers=[("X-Auto-Response-Suppress", "All")])) == (
        "an automatic message (x-auto-response-suppress)"
    )
    assert automatic_mail(mail(author="postmaster@example.com")) == "a message from the mail system"
    assert automatic_mail(mail(subject="Automatic reply: WA | 1")) == "an automatic message (subject)"


# ----------------------------------------------------------------------------- Exchange's AuthAs mark
def test_auth_marks_lists_every_copy_trimmed_and_in_lower_case():
    info = mail(
        auth=None,
        headers=[("X-MS-Exchange-Organization-AuthAs", " Internal "), (AUTH_AS_HEADER, "ANONYMOUS")],
    )
    assert auth_marks(info) == ["internal", "anonymous"]
    assert auth_marks(mail(auth=None)) == []


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("value", ["Internal", "internal", "INTERNAL", " Internal "])
def test_an_email_exchange_marked_internal_is_trusted(value, required):
    assert internal_auth_ok(mail(auth=value), required=required) is True


@pytest.mark.parametrize("required", [True, False])
def test_two_copies_that_both_say_internal_are_trusted(required):
    info = mail(
        auth=None, headers=[(AUTH_AS_HEADER, "Internal"), ("X-MS-Exchange-Organization-AuthAs", "Internal")]
    )
    assert internal_auth_ok(info, required=required) is True


@pytest.mark.parametrize("required", [True, False])
def test_a_forged_internal_mark_next_to_exchanges_own_verdict_is_not_trusted(required):
    info = mail(
        auth=None,
        headers=[
            ("X-MS-Exchange-Organization-AuthAs", "Internal"),  # as a sender can write it
            ("In-Reply-To", "<earlier@example.com>"),
            ("x-ms-exchange-organization-authas", "Anonymous"),  # Exchange's own verdict
        ],
    )
    assert internal_auth_ok(info, required=required) is False


@pytest.mark.parametrize("required", [True, False])
@pytest.mark.parametrize("value", ["Anonymous", "Partner", "External", ""])
def test_any_mark_other_than_internal_is_not_trusted(value, required):
    assert internal_auth_ok(mail(auth=value), required=required) is False


def test_a_missing_mark_fails_only_when_it_is_required():
    unmarked = mail(auth=None)
    assert internal_auth_ok(unmarked, required=True) is False  # RELAY_REQUIRE_INTERNAL_AUTH=true
    assert internal_auth_ok(unmarked, required=False) is True
