from __future__ import annotations

import json

import httpx
import pytest
import respx
from app.whatsapp.admin import appsecret_proof
from app.whatsapp.send import WhatsAppClient, WhatsAppSendError, explain_error

from tests.conftest import make_settings

MESSAGES = "https://graph.facebook.com/v27.0/106540352242922/messages"
MEDIA = "https://graph.facebook.com/v27.0/106540352242922/media"
NUMBER = "106540352242922"
ACCEPTED = {"messaging_product": "whatsapp", "messages": [{"id": "wamid.OUT1"}]}


def error(code: int, message: str = "boom", details: str | None = None) -> dict:
    err: dict = {"message": message, "type": "OAuthException", "code": code}
    if details:
        err["error_data"] = {"details": details}
    return {"error": err}


@respx.mock
async def test_text_to_a_phone_number_quotes_the_customers_message(tmp_path):
    settings = make_settings(tmp_path)
    route = respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=ACCEPTED))
    async with httpx.AsyncClient() as http:
        wamid = await WhatsAppClient(http, settings).send_text(
            NUMBER, phone="16505551234", bsuid="US.1", body="Hello", reply_to="wamid.IN"
        )
    assert wamid == "wamid.OUT1"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer wa-token"
    assert request.url.params["appsecret_proof"] == appsecret_proof("wa-token", settings.wa_app_secret)
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "+16505551234",  # the phone number wins over the user id
        "context": {"message_id": "wamid.IN"},
        "type": "text",
        "text": {"preview_url": False, "body": "Hello"},
    }


@respx.mock
async def test_customer_without_phone_number_is_addressed_by_user_id(tmp_path):
    route = respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=ACCEPTED))
    async with httpx.AsyncClient() as http:
        client = WhatsAppClient(http, make_settings(tmp_path))
        await client.send_media(
            NUMBER,
            phone=None,
            bsuid="US.13491208655302741918",
            kind="document",
            media_id="55",
            filename="a.pdf",
        )
        body = json.loads(route.calls.last.request.content)
        assert "to" not in body and body["recipient"] == "US.13491208655302741918"
        assert body["type"] == "document" and body["document"] == {"id": "55", "filename": "a.pdf"}
        assert "context" not in body
        await client.send_media(
            NUMBER, phone="1650", bsuid=None, kind="image", media_id="56", filename="a.png"
        )
        assert json.loads(route.calls.last.request.content)["image"] == {
            "id": "56"
        }  # only documents carry a name
        with pytest.raises(WhatsAppSendError) as exc:
            await client.send_text(NUMBER, phone=None, bsuid=None, body="x")
        assert not exc.value.retriable and not exc.value.uncertain
        with pytest.raises(WhatsAppSendError):
            await client.send_text("12/../34", phone="1", bsuid=None, body="x")
    assert route.call_count == 2  # the two refused calls never reached WhatsApp


@respx.mock
async def test_failures_are_sorted_into_retriable_uncertain_and_permanent(tmp_path):
    async with httpx.AsyncClient() as http:
        client = WhatsAppClient(http, make_settings(tmp_path))

        async def send() -> WhatsAppSendError:
            with pytest.raises(WhatsAppSendError) as exc:
                await client.send_text(NUMBER, phone="1650", bsuid=None, body="x")
            return exc.value

        respx.post(MESSAGES).mock(
            return_value=httpx.Response(400, json=error(131047, "Re-engagement message"))
        )
        window = await send()
        assert (window.code, window.retriable, window.uncertain) == (131047, False, False)
        assert "More than 24 hours" in window.explanation

        respx.post(MESSAGES).mock(return_value=httpx.Response(400, json=error(131056, "pair rate limit")))
        pair = await send()
        assert pair.retriable and pair.retry_after == 10.0

        respx.post(MESSAGES).mock(
            return_value=httpx.Response(503, json=error(2, "Service down"), headers={"Retry-After": "7"})
        )
        outage = await send()  # Meta itself says it failed: safe to try again
        assert outage.retriable and not outage.uncertain and outage.retry_after == 7.0

        respx.post(MESSAGES).mock(return_value=httpx.Response(502, text="<html>Bad Gateway</html>"))
        gateway = await send()  # no word from Meta on what happened: it may have gone out
        assert gateway.uncertain and not gateway.retriable and gateway.code is None
        for code, title in ((1, "An unknown error occurred"), (131000, "Something went wrong")):
            respx.post(MESSAGES).mock(return_value=httpx.Response(500, json=error(code, title)))
            unknown = await send()  # Meta does not know what happened either
            assert unknown.uncertain and not unknown.retriable and unknown.code == code
        respx.post(MESSAGES).mock(
            return_value=httpx.Response(400, json=error(131000, "Something went wrong"))
        )
        refused = await send()  # "unknown error" never says whether it went out, whatever the status
        assert refused.uncertain and not refused.retriable and refused.code == 131000

        respx.post(MESSAGES).mock(side_effect=httpx.ConnectError("no route"))
        unreachable = await send()
        assert unreachable.retriable and not unreachable.uncertain  # never left: safe to try again

        respx.post(MESSAGES).mock(side_effect=httpx.ReadTimeout("slow"))
        silent = await send()
        assert silent.uncertain and not silent.retriable  # may have been sent: never tried again
        assert "may or may not have been sent" in silent.explanation

        respx.post(MESSAGES).mock(return_value=httpx.Response(200, json={"messages": []}))
        assert (await send()).uncertain

        respx.post(MESSAGES).mock(return_value=httpx.Response(401, json=error(190, "token expired")))
        token = await send()
        assert token.code == 190 and not token.retriable and "access token" in token.explanation


@respx.mock
async def test_upload_sends_the_file_and_can_always_be_repeated(tmp_path):
    path = tmp_path / "0.bin"
    path.write_bytes(b"%PDF-1.7 content")
    route = respx.post(MEDIA).mock(return_value=httpx.Response(200, json={"id": "900"}))
    async with httpx.AsyncClient() as http:
        client = WhatsAppClient(http, make_settings(tmp_path))
        assert await client.upload_media(NUMBER, path, "application/pdf", "release form.pdf") == "900"
        body = route.calls.last.request.content
        assert b'name="messaging_product"' in body and b"whatsapp" in body
        assert b'filename="release form.pdf"' in body and b"%PDF-1.7 content" in body
        assert b"application/pdf" in body
        respx.post(MEDIA).mock(side_effect=httpx.ReadTimeout("slow"))
        with pytest.raises(WhatsAppSendError) as exc:
            await client.upload_media(NUMBER, path, "application/pdf", "a.pdf")
        assert exc.value.retriable and not exc.value.uncertain
        respx.post(MEDIA).mock(return_value=httpx.Response(400, json=error(131053, "Media upload error")))
        with pytest.raises(WhatsAppSendError) as exc:
            await client.upload_media(NUMBER, path, "application/pdf", "a.pdf")
        assert exc.value.code == 131053 and not exc.value.retriable
        respx.post(MEDIA).mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(WhatsAppSendError) as exc:
            await client.upload_media(NUMBER, path, "application/pdf", "a.pdf")
        assert exc.value.retriable


async def test_missing_token_is_a_setup_error(tmp_path):
    settings = make_settings(tmp_path, wa_access_token="")
    async with httpx.AsyncClient() as http:
        with pytest.raises(WhatsAppSendError) as exc:
            await WhatsAppClient(http, settings).send_text(NUMBER, phone="1", bsuid=None, body="x")
    assert exc.value.code == 190 and not exc.value.retriable


@respx.mock
async def test_template_fills_the_body_variables_in_order(tmp_path):
    settings = make_settings(tmp_path)
    route = respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=ACCEPTED))
    async with httpx.AsyncClient() as http:
        wamid = await WhatsAppClient(http, settings).send_template(
            NUMBER,
            phone="+1 650-555-1234",
            name="vship_update",
            language="en_US",
            body_params=["Maria", "Your container has arrived at the port."],
        )
    assert wamid == "wamid.OUT1"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer wa-token"
    assert request.url.params["appsecret_proof"] == appsecret_proof("wa-token", settings.wa_app_secret)
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "+16505551234",  # digits only, with the plus WhatsApp expects
        "type": "template",
        "template": {
            "name": "vship_update",
            "language": {"code": "en_US"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": "Maria"},
                        {"type": "text", "text": "Your container has arrived at the port."},
                    ],
                }
            ],
        },
    }


@respx.mock
async def test_template_with_a_document_header_carries_the_file_first(tmp_path):
    route = respx.post(MESSAGES).mock(return_value=httpx.Response(200, json=ACCEPTED))
    async with httpx.AsyncClient() as http:
        client = WhatsAppClient(http, make_settings(tmp_path))

        async def components(**kwargs) -> list:
            await client.send_template(
                NUMBER, phone="16505551234", name="vship_document", language="en_US", **kwargs
            )
            body = json.loads(route.calls.last.request.content)
            assert "context" not in body and body["type"] == "template"
            return body["template"]["components"]

        file = {"id": "900", "filename": "invoice.pdf"}
        header = {"type": "header", "parameters": [{"type": "document", "document": file}]}
        values = [{"type": "text", "text": "Maria"}, {"type": "text", "text": "BK-1001"}]
        assert await components(body_params=["Maria", "BK-1001"], header_document=dict(file)) == [
            header,
            {"type": "body", "parameters": values},
        ]
        # a template without variables gets no body component at all
        assert await components(body_params=[], header_document=dict(file)) == [header]
        assert await components(body_params=[]) == []
        # a header without its uploaded file could only be refused: it is never sent
        with pytest.raises(WhatsAppSendError) as exc:
            await components(body_params=["Maria"], header_document={"id": "", "filename": "invoice.pdf"})
        assert not exc.value.retriable and not exc.value.uncertain
    assert route.call_count == 3


@respx.mock
async def test_template_failures_are_sorted_into_retriable_uncertain_and_permanent(tmp_path):
    async with httpx.AsyncClient() as http:
        client = WhatsAppClient(http, make_settings(tmp_path))

        async def send() -> WhatsAppSendError:
            with pytest.raises(WhatsAppSendError) as exc:
                await client.send_template(
                    NUMBER, phone="16505551234", name="vship_update", language="en_US", body_params=["A", "B"]
                )
            return exc.value

        # permanent: Meta refused the template message itself, so trying again cannot help
        for code, words in (
            (132018, "line breaks"),
            (132001, "language"),
            (132000, "number of values"),
            (132005, "too long"),
            (132015, "paused"),
            (132016, "permanently disabled"),
            (131008, "missing or empty"),
        ):
            respx.post(MESSAGES).mock(return_value=httpx.Response(400, json=error(code, "Template problem")))
            refused = await send()
            assert (refused.code, refused.retriable, refused.uncertain) == (code, False, False)
            assert words in refused.explanation

        # retriable: maintenance mode plainly says the request was turned away, whatever the status
        for status in (400, 503):
            respx.post(MESSAGES).mock(
                return_value=httpx.Response(
                    status, json=error(131057, "Business Account is in maintenance mode")
                )
            )
            maintenance = await send()
            assert (maintenance.code, maintenance.retriable, maintenance.uncertain) == (131057, True, False)
        respx.post(MESSAGES).mock(
            return_value=httpx.Response(429, text="Too Many Requests", headers={"Retry-After": "30"})
        )
        limited = await send()
        assert limited.retriable and not limited.uncertain and limited.retry_after == 30.0
        respx.post(MESSAGES).mock(side_effect=httpx.ConnectError("no route"))
        assert (await send()).retriable  # never left: safe to try again

        # uncertain: Meta's "unknown error" never says whether the message went out
        for status, code in ((400, 131000), (400, 1), (403, 1), (500, 1), (500, 131000)):
            respx.post(MESSAGES).mock(
                return_value=httpx.Response(status, json=error(code, "Something went wrong"))
            )
            unknown = await send()
            assert (unknown.code, unknown.retriable, unknown.uncertain) == (code, False, True)
            assert "may or may not have been sent" in unknown.explanation
        respx.post(MESSAGES).mock(side_effect=httpx.ReadTimeout("slow"))
        assert (await send()).uncertain
        respx.post(MESSAGES).mock(return_value=httpx.Response(200, json={"messaging_product": "whatsapp"}))
        assert (await send()).uncertain  # accepted, but no message id came back


@respx.mock
async def test_unknown_error_on_an_upload_is_simply_tried_again(tmp_path):
    path = tmp_path / "0.bin"
    path.write_bytes(b"%PDF-1.7 content")
    for code in (131000, 1):
        respx.post(MEDIA).mock(return_value=httpx.Response(400, json=error(code, "Something went wrong")))
        async with httpx.AsyncClient() as http:
            with pytest.raises(WhatsAppSendError) as exc:
                await WhatsAppClient(http, make_settings(tmp_path)).upload_media(
                    NUMBER, path, "application/pdf", "a.pdf"
                )
        # nothing reaches the customer on an upload, so repeating it is always safe
        assert exc.value.retriable and not exc.value.uncertain


def test_template_errors_are_explained_in_plain_words():
    assert "line breaks, tabs or more than four spaces" in explain_error(132018)
    assert "number of values" in explain_error(132000)
    assert "language" in explain_error(132001) and "approved" in explain_error(132001)
    assert "Shorten the message" in explain_error(132005)
    assert "policy" in explain_error(132007)
    assert "format" in explain_error(132012)
    assert "paused" in explain_error(132015)
    assert "permanently disabled" in explain_error(132016)
    assert "missing or empty" in explain_error(131008)
    assert "not valid" in explain_error(131009)
    assert "Marketing" in explain_error(131049) and "US numbers" in explain_error(131049)
    assert "stop receiving marketing messages" in explain_error(131050)
    assert "kind of message" in explain_error(131051)
    assert "maintenance mode" in explain_error(131057)
    assert "wrong category" in explain_error(131064)
    for code in (132000, 132001, 132005, 132007, 132012, 132015, 132016, 132018, 131008, 131009, 131049):
        # each has its own words; the generic text is only for codes nobody has explained yet
        assert not explain_error(code, "Title", "Details").startswith("WhatsApp reported an error")
    for code in (131050, 131051, 131057, 131064):
        assert not explain_error(code, "Title", "Details").startswith("WhatsApp reported an error")


def test_errors_are_explained_in_plain_words():
    assert "24 hours" in explain_error(131047)
    assert "Billing Hub" in explain_error(131042)
    assert "whatsapp_business_messaging" in explain_error(200)
    assert (
        explain_error(131999, "Odd thing", "Odd thing")
        == "WhatsApp reported an error: Odd thing (code 131999)."
    )
    assert explain_error(None) == "WhatsApp reported an error: no description given."
    assert (
        explain_error(5, "Title.", "More detail")
        == "WhatsApp reported an error: Title. More detail (code 5)."
    )
