from __future__ import annotations

import json

import httpx
import pytest
import respx
from app.mail.transports import OAuthTokenProvider, SendError
from app.relay.mailbox import IMMUTABLE_IDS, GraphMailbox, MailAttachment, valid_address

from tests.conftest import make_settings

TOKEN_URL = "https://login.microsoftonline.com/tenant-id/oauth2/v2.0/token"
BOX = "https://graph.microsoft.com/v1.0/users/whatsapp@example.com"
MSG = "AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AZ="
MSG_URL = f"{BOX}/messages/AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AZ%3D"
FOLDER = "WhatsApp updates - handled"


def relay_settings(tmp_path, **overrides):
    """Settings for reading the relay mailbox through Graph (the mailbox code does not need RELAY_ENABLED)."""
    base = {
        "mail_transport": "graph",
        "graph_sender": "whatsapp@example.com",
        "oauth_tenant_id": "tenant-id",
        "oauth_client_id": "client-id",
        "oauth_client_secret": "client-secret",
    }
    base.update(overrides)
    return make_settings(tmp_path, **base)


def token_route():
    return respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
    )


def mailbox_for(settings, http) -> GraphMailbox:
    return GraphMailbox(http, settings, OAuthTokenProvider(http, settings), settings.relay_mailbox_address)


def test_valid_address():
    assert valid_address(" Chris@Example.com ") == "chris@example.com"
    assert valid_address("O'Brien@Example.com") == "o'brien@example.com"
    for bad in (
        None,
        "",
        "chris",
        "a b@example.com",
        "x@y",
        "a@b.com/../users",
        "<a@b.com>",
        "a@b.com,c@d.com",
    ):
        assert valid_address(bad) is None


@respx.mock
async def test_inbox_listing_and_message_reading(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    inbox = respx.get(f"{BOX}/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": MSG, "receivedDateTime": "2026-11-18T15:00:00Z"}, {}]}
        )
    )
    message = respx.get(MSG_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "expand not supported"}}),
            httpx.Response(200, json={"id": MSG, "subject": "WA | 650-555-1234 | B-1001"}),
            httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}}),
        ]
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        listed = await box.list_inbox("2026-11-18T00:00:00Z")
        assert [m["id"] for m in listed] == [MSG]
        params = inbox.calls.last.request.url.params
        assert params["$filter"] == "receivedDateTime ge 2026-11-18T00:00:00Z"
        assert params["$orderby"] == "receivedDateTime desc" and params["$top"] == "50"
        assert params["$select"] == "id,receivedDateTime,from,subject"
        assert inbox.calls.last.request.headers["Prefer"] == 'IdType="ImmutableId"'
        # the message-class lookup is dropped when Graph refuses it
        assert (await box.get_message(MSG)) == {"id": MSG, "subject": "WA | 650-555-1234 | B-1001"}
        first, second = message.calls[0].request, message.calls[1].request
        assert (
            "singleValueExtendedProperties" in first.url.params["$expand"]
            and "$expand" not in second.url.params
        )
        assert "internetMessageHeaders" in second.url.params["$select"]
        assert "sender" in second.url.params["$select"].split(",")  # From == Sender is checked
        assert "replyTo" in second.url.params["$select"]
        assert second.headers["Prefer"] == 'IdType="ImmutableId", outlook.body-content-type="html"'
        assert await box.get_message(MSG) is None  # deleted or recalled in the meantime


@respx.mock
async def test_an_email_can_be_read_without_its_text(tmp_path):
    """Enough to decide who sent it, without downloading a text that may be megabytes long."""
    settings = relay_settings(tmp_path)
    token_route()
    message = respx.get(MSG_URL).mock(
        side_effect=[
            httpx.Response(200, json={"id": MSG, "subject": "WA | 650-555-1234"}),
            httpx.Response(400, json={"error": {"message": "expand not supported"}}),
            httpx.Response(200, json={"id": MSG}),
            httpx.Response(404, json={"error": {"code": "ErrorItemNotFound"}}),
        ]
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        assert await box.get_message(MSG, with_body=False) == {"id": MSG, "subject": "WA | 650-555-1234"}
        request = message.calls.last.request
        fields = request.url.params["$select"].split(",")
        assert "body" not in fields
        for needed in ("subject", "from", "sender", "receivedDateTime", "isDraft", "internetMessageHeaders"):
            assert needed in fields  # everything the sender checks need
        assert "String 0x001A" in request.url.params["$expand"]  # the message class, for automatic mail
        assert request.headers["Prefer"] == IMMUTABLE_IDS  # no body, so no body format either
        assert await box.get_message(MSG, with_body=False) == {"id": MSG}  # without the message class
        assert "body" not in message.calls.last.request.url.params["$select"].split(",")
        assert await box.get_message(MSG, with_body=False) is None


@respx.mock
async def test_attachments_are_listed_without_their_content(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    page_two = "https://graph.microsoft.com/v1.0/next-page"
    listing = respx.get(f"{MSG_URL}/attachments").mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "id": "att1",
                        "name": "form.pdf",
                        "contentType": "application/pdf",
                        "size": 1200,
                        "isInline": False,
                    },
                    {
                        "@odata.type": "#microsoft.graph.fileAttachment",
                        "id": "att2",
                        "name": "image001.png",
                        "contentType": "image/png",
                        "size": 300,
                        "isInline": True,
                        "contentId": "image001.png@01DC",
                    },
                ],
                "@odata.nextLink": page_two,
            },
        )
    )
    respx.get(page_two).mock(
        return_value=httpx.Response(
            200,
            json={
                "value": [
                    {
                        "@odata.type": "#microsoft.graph.itemAttachment",
                        "id": "att3",
                        "name": "FW: old",
                        "size": 9,
                    },
                    {"@odata.type": "#microsoft.graph.referenceAttachment", "id": "att4", "name": "big.xlsx"},
                    {"name": "no id"},
                ]
            },
        )
    )
    async with httpx.AsyncClient() as http:
        found = await mailbox_for(settings, http).list_attachments(MSG)
    assert listing.calls.last.request.url.params["$select"] == (
        "id,name,contentType,size,isInline,microsoft.graph.fileAttachment/contentId"
    )
    assert [(a.id, a.kind, a.is_inline, a.content_id) for a in found] == [
        ("att1", "file", False, None),
        ("att2", "file", True, "image001.png@01DC"),
        ("att3", "item", False, None),
        ("att4", "reference", False, None),
    ]
    assert found[2].content_type == "application/octet-stream" and found[3].size == 0


@pytest.mark.parametrize(
    "link",
    [
        "https://attacker.example/collect",
        # looks like Graph, but the host is attacker.example's
        "https://graph.microsoft.com.attacker.example/v1.0/users/whatsapp@example.com/messages/x/attachments",
        "http://graph.microsoft.com/v1.0/next-page",  # the token would travel unencrypted
    ],
)
@respx.mock
async def test_attachment_paging_never_takes_the_access_token_anywhere_but_graph(tmp_path, caplog, link):
    settings = relay_settings(tmp_path)
    token_route()
    first = {"@odata.type": "#microsoft.graph.fileAttachment", "id": "att1", "name": "form.pdf", "size": 4}
    respx.get(f"{MSG_URL}/attachments").mock(
        return_value=httpx.Response(200, json={"value": [first], "@odata.nextLink": link})
    )
    elsewhere = respx.route().mock(  # anything else that is asked for
        return_value=httpx.Response(200, json={"value": [{"id": "att2", "name": "more.pdf"}]})
    )
    async with httpx.AsyncClient() as http:
        found = await mailbox_for(settings, http).list_attachments(MSG)
    assert [attachment.id for attachment in found] == ["att1"]
    assert not elsewhere.called  # nothing, so no "Authorization: Bearer ..." either, went anywhere else
    assert "next page of attachments is on another web address" in caplog.text
    assert "Bearer" not in caplog.text and MSG not in caplog.text


@respx.mock
async def test_inbox_listing_follows_the_pages_but_not_for_ever(tmp_path):
    """Emails that stay in the Inbox must not push newer ones out of sight."""
    settings = relay_settings(tmp_path)
    token_route()
    url = f"{BOX}/mailFolders/inbox/messages"
    asked: list[str] = []

    def listing(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        asked.append(str(request.url))
        body = {
            "value": [{"id": f"MSG{page}-{n}", "receivedDateTime": "2026-11-18T15:00:00Z"} for n in range(2)]
        }
        if page < 30:
            body["@odata.nextLink"] = f"{url}?page={page + 1}"
        return httpx.Response(200, json=body)

    respx.get(url).mock(side_effect=listing)
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        assert len(await box.list_inbox("2026-11-18T00:00:00Z")) == 20  # ten pages in one round
        assert asked[1] == f"{url}?page=2" and len(asked) == 10
        assert len(await box.list_inbox("2026-11-18T00:00:00Z", pages=1)) == 2
    # a link that leads somewhere else is not followed (the access token goes with every request)
    respx.get(url).mock(
        return_value=httpx.Response(
            200, json={"value": [{"id": "A"}], "@odata.nextLink": "https://elsewhere.example/steal"}
        )
    )
    async with httpx.AsyncClient() as http:
        assert [m["id"] for m in await mailbox_for(settings, http).list_inbox("2026-11-18T00:00:00Z")] == [
            "A"
        ]


@respx.mock
async def test_attachment_content_ids_are_read_one_by_one_when_graph_cannot_list_them(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    values = [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "a1",
            "name": "x.png",
            "size": 10,
            "isInline": True,
        },
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "a2",
            "name": "y.pdf",
            "size": 10,
            "isInline": False,
        },
        # a picture of the email text that Graph does not mark as such: it is asked about all the same
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "a3",
            "name": "Outlook-ielmn2d3.png",
            "contentType": "image/png",
            "size": 10,
            "isInline": False,
        },
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "a4",
            "name": "huge.png",
            "contentType": "image/png",
            "size": 9_000_000,
            "isInline": False,
        },
    ]
    listing = respx.get(f"{MSG_URL}/attachments").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "Could not find a property named contentId"}}),
            httpx.Response(200, json={"value": values}),
            httpx.Response(200, json={"value": values}),
        ]
    )
    one = respx.get(f"{MSG_URL}/attachments/a1").mock(
        return_value=httpx.Response(200, json={"id": "a1", "contentId": "cid-1", "contentBytes": "AAAA"})
    )
    other = respx.get(f"{MSG_URL}/attachments/a3").mock(
        return_value=httpx.Response(200, json={"id": "a3", "contentId": "cid-3", "contentBytes": "AAAA"})
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        found = await box.list_attachments(MSG)
        assert [(a.id, a.content_id) for a in found] == [
            ("a1", "cid-1"),
            ("a2", None),
            ("a3", "cid-3"),
            ("a4", None),  # too large to read just for its id
        ]
        await box.list_attachments(MSG)  # remembers that the longer field list does not work
    assert [c.request.url.params["$select"] for c in listing.calls] == [
        "id,name,contentType,size,isInline,microsoft.graph.fileAttachment/contentId",
        "id,name,contentType,size,isInline",
        "id,name,contentType,size,isInline",
    ]
    assert one.call_count == 2 and other.call_count == 2


@respx.mock
async def test_download_move_and_folders(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    attachment = MailAttachment("att/1", "form.pdf", "application/pdf", 5, False, None, "file")
    respx.get(f"{MSG_URL}/attachments/att%2F1/$value").mock(
        side_effect=[httpx.Response(200, content=b"%PDF-data"), httpx.Response(503, text="busy")]
    )
    folders = respx.get(f"{BOX}/mailFolders").mock(
        side_effect=[
            httpx.Response(200, json={"value": []}),
            httpx.Response(200, json={"value": [{"id": "F2"}]}),
        ]
    )
    created = respx.post(f"{BOX}/mailFolders").mock(return_value=httpx.Response(201, json={"id": "F1"}))
    move = respx.post(f"{MSG_URL}/move").mock(
        side_effect=[
            httpx.Response(201, json={"id": MSG}),
            httpx.Response(
                404, json={"error": {"code": "ErrorItemNotFound", "message": "The item was not found"}}
            ),
            httpx.Response(429, text="slow down", headers={"Retry-After": "12"}),
        ]
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        dest = tmp_path / "0.bin"
        assert await box.download_attachment(MSG, attachment, dest) == 9 and dest.read_bytes() == b"%PDF-data"
        with pytest.raises(SendError) as exc:
            await box.download_attachment(MSG, attachment, dest)
        assert exc.value.retriable and not dest.exists()

        await box.move(MSG, FOLDER)  # the folder is created on first use
        assert folders.calls[0].request.url.params["$filter"] == f"displayName eq '{FOLDER}'"
        assert json.loads(created.calls.last.request.content) == {"displayName": FOLDER}
        assert json.loads(move.calls.last.request.content) == {"destinationId": "F1"}
        await box.move(MSG, FOLDER)  # already gone: counts as filed
        assert created.call_count == 1 and folders.call_count == 1  # the folder id is remembered
        with pytest.raises(SendError) as exc:
            await box.move(MSG, "VShip's updates")
        assert exc.value.retriable and exc.value.retry_after == 12.0
        assert folders.calls.last.request.url.params["$filter"] == "displayName eq 'VShip''s updates'"


@respx.mock
async def test_a_folder_that_was_deleted_is_looked_up_again(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    folders = respx.get(f"{BOX}/mailFolders").mock(
        side_effect=[
            httpx.Response(200, json={"value": [{"id": "OLD"}]}),
            httpx.Response(200, json={"value": [{"id": "NEW"}]}),
        ]
    )
    gone = {"error": {"code": "ErrorItemNotFound", "message": "The destination folder could not be found."}}
    move = respx.post(f"{MSG_URL}/move").mock(
        side_effect=[httpx.Response(404, json=gone), httpx.Response(201, json={"id": MSG})]
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        with pytest.raises(SendError) as exc:
            await box.move(MSG, FOLDER)  # not "the email is gone": the worker tries again
        assert exc.value.retriable and exc.value.status_code == 404
        await box.move(MSG, FOLDER)
    assert [json.loads(c.request.content) for c in move.calls] == [
        {"destinationId": "OLD"},
        {"destinationId": "NEW"},
    ]
    assert folders.call_count == 2


@respx.mock
async def test_a_folder_created_in_the_meantime_is_found_and_other_refusals_are_errors(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    respx.get(f"{BOX}/mailFolders").mock(
        side_effect=[
            httpx.Response(200, json={"value": []}),
            httpx.Response(200, json={"value": [{"id": "F9", "displayName": FOLDER}]}),
            httpx.Response(200, json={"value": []}),
            httpx.Response(200, json={"value": []}),
            httpx.Response(200, json={"value": []}),
        ]
    )
    respx.post(f"{BOX}/mailFolders").mock(
        side_effect=[
            httpx.Response(409, json={"error": {"code": "ErrorFolderExists"}}),
            httpx.Response(409, json={"error": {"code": "ErrorFolderExists"}}),
            httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}),
        ]
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        assert await box.folder_id(FOLDER) == "F9"  # someone else created it a moment ago
        with pytest.raises(SendError) as exc:
            await box.folder_id("Lost")  # "exists", yet not to be found
        assert "could not find or create" in str(exc.value)
        with pytest.raises(SendError) as exc:
            await box.folder_id("Forbidden")
        assert exc.value.status_code == 403


@respx.mock
async def test_find_by_subject_searches_every_folder(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    search = respx.get(f"{BOX}/messages").mock(
        side_effect=[
            httpx.Response(200, json={"value": [{"id": "A"}, {"id": ""}, {}, "x", {"id": "B"}]}),
            httpx.Response(503, text="busy"),
        ]
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        assert await box.find_by_subject("WA | 650-555-1234 | O'Brien") == ["A", "B"]
        params = search.calls.last.request.url.params
        assert params["$filter"] == "subject eq 'WA | 650-555-1234 | O''Brien'"
        assert params["$select"] == "id" and params["$top"] == "10"
        with pytest.raises(SendError) as exc:
            await box.find_by_subject("anything")
        assert exc.value.retriable


@respx.mock
async def test_graph_failures_become_send_errors_the_worker_understands(tmp_path):
    settings = relay_settings(tmp_path)
    token_route()
    respx.get(MSG_URL).mock(
        side_effect=[
            httpx.Response(503, text="busy", headers={"Retry-After": "7"}),
            httpx.ConnectError("down"),
            httpx.Response(400, json={"error": {"code": "BadRequest"}}),  # the $expand is dropped ...
            httpx.Response(400, json={"error": {"code": "BadRequest"}}),  # ... and the plain read fails too
        ]
    )
    respx.get(f"{BOX}/mailFolders/inbox/messages").mock(
        return_value=httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}})
    )
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        with pytest.raises(SendError) as exc:
            await box.get_message(MSG)
        assert exc.value.retriable and exc.value.status_code == 503 and exc.value.retry_after == 7.0
        with pytest.raises(SendError) as exc:
            await box.get_message(MSG)
        assert exc.value.retriable and exc.value.status_code is None  # never reached Graph
        with pytest.raises(SendError) as exc:
            await box.get_message(MSG)
        assert not exc.value.retriable and exc.value.status_code == 400
        # the app may not read the mailbox (yet): tried again, so the outage alert can tell someone
        with pytest.raises(SendError) as exc:
            await box.list_inbox("2026-11-18T00:00:00Z")
        assert exc.value.retriable and exc.value.status_code == 403


@respx.mock
async def test_a_rejected_token_is_replaced_once(tmp_path):
    settings = relay_settings(tmp_path)
    tokens = token_route()
    respx.get(f"{BOX}/mailFolders/inbox/messages").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"value": []})]
    )
    async with httpx.AsyncClient() as http:
        assert await mailbox_for(settings, http).list_inbox("2026-11-18T00:00:00Z") == []
    assert tokens.call_count == 2


@respx.mock
async def test_every_graph_request_asks_for_immutable_ids(tmp_path):
    """The queue knows an email by its Graph id; only an immutable id stays the same when it is moved."""
    settings = relay_settings(tmp_path)
    token_route()
    inbox_url = f"{BOX}/mailFolders/inbox/messages"
    respx.get(inbox_url).mock(
        side_effect=[
            httpx.Response(200, json={"value": [{"id": "M1"}], "@odata.nextLink": f"{inbox_url}?$skip=50"}),
            httpx.Response(200, json={"value": [{"id": "M2"}]}),
        ]
    )
    respx.get(f"{BOX}/messages").mock(return_value=httpx.Response(200, json={"value": [{"id": MSG}]}))
    respx.get(MSG_URL).mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "expand not supported"}}),
            httpx.Response(200, json={"id": MSG}),
        ]
    )
    page_two = "https://graph.microsoft.com/v1.0/next-page"
    attachments = [
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a1", "name": "x.png", "isInline": True},
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "a2", "name": "y.pdf", "size": 4},
    ]
    respx.get(f"{MSG_URL}/attachments").mock(
        side_effect=[
            httpx.Response(400, json={"error": {"message": "Could not find a property named contentId"}}),
            httpx.Response(200, json={"value": attachments, "@odata.nextLink": page_two}),
        ]
    )
    respx.get(page_two).mock(return_value=httpx.Response(200, json={"value": []}))
    respx.get(f"{MSG_URL}/attachments/a1").mock(return_value=httpx.Response(200, json={"contentId": "cid-1"}))
    respx.get(f"{MSG_URL}/attachments/a2/$value").mock(return_value=httpx.Response(200, content=b"%PDF"))
    respx.get(f"{BOX}/mailFolders").mock(return_value=httpx.Response(200, json={"value": []}))
    respx.post(f"{BOX}/mailFolders").mock(return_value=httpx.Response(201, json={"id": "F1"}))
    respx.post(f"{MSG_URL}/move").mock(return_value=httpx.Response(201, json={"id": MSG}))
    async with httpx.AsyncClient() as http:
        box = mailbox_for(settings, http)
        assert [m["id"] for m in await box.list_inbox("2026-11-18T00:00:00Z")] == ["M1", "M2"]
        assert await box.find_by_subject("WA | 650-555-1234") == [MSG]
        assert await box.get_message(MSG) == {"id": MSG}
        found = await box.list_attachments(MSG)
        assert [(a.id, a.content_id) for a in found] == [("a1", "cid-1"), ("a2", None)]
        assert await box.download_attachment(MSG, found[1], tmp_path / "a2.bin") == 4
        await box.move(MSG, FOLDER)
    graph = [call.request for call in respx.calls if call.request.url.host == "graph.microsoft.com"]
    endpoints = {(request.method, request.url.path) for request in graph}
    message_path = "/v1.0/users/whatsapp@example.com/messages/AAkALgAAAAAAHYQDEapmEc2byACqAC-EWg0AZ="
    assert endpoints == {
        ("GET", "/v1.0/users/whatsapp@example.com/mailFolders/inbox/messages"),
        ("GET", "/v1.0/users/whatsapp@example.com/messages"),
        ("GET", message_path),
        ("GET", message_path + "/attachments"),
        ("GET", "/v1.0/next-page"),
        ("GET", message_path + "/attachments/a1"),
        ("GET", message_path + "/attachments/a2/$value"),
        ("GET", "/v1.0/users/whatsapp@example.com/mailFolders"),
        ("POST", "/v1.0/users/whatsapp@example.com/mailFolders"),
        ("POST", message_path + "/move"),
    }
    assert len(graph) == 13  # every page, retry and look-up counted
    for request in graph:
        assert IMMUTABLE_IDS in request.headers["Prefer"], f"{request.method} {request.url}"
        assert request.headers["Authorization"] == "Bearer T"
