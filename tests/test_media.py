from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
import respx
from app.whatsapp.media import (
    MediaDownloadError,
    default_filename,
    download_media,
    extension_for,
    safe_filename,
)
from app.whatsapp.parser import MediaRef

GRAPH = "https://graph.facebook.com"
LOOKASIDE = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=123&ext=456&hash=abc"


def _ref(**kw) -> MediaRef:
    base = {"kind": "image", "id": "1385223789055226", "mime_type": "image/jpeg", "sha256": None}
    base.update(kw)
    return MediaRef(**base)


@respx.mock
async def test_download_verifies_digest_and_size(tmp_path):
    data = b"\xff\xd8" + b"x" * 5000
    digest = hashlib.sha256(data).digest()
    info = respx.get(f"{GRAPH}/v27.0/1385223789055226").mock(
        return_value=httpx.Response(
            200,
            json={
                "url": LOOKASIDE,
                "mime_type": "image/jpeg",
                "sha256": digest.hex(),
                "file_size": len(data),
                "id": "1385223789055226",
            },
        )
    )
    blob = respx.get(LOOKASIDE).mock(return_value=httpx.Response(200, content=data))
    async with httpx.AsyncClient() as http:
        media = await download_media(
            http,
            media=_ref(sha256=base64.b64encode(digest).decode()),
            wamid="wamid.ABC",
            access_token="tok",
            graph_base_url=GRAPH,
            api_version="v27.0",
            dest_dir=tmp_path / "spool",
        )
    assert info.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert "appsecret_proof" not in info.calls[0].request.url.params  # no app secret given
    assert blob.calls[0].request.headers["Authorization"] == "Bearer tok"
    assert media.size == len(data)
    assert media.integrity_verified is True
    assert media.filename == "whatsapp-image-wamidABC.jpg"
    assert media.path.read_bytes() == data
    media.cleanup()
    assert not media.path.exists()


@respx.mock
async def test_download_size_mismatch_is_retriable(tmp_path):
    respx.get(f"{GRAPH}/v27.0/1385223789055226").mock(
        return_value=httpx.Response(200, json={"url": LOOKASIDE, "mime_type": "image/jpeg", "file_size": 999})
    )
    respx.get(LOOKASIDE).mock(return_value=httpx.Response(200, content=b"short"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(MediaDownloadError) as exc:
            await download_media(
                http,
                media=_ref(),
                wamid="w",
                access_token="tok",
                graph_base_url=GRAPH,
                api_version="v27.0",
                dest_dir=tmp_path,
            )
    assert exc.value.retriable is True
    assert list(tmp_path.glob("*.part")) == []


@respx.mock
async def test_download_graph_error_and_missing_token(tmp_path):
    respx.get(f"{GRAPH}/v27.0/1385223789055226").mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {"message": "Unsupported get request", "type": "GraphMethodException", "code": 100}
            },
        )
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(MediaDownloadError) as exc:
            await download_media(
                http,
                media=_ref(),
                wamid="w",
                access_token="tok",
                graph_base_url=GRAPH,
                api_version="v27.0",
                dest_dir=tmp_path,
            )
        assert exc.value.status_code == 400
        assert "Unsupported get request" in str(exc.value)
        with pytest.raises(MediaDownloadError):
            await download_media(
                http,
                media=_ref(),
                wamid="w",
                access_token="",
                graph_base_url=GRAPH,
                api_version="v27.0",
                dest_dir=tmp_path,
            )


def test_filename_helpers():
    assert safe_filename("../../etc/passwd", "x") == "passwd"
    assert safe_filename("Résumé (final).pdf", "x") == "Résumé (final).pdf"
    assert safe_filename("报价单A.pdf", "x") == "报价单A.pdf"
    assert safe_filename("bad\x00name\r\n<>:|.pdf", "x") == "bad_name_.pdf"
    assert safe_filename("C:\\Users\\me\\quote.xlsx", "x") == "quote.xlsx"
    assert safe_filename("", "fallback.bin") == "fallback.bin"
    assert len(safe_filename("a" * 300 + ".pdf", "x")) <= 150
    assert extension_for("audio/ogg; codecs=opus") == ".ogg"
    assert extension_for("application/pdf") == ".pdf"
    assert extension_for(None) == ".bin"
    assert (
        default_filename(_ref(kind="audio", voice=True), "wamid.X1", "audio/ogg")
        == "whatsapp-voice-note-wamidX1.ogg"
    )
    assert (
        default_filename(_ref(kind="document", filename="Q3 report.xlsx"), "w", "application/octet-stream")
        == "Q3 report.xlsx"
    )
