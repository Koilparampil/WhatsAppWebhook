from __future__ import annotations

import pytest
from app.crm.client import CrmError
from app.crm.directory import Directory, LookupStatus


class FakeClient:
    def __init__(self, data: dict[str, list[dict]]):
        self.data = data
        self.calls: list[str] = []
        self.fail = False

    async def get_list(self, entity: str) -> list[dict]:
        self.calls.append(entity)
        if self.fail:
            raise CrmError("down")
        return list(self.data.get(entity, []))


CUSTOMERS = [
    {
        "id": 1,
        "companyName": "Acme",
        "contactPerson": "John",
        "mobile": "650-555-1234",
        "email1": "john@acme.com",
        "email2": "ops@acme.com",
        "active": True,
    },
    {
        "id": 2,
        "companyName": "Beta",
        "businessPhone": "(650) 555-1234",
        "email1": "beta@example.com",
        "active": True,
    },
    {
        "id": 3,
        "companyName": "Gamma",
        "mobile": "555-555-1234",
        "email1": "gamma@example.com",
        "active": True,
    },
    {
        "id": 4,
        "companyName": "Inactive",
        "mobile": "650-555-7777",
        "email1": "old@example.com",
        "active": False,
    },
    {"id": 5, "companyName": "NoEmail", "mobile": "650-555-8888", "active": True},
]


def make_directory(client, clock, **kw) -> Directory:
    defaults = {
        "entities": ["Customer"],
        "default_region": "US",
        "include_inactive": False,
        "reply_to_all": True,
        "refresh_seconds": 900,
        "miss_refresh_min_age_seconds": 60,
        "clock": clock,
    }
    defaults.update(kw)
    return Directory(client, **defaults)


async def test_exact_match_ranks_mobile_first_and_returns_all_emails(clock):
    client = FakeClient({"Customer": CUSTOMERS})
    directory = make_directory(client, clock)
    result = await directory.lookup("16505551234")
    assert result.status is LookupStatus.MATCHED
    assert result.best.record.record_id == "1"
    assert result.best.matched_field == "mobile"
    assert result.reply_to == ["john@acme.com", "ops@acme.com"]
    assert [c.record.record_id for c in result.candidates] == ["1", "2"]
    assert result.lookup_key == "+16505551234"


async def test_reply_to_first_only(clock):
    directory = make_directory(FakeClient({"Customer": CUSTOMERS}), clock, reply_to_all=False)
    result = await directory.lookup("16505551234")
    assert result.reply_to == ["john@acme.com"]


async def test_suffix_fallback_for_unparseable_crm_number(clock):
    directory = make_directory(FakeClient({"Customer": CUSTOMERS}), clock)
    result = await directory.lookup("15555551234")
    assert result.status is LookupStatus.MATCHED
    assert result.best.record.record_id == "3"
    assert result.best.exact is False


async def test_inactive_excluded_and_no_email(clock):
    directory = make_directory(FakeClient({"Customer": CUSTOMERS}), clock)
    assert (await directory.lookup("16505557777")).status is LookupStatus.NO_MATCH
    no_email = await directory.lookup("16505558888")
    assert no_email.status is LookupStatus.NO_MATCH
    assert "no email" in no_email.reason
    incl = make_directory(FakeClient({"Customer": CUSTOMERS}), clock, include_inactive=True)
    assert (await incl.lookup("16505557777")).status is LookupStatus.MATCHED


async def test_no_phone_and_disabled(clock):
    directory = make_directory(FakeClient({"Customer": CUSTOMERS}), clock)
    assert (await directory.lookup(None)).status is LookupStatus.NO_PHONE
    disabled = make_directory(None, clock)
    assert (await disabled.lookup("16505551234")).status is LookupStatus.DISABLED


async def test_on_miss_refresh_only_after_min_age(clock):
    client = FakeClient({"Customer": list(CUSTOMERS)})
    directory = make_directory(client, clock)
    await directory.refresh()
    assert client.calls == ["Customer"]
    assert (await directory.lookup("16505550000")).status is LookupStatus.NO_MATCH
    assert client.calls == ["Customer"]  # index is fresh; no refresh on miss
    clock.advance(61)
    client.data["Customer"].append(
        {"id": 9, "companyName": "New", "mobile": "650-555-0000", "email1": "new@example.com", "active": True}
    )
    result = await directory.lookup("16505550000")
    assert client.calls == ["Customer", "Customer"]
    assert result.status is LookupStatus.MATCHED and result.best.record.record_id == "9"


async def test_error_without_cache_and_stale_serving(clock):
    client = FakeClient({"Customer": CUSTOMERS})
    client.fail = True
    directory = make_directory(client, clock)
    result = await directory.lookup("16505551234")
    assert result.status is LookupStatus.ERROR
    assert "CRM unavailable" in result.reason
    client.fail = False
    await directory.refresh()
    client.fail = True
    clock.advance(3600)
    stale = await directory.lookup("16505551234")  # on-miss refresh fails, cached index still answers
    assert stale.status is LookupStatus.MATCHED
    assert stale.index_age_seconds == pytest.approx(3600)
    with pytest.raises(CrmError):
        await directory.refresh()
    assert directory.status()["last_error"]
