from app.security import compute_signature, verify_signature, verify_token_matches


def test_signature_roundtrip():
    body = b'{"object":"whatsapp_business_account"}'
    header = compute_signature("s3cret", body)
    assert header.startswith("sha256=")
    assert verify_signature("s3cret", body, header)
    assert verify_signature("s3cret", body, header.upper().replace("SHA256=", "sha256="))


def test_signature_rejects_tampering_and_missing():
    body = b'{"a":1}'
    header = compute_signature("s3cret", body)
    assert not verify_signature("s3cret", b'{"a":2}', header)
    assert not verify_signature("other", body, header)
    assert not verify_signature("s3cret", body, None)
    assert not verify_signature("s3cret", body, "")
    assert not verify_signature("s3cret", body, "sha1=abc")
    assert not verify_signature("", body, header)


def test_verify_token():
    assert verify_token_matches("abc", "abc")
    assert not verify_token_matches("abc", "abd")
    assert not verify_token_matches("abc", None)
    assert not verify_token_matches("", "")
