from __future__ import annotations

import httpx
import pytest
import respx
from app.relay.templates import (
    MAX_PAGES,
    TemplateGuard,
    TemplateInfo,
    TemplateSpec,
    TemplateValueMissing,
    build_body_params,
    render_body,
    specs_from_settings,
)
from app.whatsapp.admin import appsecret_proof
from app.whatsapp.send import WhatsAppSendError

from tests.conftest import FakeClock, make_settings

WABA = "102290129340398"
TEMPLATES = f"https://graph.facebook.com/v27.0/{WABA}/message_templates"
BODY = (
    "Hello {{1}}, VShip has an update:\n{{2}}\nPlease either reply to this message or send us an email at"
    " Elizabeth@vship2000.com if you have any Questions"
)
UPDATE = TemplateSpec("vship_update", "en_US", ("name", "message"), "none")
DOCUMENT = TemplateSpec("vship_document", "en_US", ("name", "filename"), "document")
PROOF = appsecret_proof("wa-token", "app-secret-app-secret")


def template(
    name: str = "vship_update",
    *,
    language: str = "en_US",
    status: str = "APPROVED",
    category: str = "UTILITY",
    body: str = BODY,
    header: str | None = None,
) -> dict:
    """One entry of Meta's template list, shaped like the Graph API answer."""
    components: list[dict] = []
    if header:
        components.append({"type": "HEADER", "format": header})
    components.append({"type": "BODY", "text": body, "example": {"body_text": [["Maria", "It arrived."]]}})
    return {
        "name": name,
        "language": language,
        "status": status,
        "category": category,
        "components": components,
        "id": "1387372356726668",
    }


def page(*entries: dict, after: str | None = None) -> dict:
    body: dict = {"data": list(entries)}
    if after:
        body["paging"] = {
            "cursors": {"before": "BEFORE", "after": after},
            "next": f"{TEMPLATES}?name=vship_update&limit=100&after={after}&access_token=wa-token",
        }
    return body


def graph_error(code: int, message: str, subcode: int | None = None) -> dict:
    err: dict = {"message": message, "type": "OAuthException", "code": code, "fbtrace_id": "Az1"}
    if subcode is not None:
        err["error_subcode"] = subcode
    return {"error": err}


def guard(tmp_path, http: httpx.AsyncClient, clock: FakeClock | None = None, **overrides) -> TemplateGuard:
    settings = make_settings(tmp_path, **{"wa_waba_id": WABA, **overrides})
    return TemplateGuard(http, settings, clock or FakeClock())


@respx.mock
async def test_an_approved_utility_template_with_matching_variables_can_be_sent(tmp_path):
    route = respx.get(TEMPLATES).mock(return_value=httpx.Response(200, json=page(template())))
    async with httpx.AsyncClient() as http:
        info = await guard(tmp_path, http).check(UPDATE)
    assert info == TemplateInfo(
        ok=True, status="APPROVED", category="UTILITY", body_text=BODY, placeholders=2, problem=None
    )
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer wa-token"
    assert dict(request.url.params) == {
        "name": "vship_update",
        "fields": "name,status,category,language,components",
        "limit": "100",
        "appsecret_proof": PROOF,
    }


@respx.mock
async def test_only_the_exact_name_and_language_count(tmp_path):
    route = respx.get(TEMPLATES).mock(
        return_value=httpx.Response(
            200,
            json=page(
                template("vship_update_old", status="REJECTED"),  # Meta's name filter returns similar names
                template(language="en", status="REJECTED"),
                template(language="en_US"),
            ),
        )
    )
    async with httpx.AsyncClient() as http:
        assert (await guard(tmp_path, http).check(UPDATE)).ok

        # the name exists, but only in other languages
        route.mock(
            return_value=httpx.Response(
                200,
                json=page(template(language="en_GB"), template(language="en"), template("vship_update_2")),
            )
        )
        info = await guard(tmp_path, http).check(UPDATE)
        assert (info.ok, info.status, info.category, info.body_text, info.placeholders) == (
            False,
            None,
            None,
            None,
            0,
        )
        assert info.problem == (
            f"WhatsApp template vship_update in language en_US was not found in the WhatsApp Business account"
            f" {WABA}. It exists in: en, en_GB. RELAY_TEMPLATE_LANGUAGE has to match one of them."
        )

        # not there at all
        route.mock(return_value=httpx.Response(200, json={"data": []}))
        info = await guard(tmp_path, http).check(UPDATE)
        assert (
            not info.ok
            and "was not found" in info.problem
            and "Create it in WhatsApp Manager" in info.problem
        )


@pytest.mark.parametrize(
    ("entry", "spec", "words"),
    [
        (template(status="PENDING"), UPDATE, ["status at Meta is PENDING", "still reviewing"]),
        (template(status="PAUSED"), UPDATE, ["status at Meta is PAUSED", "low quality"]),
        (template(status="REJECTED"), UPDATE, ["REJECTED", "WhatsApp Manager shows the reason"]),
        (template(status="DISABLED"), UPDATE, ["DISABLED", "new template"]),
        (template(category="MARKETING"), UPDATE, ["category at Meta is MARKETING", "US numbers"]),
        (
            template(body="Hello {{1}}, VShip has an update."),
            UPDATE,
            ["has 1 variable,", "fill 2 (name, message)"],
        ),
        (template(body="Hello {{1}} {{2}} {{3}} {{2}}."), UPDATE, ["has 3 variables"]),
        (template(body="Hello {{name}}: {{update}}."), UPDATE, ["has 0 variables", "named variables"]),
        (template(header="DOCUMENT"), UPDATE, ["has a document header", "text updates"]),
        (template("vship_document", body="Hello {{1}}, here is {{2}}."), DOCUMENT, ["no document header"]),
    ],
)
@respx.mock
async def test_a_template_that_does_not_fit_is_not_sent(tmp_path, entry, spec, words):
    respx.get(TEMPLATES).mock(return_value=httpx.Response(200, json=page(entry)))
    async with httpx.AsyncClient() as http:
        info = await guard(tmp_path, http).check(spec)
    assert not info.ok
    assert (info.status, info.category) == (entry["status"], entry["category"])
    assert info.problem.startswith(f"WhatsApp template {spec.name} ({spec.language}) cannot be used. ")
    for word in words:
        assert word in info.problem


@respx.mock
async def test_every_reason_is_listed(tmp_path):
    entry = template(status="PAUSED", category="MARKETING", body="Hi {{1}}", header="DOCUMENT")
    respx.get(TEMPLATES).mock(return_value=httpx.Response(200, json=page(entry)))
    async with httpx.AsyncClient() as http:
        info = await guard(tmp_path, http).check(UPDATE)
    assert not info.ok and info.placeholders == 1 and info.body_text == "Hi {{1}}"
    for word in ("PAUSED", "MARKETING", "has 1 variable", "document header"):
        assert word in info.problem


@respx.mock
async def test_a_document_template_needs_a_document_header_and_other_headers_do_not_matter(tmp_path):
    respx.get(TEMPLATES).mock(
        side_effect=[
            httpx.Response(
                200, json=page(template("vship_document", body="Hi {{1}}, here is {{2}}.", header="document"))
            ),
            httpx.Response(200, json=page(template(header="TEXT"))),
        ]
    )
    async with httpx.AsyncClient() as http:
        checker = guard(tmp_path, http)
        assert (await checker.check(DOCUMENT)).ok
        assert (await checker.check(UPDATE)).ok  # a text header is not a document header


@respx.mock
async def test_the_list_is_followed_page_by_page(tmp_path):
    route = respx.get(TEMPLATES).mock(
        side_effect=[
            httpx.Response(200, json=page(template("vship_update_2"), after="CURSOR1")),
            httpx.Response(200, json=page(template())),
        ]
    )
    async with httpx.AsyncClient() as http:
        info = await guard(tmp_path, http).check(UPDATE)
    assert info.ok and route.call_count == 2
    second = route.calls[1].request
    assert second.url.params["after"] == "CURSOR1"  # the cursor from Meta's next link is kept
    assert second.url.params["name"] == "vship_update"
    assert second.url.params["appsecret_proof"] == PROOF
    assert "access_token" not in second.url.params  # the token only travels in the header
    assert second.headers["Authorization"] == "Bearer wa-token"


@respx.mock
async def test_paging_stops_after_five_pages(tmp_path):
    route = respx.get(TEMPLATES).mock(
        return_value=httpx.Response(200, json=page(template("vship_update_2"), after="AGAIN"))
    )
    async with httpx.AsyncClient() as http:
        info = await guard(tmp_path, http).check(UPDATE)
    assert route.call_count == MAX_PAGES == 5
    assert not info.ok and "not found in the first 5 pages" in info.problem


@respx.mock
async def test_a_next_page_on_another_address_is_not_followed(tmp_path):
    elsewhere = respx.get(url__startswith="https://graph.example.net/").mock(
        return_value=httpx.Response(200, json=page(template()))
    )
    for link in (
        "https://graph.example.net/v27.0/1/message_templates?after=C",
        "http://graph.facebook.com/v27.0/1/message_templates?after=C",  # not encrypted
        "https://[::1/broken",
    ):
        body = page(template("vship_update_2"))
        body["paging"] = {"next": link}
        route = respx.get(TEMPLATES).mock(return_value=httpx.Response(200, json=body))
        async with httpx.AsyncClient() as http:
            checker = guard(tmp_path, http)
            info = await checker.check(UPDATE)
            assert not info.ok and "unexpected web address" in info.problem
            await checker.check(UPDATE)
        assert route.call_count == 2  # not kept: the next check asks again
        route.reset()
    assert not elsewhere.called


@respx.mock
async def test_answers_are_kept_for_the_check_interval(tmp_path):
    clock = FakeClock()
    route = respx.get(TEMPLATES).mock(return_value=httpx.Response(200, json=page(template())))
    async with httpx.AsyncClient() as http:
        checker = guard(tmp_path, http, clock, relay_template_check_seconds=600)
        assert (await checker.check(UPDATE)).ok
        clock.advance(599)
        assert (await checker.check(UPDATE)).ok
        assert route.call_count == 1

        clock.advance(2)
        route.mock(return_value=httpx.Response(200, json=page(template(status="PAUSED"))))
        assert (await checker.check(UPDATE)).status == "PAUSED"
        clock.advance(300)
        assert (await checker.check(UPDATE)).status == "PAUSED"  # a "cannot be used" answer is kept too
        assert route.call_count == 2

        # each template has its own answer
        document = template("vship_document", body="Hi {{1}}, here is {{2}}.", header="DOCUMENT")
        route.mock(return_value=httpx.Response(200, json=page(document)))
        assert (await checker.check(DOCUMENT)).ok and route.call_count == 3

        # forget() makes the next check ask Meta again
        route.mock(return_value=httpx.Response(200, json=page(template())))
        checker.forget(UPDATE)
        assert (await checker.check(UPDATE)).ok and route.call_count == 4
        assert (await checker.check(DOCUMENT)).ok and route.call_count == 4
        checker.forget()
        route.mock(return_value=httpx.Response(200, json=page(document)))
        assert (await checker.check(DOCUMENT)).ok and route.call_count == 5


@respx.mock
async def test_temporary_failures_are_raised_as_retriable(tmp_path):
    route = respx.get(TEMPLATES)
    async with httpx.AsyncClient() as http:
        checker = guard(tmp_path, http)

        async def failure() -> WhatsAppSendError:
            with pytest.raises(WhatsAppSendError) as exc:
                await checker.check(UPDATE)
            return exc.value

        route.mock(side_effect=httpx.ConnectError("no route"))
        assert (await failure()).retriable
        route.mock(side_effect=httpx.ReadTimeout("slow"))
        silent = await failure()
        assert silent.retriable and not silent.uncertain  # only a read: always safe to repeat

        route.mock(
            return_value=httpx.Response(
                503, text="<html>Service Unavailable</html>", headers={"Retry-After": "12"}
            )
        )
        outage = await failure()
        assert (outage.retriable, outage.uncertain, outage.status_code, outage.retry_after) == (
            True,
            False,
            503,
            12.0,
        )
        route.mock(return_value=httpx.Response(500, json=graph_error(2, "Service temporarily unavailable")))
        assert (await failure()).code == 2
        route.mock(return_value=httpx.Response(429, text="Too Many Requests"))
        assert (await failure()).retriable

        # Graph reports its rate limits as HTTP 400 with a rate-limit code
        for code in (4, 80007):
            route.mock(
                return_value=httpx.Response(400, json=graph_error(code, "Application request limit reached"))
            )
            limited = await failure()
            assert limited.retriable and limited.code == code and limited.status_code == 400

        # none of that was kept: the next check asks Meta again
        route.mock(return_value=httpx.Response(200, json=page(template())))
        assert (await checker.check(UPDATE)).ok


@respx.mock
async def test_a_refusal_from_meta_is_explained_and_not_kept(tmp_path):
    route = respx.get(TEMPLATES).mock(
        return_value=httpx.Response(
            400, json=graph_error(190, "Error validating access token: Session has expired on Monday.")
        )
    )
    async with httpx.AsyncClient() as http:
        checker = guard(tmp_path, http)
        info = await checker.check(UPDATE)
        assert (info.ok, info.status, info.category, info.placeholders) == (False, None, None, 0)
        assert info.problem.startswith(
            "Meta refused the check of WhatsApp template vship_update (en_US) (HTTP 400, code 190):"
            " Error validating access token: Session has expired on Monday. "
        )
        assert "replace WA_ACCESS_TOKEN" in info.problem  # what an administrator has to do about it
        await checker.check(UPDATE)
        assert route.call_count == 2  # not kept: a replaced token works at once

        route.mock(return_value=httpx.Response(403, json=graph_error(200, "Permissions error")))
        problem = (await checker.check(UPDATE)).problem
        assert "Permissions error" in problem and "whatsapp_business_management" in problem

        route.mock(
            return_value=httpx.Response(
                400, json=graph_error(100, "Unsupported get request. Object with ID '1' does not exist", 33)
            )
        )
        problem = (await checker.check(UPDATE)).problem
        assert (
            "code 100" in problem
            and "Object with ID '1' does not exist" in problem
            and "WA_WABA_ID" in problem
        )

        route.mock(return_value=httpx.Response(400, json=graph_error(100, "Invalid parameter")))
        assert (await checker.check(UPDATE)).problem.endswith("(HTTP 400, code 100): Invalid parameter.")

        route.mock(return_value=httpx.Response(404, text="<html>Not Found</html>"))
        info = await checker.check(UPDATE)
        assert not info.ok and "HTTP 404 and no explanation" in info.problem

        route.mock(return_value=httpx.Response(200, text="<html>Sign in</html>"))
        info = await checker.check(UPDATE)
        assert not info.ok and "could not be read" in info.problem
        assert route.call_count == 7


@respx.mock
async def test_missing_settings_are_reported_without_asking_meta(tmp_path):
    route = respx.get(url__startswith="https://graph.facebook.com/").mock(
        return_value=httpx.Response(200, json=page(template()))
    )
    cases = [
        ({"wa_waba_id": ""}, UPDATE, "WA_WABA_ID is not set"),
        ({"wa_waba_id": "12/../34"}, UPDATE, "numeric id"),
        ({"wa_access_token": ""}, UPDATE, "WA_ACCESS_TOKEN is not set"),
        ({}, TemplateSpec("", "en_US", ("name", "message")), "No WhatsApp template name"),
    ]
    async with httpx.AsyncClient() as http:
        for overrides, spec, words in cases:
            info = await guard(tmp_path, http, **overrides).check(spec)
            assert not info.ok and words in info.problem
    assert not route.called


def test_the_confirmation_shows_what_the_customer_received():
    assert render_body(BODY, ["Maria", "Your container has arrived."]) == (
        "Hello Maria, VShip has an update:\nYour container has arrived.\nPlease either reply to this"
        " message or send us an email at Elizabeth@vship2000.com if you have any Questions"
    )
    # a value is never filled in twice, and a number without a value stays visible
    assert render_body("{{1}} and {{2}} and {{3}}", ["{{2}}", "b"]) == "{{2}} and b and {{3}}"
    assert render_body("No variables.", []) == "No variables."


def test_values_follow_the_configured_order():
    booking = TemplateSpec("vship_booking_update", "en_US", ("booking", "name", "message"))
    assert build_body_params(UPDATE, name="Maria", message="It arrived.", booking="BK-1") == [
        "Maria",
        "It arrived.",
    ]
    assert build_body_params(booking, name="Maria", message="It arrived.", booking="BK-1") == [
        "BK-1",
        "Maria",
        "It arrived.",
    ]
    assert build_body_params(
        DOCUMENT, name="Maria", message="not used", booking=None, filename="invoice  march.pdf"
    ) == ["Maria", "invoice march.pdf"]
    # WhatsApp refuses line breaks, tabs and runs of spaces in a variable
    assert build_body_params(UPDATE, name="Maria\tLopez", message="line one\r\nline   two", booking=None) == [
        "Maria Lopez",
        "line one line two",
    ]
    assert build_body_params(TemplateSpec("plain", "en_US", ()), name="x", message="y", booking=None) == []


def test_an_empty_value_is_refused_instead_of_made_up():
    needs_booking = TemplateSpec("vship_document", "en_US", ("name", "booking"), "document")
    with pytest.raises(TemplateValueMissing) as exc:
        build_body_params(needs_booking, name="Maria", message="m", booking=None, filename="a.pdf")
    assert exc.value.token == "booking"
    assert exc.value.text == (
        "The WhatsApp template vship_document needs a booking number, but the email's subject has none."
        " Write the subject as: WA | phone number | booking number."
    )
    with pytest.raises(TemplateValueMissing) as exc:
        build_body_params(DOCUMENT, name="Maria", message="m", booking=None)
    assert exc.value.token == "filename"
    with pytest.raises(TemplateValueMissing) as exc:
        build_body_params(
            UPDATE, name=" " + chr(0xA0) + "\t", message="m", booking=None
        )  # no-break space, tab
    assert exc.value.token == "name"
    with pytest.raises(ValueError, match="unknown parameter 'price'"):
        build_body_params(TemplateSpec("x", "en_US", ("price",)), name="a", message="b", booking=None)


def test_a_spec_is_always_usable_as_a_cache_key():
    assert TemplateSpec("vship_update", "en_US", ["name", "message"]) == UPDATE  # type: ignore[arg-type]
    assert hash(TemplateSpec("vship_update", "en_US", ["name", "message"])) == hash(UPDATE)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="header"):
        TemplateSpec("vship_document", "en_US", (), "Document")  # type: ignore[arg-type]


def test_specs_follow_the_settings(tmp_path):
    assert specs_from_settings(make_settings(tmp_path)) == {
        "update": TemplateSpec("vship_update", "en_US", ("name", "message"), "none"),
        "booking": None,
        "document": None,
    }
    specs = specs_from_settings(
        make_settings(
            tmp_path,
            relay_template_language="en",
            relay_template_params="Name, Message",
            relay_booking_template_name="vship_booking_update",
            relay_booking_template_params="name,booking;message",
            relay_document_template_name="vship_document",
            relay_document_template_params="",
        )
    )
    assert specs == {
        "update": TemplateSpec("vship_update", "en", ("name", "message"), "none"),
        "booking": TemplateSpec("vship_booking_update", "en", ("name", "booking", "message"), "none"),
        "document": TemplateSpec("vship_document", "en", (), "document"),
    }
