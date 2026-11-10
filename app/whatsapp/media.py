"""Download inbound media from the WhatsApp Cloud API.

Two requests: ``GET /{version}/{media-id}`` returns a short-lived URL (valid ~5 minutes), then
``GET <url>`` with the same bearer token streams the bytes. Files are spooled to disk so a
100 MB document never has to live in memory twice.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import mimetypes
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from app.whatsapp.parser import MediaRef

log = logging.getLogger(__name__)

_EXTENSION_OVERRIDES = {
    "audio/ogg": ".ogg",
    "audio/ogg; codecs=opus": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
}


class MediaDownloadError(Exception):
    """Raised when media cannot be fetched. ``retriable`` tells the worker whether to retry."""

    def __init__(self, message: str, *, retriable: bool = True, status_code: int | None = None):
        super().__init__(message)
        self.retriable = retriable
        self.status_code = status_code


@dataclass(slots=True)
class DownloadedMedia:
    path: Path
    size: int
    mime_type: str
    filename: str
    sha256_hex: str
    integrity_verified: bool | None  # None when Meta provided no digest to compare against

    def cleanup(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - best effort
            log.warning("could not delete spooled media %s", self.path)


_UNSAFE_NAME_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f/\\:*?\"<>|]+")
_MEDIA_ID = re.compile(r"[0-9]{1,40}")


def safe_filename(name: str | None, fallback: str) -> str:
    """Return a header- and filesystem-safe filename: basename only, no control characters, bounded.

    Letters from any script are kept (the email package encodes them per RFC 2231); only path
    separators, control characters and characters Windows refuses in file names are replaced.
    """
    candidate = unicodedata.normalize("NFC", name or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidate = " ".join(_UNSAFE_NAME_CHARS.sub("_", candidate).split()).strip(" .")
    if not candidate:
        candidate = fallback
    if len(candidate) > 150:
        stem, dot, ext = candidate.rpartition(".")
        if dot and len(ext) <= 10:
            candidate = stem[: 150 - len(ext) - 1] + "." + ext
        else:
            candidate = candidate[:150]
    return candidate


def extension_for(mime_type: str | None) -> str:
    if not mime_type:
        return ".bin"
    base = mime_type.split(";")[0].strip().lower()
    if mime_type.lower() in _EXTENSION_OVERRIDES:
        return _EXTENSION_OVERRIDES[mime_type.lower()]
    if base in _EXTENSION_OVERRIDES:
        return _EXTENSION_OVERRIDES[base]
    return mimetypes.guess_extension(base) or ".bin"


def default_filename(media: MediaRef, wamid: str, mime_type: str | None) -> str:
    if media.filename:
        return safe_filename(media.filename, f"{media.kind}{extension_for(mime_type)}")
    stem = re.sub(r"[^A-Za-z0-9]+", "", wamid)[-24:] or media.id
    kind = "voice-note" if media.kind == "audio" and media.voice else media.kind
    return f"whatsapp-{kind}-{stem}{extension_for(mime_type)}"


def _digest_matches(expected: str | None, sha256_hex: str) -> bool | None:
    """Compare Meta's digest (base64 in webhooks, hex in the media endpoint) with ours."""
    if not expected:
        return None
    candidate = expected.strip()
    if re.fullmatch(r"[0-9a-fA-F]{64}", candidate):
        return candidate.lower() == sha256_hex
    try:
        decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != 32:
        return None
    return decoded.hex() == sha256_hex


def _write_and_hash(fh, hasher, chunk: bytes) -> None:
    fh.write(chunk)
    hasher.update(chunk)


def _graph_error_text(response: httpx.Response) -> str:
    try:
        err = response.json().get("error", {})
    except (ValueError, AttributeError):
        return response.text[:300]
    return (
        f"{err.get('message')} (type={err.get('type')}, code={err.get('code')},"
        f" subcode={err.get('error_subcode')})"
    )


async def download_media(
    client: httpx.AsyncClient,
    *,
    media: MediaRef,
    wamid: str,
    access_token: str,
    graph_base_url: str,
    api_version: str,
    dest_dir: Path,
    request_timeout: float = 120.0,
) -> DownloadedMedia:
    """Fetch ``media`` to ``dest_dir`` and return its metadata. Raises :class:`MediaDownloadError`."""
    if not access_token:
        raise MediaDownloadError("WA_ACCESS_TOKEN is not configured; cannot download media", retriable=True)
    if not _MEDIA_ID.fullmatch(media.id):
        # Meta media ids are numeric; anything else would be interpolated into an authenticated URL
        raise MediaDownloadError(f"refusing malformed media id {media.id[:40]!r}", retriable=False)
    await asyncio.to_thread(dest_dir.mkdir, parents=True, exist_ok=True)
    timeout = request_timeout
    headers = {"Authorization": f"Bearer {access_token}"}
    info_url = f"{graph_base_url.rstrip('/')}/{api_version}/{quote(media.id, safe='')}"

    try:
        info_resp = await client.get(info_url, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise MediaDownloadError(f"media info request failed: {exc!r}") from exc
    if info_resp.status_code != 200:
        retriable = info_resp.status_code >= 500 or info_resp.status_code == 429
        raise MediaDownloadError(
            f"media info returned HTTP {info_resp.status_code}: {_graph_error_text(info_resp)}",
            retriable=retriable or info_resp.status_code in (400, 401, 403),
            status_code=info_resp.status_code,
        )
    try:
        info = info_resp.json()
    except ValueError as exc:
        raise MediaDownloadError("media info response was not JSON") from exc
    url = info.get("url")
    if not url:
        raise MediaDownloadError(f"media info had no url: {str(info)[:200]}")
    mime_type = (info.get("mime_type") or media.mime_type or "application/octet-stream").strip()
    expected_size = info.get("file_size")

    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", media.id)
    target = dest_dir / f"{safe_id}.part"
    hasher = hashlib.sha256()
    size = 0
    try:
        async with client.stream("GET", url, headers=headers, timeout=timeout, follow_redirects=True) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:300]
                retriable = resp.status_code >= 500 or resp.status_code == 429
                raise MediaDownloadError(
                    f"media download returned HTTP {resp.status_code}: {body!r}",
                    retriable=retriable or resp.status_code in (400, 401, 403, 404),
                    status_code=resp.status_code,
                )
            with target.open("wb") as fh:
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    # disk writes and hashing of a 100 MB file must not stall the event loop
                    await asyncio.to_thread(_write_and_hash, fh, hasher, chunk)
                    size += len(chunk)
    except httpx.HTTPError as exc:
        target.unlink(missing_ok=True)
        raise MediaDownloadError(f"media download failed: {exc!r}") from exc
    except MediaDownloadError:
        target.unlink(missing_ok=True)
        raise

    if isinstance(expected_size, int) and expected_size > 0 and size != expected_size:
        target.unlink(missing_ok=True)
        raise MediaDownloadError(f"media size mismatch: expected {expected_size} bytes, got {size}")

    sha256_hex = hasher.hexdigest()
    verified = _digest_matches(media.sha256, sha256_hex)
    if verified is None:
        verified = _digest_matches(info.get("sha256"), sha256_hex)
    if verified is False:
        log.warning("sha256 mismatch for media %s (wamid %s); attaching anyway", media.id, wamid)

    filename = default_filename(media, wamid, mime_type)
    # the spool path never uses the sender's file name; the name only travels in the email headers
    final = dest_dir / f"{safe_id}{extension_for(mime_type)}"
    target.replace(final)
    return DownloadedMedia(
        path=final,
        size=size,
        mime_type=mime_type,
        filename=filename,
        sha256_hex=sha256_hex,
        integrity_verified=verified,
    )
