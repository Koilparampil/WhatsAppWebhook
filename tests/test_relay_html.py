from __future__ import annotations

import time

import pytest
from app.relay.html import (
    MAX_OPEN_LINKS,
    cut_quoted_text,
    extract_reply,
    extract_reply_plain,
    html_to_text,
    link_target,
    text_to_html,
    unwrap_safelink,
)

QUOTED_NOTIFICATION = (
    '<h2 style="margin:0 0 10px;font-size:16px">WhatsApp message</h2>'
    "<table><tr><th>Sender</th><td>Sheena Nelson</td></tr></table>"
    "<h3>VShipCRM</h3><div>Match: Customer #42: Acme Imports / John Doe</div>"
    "<p>This email was generated automatically by the WhatsApp email forwarder. Ref: WA-R7K2M5X4Q6WTZ5AB</p>"
)

OUTLOOK_DESKTOP = (
    '<html xmlns:o="urn:schemas-microsoft-com:office:office"><head>'
    '<meta http-equiv="Content-Type" content="text/html; charset=us-ascii">'
    "<style><!-- p.MsoNormal {margin:0in;} --></style></head>"
    '<body lang="EN-US"><div class="WordSection1">'
    '<p class="MsoNormal">Yes, your container cleared customs this morning.<o:p></o:p></p>'
    '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'
    '<p class="MsoNormal">The <b>release form</b> is attached.<o:p></o:p></p>'
    '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'
    '<p class="MsoNormal"><a name="_MailAutoSig"><span style="font-size:10.0pt">Chris Koilparampil</span>'
    "</a><o:p></o:p></p>"
    '<p class="MsoNormal"><span style="mso-bookmark:_MailAutoSig">'
    '<img width="120" height="40" src="cid:image001.png@01DC5A11.1B2C3D40"></span><o:p></o:p></p>'
    '<p class="MsoNormal"><o:p>&nbsp;</o:p></p>'
    '<div><div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">'
    '<p class="MsoNormal"><b>From:</b> Sheena Nelson via WhatsApp &lt;whatsapp@example.com&gt; <br>'
    "<b>Sent:</b> Wednesday, November 18, 2026 9:41 AM<br><b>To:</b> ops@example.com<br>"
    "<b>Subject:</b> [WhatsApp] Sheena Nelson (+1 650-555-1234): Has my container cleared yet?<o:p></o:p></p>"
    "</div></div>" + QUOTED_NOTIFICATION + "</div></body></html>"
)

OUTLOOK_WEB = (
    '<html><head><style type="text/css">p {margin:0}</style></head><body dir="ltr">'
    '<div class="x_elementToProof" style="font-family: Aptos; font-size: 12pt; position: fixed">'
    "Hi Ana,</div>"
    '<div class="x_elementToProof"><br></div>'
    '<div class="x_elementToProof">It cleared. See our <a href="https://vship.example/track?id=1">'
    "tracking page</a> and the screenshot:</div>"
    '<div class="x_elementToProof"><img src="cid:f3a1-pasted" width="400" onerror="alert(1)"></div>'
    '<div id="x_Signature"><div>Chris</div><div><a href="https://vship.example">'
    '<img src="cid:logo@vship"></a></div></div>'
    '<div id="x_appendonsend"></div>'
    '<hr style="display:inline-block;width:98%" tabindex="-1">'
    '<div id="x_divRplyFwdMsg" dir="ltr"><font face="Calibri, sans-serif" style="font-size:11pt">'
    "<b>From:</b> Sheena Nelson via WhatsApp &lt;whatsapp@example.com&gt;</font><div>&nbsp;</div></div>"
    + QUOTED_NOTIFICATION
    + "</body></html>"
)


def test_classic_outlook_reply_is_cut_above_the_quoted_header():
    reply = extract_reply(OUTLOOK_DESKTOP)
    assert reply.text == (
        "Yes, your container cleared customs this morning.\n\n"
        "The release form is attached.\n\nChris Koilparampil"
    )
    assert reply.cut is True
    assert [(i.cid, i.in_signature) for i in reply.images] == [("image001.png@01DC5A11.1B2C3D40", True)]
    assert "From:" not in reply.html and "WhatsApp message" not in reply.html and "Acme" not in reply.html
    assert "border-top" not in reply.html  # the box that starts the quote is gone too
    assert '<p style="margin:0">Yes, your container' in reply.html
    assert "<b>release form</b>" in reply.html
    assert reply.html.count("<div") == reply.html.count("</div>")  # still well formed after the cut


def test_outlook_on_the_web_reply_with_exchange_prefixes():
    reply = extract_reply(OUTLOOK_WEB)
    assert reply.text == (
        "Hi Ana,\n\nIt cleared. See our tracking page (https://vship.example/track?id=1) and the screenshot:"
        "\n\nChris"
    )
    assert reply.cut is True
    assert [(i.cid, i.in_signature) for i in reply.images] == [("f3a1-pasted", False), ("logo@vship", True)]
    assert "<hr" not in reply.html and "divRplyFwdMsg" not in reply.html and "Sheena" not in reply.html
    # unsafe or pointless attributes are dropped, the rest is kept
    assert "onerror" not in reply.html and "position" not in reply.html and "class=" not in reply.html
    assert 'style="font-family: Aptos; font-size: 12pt"' in reply.html
    assert '<a href="https://vship.example/track?id=1">tracking page</a>' in reply.html
    assert '<img src="cid:f3a1-pasted" width="400">' in reply.html


def test_outlook_mobile_and_gmail_and_apple_markers():
    mobile = (
        '<div dir="ltr">Sure, will do.</div>'
        '<div id="ms-outlook-mobile-signature"><div><br></div>Get <a href="https://aka.ms/o0ukef">'
        "Outlook for iOS</a></div>"
        '<div id="mail-editor-reference-message-container"><hr><div id="divRplyFwdMsg"><b>From:</b> x</div>'
        "secret</div>"
    )
    reply = extract_reply(mobile)
    assert reply.text == "Sure, will do.\n\nGet Outlook for iOS (https://aka.ms/o0ukef)" and reply.cut
    gmail = (
        '<div dir="ltr">Thanks, noted.</div><br><div class="gmail_quote">'
        '<div class="gmail_attr">On Wed, Nov 18, 2026 at 9:41 AM Sheena wrote:<br></div>'
        "<blockquote>secret</blockquote></div>"
    )
    assert extract_reply(gmail).text == "Thanks, noted."
    apple = '<div>OK</div><blockquote type="cite"><div>secret</div></blockquote>'
    assert extract_reply(apple).text == "OK"
    for sample in (mobile, gmail, apple):
        assert "secret" not in extract_reply(sample).html


def test_reply_without_html_markers_is_cut_as_text_and_html_is_not_trusted():
    html = (
        "<div>All good.</div><div>From: Sheena Nelson via WhatsApp &lt;whatsapp@example.com&gt;</div>"
        "<div>Sent: Wednesday, November 18, 2026 9:41 AM</div><div>To: ops@example.com</div>"
        "<div>Subject: [WhatsApp] Sheena</div><div>WhatsApp message</div>"
    )
    reply = extract_reply(html)
    assert reply.text == "All good." and reply.cut is True
    assert reply.html is None and reply.images == []
    wrote = "<div>Fine.</div><div>On Nov 18, 2026, at 9:41 AM, Sheena via WhatsApp wrote:</div><div>old</div>"
    assert extract_reply(wrote).text == "Fine."


def test_text_patterns_do_not_cut_ordinary_shipping_details():
    text = "Your shipment:\nFrom: Shanghai\nDate: 12 Dec\nTo: Newark\nThanks"
    assert cut_quoted_text(text) == (text, False, None)
    quoted = "Done.\n\n-----Original Message-----\nFrom: a\nold"
    assert cut_quoted_text(quoted) == ("Done.", True, None)
    underscores = "Done.\n________________________________\nFrom: a\nSent: b"
    assert cut_quoted_text(underscores) == ("Done.", True, None)
    # wording a person could have typed as well: cut, and the first line that went is handed back
    first = "On Wed, Nov 18, 2026 at 9:41 AM Sheena Nelson via WhatsApp"
    two_lines = f"Done.\n{first}\n<wa@example.com> wrote:\nold"
    assert cut_quoted_text(two_lines) == ("Done.", True, first)
    assert cut_quoted_text("Done.\nOn Monday the terminal wrote:\nreleased") == (
        "Done.",
        True,
        "On Monday the terminal wrote:",
    )
    # ... unless the lines below it are marked as quoted
    assert cut_quoted_text("Done.\nOn Nov 18 Sheena wrote:\n> old\n> older") == ("Done.", True, None)


def test_plain_text_reply():
    reply = extract_reply_plain(
        "Hello\r\n\r\n\r\nBye\r\n\r\nFrom: a\r\nSent: b\r\nTo: c\r\nSubject: d\r\nold"
    )
    assert reply.text == "Hello\n\nBye" and reply.cut is True
    assert reply.html == "<div>Hello<br><br>Bye</div>"
    assert text_to_html("a <b> & c") == "<div>a &lt;b&gt; &amp; c</div>"


def test_text_layout_lists_paragraphs_tables_and_hidden_content():
    html = (
        "<p>First paragraph</p><p>Second<br>line</p>"
        "<ul><li>one</li><li>two</li></ul><ol><li>uno</li><li>dos</li><li></li></ol>"
        "<table><tr><td>Name</td><td>Chris</td></tr></table>"
        '<span style="display:none">tracking</span><script>alert(1)</script>'
        '<pre>a\n  b</pre><a href="mailto:chris@example.com">chris@example.com</a> '
        '<a href="http://www.vship.example/">www.vship.example</a> <a href="javascript:x()">click</a>'
    )
    reply = extract_reply(html)
    assert reply.text == (
        "First paragraph\n\nSecond\nline\n\n- one\n- two\n1. uno\n2. dos\nName\nChris\na\n  b\n"
        "chris@example.com www.vship.example click"
    )
    assert "tracking" not in reply.html and "alert" not in reply.html and "javascript" not in reply.html
    assert reply.cut is False


def test_html_to_text_keeps_the_quoted_part():
    text = html_to_text(OUTLOOK_WEB)
    assert "Hi Ana," in text and "Ref: WA-R7K2M5X4Q6WTZ5AB" in text and "From: Sheena" in text


def test_a_rule_that_is_not_followed_by_a_header_is_kept():
    reply = extract_reply("<div>Above</div><hr><div>Below, still mine</div>")
    assert reply.text == "Above\nBelow, still mine" and reply.cut is False and "<hr>" in reply.html


# ----------------------------------------------------------------------------- what reaches WhatsApp as text
def test_the_address_of_a_link_is_written_out_unless_the_link_shows_it():
    def text(html: str) -> str:
        return extract_reply(html).text

    assert text('<div>Follow it: <a href="https://www.vship2000.com/track/MSKU1">Tracking</a></div>') == (
        "Follow it: Tracking (https://www.vship2000.com/track/MSKU1)"
    )
    # also when the visible words happen to occur in the address
    assert text('<div>Pay through <a href="https://portal.vship2000.com/i/8841?t=abc">VShip</a>.</div>') == (
        "Pay through VShip (https://portal.vship2000.com/i/8841?t=abc)."
    )
    assert "(https://vship.example/track?id=1)" in text(
        '<div><a href="https://vship.example/track?id=1">Track your<br>shipment</a></div>'
    )
    assert text(
        '<div>Write to <a href="mailto:claims@vship2000.com?subject=Hi">our claims desk</a>.</div>'
    ) == ("Write to our claims desk (claims@vship2000.com).")
    assert (
        text('<div>Call <a href="tel:+13055550100">the office</a>.</div>')
        == "Call the office (+13055550100)."
    )
    # a link that shows its own address is left as it is
    assert text('<a href="https://vship.example/a">https://vship.example/a</a>') == "https://vship.example/a"
    assert text('<a href="mailto:Chris@Example.com">chris@example.com</a>') == "chris@example.com"
    assert text('<a href="http://www.vship.example/">www.vship.example</a>') == "www.vship.example"


def test_links_rewritten_by_microsoft_defender_are_shown_as_they_were_written():
    wrapped = (
        "https://nam02.safelinks.protection.outlook.com/?url=https%3A%2F%2Ftrack.example%2Fbl%2F123"
        "&amp;data=05%7C02%7Cwhatsapp%2Babc%40example.com%7Cabc&amp;reserved=0"
    )
    reply = extract_reply(f'<div>Track it <a href="{wrapped}">here</a></div>')
    assert reply.text == "Track it here (https://track.example/bl/123)"
    assert reply.html == '<div>Track it <a href="https://track.example/bl/123">here</a></div>'
    assert unwrap_safelink("https://example.com/?url=https%3A%2F%2Fx.example") == (
        "https://example.com/?url=https%3A%2F%2Fx.example"
    )  # only Microsoft's own wrapper is taken off
    assert link_target("javascript:x()") is None and link_target("mailto:") is None


def test_what_must_not_be_passed_on_is_dropped_from_links_and_attributes():
    def private(value: str) -> bool:
        return "secret@example.com" in value

    html = (
        '<div><a href="mailto:secret@example.com" title="secret@example.com">@Team</a> and '
        '<a href="https://ok.example/">ok</a>'
        '<img src="https://ok.example/p.png" alt="secret@example.com"></div>'
    )
    reply = extract_reply(html, private=private)
    assert reply.text == "@Team and ok (https://ok.example/)"
    assert reply.html == (
        '<div><a>@Team</a> and <a href="https://ok.example/">ok</a><img src="https://ok.example/p.png"></div>'
    )


def test_lists_keep_their_numbers_and_bullets():
    # Outlook on the web wraps the text of a list item in a block of its own
    html = (
        '<div class="elementToProof">Please do this:</div>'
        '<ol style="margin-top: 0px; margin-bottom: 0px;" start="1">'
        '<li style="font-size: 12pt"><div role="presentation" class="x">Sign the form</div></li>'
        '<li style="font-size: 12pt"><div role="presentation" class="x">Pay the fee</div></li></ol>'
        '<div class="elementToProof">Tell me when step 2 is done.</div>'
    )
    assert extract_reply(html).text == (
        "Please do this:\n1. Sign the form\n2. Pay the fee\nTell me when step 2 is done."
    )
    bullets = extract_reply("<ul><li><p>MSKU1: released</p></li><li><p>TCLU7: on hold</p></li></ul>").text
    assert bullets.startswith("- MSKU1: released") and "- TCLU7: on hold" in bullets
    later = (
        '<ol><li>Book the truck</li></ol><div>(wait for our call)</div><ol start="2"><li>Collect it</li></ol>'
    )
    assert extract_reply(later).text == "1. Book the truck\n(wait for our call)\n2. Collect it"


def test_signature_ends_where_its_element_ends():
    html = (
        "<div>The damage is marked in the picture below.</div>"
        '<div id="Signature"><div>Chris</div><img src="cid:logo"></div>'
        '<div><img src="cid:shot" width="600"></div>'
    )
    reply = extract_reply(html)
    assert [(i.cid, i.in_signature) for i in reply.images] == [("logo", True), ("shot", False)]
    assert reply.own_text is True and reply.cids == {"logo", "shot"}
    only_signature = '<div><br></div><div id="Signature"><div>Chris</div><div>VShip</div></div>'
    assert (
        extract_reply(only_signature).own_text is False
        and extract_reply(only_signature).text == "Chris\nVShip"
    )
    mobile = '<div><br></div><div id="ms-outlook-mobile-signature">Get <a href="https://aka.ms/x">Outlook</a></div>'
    assert extract_reply(mobile).own_text is False
    assert extract_reply("<div></div>").own_text is False and extract_reply_plain("Hi").own_text is True


def test_a_file_shown_inside_the_text_is_noted_by_its_content_id():
    shown = '<object type="application/x-apple-msg-attachment" data="cid:6F1D@apple.com"></object>'
    html = f"<div>Attached.{shown}</div>"
    reply = extract_reply(html)
    assert reply.cids == {"6F1D@apple.com"} and reply.images == [] and "object" not in reply.html


LABELS = {
    "English": ("From:", "Sent:", "To:", "Subject:"),
    "Spanish": ("De:", "Enviado el:", "Para:", "Asunto:"),
    "Portuguese": ("De:", "Enviada em:", "Para:", "Assunto:"),
    "German": ("Von:", "Gesendet:", "An:", "Betreff:"),
    "French": ("De :", "Envoyé :", "À :", "Objet :"),
    "Polish (labels the forwarder has no list for)": ("Od:", "Wysłano:", "Do:", "Temat:"),
}


def classic_header(labels: tuple[str, str, str, str]) -> str:
    sender, sent, to, subject = labels
    return (
        '<div><div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0in 0in 0in">'
        f'<p class="MsoNormal"><b>{sender}</b> Sheena via WhatsApp &lt;whatsapp@example.com&gt; <br>'
        f"<b>{sent}</b> Wednesday, November 18, 2026 9:41 AM<br><b>{to}</b> ops@example.com<br>"
        f"<b>{subject}</b> [WhatsApp] Sheena<o:p></o:p></p></div></div>"
    )


def test_classic_outlook_header_is_known_in_any_language():
    for language, labels in LABELS.items():
        reply = extract_reply(
            '<p class="MsoNormal">It cleared.</p>' + classic_header(labels) + "<p>secret</p>"
        )
        assert reply.text == "It cleared." and reply.cut and reply.doubt is None, language
        assert "secret" not in reply.html and "border-top" not in reply.html, language


def test_a_rule_and_lines_typed_by_the_sender_are_not_taken_for_a_quoted_email_when_a_real_one_follows():
    own = "<div>Your routing:</div><hr><div>From: Shanghai</div><div>To: Newark</div><div>Date: 12 Dec</div>"
    # ... marked by Outlook on the web
    reply = extract_reply(
        own + '<div id="appendonsend"></div><hr><div id="divRplyFwdMsg"><b>From:</b> x</div>old'
    )
    assert reply.text == "Your routing:\nFrom: Shanghai\nTo: Newark\nDate: 12 Dec" and reply.doubt is None
    assert reply.html.count("<hr>") == 1 and "old" not in reply.html
    # ... or by classic Outlook's header
    reply = extract_reply(own + classic_header(LABELS["English"]) + "<p>old</p>")
    assert reply.text == "Your routing:\nFrom: Shanghai\nTo: Newark\nDate: 12 Dec" and reply.doubt is None
    # with nothing else to go by, the rule is taken for the start, and the caller can say so
    reply = extract_reply(own + "<div>Regards</div>")
    assert reply.text == "Your routing:" and reply.cut and reply.doubt == "From: Shanghai"
    # two labels are not a header
    reply = extract_reply(
        "<div>Routing:</div><hr><div>From: Shanghai</div><div>To: Newark</div><div>Bye</div>"
    )
    assert reply.text == "Routing:\nFrom: Shanghai\nTo: Newark\nBye" and not reply.cut


def test_the_first_header_of_a_quoted_email_is_where_the_cut_is_even_if_a_marker_follows_further_down():
    """Classic Outlook answering an email that was written in Outlook on the web."""
    html = (
        "<p>New text</p>"
        + classic_header(LABELS["English"])
        + "<p>earlier answer</p>"
        + '<div id="appendonsend"></div><hr><div id="divRplyFwdMsg"><b>From:</b> x</div>oldest'
    )
    reply = extract_reply(html)
    assert reply.text == "New text" and "earlier answer" not in reply.html


def test_wording_that_looks_like_a_quoted_email_is_left_alone_when_a_marker_follows():
    html = (
        "<div>Good news.</div><div>On Monday the terminal wrote:</div><div>released</div>"
        '<div class="gmail_quote">On Wed, Nov 18, 2026 Sheena wrote:<blockquote>old</blockquote></div>'
    )
    assert extract_reply(html).text == "Good news.\nOn Monday the terminal wrote:\nreleased"
    # without one, the caller is handed the line where the text was cut
    reply = extract_reply("<div>Good news.</div><div>On Monday the terminal wrote:</div><div>released</div>")
    assert (
        reply.text == "Good news." and reply.html is None and reply.doubt == "On Monday the terminal wrote:"
    )


# ----------------------------------------------------------------------------- time taken
# A command email is read on the event loop that also answers Meta's webhook, so reading it must take about
# as long as reading an ordinary email of the same size, however its elements are nested. Before this was
# so, an email that opened 15,000 elements took 1.5 to 30 seconds here, 10 to 90 times as long as an
# ordinary one (each piece of text, line break or end tag went over everything still open), and four times
# as long for twice as many.
DEPTH = 15_000
CRAFTED = {
    "nested blocks with text": "<div>x" * DEPTH,
    "list items never closed": "<ol>" + "<li>x" * DEPTH,
    "end tags of blocks that are not open": "<div>" * DEPTH + "</p>" * DEPTH,
    "end tags of other elements that are not open": "<div>" * DEPTH + "</span>" * DEPTH,
    "links inside links over many lines": (
        '<a href="https://x.example/a">' * DEPTH + "y<br>" * DEPTH + "</a>" * DEPTH
    ),
    "links inside links": (
        "".join(f'<a href="https://x.example/{i}">' for i in range(DEPTH)) + "y" + "</a>" * DEPTH
    ),
}


def _seconds(html: str) -> float:
    best = float("inf")
    for _ in range(2):  # the quicker of two runs: another program on the machine can slow one down
        started = time.perf_counter()
        html_to_text(html)
        best = min(best, time.perf_counter() - started)
    return best


@pytest.mark.parametrize("shape", sorted(CRAFTED))
def test_deeply_nested_html_is_read_about_as_fast_as_an_ordinary_email_of_its_size(shape):
    crafted = CRAFTED[shape]
    ordinary = "<div>x</div>" * (len(crafted) // 12 + 1)
    assert _seconds(crafted) < 5 * _seconds(ordinary) + 0.05  # it takes 0.5 to 2 times as long now


def test_nesting_and_stray_end_tags_do_not_change_the_text():
    assert html_to_text("<div>Name: Sheena<div>Message:<div>Your box arrived.<div>##") == (
        "Name: Sheena\nMessage:\nYour box arrived.\n##"
    )
    assert html_to_text("<div>Name: Sheena</p></li></span><br>Message: hi</td></p><br>##</div>") == (
        "Name: Sheena\nMessage: hi\n##"
    )
    assert html_to_text("<ol><li>one<li>two<li>three") == "1. one\n2. two\n3. three"
    assert html_to_text("<div><p></p><p></p>x</div>") == "x"
    assert html_to_text("<div><p></p>x</div><p></p>y") == "x\n\ny"


def test_links_inside_links_keep_their_words_and_the_addresses_of_the_outer_ones():
    def nested(depth: int) -> str:
        opened = "".join(f'<a href="https://h{i}.example/">' for i in range(depth))
        return html_to_text(opened + "deep" + "</a>" * depth)

    # links are not valid inside links; beyond MAX_OPEN_LINKS of them only their words are kept
    assert nested(MAX_OPEN_LINKS + 5) == "deep " + " ".join(
        f"(https://h{i}.example/)" for i in reversed(range(MAX_OPEN_LINKS))
    )
    assert nested(3) == "deep (https://h2.example/) (https://h1.example/) (https://h0.example/)"
