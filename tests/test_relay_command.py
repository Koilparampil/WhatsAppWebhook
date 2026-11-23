from __future__ import annotations

import dataclasses
import time

import pytest
from app.relay.command import (
    BodyParts,
    CommandRefused,
    SubjectParts,
    flatten,
    looks_like_command,
    normalize_target_phone,
    parse_body,
    parse_subject,
)
from app.relay.html import html_to_text

US = "+16505551234"
UK = "+442079460958"
TRACKING = "https://vship2000.com/track?id=42"
# How Microsoft Defender rewrites that address in delivered mail
SAFELINK = (
    "https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Fvship2000.com%2Ftrack%3Fid%3D42"
    "&data=05%7C02%7Cwhatsapp%40vship2000.com%7C1b2c3d4e5f60%7C0%7C0%7C638672112%7CUnknown%7C"
    "&sdata=Zm9vYmFyYmF6%3D&reserved=0"
)
ALL_CODES = {
    "subject_fields",
    "phone_missing",
    "phone_invalid",
    "phone_prefix",
    "name_missing",
    "name_too_long",
    "message_missing",
    "end_missing",
    "message_empty",
    "message_too_long",
}


def refused(call, *args, **kwargs) -> CommandRefused:
    with pytest.raises(CommandRefused) as exc:
        call(*args, **kwargs)
    return exc.value


def body(*lines: str, **limits) -> BodyParts:
    return parse_body("\n".join(lines), **limits)


# ----------------------------------------------------------------------------- Outlook's HTML for a new email
WORD_HEAD = (
    '<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office"'
    ' xmlns:w="urn:schemas-microsoft-com:office:word"'
    ' xmlns:m="http://schemas.microsoft.com/office/2004/12/omml" xmlns="http://www.w3.org/TR/REC-html40">'
    '<head><meta http-equiv="Content-Type" content="text/html; charset=us-ascii">'
    '<meta name="Generator" content="Microsoft Word 15 (filtered medium)">'
    '<style><!--\n/* Font Definitions */\n@font-face\n\t{font-family:"Cambria Math";}\n'
    "/* Style Definitions */\np.MsoNormal, li.MsoNormal, div.MsoNormal\n\t{margin:0in;\n\tfont-size:12.0pt;"
    '\n\tfont-family:"Aptos",sans-serif;}\nspan.EmailStyle17\n\t{mso-style-type:personal-compose;}\n'
    "@page WordSection1\n\t{size:8.5in 11.0in;}\ndiv.WordSection1\n\t{page:WordSection1;}\n--></style>"
    '<!--[if gte mso 9]><xml>\n<o:shapedefaults v:ext="edit" spidmax="1026" />\n</xml><![endif]-->'
    '<!--[if gte mso 9]><xml>\n<o:shapelayout v:ext="edit">\n<o:idmap v:ext="edit" data="1" />\n'
    "</o:shapelayout></xml><![endif]--></head>"
    '<body lang="EN-US" link="#467886" vlink="#96607D" style="word-wrap:break-word">'
    '<div class="WordSection1">'
)
WORD_TAIL = "</div></body></html>"
# classic Outlook's automatic signature, below the employee's text
SIGNATURE = (
    '<p class="MsoNormal"><a name="_MailAutoSig"><b><span style="font-size:10.0pt">Jordan Rivera'
    "</span></b></a><o:p></o:p></p>"
    '<p class="MsoNormal"><span style="mso-bookmark:_MailAutoSig"><span style="font-size:10.0pt">'
    "Operations | VShip | Mobile: 650-555-0100</span></span><o:p></o:p></p>"
    '<p class="MsoNormal"><span style="mso-bookmark:_MailAutoSig"><img width="120" height="40"'
    ' style="width:1.25in;height:.4166in" id="Picture_x0020_1" src="cid:image001.png@01DC5A11.1B2C3D40"'
    ' alt="VShip"></span><o:p></o:p></p>'
)
# a warning that a mail flow rule puts on top, and a sensitivity label's header marking
BANNER = (
    '<table class="MsoNormalTable" border="0" cellspacing="0" cellpadding="0" width="100%"'
    ' style="width:100.0%;background:#FFEB9C"><tr><td style="padding:3.75pt">'
    '<p class="MsoNormal"><b><span style="color:#9C6500">CAUTION:</span></b>'
    '<span style="color:black"> Do not click links or open attachments unless you recognize the sender.'
    "</span><o:p></o:p></p></td></tr></table>"
    '<p class="MsoNormal" align="center" style="text-align:center"><span style="font-size:8.0pt;'
    'color:#737373">Sensitivity: Internal</span><o:p></o:p></p>'
)
OWA_STYLE = (
    "font-family: Aptos, Aptos_EmbeddedFont, Aptos_MSFontService, Calibri, Helvetica, sans-serif;"
    " font-size: 12pt; color: rgb(0, 0, 0);"
)


def word_paragraph(line: str) -> str:
    if not line:
        return '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'  # an empty line
    return f'<p class="MsoNormal">{line}<o:p></o:p></p>'


def outlook_desktop(*lines: str, top: str = "") -> str:
    """A new email as classic Outlook for Windows writes it: Word HTML, one paragraph per typed line."""
    paragraphs = "".join(word_paragraph(line) for line in lines)
    return WORD_HEAD + top + paragraphs + word_paragraph("") + SIGNATURE + WORD_TAIL


def outlook_web(*lines: str) -> str:
    """A new email as Outlook on the web and the new Outlook write it: one div per typed line."""
    divs = "".join(
        f'<div class="elementToProof" style="{OWA_STYLE}">{line or "<br>"}</div>' for line in lines
    )
    return (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">'
        '<style type="text/css" style="display:none;"> P {margin-top:0;margin-bottom:0;} </style></head>'
        f'<body dir="ltr">{divs}'
        '<div id="Signature"><p>Jordan Rivera<br>Operations | VShip</p>'
        "<p>Name: Jordan Rivera, Message: call me any time</p><p>##</p></div>"
        "</body></html>"
    )


# ----------------------------------------------------------------------------- subject: is it a command?
@pytest.mark.parametrize(
    "subject",
    [
        "WA | 650-555-1234 | B-1001",
        "WA|6505551234",
        "wa | 6505551234",
        "Wa   |   6505551234",
        "wA|6505551234|",
        "   WA | 6505551234   ",  # trimmed first
        "WA\t| 6505551234",
        "WA\u00a0| 6505551234",  # a no-break space, as pasting can leave
        "WA |",  # a command, if a broken one: it is refused, not ignored
    ],
)
def test_a_subject_that_starts_with_wa_and_a_bar_is_a_command(subject):
    assert looks_like_command(subject) is True


@pytest.mark.parametrize(
    "subject",
    [
        # replying to or forwarding a command (or its confirmation) must never send anything again
        "RE: WA | 650-555-1234 | B-1001",
        "Re:WA|6505551234",
        "FW: WA | 650-555-1234",
        "Fwd: WA | 650-555-1234",
        "AW: WA | 650-555-1234",
        "WG: WA | 650-555-1234",
        "SV: WA | 650-555-1234",
        "Automatic reply: WA | 650-555-1234",
        "[EXTERNAL] WA | 650-555-1234",
        "Update WA | 650-555-1234",
        "- WA | 650-555-1234",
        "|WA| 650-555-1234",
        # no bar right after WA
        "WA 650-555-1234",
        "WA: 650-555-1234",
        "WA - 650-555-1234",
        "WAX | 650-555-1234",
        "W A | 650-555-1234",
        "WhatsApp | 650-555-1234",
        # the subjects of the feature's own emails, should one come back to the mailbox
        "WhatsApp update sent: +1 650-555-1234 (booking B-1001)",
        "WhatsApp update NOT sent: the phone number is not valid",
        "WhatsApp update MAY NOT have been sent: +1 650-555-1234",
        "DRY RUN - WhatsApp update not sent: +1 650-555-1234",
        "WhatsApp could not deliver your update to +1 650-555-1234",
        "",
        None,
    ],
)
def test_anything_else_is_not_a_command(subject):
    assert looks_like_command(subject) is False


# ----------------------------------------------------------------------------- subject: the fields
@pytest.mark.parametrize(
    ("subject", "phone_raw", "booking"),
    [
        ("WA | 650-555-1234 | B-1001", "650-555-1234", "B-1001"),
        ("WA | 650-555-1234", "650-555-1234", None),
        ("WA | 650-555-1234 |", "650-555-1234", None),  # an empty booking field: no booking
        ("WA | 650-555-1234 |    ", "650-555-1234", None),
        ("WA|+44 20 7946 0958|  VS 2026/77  ", "+44 20 7946 0958", "VS 2026/77"),
        ("  wa |6505551234|B1  ", "6505551234", "B1"),
    ],
)
def test_the_subject_names_the_phone_number_and_maybe_a_booking_number(subject, phone_raw, booking):
    assert parse_subject(subject) == SubjectParts(phone_raw=phone_raw, booking=booking)


@pytest.mark.parametrize(
    "subject",
    [
        "WA | 650-555-1234 | B-1001 | urgent",
        "WA | 650-555-1234 | B-1001 |",  # a bar after the booking number makes a fourth field
        "WA | 650-555-1234 | | ",
        "WA ||||",
    ],
)
def test_a_fourth_field_is_refused(subject):
    exc = refused(parse_subject, subject)
    assert exc.code == "subject_fields" and "too many |" in exc.text


@pytest.mark.parametrize("subject", ["WA |", "WA | | B-1001", "WA |    | B-1001", "WA"])
def test_a_subject_without_a_phone_number_is_refused(subject):
    assert refused(parse_subject, subject).code == "phone_missing"


# ----------------------------------------------------------------------------- the phone number
@pytest.mark.parametrize(
    "raw",
    [
        "6505551234",
        "650-555-1234",
        "650.555.1234",
        "(650) 555-1234",
        "(650)555-1234",
        "650 555 1234",
        "  650 - 555 - 1234  ",
        "16505551234",
        "1-650-555-1234",
        "1 (650) 555-1234",
        "1.650.555.1234",
        "+1 650 555 1234",
        "+16505551234",
        "+1 (650) 555-1234",
        "+1-650-555-1234",
        "+ 1 650 555 1234",
        "650\u00a0555\u00a01234",  # no-break spaces, as copying from a web page can leave
        "650\u2009555\u20091234",  # thin spaces
    ],
)
def test_us_numbers_in_the_usual_writings(raw):
    assert normalize_target_phone(raw, "US") == US


def test_the_default_region_decides_how_ten_digits_are_read():
    assert normalize_target_phone("650-555-1234") == US  # RELAY_DEFAULT_REGION is US unless set otherwise
    assert normalize_target_phone("20 7946 0958", "GB") == UK


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+44 20 7946 0958", UK),
        ("+44 (0)20 7946 0958", UK),
        ("+49 151 12345678", "+4915112345678"),
        ("+52 55 1234 5678", "+525512345678"),
        ("+91 98765 43210", "+919876543210"),
        ("+33 6 12 34 56 78", "+33612345678"),
        ("+1 416 555 1234", "+14165551234"),  # Canada shares +1
    ],
)
def test_other_countries_are_written_with_plus_and_the_country_code(raw, expected):
    assert normalize_target_phone(raw, "US") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "650-555-123",  # 9 digits
        "555-1234",
        "650-555-12345",  # 11 digits that do not start with 1
        "26505551234",
        "165055512345",  # 12 digits
        "442079460958",  # another country's number without the +
        "020 7946 0958",
        "555-555-5555",  # 10 digits, but no such US number
        "123-456-7890",
        "+1 123 456 7890",
        "+1 650 555 12345",
        "+44 20 7946",
        "+999 1234 5678",  # no such country code
        "+6505551234",  # a + makes 65 the country code (Singapore), where it is no number
    ],
)
def test_a_number_that_is_not_valid_is_refused(raw):
    exc = refused(normalize_target_phone, raw, "US")
    assert exc.code == "phone_invalid" and raw.strip() in exc.text


@pytest.mark.parametrize(
    "raw",
    [
        "0044 20 7946 0958",
        "00 44 20 7946 0958",
        "0044-20-7946-0958",
        "011 44 20 7946 0958",
        "011-44-20-7946-0958",
        "(011) 44 20 7946 0958",
        "001 650 555 1234",
    ],
)
def test_an_international_dialling_prefix_is_refused_not_guessed(raw):
    exc = refused(normalize_target_phone, raw, "US")
    assert exc.code == "phone_prefix" and "with + and the country code" in exc.text


@pytest.mark.parametrize(
    "raw",
    [
        "650-555-1234 x12",
        "650-555-1234 ext. 12",
        "650-555-CALL",
        "tel:6505551234",
        "650-555-1234, 650-555-9876",  # two numbers
        "650+555+1234",
        "++44 20 7946 0958",
        "#6505551234",
        "+",
    ],
)
def test_extensions_letters_and_stray_signs_are_refused(raw):
    assert refused(normalize_target_phone, raw, "US").code == "phone_invalid"


@pytest.mark.parametrize("raw", ["", "   ", "()", " - ", "...", "\u00a0"])
def test_no_digits_at_all_is_a_missing_number(raw):
    assert refused(normalize_target_phone, raw, "US").code == "phone_missing"


@pytest.mark.parametrize("dash", ["\u2013", "\u2011", "\u2212"])
def test_other_dashes_are_removed_like_hyphens(dash):
    assert normalize_target_phone(f"650{dash}555{dash}1234", "US") == US


# ----------------------------------------------------------------------------- the body: Name, Message, ##
def test_the_agreed_format():
    parts = body("Name:Sheena Nelson", "Message:", "Your container cleared customs.", "##", "Chris")
    assert parts == BodyParts(
        name="Sheena Nelson",
        message="Your container cleared customs.",
        name_was_empty=False,
        lines_joined=False,
    )


@pytest.mark.parametrize(
    ("name_label", "message_label"),
    [("Name:", "Message:"), ("NAME:", "MESSAGE:"), ("name:", "message:"), ("nAmE:", "mEsSaGe:")],
)
def test_the_labels_are_found_in_any_letter_case(name_label, message_label):
    parts = body(f"{name_label} Sheena", message_label, "Hello", "##")
    assert (parts.name, parts.message) == ("Sheena", "Hello")


def test_anything_before_the_name_line_is_ignored_even_if_it_looks_like_the_format():
    parts = body(
        "Hi team, please send this one:",
        "Message: this line is above the Name line",
        "##",
        "   Name: Sheena",
        "Message:",
        "Hello",
        "##",
    )
    assert (parts.name, parts.message) == ("Sheena", "Hello")


def test_only_the_first_name_line_counts():
    parts = body("Name: Sheena", "Name: John", "Message:", "Name: as on the bill of lading", "##")
    assert (parts.name, parts.message) == ("Sheena", "Name: as on the bill of lading")


@pytest.mark.parametrize(
    "line", ["Customer Name: Sheena", "Names: Sheena", "Nam: Sheena", "Name Sheena", "> Name: Sheena"]
)
def test_a_line_that_does_not_start_with_name_and_a_colon_is_no_name_line(line):
    assert refused(parse_body, "\n".join([line, "Message:", "Hello", "##"])).code == "name_missing"


def test_the_name_may_stand_on_the_next_line():
    parts = body("Name:", "", "   Sheena Nelson   ", "Message:", "Hello", "##")
    assert (parts.name, parts.name_was_empty, parts.message) == ("Sheena Nelson", False, "Hello")


def test_an_empty_name_is_the_fallback_word():
    parts = body("Name:", "Message:", "Hello", "##")
    assert (parts.name, parts.name_was_empty, parts.message) == ("there", True, "Hello")
    # the next line that is not empty is the Message line, so it is not the name
    parts = body("Name:   ", "", "MESSAGE: Hello", "##")
    assert (parts.name, parts.name_was_empty, parts.message) == ("there", True, "Hello")
    # nothing visible is nothing; the word comes from the settings
    parts = body("Name:\u00a0\u200b", "Message:", "Hello", "##", fallback_name="Customer")
    assert (parts.name, parts.name_was_empty) == ("Customer", True)


def test_text_on_the_message_line_counts():
    assert body("Name: Sheena", "Message: Your container cleared.", "##").message == "Your container cleared."
    parts = body("Name: Sheena", "Message:   Your container cleared.", "The truck comes Friday.", "##")
    assert parts.message == "Your container cleared. The truck comes Friday." and parts.lines_joined


def test_lines_between_the_name_and_the_message_line_are_ignored():
    assert body("Name: Sheena", "Booking B-1001", "", "Message:", "Hello", "##").message == "Hello"


@pytest.mark.parametrize("end", ["##", "  ##  ", "##\t", "\u00a0##\u00a0"])
def test_the_message_ends_at_the_first_line_that_is_only_two_hashes(end):
    parts = body("Name: Sheena", "Message:", "Hello", end, "Jordan Rivera", "##", "more")
    assert parts.message == "Hello"


def test_two_hashes_inside_a_line_do_not_end_the_message():
    parts = body(
        "Name: Sheena", "Message:", "Use dock ## 4 at the warehouse.", "Call us ##", "## not yet", "###", "##"
    )
    assert parts.message == "Use dock ## 4 at the warehouse. Call us ## ## not yet ###"


def test_everything_after_the_end_is_ignored():
    parts = body(
        "Name: Sheena",
        "Message:",
        "Hello",
        "##",
        "Jordan Rivera",
        "Name: Chris",
        "Message: not this",
        "##",
    )
    assert (parts.name, parts.message) == ("Sheena", "Hello")


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("Message:\nHello\n##", "name_missing"),
        ("Hello Sheena, your container cleared.\n##", "name_missing"),
        ("", "name_missing"),
        ("Name: Sheena\nHello\n##", "message_missing"),
        ("Name: Sheena\nMessage\nHello\n##", "message_missing"),  # no colon
        ("Name: Sheena Message: Hello\n##", "message_missing"),  # both on one line
        ("Message:\nHello\nName: Sheena\n##", "message_missing"),  # Message: has to come after Name:
        ("Name: Sheena", "message_missing"),
        ("Name: Sheena\nMessage:\nHello", "end_missing"),
        ("Name: Sheena\n##\nMessage:\nHello", "end_missing"),  # an end above the message does not count
        ("Name: Sheena\nMessage: Hello ##\nChris", "end_missing"),  # ## has to stand on its own line
        ("Name: Sheena\nMessage:\nHello\n# #\nChris", "end_missing"),
        ("Name: Sheena\nMessage:\n##", "message_empty"),
        ("Name: Sheena\nMessage:   \n\n \u00a0 \n\t\n##", "message_empty"),
        ("Name: Sheena\nMessage:\u200b\n\ufeff\n##", "message_empty"),  # nothing visible
    ],
)
def test_a_body_that_breaks_the_format_is_refused(text, code):
    assert refused(parse_body, text).code == code


# ----------------------------------------------------------------------------- joining lines and spaces
def test_line_breaks_tabs_and_runs_of_spaces_become_single_spaces():
    parts = body(
        "Name: Sheena",
        "Message:",
        "  Your container   cleared  ",
        "",
        "",
        "\tThe truck\t\tcomes Friday.   ",
        "Call  us.",
        "##",
    )
    assert parts.message == "Your container cleared The truck comes Friday. Call us."
    assert parts.lines_joined is True


def test_a_message_on_one_line_has_no_joined_lines():
    parts = body("Name: Sheena", "Message:", "", "Hello   there", "", "##")
    assert parts.message == "Hello there" and parts.lines_joined is False


@pytest.mark.parametrize("newline", ["\r\n", "\r", "\n"])
def test_any_kind_of_line_ending(newline):
    text = newline.join(["Name: Sheena", "Message:", "Hello", "there", "##", "Chris"])
    assert parse_body(text) == BodyParts(
        name="Sheena", message="Hello there", name_was_empty=False, lines_joined=True
    )


def test_unicode_spaces_are_spaces():
    spaces = "\u00a0\u1680\u2002\u2003\u2007\u2009\u202f\u205f\u3000"
    parts = body("Name:\u00a0Sheena\u2007\u2007Nelson", f"Message:\u00a0Hello{spaces}there", "##\u00a0")
    assert (parts.name, parts.message) == ("Sheena Nelson", "Hello there")


@pytest.mark.parametrize(
    ("text", "flat"),
    [
        ("one\ntwo", "one two"),
        ("one\r\n\r\ntwo", "one two"),
        ("one\ttwo", "one two"),
        ("one     two", "one two"),
        ("  one two  ", "one two"),
        ("one\u00a0\u00a0two", "one two"),
        ("one\u3000two", "one two"),
        ("one\u2028two", "one two"),  # a line separator
        ("", ""),
    ],
)
def test_flatten(text, flat):
    assert flatten(text) == flat


# ----------------------------------------------------------------------------- limits
def test_a_message_of_the_maximum_length_after_joining_is_accepted():
    lines = ["    " + "x" * 99 + "   "] * 9 + ["", "x" * 100]
    raw = "\n".join(lines)
    assert len(raw) > 1000  # longer as typed, but 1,000 characters once the lines are joined
    assert len(body("Name: Sheena", "Message:", raw, "##", max_message=1000).message) == 1000


def test_one_character_more_than_the_limit_is_refused():
    lines = ["x" * 99] * 9 + ["x" * 101]
    exc = refused(parse_body, "\n".join(["Name: Sheena", "Message:", *lines, "##"]), max_message=1000)
    assert exc.code == "message_too_long" and "1,001" in exc.text and "1,000" in exc.text


def test_a_name_of_up_to_60_characters_is_accepted():
    name = "A" * 30 + " " + "B" * 29
    assert len(name) == 60 and body(f"Name: {name}", "Message: hi", "##").name == name
    assert body("Name: " + "A" * 30 + "     " + "B" * 29, "Message: hi", "##").name == name  # spaces collapse
    exc = refused(parse_body, "\n".join([f"Name: {name}C", "Message: hi", "##"]))
    assert exc.code == "name_too_long" and "61" in exc.text and "60" in exc.text


def test_the_limits_come_from_the_settings():
    assert body("Name: Sheena", "Message: 12345", "##", max_message=5).message == "12345"
    assert refused(parse_body, "Name: Sheena\nMessage: 123456\n##", max_message=5).code == "message_too_long"
    assert body("Name: Shee", "Message: hi", "##", max_name=4).name == "Shee"
    assert refused(parse_body, "Name: Sheena\nMessage: hi\n##", max_name=4).code == "name_too_long"


def test_every_refusal_has_a_stable_code_and_a_plain_english_text():
    cases = [
        (parse_subject, ("WA | 650-555-1234 | B-1001 | x",)),
        (parse_subject, ("WA | | B-1001",)),
        (normalize_target_phone, ("650-555-123", "US")),
        (normalize_target_phone, ("0044 20 7946 0958", "US")),
        (parse_body, ("Message: hi\n##",)),
        (parse_body, ("Name: " + "x" * 61 + "\nMessage: hi\n##",)),
        (parse_body, ("Name: Sheena\nhi\n##",)),
        (parse_body, ("Name: Sheena\nMessage: hi",)),
        (parse_body, ("Name: Sheena\nMessage:\n##",)),
        (parse_body, ("Name: Sheena\nMessage: " + "x" * 1001 + "\n##",)),
    ]
    seen: dict[str, str] = {}
    for call, args in cases:
        exc = refused(call, *args)
        assert exc.text.strip() and str(exc) == exc.text
        seen[exc.code] = exc.text
    assert set(seen) == ALL_CODES


def test_the_results_cannot_be_changed_by_accident():
    with pytest.raises(dataclasses.FrozenInstanceError):
        body("Name: Sheena", "Message: hi", "##").name = "John"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        parse_subject("WA | 650-555-1234").booking = "B-1"  # type: ignore[misc]


# ----------------------------------------------------------------------------- Safe Links
@pytest.mark.parametrize("after", ["", " please", "\nThanks"])
def test_a_bare_safe_links_address_is_unwrapped(after):
    parts = body("Name: Sheena", "Message:", f"Track it at {SAFELINK}{after}", "##")
    assert parts.message == flatten(f"Track it at {TRACKING}{after}")
    loud = SAFELINK.replace("https://nam12.safelinks", "HTTPS://NAM12.SAFELINKS")
    assert body("Name: Sheena", f"Message: Track it at {loud}", "##").message == f"Track it at {TRACKING}"


@pytest.mark.parametrize(("before", "after"), [("(", ")."), ("", "."), ("", ", thanks")])
def test_punctuation_right_after_a_bare_safe_links_address_is_kept(before, after):
    parts = body("Name: Sheena", "Message:", f"Track it here {before}{SAFELINK}{after}", "##")
    assert parts.message == f"Track it here {before}{TRACKING}{after}"


@pytest.mark.parametrize("mark", [".", ")", "!", "'", '"', ";"])
def test_a_long_run_of_punctuation_after_a_safe_links_address_is_read_at_once(mark):
    """The email is read on the event loop: a run of 30,000 dots used to take seconds (minutes for more)."""
    run = mark * 30_000
    started = time.perf_counter()
    # the run and the letter after it belong to the address (no space between), so they go with its wrapper
    assert body("Name: Sheena", f"Message: Track it at {SAFELINK}{run}x", "##").message == (
        f"Track it at {TRACKING}"
    )
    assert body(f"Name: {SAFELINK}{run}x", "Message: hi", "##").name == TRACKING
    # without the letter the run is punctuation after the address, and stays: far too long a message
    assert refused(body, "Name: Sheena", f"Message: {SAFELINK}{run}", "##").code == "message_too_long"
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("after", "kept"),
    [(")", ")"), ("...", "..."), ("?!", "?!"), ("'\"", "'\""), (").x", ""), ("x).", ").")],
)
def test_where_a_bare_safe_links_address_ends(after, kept):
    # punctuation at the end of the address is not part of it; anything else up to the next space is
    assert body("Name: A", f"Message: see {SAFELINK}{after} now", "##").message == f"see {TRACKING}{kept} now"


def test_a_safe_links_address_of_punctuation_only_stays_as_it_is():
    bare = "https://nam12.safelinks.protection.outlook.com/"
    for after in (")", ").", "?."):
        assert body("Name: A", f"Message: see {bare}{after} now", "##").message == f"see {bare}{after} now"


def test_an_emoji_built_with_a_zero_width_joiner_reaches_the_customer_whole():
    technologist = "\U0001f469\u200d\U0001f4bb"
    parts = body("Name: Sheena", f"Message: Our team {technologist} is on it.", "##")
    assert parts.message == f"Our team {technologist} is on it."


# ----------------------------------------------------------------------------- real Outlook emails
def test_a_classic_outlook_email_with_its_signature_below_the_end():
    html = outlook_desktop(
        "Name:&nbsp;Sheena Nelson",
        "Message:",
        "Your container MSCU1234567 cleared customs this morning.",
        "",
        "The truck is booked for Friday,&nbsp;&nbsp;8&nbsp;AM.",
        "##",
    )
    assert parse_body(html_to_text(html)) == BodyParts(
        name="Sheena Nelson",
        message=(
            "Your container MSCU1234567 cleared customs this morning. The truck is booked for Friday, 8 AM."
        ),
        name_was_empty=False,
        lines_joined=True,
    )


def test_a_banner_above_the_name_line_is_ignored():
    link = SAFELINK.replace("&", "&amp;")
    html = outlook_desktop(
        "Name:",
        "Sheena Nelson",
        f'Message: Your container cleared customs.<br>Pick-up details: <a href="{link}">tracking page</a>',
        "##",
        top=BANNER,
    )
    parts = parse_body(html_to_text(html))
    assert parts.name == "Sheena Nelson"  # on the line below Name:
    assert parts.message == f"Your container cleared customs. Pick-up details: tracking page ({TRACKING})"


def test_an_email_from_outlook_on_the_web():
    html = outlook_web(
        "Name: Sheena Nelson",
        "Message:",
        "Your container cleared customs.",
        "",
        "Use dock ## 4 at the warehouse. Questions? Call 650-555-1234.",
        "##",
    )
    assert parse_body(html_to_text(html)) == BodyParts(
        name="Sheena Nelson",
        message=(
            "Your container cleared customs. Use dock ## 4 at the warehouse. Questions? Call 650-555-1234."
        ),
        name_was_empty=False,
        lines_joined=True,
    )


def test_line_breaks_inside_one_outlook_paragraph():
    html = outlook_desktop("Name: Sheena<br>Message:<br>Line one<br>Line two<br>##<br>Chris")
    parts = parse_body(html_to_text(html))
    assert (parts.name, parts.message, parts.lines_joined) == ("Sheena", "Line one Line two", True)


def test_labels_that_word_split_into_pieces_or_made_bold_are_still_found():
    html = outlook_desktop(
        '<span style="font-size:11.0pt">Na</span><span lang="EN-GB" style="font-size:11.0pt">me:</span>'
        '<span style="font-size:11.0pt">&nbsp;Sheena</span>',
        "<b>Message:</b>",
        "Hello",
        "<b>##</b>",
    )
    parts = parse_body(html_to_text(html))
    assert (parts.name, parts.message) == ("Sheena", "Hello")


def test_the_message_limit_in_outlook_html_counts_the_joined_text():
    lines = ["x" * 99] * 9 + ["x" * 100]
    paragraphs = [part for line in lines for part in (line, "")]  # an empty paragraph after each line
    parts = parse_body(
        html_to_text(outlook_desktop("Name: Sheena", "Message:", *paragraphs, "##")), max_message=1000
    )
    assert len(parts.message) == 1000
    lines[-1] += "x"
    paragraphs = [part for line in lines for part in (line, "")]
    text = html_to_text(outlook_desktop("Name: Sheena", "Message:", *paragraphs, "##"))
    assert refused(parse_body, text, max_message=1000).code == "message_too_long"


def test_an_outlook_email_without_the_end_mark_is_refused_so_the_signature_never_goes_out():
    html = outlook_desktop("Name: Sheena", "Message:", "Your container cleared customs.")
    assert refused(parse_body, html_to_text(html)).code == "end_missing"


# ------------------------------------------------- the accepted rules, to the letter (review fixes)
@pytest.mark.parametrize("raw", ["650/555/1234", "650_555_1234", "650,555,1234"])
def test_only_spaces_dashes_dots_and_brackets_may_separate_the_digits(raw):
    with pytest.raises(CommandRefused) as exc:
        normalize_target_phone(raw, "US")
    assert exc.value.code == "phone_invalid"


@pytest.mark.parametrize("raw", ["[650] 555-1234", "(650) 555.1234", "650 555 1234"])
def test_round_and_square_brackets_are_both_brackets(raw):
    assert normalize_target_phone(raw, "US") == "+16505551234"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("Name : Maria\nMessage:\nHi\n##", "name_missing"),
        ("Name: Maria\nMessage :\nHi\n##", "message_missing"),
    ],
)
def test_a_space_before_the_colon_does_not_make_a_label(body, code):
    with pytest.raises(CommandRefused) as exc:
        parse_body(body)
    assert exc.value.code == code


def test_the_default_limit_keeps_the_whole_template_within_metas_1024_characters():
    """Owner decision (19 Nov 2026): the Message limit is 820, so the update template's fixed text (138
    characters) plus a 60-character name plus the message never exceed Meta's 1,024-character body limit."""
    template = (
        "Hello {{1}}, VShip has an update:\n{{2}}\nPlease either reply to this message or send us an email at"
        " Elizabeth@vship2000.com if you have any Questions"
    )
    longest = template.replace("{{1}}", "n" * 60).replace("{{2}}", "m" * 820)
    assert len(longest) <= 1024
    assert len(body("Name: " + "n" * 60, "Message:", "m" * 820, "##").message) == 820
    text = "Name: A\nMessage:\n" + "m" * 821 + "\n##"
    assert refused(parse_body, text).code == "message_too_long"
