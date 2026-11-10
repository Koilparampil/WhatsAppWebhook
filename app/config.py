"""Application configuration, loaded from environment variables (or an env file).

Every setting is documented in ``.env.example`` at the repository root. Secrets are never
logged; ``Settings.redacted()`` is what the CLI prints.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    mail_timezone: str = "UTC"
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
    def oauth_token_url(self) -> str:
        return self.oauth_token_url_template.format(tenant=self.oauth_tenant_id)

    @field_validator("wa_graph_api_version")
    @classmethod
    def _version_format(cls, value: str) -> str:
        value = value.strip()
        if not value.startswith("v"):
            value = "v" + value
        return value

    @field_validator(
        "wa_verify_token",
        "wa_app_secret",
        "wa_access_token",
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
        if info.field_name in {"wa_verify_token", "wa_app_secret"} and 0 < len(value) < 16:
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
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def redacted(self) -> dict[str, object]:
        """Settings as a dict with secret values replaced, for diagnostics."""
        secret_names = {
            "wa_verify_token",
            "wa_app_secret",
            "wa_access_token",
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
