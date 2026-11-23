from __future__ import annotations

import json
import re
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate

import pytest
from app.crm.directory import LookupResult, LookupStatus
from app.mail.compose import build_emails
from app.mail.transports import recipients_of
from app.relay.command import CommandRefused, looks_like_command, parse_body, parse_subject
from app.relay.notices import (
    KIND_ALERT,
    KIND_DELIVERY_FAILED,
    KIND_DRY_RUN,
    KIND_REFUSED,
    KIND_SENT,
    KIND_UNCERTAIN,
    RELAY_HEADER,
    FileResult,
    build_delivery_failed_notice,
    build_dry_run_notice,
    build_refused_notice,
    build_relay_alert,
    build_sent_notice,
    build_uncertain_notice,
    from_payload,
    to_payload,
)
from app.whatsapp.media import DownloadedMedia
from app.whatsapp.parser import parse_webhook
from app.whatsapp.send import explain_error

from tests.conftest import load_payload, make_settings, text_payload

NOW = 1_700_000_000  # Tue 14 Nov 2023 22:13:20 UTC, 17:13:20 in New York
NOW_NEW_YORK = "Tue 14 Nov 2023 17:13:20 EST"
COMMAND_ID = "<SN6PR01MB4321ABCD@SN6PR01MB4321.namprd01.prod.outlook.com>"
EMPLOYEE = {
    "employee_address": "chris@vship2000.com",
    "employee_name": "Chris K",
    "command_message_id": COMMAND_ID,
    "command_subject": "WA | 650 555 1234 | B12345",
}
RENDERED = (
    "Hello Sheena, VShip has an update:\n"
    "Your container arrived in Newark.\n"
    "Please either reply to this message or send us an email at Elizabeth@vship2000.com if you have any"
    " Questions"
)
TOKEN = "EAAGsecretAccessToken0123456789"
CRM_PASSWORD = "crm-pass-word-2026"
UNCERTAIN_GIVEN = "No answer came back from WhatsApp within 20 seconds."
UNCERTAIN_DEFAULT = "WhatsApp did not confirm that it took the message, so it may or may not have been sent."


@pytest.fixture
def settings(tmp_path):
    return make_settings(
        tmp_path,
        mail_from="whatsapp@vship2000.com",
        relay_mailbox="whatsapp@vship2000.com",
        wa_access_token=TOKEN,
        crm_password=CRM_PASSWORD,
        wa_phone_number_id="106540352242922",
        wa_waba_id="102290129340398",
    )


def sent(settings, **overrides) -> EmailMessage:
    args = {
        **EMPLOYEE,
        "phone": "+16505551234",
        "booking": "B12345",
        "crm_match": "Customer #42: Acme Imports / John Doe",
        "template_name": "vship_update",
        "template_language": "en_US",
        "customer_text": RENDERED,
        "sent_at": NOW,
        "now": NOW,
        **overrides,
    }
    return build_sent_notice(settings, **args)


def dry_run(settings, **overrides) -> EmailMessage:
    args = {
        **EMPLOYEE,
        "phone": "+16505551234",
        "crm_match": "no CRM customer has this number",
        "template_name": "vship_update",
        "template_language": "en_US",
        "customer_text": RENDERED,
        "now": NOW,
        **overrides,
    }
    return build_dry_run_notice(settings, **args)


def uncertain(settings, **overrides) -> EmailMessage:
    args = {
        **EMPLOYEE,
        "phone": "+16505551234",
        "template_name": "vship_update",
        "template_language": "en_US",
        "customer_text": RENDERED,
        "uncertainty": UNCERTAIN_GIVEN,
        "sent_at": NOW,
        "now": NOW,
        **overrides,
    }
    return build_uncertain_notice(settings, **args)


def refused(settings, **overrides) -> EmailMessage:
    args = {
        **EMPLOYEE,
        "refusal_code": "phone_invalid",
        "refusal_text": "The phone number '650 555' is not a 10-digit US number.",
        "now": NOW,
        **overrides,
    }
    return build_refused_notice(settings, **args)


def delivery_failed(settings, **overrides) -> EmailMessage:
    args = {
        **EMPLOYEE,
        "phone": "+16505551234",
        "template_name": "vship_update",
        "error_code": 131026,
        "sent_at": NOW,
        "now": NOW,
        **overrides,
    }
    return build_delivery_failed_notice(settings, **args)


def alert(settings, **overrides) -> EmailMessage:
    args = {
        "subject": "template vship_update is not approved",
        "lines": ["WhatsApp reports the template as PAUSED.", "Check it in WhatsApp Manager."],
        "settings": settings,
        "now": NOW,
        **overrides,
    }
    return build_relay_alert(args.pop("subject"), args.pop("lines"), args.pop("settings"), **args)


EMPLOYEE_NOTICES = {
    KIND_SENT: sent,
    KIND_DRY_RUN: dry_run,
    KIND_UNCERTAIN: uncertain,
    KIND_REFUSED: refused,
    KIND_DELIVERY_FAILED: delivery_failed,
}
EVERY_EMAIL = {**EMPLOYEE_NOTICES, KIND_ALERT: alert}


def text_of(message: EmailMessage) -> str:
    return message.get_body(("plain",)).get_content()


def html_of(message: EmailMessage) -> str:
    return message.get_body(("html",)).get_content()


# ----------------------------------------------------------------------------- subjects
@pytest.mark.parametrize(
    ("kind", "overrides", "expected"),
    [
        (KIND_SENT, {}, "WhatsApp update sent: +1 650-555-1234 (booking B12345)"),
        (KIND_SENT, {"booking": None}, "WhatsApp update sent: +1 650-555-1234"),
        (KIND_SENT, {"booking": "  "}, "WhatsApp update sent: +1 650-555-1234"),
        (KIND_DRY_RUN, {}, "DRY RUN - WhatsApp update not sent: +1 650-555-1234"),
        (KIND_DRY_RUN, {"phone": "+442079460958"}, "DRY RUN - WhatsApp update not sent: +44 20 7946 0958"),
        (KIND_UNCERTAIN, {}, "WhatsApp update MAY NOT have been sent: +1 650-555-1234"),
        (KIND_REFUSED, {}, "WhatsApp update NOT sent: the phone number is not valid"),
        (KIND_DELIVERY_FAILED, {}, "WhatsApp could not deliver your update to +1 650-555-1234"),
        (KIND_ALERT, {}, "[WhatsApp] ALERT: template vship_update is not approved"),
    ],
)
def test_subjects_are_exactly_as_specified(settings, kind, overrides, expected):
    assert EVERY_EMAIL[kind](settings, **overrides)["Subject"] == expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("subject_fields", "too many | in the subject"),
        ("phone_missing", "no phone number in the subject"),
        ("phone_invalid", "the phone number is not valid"),
        ("phone_prefix", "write the number with + and the country code"),
        ("name_missing", 'no "Name:" line'),
        ("name_too_long", "the name is too long"),
        ("message_missing", 'no "Message:" line'),
        ("end_missing", "no line with only ## after the message"),
        ("message_empty", "the message is empty"),
        ("message_too_long", "the message is too long"),
        ("too_old", "your email is too old"),
        ("test_mode", "test mode is on (not a test number)"),
        ("template_not_ready", "the WhatsApp template is not ready"),
    ],
)
def test_refused_subject_names_the_reason_for_every_code(settings, code, expected):
    message = refused(settings, refusal_code=code, refusal_text="Some explanation.")
    assert message["Subject"] == f"WhatsApp update NOT sent: {expected}"


@pytest.mark.parametrize(
    ("subject", "body", "code"),
    [
        ("WA | 650 555 1234 | B1 | extra", None, "subject_fields"),
        ("WA |  | B1", None, "phone_missing"),
        (None, "Hello,\nplease send this.\n##", "name_missing"),
        (None, "Name: Sheena\nYour box arrived.\n##", "message_missing"),
        (None, "Name: Sheena\nMessage:\nYour box arrived.", "end_missing"),
        (None, "Name: Sheena\nMessage:\n\n##", "message_empty"),
    ],
)
def test_refused_notice_from_a_real_parser_refusal(settings, subject, body, code):
    with pytest.raises(CommandRefused) as caught:
        if subject is not None:
            parse_subject(subject)
        else:
            parse_body(body)
    refusal = caught.value
    assert refusal.code == code
    message = refused(settings, refusal_code=refusal.code, refusal_text=refusal.text)
    assert message["Subject"].startswith("WhatsApp update NOT sent: ")
    assert f"Reason: {refusal.text}" in text_of(message).splitlines()


def test_unknown_refusal_code_uses_the_first_sentence_or_the_given_short_reason(settings):
    text = explain_error(131026)
    message = refused(settings, refusal_code="whatsapp_refused", refusal_text=text)
    assert message["Subject"] == (
        "WhatsApp update NOT sent: WhatsApp could not deliver the message to this customer"
    )
    long_text = "x" * 200
    subject = refused(settings, refusal_code="other", refusal_text=long_text)["Subject"]
    assert subject == "WhatsApp update NOT sent: " + "x" * 77 + "..."
    given = refused(settings, refusal_code="other", refusal_text=text, short_reason="WhatsApp refused it")
    assert given["Subject"] == "WhatsApp update NOT sent: WhatsApp refused it"


@pytest.mark.parametrize("kind", sorted(EVERY_EMAIL))
def test_no_subject_starts_with_wa_or_looks_like_a_command(settings, kind):
    subject = EVERY_EMAIL[kind](settings)["Subject"]
    assert not subject.lower().startswith("wa")
    assert not looks_like_command(subject)


def test_alert_subject_never_starts_with_wa_even_with_such_a_prefix(tmp_path):
    settings = make_settings(tmp_path, mail_subject_prefix="WA |")
    subject = alert(settings)["Subject"]
    assert subject == "Notice: WA | ALERT: template vship_update is not approved"
    assert not looks_like_command(subject)


# ----------------------------------------------------------------------------- headers
@pytest.mark.parametrize("kind", sorted(EMPLOYEE_NOTICES))
def test_employee_notices_go_only_to_the_employee_from_the_relay_name(settings, kind):
    message = EMPLOYEE_NOTICES[kind](settings)
    assert message["From"] == "WhatsApp updates <whatsapp@vship2000.com>"
    assert message["To"] == "Chris K <chris@vship2000.com>"
    assert recipients_of(message) == ["chris@vship2000.com"]
    for absent in ("Cc", "Bcc", "Reply-To"):
        assert absent not in message


def test_display_name_with_a_comma_is_quoted(settings):
    message = sent(settings, employee_name="Rivera, Jordan")
    assert message["To"] == '"Rivera, Jordan" <chris@vship2000.com>'
    assert recipients_of(message) == ["chris@vship2000.com"]
    assert sent(settings, employee_name=None)["To"] == "chris@vship2000.com"


@pytest.mark.parametrize("address", ["", "   ", "not-an-address", "a@b.com, c@d.com", "<a@b.com>"])
def test_a_notice_without_a_usable_address_is_refused(settings, address):
    with pytest.raises(ValueError, match="not a usable email address"):
        sent(settings, employee_address=address)


def test_alert_goes_to_alert_to_or_else_mail_to(settings, tmp_path):
    message = alert(settings)
    assert message["From"] == "WhatsApp updates <whatsapp@vship2000.com>"
    assert recipients_of(message) == ["ops@example.com", "second@example.com"]
    own = make_settings(tmp_path, alert_to="admin@vship2000.com; it@vship2000.com")
    assert recipients_of(alert(own)) == ["admin@vship2000.com", "it@vship2000.com"]


@pytest.mark.parametrize("kind", sorted(EMPLOYEE_NOTICES))
def test_notices_thread_under_the_employee_command(settings, kind):
    message = EMPLOYEE_NOTICES[kind](settings)
    assert message["In-Reply-To"] == COMMAND_ID
    assert message["References"] == COMMAND_ID


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("", None),
        (None, None),
        ("   ", None),
        ("<a b@host>", None),
        ("<<x@host>>", None),
        ("abc123@host.example", "<abc123@host.example>"),
        ("  <abc123@host.example>\r\n", "<abc123@host.example>"),
    ],
)
def test_threading_headers_are_omitted_or_normalised(settings, given, expected):
    for build in EMPLOYEE_NOTICES.values():
        message = build(settings, command_message_id=given)
        assert message["In-Reply-To"] == expected
        assert message["References"] == expected


@pytest.mark.parametrize("kind", sorted(EVERY_EMAIL))
def test_every_email_is_marked_automatic_and_carries_its_kind(settings, kind):
    message = EVERY_EMAIL[kind](settings)
    assert message["Auto-Submitted"] == "auto-generated"
    assert message["X-Auto-Response-Suppress"] == "All"
    assert message[RELAY_HEADER] == kind
    assert message["Date"] == formatdate(NOW, localtime=False)
    assert re.fullmatch(rf"<relay\.{kind}\.[0-9a-f]{{32}}@vship2000\.com>", message["Message-ID"])


def test_alert_does_not_thread(settings):
    message = alert(settings)
    assert "In-Reply-To" not in message and "References" not in message


# ----------------------------------------------------------------------------- bodies
@pytest.mark.parametrize("kind", sorted(EVERY_EMAIL))
def test_text_and_html_alternatives_survive_serialisation(settings, kind):
    message = EVERY_EMAIL[kind](settings)
    assert message.get_content_type() == "multipart/alternative"
    assert [part.get_content_type() for part in message.iter_parts()] == ["text/plain", "text/html"]
    parsed = message_from_bytes(message.as_bytes(), policy=SMTP)  # what the recipient's server receives
    assert text_of(parsed).replace("\r\n", "\n") == text_of(message)
    assert html_of(parsed).replace("\r\n", "\n") == html_of(message)
    if kind != KIND_ALERT:
        for section in ("What happened", "What to do next"):
            assert f"----- {section} -----" in text_of(message)
            assert section in html_of(message)


H2 = re.compile(r"<h2[^>]*>(.*?)</h2>")
H3 = re.compile(r"<h3[^>]*>(.*?)</h3>")


@pytest.mark.parametrize("kind", sorted(EVERY_EMAIL))
def test_every_email_is_headed_whatsapp_update_not_by_its_last_section(settings, kind):
    """The renderers are the forwarder's; their heading is the title given, never a section's name."""
    message = EVERY_EMAIL[kind](settings)
    assert H2.findall(html_of(message)) == ["WhatsApp update"]
    assert text_of(message).splitlines()[0] == "WhatsApp update"
    assert H3.findall(html_of(message))  # the sections themselves still have their own headings


def test_forwarded_emails_keep_their_whatsapp_message_heading(settings, tmp_path):
    """The forwarder's emails look exactly as before the feature (570fd03), attachment or not."""
    lookup = LookupResult(LookupStatus.NO_MATCH, reason="none")
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(b"\xff\xd8 not really a picture")
    media = DownloadedMedia(
        path=photo,
        size=photo.stat().st_size,
        mime_type="image/jpeg",
        filename="photo.jpg",
        sha256_hex="0" * 64,
        integrity_verified=True,
    )
    cases = [
        (text_payload(), None, ["Message", "VShipCRM"]),
        (load_payload("image"), media, ["Message", "VShipCRM", "Attachment"]),
    ]
    for payload, downloaded, sections in cases:
        (email,) = build_emails(parse_webhook(payload).messages[0], lookup, downloaded, settings)
        html = html_of(email.message)
        assert '<h2 style="margin:0 0 10px;font-size:16px">WhatsApp message</h2>' in html
        assert H2.findall(html) == ["WhatsApp message"]
        assert H3.findall(html) == sections
        text = text_of(email.message)
        assert text.splitlines()[0] == "WhatsApp message"
        assert [line[6:-6] for line in text.splitlines() if line.startswith("----- ")] == sections


def test_employee_supplied_text_is_escaped_in_html(settings):
    hostile = "<script>alert('x')</script> Tom & Jerry <b>bold</b>"
    message = sent(
        settings,
        customer_text=f"Hello {hostile}, VShip has an update:\nBox & lid <img src=x onerror=alert(1)>",
        booking="B<1>&2",
        crm_match="Customer #7: A & B <Imports>",
        command_subject="WA | 650 555 1234 | <i>B</i>",
        files=[("<evil>.pdf", False, "type & size")],
        notes=["<u>note</u>"],
    )
    html = html_of(message)
    assert "<script" not in html and "<img" not in html and "<b>bold" not in html and "<i>" not in html
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt; Tom &amp; Jerry" in html
    assert "Box &amp; lid &lt;img src=x onerror=alert(1)&gt;" in html
    assert "B&lt;1&gt;&amp;2" in html and "A &amp; B &lt;Imports&gt;" in html
    assert "&lt;evil&gt;.pdf - type &amp; size" in html and "&lt;u&gt;note&lt;/u&gt;" in html
    text = text_of(message)  # the text part shows the words exactly as written
    assert f"  Hello {hostile}, VShip has an update:" in text.splitlines()
    assert "  Box & lid <img src=x onerror=alert(1)>" in text.splitlines()


def test_refusal_text_is_escaped_in_html(settings):
    message = refused(
        settings, refusal_code="other", refusal_text="Bad <img src=x onerror=alert(1)> & worse."
    )
    html = html_of(message)
    assert "<img" not in html
    assert "Reason: Bad &lt;img src=x onerror=alert(1)&gt; &amp; worse." in html


def test_sent_notice_shows_recipient_text_template_files_and_notes(settings):
    message = sent(
        settings,
        files=[
            FileResult("invoice.pdf", True, "as a document"),
            ("photo.heic", False, "WhatsApp does not accept this file type"),
            ("clip.mp4", None, "WhatsApp gave no clear answer"),
        ],
        name_fallback_used=True,
        lines_joined=True,
        test_mode=True,
        notes=["Extra note from the processor."],
    )
    lines = text_of(message).splitlines()
    for expected in (
        "Result     : Sent",
        "Booking    : B12345",
        f"Sent       : {NOW_NEW_YORK}",
        "Your email : WA | 650 555 1234 | B12345",
        "WhatsApp accepted your update for +1 650-555-1234.",
        "Attached files NOT sent: 1 of 3 (see Files below).",
        "Attached files that MAY NOT have been sent: 1 of 3 (see Files below).",
        "Send the files marked NOT sent to the customer another way, for example by email.",
        "Phone number: +1 650-555-1234",
        "VShipCRM: Customer #42: Acme Imports / John Doe",
        "  Hello Sheena, VShip has an update:",
        "  Your container arrived in Newark.",
        "Name: vship_update",
        "Language: en_US",
        "Sent: invoice.pdf (as a document)",
        "NOT sent: photo.heic - WhatsApp does not accept this file type",
        "MAY NOT have been sent: clip.mp4 - WhatsApp gave no clear answer",
        "The Name: line was empty, so a general greeting was used instead of the customer's name.",
        "Test mode is on: updates can only be sent to the test numbers the administrator set up.",
        "Extra note from the processor.",
    ):
        assert expected in lines, expected
    assert any(line.startswith("Your message had line breaks.") for line in lines)
    assert "Nothing more to do." not in lines
    assert not any(line.startswith("From ") for line in lines)


def test_sent_notice_without_file_problems_says_nothing_more_to_do(settings):
    lines = text_of(sent(settings, files=[("invoice.pdf", True, None)])).splitlines()
    assert "Nothing more to do." in lines
    assert "Sent: invoice.pdf" in lines
    assert not any(line.startswith("----- Notes") for line in lines)  # no notes, no empty section


def test_parameter_values_are_shown_when_the_template_text_is_unknown(settings):
    lines = text_of(sent(settings, customer_text=None, parameters=["Sheena", "Box arrived."])).splitlines()
    assert "The template's text could not be shown here; these values were filled in:" in lines
    assert "  {{1}}: Sheena" in lines and "  {{2}}: Box arrived." in lines
    assert "(not available)" in text_of(sent(settings, customer_text=None))


def test_dry_run_notice_says_nothing_was_sent(settings):
    message = dry_run(settings, files=[("a.pdf", True, None), ("b.heic", False, "not supported")])
    lines = text_of(message).splitlines()
    assert "Result     : DRY RUN - nothing sent" in lines
    assert (
        "DRY RUN: this is what would have been sent. Nothing was sent to WhatsApp, and the customer"
        " received nothing."
    ) in lines
    assert "----- What the customer would receive -----" in lines
    assert "Would be sent: a.pdf" in lines and "Would NOT be sent: b.heic - not supported" in lines
    assert "VShipCRM: no CRM customer has this number" in lines
    assert not any(line.startswith("Sent") for line in lines)


def test_uncertain_notice_tells_the_employee_to_check_the_chat(settings):
    message = uncertain(settings, files=[("invoice.pdf", False, None)])
    lines = text_of(message).splitlines()
    assert UNCERTAIN_GIVEN in lines and UNCERTAIN_DEFAULT not in lines
    assert "Open the chat with +1 650-555-1234 in the WhatsApp Business app." in lines
    assert "If it is not there, send it again as a new email to whatsapp@vship2000.com." in lines
    assert f"Tried at   : {NOW_NEW_YORK}" in lines
    assert "NOT sent: invoice.pdf - the update itself may not have been sent" in lines
    assert "----- What the customer may have received -----" in lines
    assert UNCERTAIN_DEFAULT in text_of(uncertain(settings, uncertainty=None)).splitlines()


def test_refused_notice_repeats_the_exact_command_format(settings):
    lines = text_of(refused(settings)).splitlines()
    assert "Your update was NOT sent. The customer received nothing." in lines
    assert (
        "Correct your email and send it again as a new email, not as a reply (the format is shown below)."
    ) in lines
    for expected in (
        "Write a new email to whatsapp@vship2000.com. A reply or a forwarded email (RE:, FW:) is ignored.",
        "    WA | phone number | booking number",
        "    Name:customer name",
        "    Message:",
        "    ##",
    ):
        assert expected in lines, expected
    assert any("at most 60 characters and the message at most 820" in line for line in lines)


def test_the_format_reminder_is_accepted_by_the_parser(settings):
    lines = text_of(refused(settings)).splitlines()
    example_subject = lines[lines.index("Subject:") + 1].strip()
    assert looks_like_command(example_subject)
    assert parse_subject(example_subject).booking == "booking number"
    body = "\n".join(lines[lines.index("Email text:") + 1 : lines.index("    ##") + 1])
    parts = parse_body(body)
    assert parts.name == "customer name"
    assert parts.message == "the update for the customer (one or more lines)"


def test_refused_notice_after_a_whatsapp_refusal(settings):
    explanation = explain_error(131026)
    message = refused(
        settings,
        refusal_code="whatsapp_refused",
        refusal_text=explanation,
        error_code=131026,
        error_explanation=explanation,
        phone="+16505551234",
        crm_match="Customer #42: Acme Imports / John Doe",
        template_name="vship_update",
        template_language="en_US",
        files=[("invoice.pdf", False, None)],
    )
    text = text_of(message)
    lines = text.splitlines()
    assert f"Reason: {explanation}" in lines
    assert "WhatsApp error code: 131026" in lines
    assert text.count(explanation) == 1  # the same explanation is not repeated
    assert "Phone number: +1 650-555-1234" in lines and "Name: vship_update" in lines
    assert "NOT sent: invoice.pdf - the update itself was not sent" in lines
    assert any(line.startswith("If the reason above can be fixed") for line in lines)


@pytest.mark.parametrize(
    ("code", "starts"),
    [
        ("too_old", "If the update is still needed, send it again as a new email."),
        ("test_mode", "While test mode is on, updates can only go to the test numbers."),
        ("template_not_ready", "Nothing can be sent until the WhatsApp template is ready again;"),
    ],
)
def test_refused_notice_next_step_fits_the_reason(settings, code, starts):
    lines = text_of(refused(settings, refusal_code=code, refusal_text="Why.", test_mode=True)).splitlines()
    todo = lines[lines.index("----- What to do next -----") + 1]
    assert todo.startswith(starts)
    test_note = "Test mode is on: updates can only be sent to the test numbers the administrator set up."
    assert (test_note in lines) == (code != "test_mode")  # the test-mode reason is not repeated as a note


def test_refused_notice_mentions_dry_run_and_a_given_next_step(settings):
    lines = text_of(refused(settings, dry_run=True, next_step="Ask Elizabeth.")).splitlines()
    assert lines[lines.index("----- What to do next -----") + 1] == "Ask Elizabeth."
    assert any(line.startswith("The WhatsApp update service is still being tested") for line in lines)


def test_delivery_failed_notice_for_the_update(settings):
    lines = text_of(delivery_failed(settings)).splitlines()
    assert "WhatsApp could not deliver your update to +1 650-555-1234." in lines
    assert f"Reason: {explain_error(131026)}" in lines  # explained by send.explain_error
    assert "WhatsApp error code: 131026" in lines
    assert f"Sent       : {NOW_NEW_YORK}" in lines
    assert "Your WhatsApp update (template vship_update)" in lines
    assert "If the phone number is wrong, send a new update email with the right number." in lines


def test_delivery_failed_notice_for_a_file_in_mail_timezone(tmp_path):
    settings = make_settings(tmp_path, mail_timezone="Europe/London")
    message = delivery_failed(
        settings, filename="invoice.pdf", error_code=None, error_explanation="The file was rejected."
    )
    lines = text_of(message).splitlines()
    assert message["Subject"] == "WhatsApp could not deliver your update to +1 650-555-1234"
    assert "WhatsApp could not deliver the file invoice.pdf to +1 650-555-1234." in lines
    assert "Reason: The file was rejected." in lines
    assert "WhatsApp gave no error code." in lines
    assert "Sent       : Tue 14 Nov 2023 22:13:20 GMT" in lines
    assert "Send the file to the customer another way, for example by email." in lines


def test_relay_alert_lists_the_lines(settings):
    lines = text_of(alert(settings)).splitlines()
    assert "Alert : template vship_update is not approved" in lines
    assert f"Time  : {NOW_NEW_YORK}" in lines
    assert "WhatsApp reports the template as PAUSED." in lines and "Check it in WhatsApp Manager." in lines


def test_relay_alert_without_now_uses_the_current_time(settings):
    message = build_relay_alert("mailbox readable again", ["All good."], settings)
    assert message["Date"] and any(line.startswith("Time  : ") for line in text_of(message).splitlines())


@pytest.mark.parametrize("kind", sorted(EVERY_EMAIL))
def test_no_secrets_or_internal_ids_in_any_email(settings, kind):
    leaky = f"token {TOKEN} and password {CRM_PASSWORD}"
    overrides = {KIND_ALERT: {"lines": [leaky]}, KIND_REFUSED: {"refusal_text": leaky}}.get(
        kind, {"notes": [leaky]} if kind != KIND_DELIVERY_FAILED else {"error_explanation": leaky}
    )
    raw = EVERY_EMAIL[kind](settings, **overrides).as_string()
    for secret in (TOKEN, CRM_PASSWORD, "106540352242922", "102290129340398", "app-secret-app-secret"):
        assert secret not in raw
    assert "[removed]" in raw


def test_phone_numbers_are_shown_in_full_to_the_employee(settings):
    text = text_of(sent(settings))
    assert "+1 650-555-1234" in text and "*" not in text


@pytest.mark.parametrize("bad", [("a.pdf", "yes", None), ("a.pdf", 1, None), "a.pdf", {"filename": "a.pdf"}])
def test_bad_file_results_are_refused(settings, bad):
    with pytest.raises(TypeError):
        sent(settings, files=[bad])


def test_two_item_file_results_are_accepted(settings):
    assert "NOT sent: a.pdf" in text_of(sent(settings, files=[("a.pdf", False)])).splitlines()


# ----------------------------------------------------------------------------- queue payload
def assert_identical(original: EmailMessage, rebuilt: EmailMessage) -> None:
    assert list(rebuilt.keys()) == list(original.keys())
    original.set_boundary("relay-test-boundary")
    rebuilt.set_boundary("relay-test-boundary")
    assert [(k, str(v)) for k, v in rebuilt.items()] == [(k, str(v)) for k, v in original.items()]
    assert rebuilt.as_bytes() == original.as_bytes()


@pytest.mark.parametrize("kind", sorted(EVERY_EMAIL))
def test_payload_round_trip_rebuilds_the_identical_email(settings, kind):
    original = EVERY_EMAIL[kind](settings)
    payload = to_payload(original)
    stored = json.loads(json.dumps(payload, separators=(",", ":")))  # as the store keeps it
    rebuilt = from_payload(stored)
    assert to_payload(rebuilt) == payload
    assert text_of(rebuilt) == text_of(original) and html_of(rebuilt) == html_of(original)
    assert_identical(original, rebuilt)


def test_payload_fields(settings):
    payload = to_payload(sent(settings, employee_name="Rivera, Jordan"))
    assert payload["to"] == ['"Rivera, Jordan" <chris@vship2000.com>']
    assert payload["subject"] == "WhatsApp update sent: +1 650-555-1234 (booking B12345)"
    names = [name for name, _ in payload["headers"]]
    assert names[:2] == ["From", "Date"]
    assert {"In-Reply-To", "References", "Auto-Submitted", "X-Auto-Response-Suppress", RELAY_HEADER} <= set(
        names
    )
    assert not {"To", "Subject", "MIME-Version", "Content-Type"} & set(names)
    assert payload["text"].startswith("WhatsApp update\n") and "\r" not in payload["text"]
    assert payload["html"].startswith("<!doctype html>")


def test_payload_round_trip_with_non_ascii_text_and_names(settings):
    text = (
        "Hello 花子, VShip has an update:\n"
        + "お荷物は明日ニューアークに到着します。" * 12
        + "\nPlease reply."
    )
    original = sent(settings, customer_text=text, employee_name="Jürgen Müller", booking="Ü-42")
    # mostly non-ASCII HTML is base64-encoded, which decodes with CRLF line ends; the payload stores LF
    html_part = original.get_body(("html",))
    assert html_part["Content-Transfer-Encoding"] == "base64" and "\r\n" in html_part.get_content()
    payload = to_payload(original)
    assert "\r" not in payload["html"] and "\r" not in payload["text"]
    rebuilt = from_payload(json.loads(json.dumps(payload)))
    assert rebuilt["To"] == "Jürgen Müller <chris@vship2000.com>"
    assert "  " + text.splitlines()[1] in text_of(rebuilt).splitlines()
    assert_identical(original, rebuilt)


def test_multi_recipient_alert_round_trip(settings):
    payload = to_payload(alert(settings))
    assert payload["to"] == ["ops@example.com", "second@example.com"]
    assert recipients_of(from_payload(payload)) == ["ops@example.com", "second@example.com"]


def _payload_with(settings, **changes) -> dict:
    payload = to_payload(sent(settings))
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "changes",
    [
        {"to": []},
        {"to": "chris@vship2000.com"},
        {"to": [""]},
        {"subject": None},
        {"text": None},
        {"html": 5},
        {"headers": "From: x"},
        {"headers": [["From"]]},
        {"headers": [["From", 1]]},
        {"headers": [["Bad Name", "x"]]},
        {"headers": [["X:Y", "x"]]},
        {"headers": [["Content-Type", "text/plain"]]},
        {"headers": [["MIME-Version", "1.0"]]},
        {"headers": [["To", "attacker@example.com"]]},
        {"headers": [["Subject", "second subject"]]},
        {"headers": [["X-Test", "a\r\nBcc: attacker@example.com"]]},
        {"headers": [["From", "a@example.com"], ["From", "b@example.com"]]},
    ],
)
def test_from_payload_refuses_a_payload_it_did_not_write(settings, changes):
    with pytest.raises(ValueError):
        from_payload(_payload_with(settings, **changes))


def test_from_payload_refuses_non_objects():
    for bad in (None, [], "text"):
        with pytest.raises(ValueError):
            from_payload(bad)


def test_from_payload_without_html_builds_a_text_email(settings):
    message = from_payload(_payload_with(settings, html=None))
    assert message.get_content_type() == "text/plain"
    assert to_payload(message)["html"] is None


def test_to_payload_refuses_emails_that_are_not_notices():
    no_recipient = EmailMessage(policy=SMTP)
    no_recipient["Subject"] = "x"
    no_recipient.set_content("x")
    with pytest.raises(ValueError, match="no recipients"):
        to_payload(no_recipient)
    with_file = EmailMessage(policy=SMTP)
    with_file["To"] = "a@example.com"
    with_file.set_content("x")
    with_file.add_attachment(b"data", maintype="application", subtype="octet-stream", filename="a.bin")
    with pytest.raises(ValueError, match="attachments"):
        to_payload(with_file)
    html_only = EmailMessage(policy=SMTP)
    html_only["To"] = "a@example.com"
    html_only.set_content("<p>x</p>", subtype="html")
    with pytest.raises(ValueError, match="no text part"):
        to_payload(html_only)
