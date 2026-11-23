"""The file rules of "WhatsApp updates" (owner decision 18, spec section 9): what goes, how, and why not."""

from __future__ import annotations

import pytest
from app.relay.files import (
    DOCUMENT,
    FILE_TYPES,
    IMAGE,
    NOT_PDF_OUTSIDE_WINDOW,
    NOT_SUPPORTED,
    PDF,
    PDF_NO_TEMPLATE,
    SIZE_LIMITS,
    VIDEO,
    WINDOW_MARGIN_SECONDS,
    WINDOW_SECONDS,
    FileVerdict,
    cid_keys,
    classify,
    document_template_not_ready,
    is_pasted_picture,
    referenced_cids,
    too_large,
    whatsapp_type,
    window_open,
    window_since,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
NOW = 1_700_000_000.0


def test_the_table_is_the_one_in_the_spec():
    assert FILE_TYPES == {
        ".pdf": ("document", "application/pdf"),
        ".doc": ("document", "application/msword"),
        ".docx": ("document", DOCX),
        ".xls": ("document", "application/vnd.ms-excel"),
        ".xlsx": ("document", XLSX),
        ".ppt": ("document", "application/vnd.ms-powerpoint"),
        ".pptx": ("document", PPTX),
        ".jpg": ("image", "image/jpeg"),
        ".jpeg": ("image", "image/jpeg"),
        ".png": ("image", "image/png"),
        ".mp4": ("video", "video/mp4"),
    }
    assert {kind: limit for kind, (limit, _) in SIZE_LIMITS.items()} == {
        "document": 100_000_000,
        "image": 5_000_000,
        "video": 16_000_000,
    }
    assert (DOCUMENT, IMAGE, VIDEO, PDF) == ("document", "image", "video", "application/pdf")


@pytest.mark.parametrize(
    ("name", "content_type", "expected"),
    [
        ("Invoice.PDF", "application/octet-stream", (DOCUMENT, PDF)),  # the extension decides, any case
        ("report.docx", "application/pdf", (DOCUMENT, DOCX)),  # the extension wins over the content type
        ("photo.JPEG", "", (IMAGE, "image/jpeg")),
        ("clip.mp4", "video/mp4", (VIDEO, "video/mp4")),
        ("scan", "application/pdf; name=scan", (DOCUMENT, PDF)),  # no extension: the content type
        ("picture.bin", "IMAGE/PNG", (IMAGE, "image/png")),
        ("sheet.xlsx ", "", (DOCUMENT, XLSX)),  # a stray space after the name
    ],
)
def test_the_type_comes_from_the_extension_then_the_content_type(name, content_type, expected):
    assert whatsapp_type(name, content_type) == expected


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("notes.txt", "text/plain"),
        ("sticker.webp", "image/webp"),
        ("funny.gif", "image/gif"),
        ("clip.3gp", "video/3gpp"),
        ("voice.mp3", "audio/mpeg"),
        ("archive.zip", "application/zip"),
        ("old.eml", "message/rfc822"),
        ("", ""),
    ],
)
def test_anything_else_is_not_sent(name, content_type):
    assert whatsapp_type(name, content_type) is None
    for is_open in (True, False):
        assert classify(name, content_type, 100, window_open=is_open) == FileVerdict(
            None, None, None, NOT_SUPPORTED
        )
    assert NOT_SUPPORTED == "WhatsApp does not take this kind of file here."


@pytest.mark.parametrize(
    ("name", "kind", "limit", "words"),
    [
        ("big.pdf", DOCUMENT, 100_000_000, "100 MB for documents"),
        ("big.png", IMAGE, 5_000_000, "5 MB for pictures"),
        ("big.mp4", VIDEO, 16_000_000, "16 MB for videos"),
    ],
)
def test_size_limits(name, kind, limit, words):
    assert classify(name, "", limit, window_open=True).route == "media"  # exactly at the limit is fine
    verdict = classify(name, "", limit + 1, window_open=True)
    assert verdict.route is None and verdict.kind == kind
    assert verdict.reason == too_large(kind) == f"WhatsApp takes at most {words}."
    # too large is said even outside the window, where saving it as a PDF would not help
    assert classify(name, "", limit + 1, window_open=False).reason == too_large(kind)


@pytest.mark.parametrize(
    ("name", "kind", "mime"),
    [
        ("a.pdf", DOCUMENT, PDF),
        ("a.doc", DOCUMENT, "application/msword"),
        ("a.docx", DOCUMENT, DOCX),
        ("a.xls", DOCUMENT, "application/vnd.ms-excel"),
        ("a.xlsx", DOCUMENT, XLSX),
        ("a.ppt", DOCUMENT, "application/vnd.ms-powerpoint"),
        ("a.pptx", DOCUMENT, PPTX),
        ("a.jpg", IMAGE, "image/jpeg"),
        ("a.png", IMAGE, "image/png"),
        ("a.mp4", VIDEO, "video/mp4"),
    ],
)
def test_inside_the_window_every_supported_file_goes_as_its_own_message(name, kind, mime):
    assert classify(name, "", 1000, window_open=True) == FileVerdict(kind, mime, "media", None)


def test_outside_the_window_only_pdf_goes_and_only_through_the_document_template():
    assert classify("a.pdf", "", 1000, window_open=False) == FileVerdict(
        DOCUMENT, PDF, "document_template", None
    )
    assert classify("scan", "application/pdf", 1000, window_open=False).route == "document_template"
    for name in ("a.docx", "a.xlsx", "a.pptx", "a.jpg", "a.png", "a.mp4"):
        verdict = classify(name, "", 1000, window_open=False)
        assert verdict.route is None
        assert verdict.reason == NOT_PDF_OUTSIDE_WINDOW
    assert "Save it as a PDF and send it again." in NOT_PDF_OUTSIDE_WINDOW
    assert "document template for files is not set up yet" in PDF_NO_TEMPLATE
    assert document_template_not_ready("Its status at Meta is PAUSED.").endswith(
        "the document template is not approved for use (Its status at Meta is PAUSED)."
    )
    assert document_template_not_ready(None).endswith("(no reason given).")


def test_the_window_closes_ten_minutes_early():
    assert WINDOW_SECONDS - WINDOW_MARGIN_SECONDS == 86400 - 600
    assert window_since(NOW) == NOW - 85800
    assert window_open(int(NOW) - 3600, NOW)
    assert window_open(int(NOW) - 85799, NOW)
    assert not window_open(int(NOW) - 85800, NOW)  # 23 h 50 min ago: too late for a file
    assert not window_open(int(NOW) - 90000, NOW)
    assert not window_open(None, NOW)  # the customer never wrote
    assert window_open(int(NOW) + 30, NOW)  # a clock a little ahead of ours


def test_pictures_shown_in_the_text_are_recognised():
    html = (
        '<p><img src="cid:image001.png@01DC5A11.1B2C3D40" alt="logo"></p>'
        "<p><img src='CID:Screenshot%201.png'></p>"
        '<p><img src="https://example.com/x.png"></p>'
    )
    shown = referenced_cids(html)
    assert "image001.png@01dc5a11.1b2c3d40" in shown
    assert "screenshot 1.png" in shown  # %-escapes resolved
    assert referenced_cids(None) == set() and referenced_cids("") == set()
    assert cid_keys("<Image001.png@01DC5A11.1B2C3D40>") == {"image001.png@01dc5a11.1b2c3d40"}
    assert cid_keys(None) == set()
    png = {"name": "image001.png", "content_type": "image/png"}
    # inline, or shown in the text although Graph did not say inline: part of the text
    assert is_pasted_picture(True, None, set(), **png)
    assert is_pasted_picture(False, "<image001.png@01DC5A11.1B2C3D40>", shown, **png)
    # a real attachment: not inline, and its content id (Outlook gives some files one) is not shown
    assert not is_pasted_picture(False, "f_lq2abc", shown, **png)
    assert not is_pasted_picture(False, None, shown, **png)


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("Invoice 123.pdf", "application/pdf"),
        ("Packing list.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("Rates.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("Walkthrough.mp4", "video/mp4"),
        ("no type", ""),
    ],
)
def test_only_pictures_count_as_pasted_even_when_marked_inline(name, content_type):
    """Owner decision (21 Nov 2026): Apple Mail / iPhone Mail can mark an attached file inline; only a
    picture is part of the text, any other file is an attached file."""
    shown = {"invoice@apple"}
    assert not is_pasted_picture(True, "<invoice@apple>", shown, name=name, content_type=content_type)
    assert not is_pasted_picture(True, None, set(), name=name, content_type=content_type)


@pytest.mark.parametrize(
    ("name", "content_type"),
    [
        ("image001.png", "image/png"),
        ("Screenshot 2026-11-21.jpg", "image/jpeg"),
        ("signature.emf", "application/octet-stream"),  # recognised by its name when the type says nothing
        ("logo", "image/gif"),  # recognised by its type when the name says nothing
    ],
)
def test_pictures_marked_inline_are_pasted(name, content_type):
    assert is_pasted_picture(True, None, set(), name=name, content_type=content_type)
