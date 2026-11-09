from datetime import UTC, datetime

from app.crm.phones import (
    CrmRecord,
    MatchCandidate,
    extract_emails,
    normalize_phone,
    normalize_phones,
    rank_candidates,
    record_from_api,
    whatsapp_phone_key,
)


def test_normalize_us_formats():
    for raw in (
        "(650) 555-1234",
        "650.555.1234",
        "+1 650-555-1234",
        "1-650-555-1234",
        "6505551234 x12",
        "650-555-1234 ext. 7",
    ):
        key = normalize_phone(raw, "US")
        assert key is not None, raw
        assert key.e164 == "+16505551234", raw
        assert key.suffix == "6505551234"


def test_normalize_international_and_invalid():
    assert normalize_phone("+44 20 7946 0958", "US").e164 == "+442079460958"
    assert normalize_phone("0044 20 7946 0958", "US").e164 == "+442079460958"
    assert normalize_phone("011 52 55 1234 5678", "US").e164 == "+525512345678"
    assert normalize_phone("12345", "US") is None
    assert normalize_phone("", "US") is None
    assert normalize_phone(None, "US") is None
    bogus = normalize_phone("555-555-1234", "US")  # 555 is not a valid US area code
    assert bogus is not None and bogus.e164 is None and bogus.suffix == "5555551234"


def test_normalize_multiple_numbers_in_one_field():
    keys = normalize_phones("650-555-1234 / 650-555-9999; +44 20 7946 0958", "US")
    assert [k.e164 for k in keys] == ["+16505551234", "+16505559999", "+442079460958"]


def test_whatsapp_key():
    key = whatsapp_phone_key("16505551234")
    assert key is not None and key.e164 == "+16505551234"
    assert whatsapp_phone_key(None) is None
    assert whatsapp_phone_key("12") is None


def test_extract_emails_order_and_dedupe():
    record = {
        "email3": "third@example.com",
        "email1": "First@Example.com; first@example.com",
        "email2": "not an email",
        "email4": "",
        "email": "zero@example.com",
    }
    assert extract_emails(record) == ["zero@example.com", "First@Example.com", "third@example.com"]


def test_record_from_api_customer():
    obj = {
        "id": 42,
        "contactPerson": "John Doe",
        "companyName": "Acme Imports",
        "businessPhone": "(650) 555-1000",
        "mobile": "650-555-1234",
        "email1": "john@acme.com",
        "email2": "ops@acme.com",
        "active": True,
        "updatedDate": "2026-10-01T12:00:00Z",
    }
    rec = record_from_api("Customer", obj, "US")
    assert rec is not None
    assert rec.record_id == "42"
    assert rec.label == "Customer #42: Acme Imports / John Doe"
    assert rec.emails == ["john@acme.com", "ops@acme.com"]
    assert [(f, k.e164) for f, _, k in rec.phones] == [
        ("mobile", "+16505551234"),
        ("businessPhone", "+16505551000"),
    ]
    assert record_from_api("Customer", {"id": 1, "companyName": "No phone"}, "US") is None


def _rec(entity: str, rid: str, active: bool = True, updated: str | None = None) -> CrmRecord:
    return CrmRecord(
        entity=entity,
        record_id=rid,
        company=f"Co {rid}",
        contact=None,
        active=active,
        updated_at=datetime.fromisoformat(updated).replace(tzinfo=UTC) if updated else None,
        emails=[f"{rid}@example.com"],
    )


def test_rank_candidates_prefers_exact_mobile_active_recent():
    cands = [
        MatchCandidate(_rec("Customer", "a", updated="2020-01-01"), "businessPhone", "x", exact=True),
        MatchCandidate(_rec("Customer", "b", updated="2026-01-01"), "mobile", "x", exact=False),
        MatchCandidate(_rec("Customer", "c", active=False), "mobile", "x", exact=True),
        MatchCandidate(_rec("Customer", "d", updated="2025-01-01"), "mobile", "x", exact=True),
        MatchCandidate(_rec("Customer", "d", updated="2025-01-01"), "businessPhone", "y", exact=True),
    ]
    ranked = rank_candidates(cands, ["Customer"])
    assert [c.record.record_id for c in ranked] == ["d", "c", "a", "b"]
    assert ranked[0].matched_field == "mobile"
