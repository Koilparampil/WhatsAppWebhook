"""Webhook authentication helpers: Meta's signature header and the verify-token handshake."""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Hub-Signature-256"


def compute_signature(app_secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` value Meta would send for ``body``."""
    digest = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(app_secret: str, body: bytes, header_value: str | None) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` against the raw request body."""
    if not app_secret or not header_value:
        return False
    header_value = header_value.strip()
    if not header_value.lower().startswith("sha256="):
        return False
    provided = header_value[len("sha256=") :].strip().lower()
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), provided.encode("ascii", "replace"))


def verify_token_matches(expected: str, provided: str | None) -> bool:
    """Constant-time comparison for the ``hub.verify_token`` handshake."""
    if not expected or provided is None:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), provided.encode("utf-8", "replace"))
