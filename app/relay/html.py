"""Pull the employee's new text out of an email reply.

A reply arrives as HTML that holds the employee's new text, usually their signature and, below
that, the quoted WhatsApp email they answered. One pass over that HTML yields:

* the new text as plain text (what goes to the customer on WhatsApp),
* the same part as cleaned HTML (what goes into the customer's email copy),
* the pictures shown inside that part, and whether each sits in the signature,
* whether the quoted message was found and cut off.

Nothing from the quote marker on is ever returned. The caller also refuses any result that still
contains text of the forwarder's own emails, because those carry internal CRM details that must
never reach a customer.

Exchange can prefix ids and class names with ``x_`` when it hands out HTML, so every id and class
is compared without such prefixes.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
    "track", "wbr",
}  # fmt: skip
# Elements dropped together with everything inside them.
DROP_CONTENT = {
    "script", "style", "head", "title", "object", "iframe", "frame", "frameset", "svg", "math",
    "template", "noscript", "select", "textarea", "applet", "audio", "video", "canvas", "xml",
}  # fmt: skip
# Tags kept in the customer's email copy; any other tag is removed but its text is kept.
ALLOWED_TAGS = {
    "a", "b", "strong", "i", "em", "u", "s", "strike", "del", "ins", "span", "div", "p", "br", "ul",
    "ol", "li", "table", "thead", "tbody", "tfoot", "tr", "td", "th", "colgroup", "col", "caption",
    "img", "font", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code", "blockquote", "hr", "sub",
    "sup", "small", "big", "center", "wbr",
}  # fmt: skip
ALLOWED_ATTRS = {
    "href", "src", "alt", "title", "width", "height", "align", "valign", "border", "cellpadding",
    "cellspacing", "colspan", "rowspan", "bgcolor", "color", "face", "size", "dir", "lang", "start",
}  # fmt: skip
BLOCK_TAGS = {
    "p", "div", "tr", "td", "th", "li", "ul", "ol", "table", "caption", "blockquote", "pre", "center",
    "h1", "h2", "h3", "h4", "h5", "h6",
}  # fmt: skip

# Where mail programs start the quoted earlier message.
QUOTE_IDS = {
    "divrplyfwdmsg",  # Outlook on the web, new Outlook, Outlook mobile: header of the quoted message
    "appendonsend",  # Outlook on the web: empty marker placed right above the quoted message
    "mail-editor-reference-message-container",  # new Outlook / Outlook mobile: the quoted message
    "olk_src_body_section",  # Outlook for Mac
    "stopspelling",
}
QUOTE_CLASSES = {"gmail_quote", "gmail_extra", "yahoo_quoted", "moz-cite-prefix", "protonmail_quote"}
# Where mail programs mark the sender's signature.
SIGNATURE_IDS = {"signature", "ms-outlook-mobile-signature", "applemailsignature"}
SIGNATURE_CLASSES = {"gmail_signature", "moz-signature"}
_AUTOSIG_STYLE = re.compile(r"mso-bookmark:(?:x_)*_mailautosig")  # classic Outlook for Windows

_BAD_STYLE = (
    "expression(",
    "javascript:",
    "vbscript:",
    "url(",
    "behavior:",
    "-moz-binding",
    "position:",
    "@import",
)
_WS = re.compile(r"[ \t\r\n\f\v\u00a0]+")
_ORIGINAL_MESSAGE = re.compile(r"-{2,} ?Original Message ?-{2,}$", re.I)
_EMPTY_ITEM = re.compile(r"-|\d+\.")
_NO_MARGIN = re.compile(r"margin(?:-bottom)?:0(?![.\d])")

# The labels mail programs put above a quoted message, in the languages Outlook is commonly set to
# (English, Spanish, Portuguese, German, French, Italian, Dutch).
_FROM = r"(?:From|De|Von|Da|Van)"
_SENT = r"(?:Sent|Date|Enviado(?: el)?|Enviada(?: em)?|Fecha|Gesendet|Datum|Envoy[ée]|Inviato|Data|Verzonden)"
_TO = r"(?:To|Para|An|À|A|Aan)"
_SUBJECT = r"(?:Subject|Asunto|Assunto|Betreff|Objet|Oggetto|Onderwerp)"
_HEADER_WORD = re.compile(rf"(?:{_FROM}|{_SENT}|{_TO}|Cc|{_SUBJECT}) ?:", re.I)
# a short label in bold that ends in a colon, whatever the language: "From:", "Enviado el:", "De :"
_BOLD_LABEL = re.compile(r"[^\W\d_][\w .'\u2019-]{0,24} ?:")
HEADER_LABELS = 3  # this many such labels right after a separator make it the header of a quoted message
HEADER_NODES = 14  # ... within this many pieces of text
# Links inside links are not valid HTML; beyond this depth their addresses are not written out. Each open
# link costs time on every piece of text that follows, and the text is read on the event loop, where an
# email nesting thousands of them would otherwise hold up the whole service.
MAX_OPEN_LINKS = 20

# Plain-text signs of a quoted message, for mail without recognisable HTML markers. The second
# value says whether the sign is only wording that a person could have typed as well: the sender is
# then told what was cut off.
_TEXT_QUOTE_STARTS = (
    (re.compile(r"^-{2,} ?Original Message ?-{2,}\s*$", re.M | re.I), False),
    (re.compile(rf"^_{{10,}}\s*\n\s*{_FROM} ?:", re.M | re.I), False),
    # The header block above a quoted message: From, Sent/Date, To and Subject lines close together.
    # All four are required so that "From: Shanghai / To: Newark" in a reply is left alone.
    (
        re.compile(
            rf"^{_FROM} ?:[^\n]*\n"
            rf"(?=(?:[^\n]*\n){{0,5}}?{_SENT} ?:)(?=(?:[^\n]*\n){{0,5}}?{_TO} ?:)"
            rf"(?=(?:[^\n]*\n){{0,6}}?{_SUBJECT} ?:)",
            re.M | re.I,
        ),
        True,
    ),
    (re.compile(r"^On [^\n]{5,300}\bwrote:\s*$", re.M), True),
    (re.compile(r"^On [^\n]{5,200}\n[^\n]{1,200}\bwrote:\s*$", re.M), True),
)
DOUBT_LENGTH = 120  # how much of the first line that was cut off is shown to the sender


@dataclass(slots=True)
class InlineImage:
    """A picture shown inside the email body (``<img src="cid:...">``)."""

    cid: str
    in_signature: bool


@dataclass(slots=True)
class ExtractedReply:
    text: str
    html: str | None  # None when only the plain text can be trusted (the HTML still holds the quote)
    images: list[InlineImage] = field(default_factory=list)
    cut: bool = False  # a quoted earlier message was found and removed
    # every content id the new part points at: its pictures, and files a mail program shows inside it
    cids: set[str] = field(default_factory=set)
    own_text: bool = True  # False when the text holds nothing but the sender's signature
    # The first line that was cut off, when the place of the cut was a guess from wording a person
    # could have typed as well ("From: ... To: ...", "On Monday X wrote:"). The sender is then told.
    doubt: str | None = None


def unwrap_safelink(url: str) -> str:
    """The address behind a Microsoft Defender "Safe Links" address; any other address as it is.

    Exchange rewrites links in delivered mail. The rewritten form is long, and it names the mailbox
    the email was delivered to, which here is the tagged reply address.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    if (parts.hostname or "").lower().endswith(".safelinks.protection.outlook.com"):
        inner = parse_qs(parts.query).get("url", [""])[0]
        if inner.lower().startswith(("http://", "https://")):
            return inner
    return url


def link_target(href: str) -> str | None:
    """What a link points at, as it is written out in plain text; None when there is nothing to show."""
    href = unwrap_safelink(href.strip())
    low = href.lower()
    if low.startswith(("http://", "https://")):
        return href
    if low.startswith("mailto:"):
        return unquote(href[7:].split("?", 1)[0]).strip() or None
    if low.startswith("tel:"):
        return unquote(href[4:]).strip() or None
    return None


def _link_key(value: str) -> str:
    """A link or its visible text in a form that compares equal when they show the same address."""
    value = re.sub(r"\s+", "", value).lower()
    return re.sub(r"^(?:https?://|mailto:|tel:)", "", value).rstrip("/")


def _norm(value: str | None) -> str:
    value = (value or "").strip().lower()
    while value.startswith("x_"):
        value = value[2:]
    return value


def _safe_style(value: str) -> str:
    kept = []
    for declaration in value.split(";"):
        declaration = declaration.strip()
        if ":" not in declaration:
            continue
        compact = re.sub(r"\s+", "", declaration).lower()
        if any(bad in compact for bad in _BAD_STYLE):
            continue
        kept.append(declaration)
    return "; ".join(kept)


class _Extractor(HTMLParser):
    def __init__(self, cut_quotes: bool = True, private: Callable[[str], bool] | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.cut_quotes = cut_quotes
        self.private = private or (lambda target: False)  # link targets that must not be passed on
        # Every step below takes about the same time however deeply the elements are nested (an email can
        # open thousands of them and never close one): the open tags and blocks are also counted by name,
        # and a block has content when text was written while it was open (``marks`` grew).
        self.html: list[str] = []
        self.open: list[str] = []  # kept, non-void tags written to self.html that are still open
        self.open_count: Counter[str] = Counter()  # ... counted by tag
        self.lines: list[str] = []
        self.cur: list[str] = []  # pieces of the line being built
        self.blocks: list[list] = []  # [tag, marks when it opened, blank line after]
        self.block_count: Counter[str] = Counter()  # the open blocks counted by tag
        self.marks = 0  # how often text or a line break was written
        self.lists: list[list] = []  # [tag, items so far]
        self.links: list[tuple[str | None, int, int]] = []
        self.marker = ""  # the number or dash of a list item, written when its first text arrives
        self.pre = 0
        self.bold = 0
        self.drop_tag: str | None = None
        self.drop_depth = 0
        self.stopped = False
        self.cut = False
        self.sig_tag: str | None = None  # the element that holds the signature, while it is open
        self.sig_depth = 0
        self.sig_once = False
        self.images: list[InlineImage] = []
        self.cids: set[str] = set()
        self.own_chars = 0  # characters of text outside the signature
        # The state of the output at a separator (a rule, or classic Outlook's bordered box). It
        # starts the quoted message if header labels ("From:", "Sent:", "To:" ...) follow at once.
        self.candidate: tuple | None = None
        self.box = False  # the separator is the bordered box, which only a mail program makes
        self.labels = 0
        self.nodes = 0
        self.first_after: str | None = None  # the first text after the separator
        # The first rule that was confirmed that way, with the text that followed it. A person can
        # type a rule and such lines too, so it is only used at the end, and only if nothing
        # further down marks the quoted message for certain.
        self.heuristic: tuple[tuple, str | None] | None = None
        self.doubt: str | None = None

    @property
    def in_signature(self) -> bool:
        return self.sig_tag is not None or self.sig_once

    def _snapshot(self) -> tuple:
        return (
            len(self.html),
            list(self.open),
            len(self.lines),
            list(self.cur),
            len(self.images),
            set(self.cids),
            self.own_chars,
        )

    def _restore(self, snapshot: tuple) -> None:
        html_len, open_tags, line_len, cur, image_len, cids, own_chars = snapshot
        del self.html[html_len:]
        self.open = list(open_tags)
        self.open_count = Counter(self.open)
        del self.lines[line_len:]
        self.cur = list(cur)
        del self.images[image_len:]
        self.cids = set(cids)
        self.own_chars = own_chars

    # ------------------------------------------------------------------ parser events
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.stopped:
            return
        if self.drop_tag is not None:
            if tag == self.drop_tag:
                self.drop_depth += 1
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        ident = _norm(a.get("id"))
        classes = {_norm(c) for c in a.get("class", "").split()}
        style = re.sub(r"\s+", "", a.get("style", "")).lower()
        if self.cut_quotes and (
            ident in QUOTE_IDS
            or classes & QUOTE_CLASSES
            or (tag == "blockquote" and a.get("type", "").lower() == "cite")
        ):
            self._cut(at_candidate=True)
            return
        hidden = "display:none" in style or "mso-hide:all" in style or "visibility:hidden" in style
        for name in ("src", "data", "background", "href"):  # whatever the new part shows by content id
            value = a.get(name, "").strip()
            if value.lower().startswith("cid:") and value[4:].strip():
                self.cids.add(value[4:].strip())
        if tag in DROP_CONTENT or hidden:
            if tag not in VOID_TAGS:
                self.drop_tag, self.drop_depth = tag, 1
            return
        signature = (
            ident in SIGNATURE_IDS
            or bool(classes & SIGNATURE_CLASSES)
            or _norm(a.get("name")) == "_mailautosig"
            or bool(_AUTOSIG_STYLE.search(style))
        )
        self.sig_once = False
        if self.sig_tag is not None:
            if tag == self.sig_tag and tag not in VOID_TAGS:
                self.sig_depth += 1
        elif signature:
            if tag in VOID_TAGS:
                self.sig_once = True  # a single element, such as a logo, marked as signature
            else:
                self.sig_tag, self.sig_depth = tag, 1
        if tag in ("b", "strong"):
            self.bold += 1
        if self.cut_quotes and (tag == "hr" or (tag == "div" and "border-top:solid" in style)):
            # (a separator that was still waiting for its labels is given up for this one)
            self.candidate, self.box = self._snapshot(), tag == "div"
            self.labels, self.nodes, self.first_after = 0, 0, None
        self._text_start(tag, a, classes, style)
        self._html_start(tag, a, classes)
        self.sig_once = False

    def handle_endtag(self, tag: str) -> None:
        if self.stopped:
            return
        if self.drop_tag is not None:
            if tag == self.drop_tag:
                self.drop_depth -= 1
                if self.drop_depth == 0:
                    self.drop_tag = None
            return
        if tag in ("b", "strong"):
            self.bold = max(0, self.bold - 1)
        self._text_end(tag)
        if tag == self.sig_tag:
            self.sig_depth -= 1
            if self.sig_depth <= 0:
                self.sig_tag = None  # what follows the signature is the sender's own again
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS and self.open_count[tag]:
            while self.open:
                closing = self.open.pop()
                self.open_count[closing] -= 1
                self.html.append(f"</{closing}>")
                if closing == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self.stopped or self.drop_tag is not None:
            return
        data = data.replace("\u200b", "").replace("\ufeff", "")
        stripped = data.strip()
        if stripped and self.cut_quotes:
            if self.candidate is not None:
                # Right after a separator: header labels in a row mean a quoted message starts here.
                self.nodes += 1
                if self.first_after is None:
                    self.first_after = stripped[:DOUBT_LENGTH]
                if (self.bold and _BOLD_LABEL.fullmatch(stripped)) or _HEADER_WORD.match(stripped):
                    self.labels += 1
                if self.labels >= HEADER_LABELS:
                    if self.box:  # classic Outlook's header of the quoted message: certain
                        self._restore(self.candidate)
                        self.cut = self.stopped = True
                        return
                    if self.heuristic is None:
                        self.heuristic = (self.candidate, self.first_after)
                    self.candidate = None
                elif self.labels == 0 or self.nodes >= HEADER_NODES:
                    self.candidate = None  # ordinary text follows the line: it is the sender's own
            if _ORIGINAL_MESSAGE.match(stripped):
                self._cut(at_candidate=False)
                return
        if stripped:
            if not self.in_signature:
                self.own_chars += len(stripped)
            if self.marker and not self.pre:
                self.cur.append(self.marker)
                self.marker = ""
        self.html.append(escape(data, quote=False))
        if self.pre:
            for index, part in enumerate(data.split("\n")):
                if index:
                    self.lines.append("".join(self.cur).rstrip())
                    self.cur = []
                if part:
                    self.cur.append(part)
            if stripped:
                self._mark()
            return
        if not stripped:
            if self.cur and not self.cur[-1].endswith(" "):
                self.cur.append(" ")
            return
        text = _WS.sub(" ", data)
        if not self.cur or self.cur[-1].endswith(" "):
            text = text.lstrip()
        self.cur.append(text)
        self._mark()

    # ------------------------------------------------------------------ plain text
    def _mark(self) -> None:
        self.marks += 1  # every block open now has content

    def _flush(self) -> None:
        line = "".join(self.cur).strip()
        self.cur = []
        if line and not _EMPTY_ITEM.fullmatch(line):
            self.lines.append(line)

    def _text_start(self, tag: str, a: dict[str, str], classes: set[str], style: str) -> None:
        if tag == "br":
            self.lines.append("".join(self.cur).strip())
            self.cur = []
            self._mark()
        elif tag in BLOCK_TAGS:
            self._flush()
            # Word-style paragraphs (class Mso...) and paragraphs without a margin sit directly under
            # each other; any other paragraph is followed by a blank line, as it is shown on screen.
            gap = tag == "p" and not (any(c.startswith("mso") for c in classes) or _NO_MARGIN.search(style))
            self.blocks.append([tag, self.marks, gap])
            self.block_count[tag] += 1
            if tag in ("ul", "ol"):
                start = a.get("start", "").strip()
                self.lists.append([tag, int(start) - 1 if tag == "ol" and start.isdigit() else 0])
            elif tag == "li":
                # Written with the first text of the item, which mail programs often wrap in a
                # paragraph of its own (<li><div>text</div></li>).
                if self.lists and self.lists[-1][0] == "ol":
                    self.lists[-1][1] += 1
                    self.marker = f"{self.lists[-1][1]}. "
                else:
                    self.marker = "- "
            elif tag == "pre":
                self.pre += 1
        elif tag == "a":
            target = link_target(a.get("href", ""))
            if target and self.private(target):
                target = None
            if len(self.links) >= MAX_OPEN_LINKS:
                target = None
            self.links.append((target, len(self.cur), len(self.lines)))

    def _text_end(self, tag: str) -> None:
        if tag in BLOCK_TAGS:
            if not self.block_count[tag]:
                return  # no such block is open (without this check every stray end tag searched them all)
            for index in range(len(self.blocks) - 1, -1, -1):
                if self.blocks[index][0] == tag:
                    _, marks, gap = self.blocks[index]
                    has_content = self.marks > marks
                    for closed in self.blocks[index:]:
                        self.block_count[closed[0]] -= 1
                    del self.blocks[index:]
                    break
            else:
                return
            if tag == "pre":
                self.pre = max(0, self.pre - 1)
                line = "".join(self.cur).rstrip()  # keep the indentation of preformatted text
                self.cur = []
                if line:
                    self.lines.append(line)
            self._flush()
            if tag == "li":
                self.marker = ""  # an item without text
            if tag in ("ul", "ol") and self.lists:
                self.lists.pop()
            if not has_content and tag in ("p", "div"):
                self.lines.append("")  # an empty paragraph is a blank line
                self._mark()
            elif gap and has_content:
                self.lines.append("")
        elif tag == "a" and self.links:
            target, start, line_count = self.links.pop()
            if target:
                if line_count == len(self.lines):
                    shown = "".join(self.cur[start:])
                else:  # the link runs over a line break
                    shown = " ".join([*self.lines[line_count:], "".join(self.cur)])
                # "our site" becomes "our site (https://...)"; a link that shows its own address stays
                if shown.strip() and _link_key(target) not in _link_key(shown):
                    self.cur.append(f" ({target})")

    # ------------------------------------------------------------------ cleaned HTML
    def _html_start(self, tag: str, a: dict[str, str], classes: set[str]) -> None:
        if tag not in ALLOWED_TAGS:
            return
        if tag == "img":
            source = a.get("src", "").strip()
            if source.lower().startswith("cid:"):
                cid = source[4:].strip()
                if not cid:
                    return
                self.images.append(InlineImage(cid=cid, in_signature=self.in_signature))
            elif not source.lower().startswith(("http://", "https://")):
                return
        parts = [tag]
        style = _safe_style(a.get("style", ""))
        if tag == "p" and any(c.startswith("mso") for c in classes) and "margin" not in style.lower():
            # Word's stylesheet (not sent along) gives these paragraphs no margin
            style = f"margin:0; {style}".rstrip("; ")
        if style:
            parts.append(f'style="{escape(style, quote=True)}"')
        for name, value in a.items():
            if name not in ALLOWED_ATTRS:
                continue
            if name == "href":
                value = unwrap_safelink(value)
                if not value.strip().lower().startswith(("http://", "https://", "mailto:", "tel:")):
                    continue
                if self.private(link_target(value) or value):
                    continue  # the words of the link stay, its address does not
            elif self.private(value):
                continue  # a title or alternative text that names what must not be passed on
            if name == "src" and tag != "img":
                continue
            parts.append(f'{name}="{escape(value, quote=True)}"')
        self.html.append("<" + " ".join(parts) + ">")
        if tag not in VOID_TAGS:
            self.open.append(tag)
            self.open_count[tag] += 1

    # ------------------------------------------------------------------ stopping
    def _cut(self, at_candidate: bool) -> None:
        """A mail program's own quote marker: everything from here on is the quoted message."""
        if at_candidate and self.candidate is not None and self.nodes == 0:
            self._restore(self.candidate)  # the separator line right above the marker goes too
        self.cut = True
        self.stopped = True

    def finish(self) -> None:
        self.close()
        if not self.stopped and self.heuristic is not None:
            snapshot, self.doubt = self.heuristic
            self._restore(snapshot)
            self.cut = True
        self._flush()
        while self.open:
            self.html.append(f"</{self.open.pop()}>")
        self.open_count.clear()


def _tidy(lines: list[str]) -> str:
    out: list[str] = []
    for line in lines:
        line = line.rstrip()
        if not line and (not out or not out[-1]):
            continue  # no leading blank lines, never two in a row
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def cut_quoted_text(text: str) -> tuple[str, bool, str | None]:
    """Remove a quoted earlier message from plain text.

    Returns the text, whether a quoted message was found, and the first line that was cut off when
    the place was found by wording that a person could have typed as well (the sender is then told
    what was left out). Lines marked as quoted (``>``) right below it settle the doubt.
    """
    found: tuple[int, bool] | None = None
    for pattern, weak in _TEXT_QUOTE_STARTS:
        match = pattern.search(text)
        if match and (found is None or match.start() < found[0]):
            found = (match.start(), weak)
    if found is None:
        return text, False, None
    first, weak = found
    doubt = None
    if weak:
        removed = text[first:].split("\n")
        below = [line for line in removed[1:] if line.strip()]
        if not (below and all(line.lstrip().startswith(">") for line in below[:3])):
            doubt = removed[0].strip()[:DOUBT_LENGTH] or None
    return text[:first].rstrip(), True, doubt


def text_to_html(text: str) -> str:
    return "<div>" + "<br>".join(escape(line, quote=False) for line in text.split("\n")) + "</div>"


def html_to_text(html: str) -> str:
    """Plain text of a whole HTML document, quoted messages included."""
    parser = _Extractor(cut_quotes=False)
    parser.feed(html or "")
    parser.finish()
    return _tidy(parser.lines)


def extract_reply(html: str, private: Callable[[str], bool] | None = None) -> ExtractedReply:
    """The part of an HTML reply above the quoted earlier message.

    ``private`` says which link targets must not be passed on (the words of such a link are kept).
    """
    parser = _Extractor(private=private)
    parser.feed(html or "")
    parser.finish()
    text = _tidy(parser.lines)
    if not parser.cut:
        # No marker and no header block in the HTML: look for the wording of a quoted message.
        new_text, found, doubt = cut_quoted_text(text)
        if found:
            # The HTML and its pictures cannot be split at that place, so only the text is used.
            return ExtractedReply(
                text=new_text, html=None, cut=True, own_text=bool(new_text.strip()), doubt=doubt
            )
    return ExtractedReply(
        text=text,
        html="".join(parser.html),
        images=parser.images,
        cut=parser.cut,
        cids=parser.cids,
        own_text=parser.own_chars > 0,
        doubt=parser.doubt,
    )


def extract_reply_plain(text: str) -> ExtractedReply:
    """Same for a plain-text email."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    new_text, cut, doubt = cut_quoted_text(_tidy(lines))
    return ExtractedReply(
        text=new_text, html=text_to_html(new_text), cut=cut, own_text=bool(new_text.strip()), doubt=doubt
    )
