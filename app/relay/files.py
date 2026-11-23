"""Which attached files of a "WhatsApp update" email go to the customer, and how (owner decision 18).

WhatsApp takes only some kinds of files, each up to a size limit, and only at certain times:

* While the customer's 24-hour customer service window is open (they wrote to VShip on WhatsApp less than
  24 hours ago), each supported file goes as its own free-form message after the update: PDF, Word, Excel
  and PowerPoint files as documents, JPEG and PNG pictures as images, MP4 films as videos.
* Outside the window WhatsApp delivers nothing but templates. The owner decided that only PDF files are
  sent then, each with the document template (a template whose header carries the file); every other file
  is not sent, and the employee is told to save it as a PDF.

Only real attached files count. A picture pasted into the text (a screenshot, a signature logo) is part of
the email, not a file; attached emails and cloud links are never passed on, because they can hold internal
details. Everything here is pure, so the rules are tested without a mailbox or WhatsApp.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote

DOCUMENT = "document"
IMAGE = "image"
VIDEO = "video"
PDF = "application/pdf"

# What WhatsApp takes, by file extension: (message kind, MIME type). Values as in the email-reply feature.
FILE_TYPES: dict[str, tuple[str, str]] = {
    ".pdf": (DOCUMENT, PDF),
    ".doc": (DOCUMENT, "application/msword"),
    ".docx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xls": (DOCUMENT, "application/vnd.ms-excel"),
    ".xlsx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".ppt": (DOCUMENT, "application/vnd.ms-powerpoint"),
    ".pptx": (DOCUMENT, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".jpg": (IMAGE, "image/jpeg"),
    ".jpeg": (IMAGE, "image/jpeg"),
    ".png": (IMAGE, "image/png"),
    ".mp4": (VIDEO, "video/mp4"),
}
_BY_MIME = {mime: (kind, mime) for kind, mime in FILE_TYPES.values()}
# WhatsApp's limits per kind, in bytes (compared with the size Graph reports for the attachment)
SIZE_LIMITS: dict[str, tuple[int, str]] = {
    DOCUMENT: (100_000_000, "100 MB for documents"),
    IMAGE: (5_000_000, "5 MB for pictures"),
    VIDEO: (16_000_000, "16 MB for videos"),
}

WINDOW_SECONDS = 24 * 3600
# The window counts as closed 10 minutes early: a file sent right at its end could arrive after it closed.
WINDOW_MARGIN_SECONDS = 600

# "media": its own free-form message (window open); "document_template": with the document template (a PDF
# outside the window, if that template is set up and approved); None: not sent (``reason`` says why).
Route = Literal["media", "document_template"]

NOT_SUPPORTED = "WhatsApp does not take this kind of file here."
NOT_A_FILE = "Attached emails and cloud links are never passed on."
NOT_PDF_OUTSIDE_WINDOW = (
    "The customer has not written to VShip on WhatsApp in the last 24 hours. At such times WhatsApp only"
    " takes PDF files (through the document template). Save it as a PDF and send it again."
)
PDF_NO_TEMPLATE = (
    "The customer has not written to VShip on WhatsApp in the last 24 hours, and the document template for"
    " files is not set up yet, so WhatsApp does not take the file."
)
PASTED_PICTURES_NOTE = (
    "Pictures pasted into the text of your email (a signature logo, for example) are never sent to the"
    " customer; only attached files are."
)

_CID_REF = re.compile(r"cid:([^\s\"'<>)]+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FileVerdict:
    """How one attached file goes to the customer: ``route`` None means not at all, for ``reason``."""

    kind: str | None  # document, image or video; None for a kind WhatsApp does not take
    mime: str | None
    route: Route | None
    reason: str | None


def whatsapp_type(name: str, content_type: str) -> tuple[str, str] | None:
    """(message kind, MIME type) for a file: by its extension first, else by its content type."""
    extension = "." + name.rsplit(".", 1)[-1].strip().lower() if "." in (name or "") else ""
    if extension in FILE_TYPES:
        return FILE_TYPES[extension]
    return _BY_MIME.get((content_type or "").split(";")[0].strip().lower())


def too_large(kind: str) -> str:
    return f"WhatsApp takes at most {SIZE_LIMITS[kind][1]}."


def document_template_not_ready(problem: str | None) -> str:
    """Why a PDF is not sent outside the window when the document template may not be used."""
    detail = (problem or "").strip().rstrip(".") or "no reason given"
    return (
        "The customer has not written to VShip on WhatsApp in the last 24 hours, and the document template"
        f" is not approved for use ({detail})."
    )


def classify(name: str, content_type: str, size: int, *, window_open: bool) -> FileVerdict:
    """The route of one real attached file (never call it for pasted pictures, emails or cloud links).

    A ``document_template`` route still depends on that template being set up and approved, which the
    caller checks (once per email, and only when some file needs it).
    """
    found = whatsapp_type(name, content_type)
    if found is None:
        return FileVerdict(None, None, None, NOT_SUPPORTED)
    kind, mime = found
    if size > SIZE_LIMITS[kind][0]:
        return FileVerdict(kind, mime, None, too_large(kind))
    if window_open:
        return FileVerdict(kind, mime, "media", None)
    if kind == DOCUMENT and mime == PDF:
        return FileVerdict(kind, mime, "document_template", None)
    return FileVerdict(kind, mime, None, NOT_PDF_OUTSIDE_WINDOW)


def window_since(now: float) -> float:
    """Earliest arrival time of a customer message that can still keep the window open."""
    return now - (WINDOW_SECONDS - WINDOW_MARGIN_SECONDS)


def window_open(last_inbound: float | None, now: float) -> bool:
    """Whether the customer service window is open, given when the customer last wrote (WhatsApp's time)."""
    return last_inbound is not None and now - last_inbound < WINDOW_SECONDS - WINDOW_MARGIN_SECONDS


def cid_keys(value: str | None) -> set[str]:
    """The forms under which a content id is compared: as written, and with %-escapes resolved."""
    plain = html_lib.unescape(value or "").strip().strip("<>").lower()
    return {key for key in (plain, unquote(plain)) if key}


def referenced_cids(html: str | None) -> set[str]:
    """Content ids of every picture the HTML text of an email shows (``src="cid:..."``)."""
    return {key for found in _CID_REF.findall(html or "") for key in cid_keys(found)}


# File name endings of pictures (screenshots, signature logos); Outlook uses EMF/WMF for some signatures.
_PICTURE_ENDINGS = (
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif", ".svg", ".emf",
    ".wmf", ".ico",
)  # fmt: skip


def is_picture(name: str | None, content_type: str | None) -> bool:
    """Whether an attachment is a picture, by its content type or, failing that, its file name."""
    mime = (content_type or "").split(";")[0].strip().lower()
    return mime.startswith("image/") or (name or "").strip().lower().endswith(_PICTURE_ENDINGS)


def is_pasted_picture(
    is_inline: bool,
    content_id: str | None,
    shown: set[str],
    *,
    name: str | None = None,
    content_type: str | None = None,
) -> bool:
    """A picture that is part of the text rather than an attached file.

    Graph marks pasted pictures as inline, but not every one of them, so a picture whose content id the
    text shows counts as pasted too. Only pictures count: some mail programs (Apple Mail and iPhone Mail)
    mark a normally attached PDF or Word file as inline as well, and such a file is an attached file like
    any other (owner decision, 21 Nov 2026).
    """
    if not is_picture(name, content_type):
        return False
    return is_inline or bool(cid_keys(content_id) & shown)
