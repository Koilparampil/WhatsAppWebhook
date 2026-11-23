"""Application configuration, loaded from environment variables (or an env file).

Every setting is documented in ``.env.example`` at the repository root. Secrets are never
logged; ``Settings.redacted()`` is what the CLI prints.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import phonenumbers
from pydantic import Field, PrivateAttr, TypeAdapter, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from pydantic.functional_validators import ModelWrapValidatorHandler

_EMAIL = re.compile(r"[^@\s<>\",;:\\/]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TEMPLATE_NAME = re.compile(r"[a-z0-9_]{1,512}")
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:_[A-Z]{2})?")
_TEXT_TOKENS = ("name", "message", "booking")  # what can fill the body variables of an update template
_DOCUMENT_TOKENS = ("name", "booking", "filename")  # ... of the document template

# The RELAY_* settings that are not plain text, and what each must hold. A value that cannot be read as that
# (RELAY_ENABLED=flase, RELAY_POLL_SECONDS=30s, RELAY_MAX_MESSAGE_CHARS=1,000, a line left empty) must not
# stop the service, and with it the forwarder: the setting keeps its default (RELAY_ENABLED: false) and
# relay_problems() names it, so the feature stays off until it is fixed. Other settings are read as before.
_RELAY_TYPED: dict[str, str] = {
    "relay_enabled": "true or false",
    "relay_require_internal_auth": "true or false",
    "relay_dry_run": "true or false",
    "relay_poll_seconds": "a number of seconds",
    "relay_template_check_seconds": "a whole number of seconds",
    "relay_max_message_chars": "a whole number of characters",
    "relay_max_name_chars": "a whole number of characters",
    "relay_max_age_hours": "a number of hours",
}
_RELAY_TYPED_READERS: dict[str, TypeAdapter[Any]] = {}  # field name -> reader, filled on first use


def _split_csv(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items = value
    else:
        items = value.replace(";", ",").split(",")
    return [item.strip() for item in items if item and item.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ WhatsApp / Meta
    wa_verify_token: str = Field(
        description="Verify token pasted into the Meta App Dashboard webhook config."
    )
    wa_app_secret: str = Field(description="Meta App Secret used to validate X-Hub-Signature-256.")
    wa_access_token: str = Field(default="", description="Permanent System User token (media download).")
    wa_graph_api_version: str = "v27.0"
    wa_graph_base_url: str = "https://graph.facebook.com"
    wa_phone_number_id: str = Field(default="", description="Optional: only process this business number.")
    wa_waba_id: str = Field(
        default="", description="Messaging account (WABA) id, used by the meta-check CLI."
    )
    wa_allow_unsigned: bool = Field(
        default=False, description="DEV ONLY: accept POSTs without a valid signature."
    )
    wa_media_timeout_seconds: float = 120.0
    # One-time /onboard page for WhatsApp Business app (Coexistence) onboarding; off unless all three set.
    wa_app_id: str = Field(default="", description="Meta App ID (App settings > Basic), used by /onboard.")
    onboard_config_id: str = Field(
        default="", description="Facebook Login for Business configuration id (WhatsApp Embedded Signup)."
    )
    onboard_key: str = Field(
        default="", description="Secret that /onboard?key= must match; empty disables the page."
    )

    # ------------------------------------------------------------------ VShipCRM
    crm_enabled: bool = True
    crm_base_url: str = "https://vship2000-prod-api.azurewebsites.net"
    crm_username: str = ""
    crm_password: str = ""
    crm_entities: str = Field(
        default="Customer", description="Comma-separated list endpoints to index, in priority order."
    )
    crm_default_region: str = Field(
        default="US", description="ISO region used to parse CRM phone numbers without a country code."
    )
    crm_refresh_seconds: int = 900
    crm_miss_refresh_min_age_seconds: int = 60
    crm_include_inactive: bool = False
    crm_reply_to_all_emails: bool = True
    crm_timeout_seconds: float = 20.0

    # ------------------------------------------------------------------ Mail transport
    mail_transport: Literal["smtp", "graph"] = "smtp"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_auth: Literal["password", "oauth", "none"] = "password"
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_timeout_seconds: float = 60.0
    # Microsoft Entra app registration (used by mail_transport=graph and smtp_auth=oauth)
    oauth_tenant_id: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_token_url_template: str = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"  # noqa: S105
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_sender: str = Field(default="", description="Mailbox (UPN) the Graph transport sends from.")

    # ------------------------------------------------------------------ Mail content
    mail_from: str = Field(description="Fixed From mailbox, e.g. whatsapp@yourdomain.tld")
    mail_from_name_template: str = "{display_name} via WhatsApp"
    mail_to: str = Field(description="Comma-separated recipient list.")
    mail_subject_prefix: str = "[WhatsApp]"
    mail_timezone: str = "America/New_York"
    mail_fallback_reply_to: str = ""
    mail_max_message_bytes: int = Field(
        default=32_000_000,
        description=(
            "Upper bound for one encoded email (Exchange Online default send limit is 35 MB);"
            " larger media is split across several emails."
        ),
    )
    mail_split_large_media: bool = True
    alert_on_dead_letter: bool = True
    alert_to: str = Field(default="", description="Comma-separated; defaults to MAIL_TO.")

    # ------------------------------------------------------------------ App / worker
    app_bind_host: str = "127.0.0.1"
    app_bind_port: int = 8000
    store_path: Path = Path("./data/queue.db")
    spool_dir: Path = Path("./data/spool")
    store_retention_days: int = 30
    worker_enabled: bool = True
    worker_retry_window_seconds: int = 600
    worker_max_crash_attempts: int = Field(
        default=3,
        description="Items found mid-processing after this many restarts are dead-lettered.",
    )
    alert_retry_window_seconds: int = Field(
        default=43200,
        description="Alert emails keep retrying this long so a mail outage is still reported afterwards.",
    )
    worker_base_delay_seconds: float = 15.0
    worker_max_delay_seconds: float = 120.0
    worker_poll_seconds: float = 2.0
    worker_shutdown_grace_seconds: float = 25.0
    log_level: str = "INFO"
    log_mask_phones: bool = True

    # ------------------------------------------------------------------ WhatsApp updates from email (relay)
    # An employee on RELAY_ALLOWED_SENDERS emails the relay mailbox with the subject "WA | phone | booking";
    # the service sends an approved WhatsApp template to that number. Off unless RELAY_ENABLED=true.
    relay_enabled: bool = False
    relay_mailbox: str = Field(default="", description="Mailbox read for commands; empty = GRAPH_SENDER.")
    relay_phone_number_id: str = Field(
        default="",
        description=(
            "Business phone number id the updates are sent from; empty = WA_PHONE_NUMBER_ID. Kept separate so"
            " that switching the feature on never turns on WA_PHONE_NUMBER_ID's forwarding filter."
        ),
    )
    relay_allowed_senders: str = Field(
        default="", description="Comma-separated addresses that may send WhatsApp updates."
    )
    relay_require_internal_auth: bool = Field(
        default=True,
        description=(
            "Only accept emails Exchange marked as sent by a signed-in internal user (AuthAs: Internal)."
        ),
    )
    relay_dry_run: bool = Field(
        default=True, description="Check commands and email the employee a preview; send nothing to WhatsApp."
    )
    relay_test_numbers: str = Field(
        default="", description="Comma-separated phone numbers; when set, only these may be messaged."
    )
    relay_poll_seconds: float = 30.0
    relay_default_region: str = Field(
        default="US", description="Country assumed for 10-digit numbers (and 11 digits starting with 1)."
    )
    relay_template_name: str = "vship_update"
    relay_template_language: str = "en_US"
    relay_template_params: str = Field(
        default="name,message", description="What fills {{1}}, {{2}}, ... of the update template, in order."
    )
    relay_booking_template_name: str = Field(
        default="", description="Template used when the subject has a booking number; empty = not set up yet."
    )
    relay_booking_template_params: str = ""
    relay_document_template_name: str = Field(
        default="", description="Template with a document header for files outside the 24-hour window."
    )
    relay_document_template_params: str = ""
    relay_template_check_seconds: int = 600
    # 820: the update template's fixed text (138 characters) + a 60-character name + the message stay within
    # Meta's 1,024-character limit for a template body (owner decision, 19 Nov 2026).
    relay_max_message_chars: int = 820
    relay_max_name_chars: int = 60
    relay_name_fallback: str = "there"
    relay_max_age_hours: float = 24.0
    relay_folder: str = "WhatsApp updates - handled"
    # (field name, value as written) of each setting in _RELAY_TYPED whose value could not be read
    _relay_unreadable: list[tuple[str, str]] = PrivateAttr(default_factory=list)

    # ------------------------------------------------------------------ derived helpers
    @property
    def crm_entity_list(self) -> list[str]:
        return _split_csv(self.crm_entities)

    @property
    def mail_to_list(self) -> list[str]:
        return _split_csv(self.mail_to)

    @property
    def alert_to_list(self) -> list[str]:
        return _split_csv(self.alert_to) or self.mail_to_list

    @property
    def mail_from_domain(self) -> str:
        return self.mail_from.rsplit("@", 1)[-1].strip().lower() or "localhost"

    @property
    def onboard_enabled(self) -> bool:
        return bool(self.wa_app_id and self.onboard_config_id and self.onboard_key)

    @property
    def relay_mailbox_address(self) -> str:
        return (self.relay_mailbox or self.graph_sender).strip().lower()

    @property
    def relay_sending_number_id(self) -> str:
        """The business number the updates go out from (RELAY_PHONE_NUMBER_ID, else WA_PHONE_NUMBER_ID)."""
        return (self.relay_phone_number_id or self.wa_phone_number_id).strip()

    @property
    def relay_allowed_sender_list(self) -> list[str]:
        return list(dict.fromkeys(address.lower() for address in _split_csv(self.relay_allowed_senders)))

    @property
    def relay_test_number_list(self) -> list[str]:
        """The test numbers in E.164 form (entries that are not valid numbers are a configuration error)."""
        from app.relay.command import CommandRefused, normalize_target_phone

        numbers: list[str] = []
        for raw in _split_csv(self.relay_test_numbers):
            try:
                numbers.append(normalize_target_phone(raw, self.relay_default_region))
            except CommandRefused:
                continue
        return list(dict.fromkeys(numbers))

    @staticmethod
    def relay_param_list(value: str) -> tuple[str, ...]:
        return tuple(token.lower() for token in _split_csv(value))

    @property
    def oauth_token_url(self) -> str:
        return self.oauth_token_url_template.format(tenant=self.oauth_tenant_id)

    @field_validator("wa_graph_api_version")
    @classmethod
    def _version_format(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("v"):
            value = "v" + value
        return value

    @field_validator("wa_app_id", "onboard_config_id")
    @classmethod
    def _numeric_id(cls, value: str, info) -> str:
        value = (value or "").strip()
        if value and not value.isdigit():
            raise ValueError(f"{info.field_name.upper()} must be the numeric id from the App Dashboard")
        return value

    @field_validator(
        "wa_verify_token",
        "wa_app_secret",
        "wa_access_token",
        "onboard_key",
        "crm_password",
        "smtp_password",
        "oauth_client_secret",
    )
    @classmethod
    def _no_placeholder_secrets(cls, value: str, info) -> str:
        value = (value or "").strip()
        lowered = value.lower()
        if lowered in {"change_me", "changeme", "todo", "xxx"} or (
            value.startswith("<") and value.endswith(">")
        ):
            raise ValueError(f"{info.field_name.upper()} still holds a placeholder value")
        if info.field_name in {"wa_verify_token", "wa_app_secret", "onboard_key"} and 0 < len(value) < 16:
            raise ValueError(f"{info.field_name.upper()} must be at least 16 characters")
        return value

    @field_validator("mail_timezone")
    @classmethod
    def _tz_exists(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:  # pragma: no cover - depends on tzdata
            raise ValueError(f"Unknown timezone {value!r}") from exc
        return value

    @model_validator(mode="wrap")
    @classmethod
    def _keep_unreadable_relay_values_out(
        cls, data: Any, handler: ModelWrapValidatorHandler[Settings]
    ) -> Settings:
        """Leave the RELAY_* values that cannot be read at all at their defaults instead of failing to load.

        Without this, one typing mistake in a RELAY_* line would stop every command and the service itself
        (exit 78, which systemd does not restart), even with RELAY_ENABLED=false. The values are checked
        with the readers of the fields themselves, so whatever a field accepts is still accepted unchanged.
        """
        unreadable: list[tuple[str, str]] = []
        if isinstance(data, dict):
            data = dict(data)
            for name in _RELAY_TYPED:
                if name not in data:
                    continue
                reader = _RELAY_TYPED_READERS.get(name)
                if reader is None:
                    reader = _RELAY_TYPED_READERS[name] = TypeAdapter(cls.model_fields[name].annotation)
                try:
                    reader.validate_python(data[name])
                except ValidationError:
                    unreadable.append((name, str(data.pop(name))))
        settings = handler(data)
        settings._relay_unreadable = unreadable
        return settings

    @model_validator(mode="after")
    def _check_transport(self) -> Settings:
        problems: list[str] = []
        if not self.mail_to_list:
            problems.append("MAIL_TO must list at least one recipient")
        if self.mail_transport == "smtp":
            if not self.smtp_host:
                problems.append("SMTP_HOST is required when MAIL_TRANSPORT=smtp")
            if self.smtp_auth == "password" and not (self.smtp_username and self.smtp_password):
                problems.append("SMTP_USERNAME and SMTP_PASSWORD are required when SMTP_AUTH=password")
            if self.smtp_auth == "oauth" and not self.smtp_username:
                problems.append("SMTP_USERNAME (the sending mailbox) is required when SMTP_AUTH=oauth")
            if self.smtp_auth != "none" and self.smtp_security == "none":
                problems.append(
                    "SMTP_AUTH=password/oauth needs SMTP_SECURITY=starttls or ssl (credentials must not be"
                    " sent over an unencrypted connection)"
                )
        if self.mail_transport == "graph" and not self.graph_sender:
            problems.append("GRAPH_SENDER is required when MAIL_TRANSPORT=graph")
        if self.mail_transport == "graph" or self.smtp_auth == "oauth":
            missing = [
                name
                for name, value in (
                    ("OAUTH_TENANT_ID", self.oauth_tenant_id),
                    ("OAUTH_CLIENT_ID", self.oauth_client_id),
                    ("OAUTH_CLIENT_SECRET", self.oauth_client_secret),
                )
                if not value
            ]
            if missing:
                problems.append(", ".join(missing) + " are required for OAuth client-credentials")
        if self.crm_enabled and not (self.crm_username and self.crm_password):
            problems.append("CRM_USERNAME and CRM_PASSWORD are required unless CRM_ENABLED=false")
        if self.mail_max_message_bytes < 1_000_000:
            problems.append("MAIL_MAX_MESSAGE_BYTES must be at least 1,000,000")
        if self.mail_max_message_bytes > 150_000_000:
            problems.append("MAIL_MAX_MESSAGE_BYTES cannot exceed 150,000,000 (Exchange Online's ceiling)")
        spool = Path(self.spool_dir).expanduser().resolve()
        store = Path(self.store_path).expanduser().resolve()
        if store == spool or spool in store.parents:
            problems.append("STORE_PATH must not be inside SPOOL_DIR (the spool is emptied at startup)")
        if spool == store.parent:
            problems.append("SPOOL_DIR must be its own directory, not the directory holding STORE_PATH")
        # The relay's own settings are deliberately NOT checked here: a mistake in them must never stop the
        # service (and with it the forwarder). The lifespan reads relay_problems() and leaves only the relay
        # off; check-config and relay-check print the problems.
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def relay_problems(self) -> list[str]:
        """Why RELAY_ENABLED=true cannot work ([] when the feature is off or complete). Never raises.

        A RELAY_* value that could not be read at all is reported even while the feature is off: a
        mistyped RELAY_ENABLED line is itself what keeps the feature off, and the operator must hear why.
        """
        try:
            if not self.relay_enabled:
                return self._unreadable_relay_problems()
            return self._relay_problems()
        except Exception as exc:  # a bug here must not stop the forwarder either
            return [f"the WhatsApp update settings could not be checked: {exc!r}"]

    def _unreadable_relay_problems(self) -> list[str]:
        problems = []
        for name, value in self._relay_unreadable:
            setting = name.upper()
            if not value.strip():
                problems.append(
                    f"{setting} is empty; it must be {_RELAY_TYPED[name]} (or remove the line to use the"
                    f" default, {str(Settings.model_fields[name].default).lower()})"
                )
            else:
                shown = value if len(value) <= 40 else value[:40] + "..."
                problems.append(f"{setting} must be {_RELAY_TYPED[name]}, not {shown!r}")
        return problems

    def _relay_problems(self) -> list[str]:
        """Settings that RELAY_ENABLED=true needs, checked whether or not the feature is switched on."""
        from app.relay.command import CommandRefused, normalize_target_phone

        problems: list[str] = self._unreadable_relay_problems()
        if self.mail_transport != "graph":
            problems.append("RELAY_ENABLED=true needs MAIL_TRANSPORT=graph (the Inbox is read through Graph)")
        for name, value in (("WA_ACCESS_TOKEN", self.wa_access_token), ("WA_WABA_ID", self.wa_waba_id)):
            if not value:
                problems.append(f"{name} is required when RELAY_ENABLED=true")
        if not self.relay_sending_number_id:
            problems.append(
                "RELAY_PHONE_NUMBER_ID (or WA_PHONE_NUMBER_ID) is required when RELAY_ENABLED=true: the"
                " business number the updates are sent from"
            )
        for name, value in (
            ("WA_PHONE_NUMBER_ID", self.wa_phone_number_id),
            ("RELAY_PHONE_NUMBER_ID", self.relay_phone_number_id),
            ("WA_WABA_ID", self.wa_waba_id),
        ):
            if value and not value.strip().isdigit():
                problems.append(f"{name} must be the numeric id from WhatsApp Manager")
        forwarding, sending = self.wa_phone_number_id.strip(), self.relay_phone_number_id.strip()
        if forwarding.isdigit() and sending.isdigit() and forwarding != sending:
            problems.append(
                "RELAY_PHONE_NUMBER_ID differs from WA_PHONE_NUMBER_ID, so customers' answers to the updates"
                " would not be forwarded (WA_PHONE_NUMBER_ID forwards only that number's messages)"
            )
        if self.relay_default_region not in phonenumbers.SUPPORTED_REGIONS:
            problems.append(
                f"RELAY_DEFAULT_REGION {self.relay_default_region!r} is not a country code such as US (two"
                " upper-case letters)"
            )
        mailbox = self.relay_mailbox_address
        if not _EMAIL.fullmatch(mailbox):
            problems.append("RELAY_MAILBOX (or GRAPH_SENDER) must be an email address")
        allowed = self.relay_allowed_sender_list
        if not allowed:
            problems.append("RELAY_ALLOWED_SENDERS must list at least one address when RELAY_ENABLED=true")
        bad = [address for address in allowed if not _EMAIL.fullmatch(address)]
        if bad:
            problems.append(
                "RELAY_ALLOWED_SENDERS holds entries that are not email addresses: " + ", ".join(bad)
            )
        if mailbox in allowed:
            problems.append("RELAY_ALLOWED_SENDERS must not contain the relay mailbox itself")
        for raw in _split_csv(self.relay_test_numbers):
            try:
                normalize_target_phone(raw, self.relay_default_region)
            except CommandRefused as exc:
                problems.append(f"RELAY_TEST_NUMBERS entry {raw!r} is not usable: {exc.text}")
        if self.relay_poll_seconds < 10:
            problems.append("RELAY_POLL_SECONDS must be at least 10")
        if self.relay_template_check_seconds < 60:
            problems.append("RELAY_TEMPLATE_CHECK_SECONDS must be at least 60")
        if not 1 <= self.relay_max_message_chars <= 30000:
            problems.append("RELAY_MAX_MESSAGE_CHARS must be between 1 and 30000")
        if not 1 <= self.relay_max_name_chars <= 200:
            problems.append("RELAY_MAX_NAME_CHARS must be between 1 and 200")
        if not self.relay_name_fallback.strip():
            problems.append("RELAY_NAME_FALLBACK must not be empty (WhatsApp refuses empty values)")
        if self.relay_max_age_hours <= 0:
            problems.append("RELAY_MAX_AGE_HOURS must be greater than 0")
        if not self.relay_folder.strip():
            problems.append("RELAY_FOLDER must not be empty")
        if not _LANGUAGE.fullmatch(self.relay_template_language):
            problems.append("RELAY_TEMPLATE_LANGUAGE must look like en_US or en")
        templates = (
            ("RELAY_TEMPLATE", self.relay_template_name, self.relay_template_params, True, _TEXT_TOKENS),
            (
                "RELAY_BOOKING_TEMPLATE",
                self.relay_booking_template_name,
                self.relay_booking_template_params,
                False,
                _TEXT_TOKENS,
            ),
            (
                "RELAY_DOCUMENT_TEMPLATE",
                self.relay_document_template_name,
                self.relay_document_template_params,
                False,
                _DOCUMENT_TOKENS,
            ),
        )
        for prefix, name, params, required, tokens in templates:
            if not name:
                if required:
                    problems.append(f"{prefix}_NAME is required when RELAY_ENABLED=true")
                continue
            if not _TEMPLATE_NAME.fullmatch(name):
                problems.append(f"{prefix}_NAME may hold only lower-case letters, digits and underscores")
            parsed = self.relay_param_list(params)
            unknown = [token for token in parsed if token not in tokens]
            if unknown:
                problems.append(
                    f"{prefix}_PARAMS holds unknown entries {', '.join(unknown)}"
                    f" (allowed: {', '.join(tokens)})"
                )
            if len(set(parsed)) != len(parsed):
                problems.append(f"{prefix}_PARAMS names an entry twice")
            if tokens is _TEXT_TOKENS:
                needed = (
                    ("name", "message", "booking")
                    if prefix == "RELAY_BOOKING_TEMPLATE"
                    else ("name", "message")
                )
                missing = [token for token in needed if token not in parsed]
                if missing:
                    problems.append(f"{prefix}_PARAMS must include {', '.join(missing)}")
        return problems

    def redacted(self) -> dict[str, object]:
        """Settings as a dict with secret values replaced, for diagnostics."""
        secret_names = {
            "wa_verify_token",
            "wa_app_secret",
            "wa_access_token",
            "onboard_key",
            "crm_password",
            "smtp_password",
            "oauth_client_secret",
        }
        out: dict[str, object] = {}
        for name, value in self.model_dump().items():
            if name in secret_names:
                out[name] = "<set>" if value else "<empty>"
            else:
                out[name] = str(value) if isinstance(value, Path) else value
        return out


def load_settings(env_file: str | Path | None = None, **overrides: object) -> Settings:
    """Load settings from the environment, applying explicit overrides.

    ``env_file`` (or the ``WHATSAPP_WEBHOOK_ENV_FILE`` variable) points at a dotenv-style file such as
    ``/etc/whatsapp-webhook/env``; real environment variables still take precedence over it.
    """
    env_file = env_file or os.environ.get("WHATSAPP_WEBHOOK_ENV_FILE") or None
    if env_file:
        return Settings(_env_file=env_file, **overrides)  # type: ignore[arg-type]
    return Settings(**overrides)  # type: ignore[arg-type]
