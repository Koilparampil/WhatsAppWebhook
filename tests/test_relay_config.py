"""Settings of "WhatsApp updates from email" (RELAY_*) and the checks made only when RELAY_ENABLED=true."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from app import cli
from app.config import _RELAY_TYPED, Settings
from dotenv import dotenv_values
from pydantic import ValidationError

from tests.conftest import make_settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

# Spec section 2: every relay setting and its default.
SPEC_DEFAULTS: dict[str, Any] = {
    "relay_enabled": False,
    "relay_mailbox": "",
    "relay_phone_number_id": "",
    "relay_allowed_senders": "",
    "relay_require_internal_auth": True,
    "relay_dry_run": True,
    "relay_test_numbers": "",
    "relay_poll_seconds": 30.0,
    "relay_default_region": "US",
    "relay_template_name": "vship_update",
    "relay_template_language": "en_US",
    "relay_template_params": "name,message",
    "relay_booking_template_name": "",
    "relay_booking_template_params": "",
    "relay_document_template_name": "",
    "relay_document_template_params": "",
    "relay_template_check_seconds": 600,
    "relay_max_message_chars": 820,
    "relay_max_name_chars": 60,
    "relay_name_fallback": "there",
    "relay_max_age_hours": 24.0,
    "relay_folder": "WhatsApp updates - handled",
}

# A complete relay configuration on top of the shared test settings.
RELAY_ON: dict[str, Any] = {
    "mail_transport": "graph",
    "graph_sender": "whatsapp@vship2000.com",
    "oauth_tenant_id": "tenant-id",
    "oauth_client_id": "client-id",
    "oauth_client_secret": "client-secret",
    "wa_phone_number_id": "106540352242922",
    "wa_waba_id": "102290129340398",
    "relay_enabled": True,
    "relay_allowed_senders": "elizabeth@vship2000.com, ops@vship2000.com",
}


def relay_settings(tmp_path: Path, **overrides: Any) -> Settings:
    return make_settings(tmp_path, **{**RELAY_ON, **overrides})


def relay_problems(tmp_path: Path, **overrides: Any) -> list[str]:
    """``_relay_problems()`` for RELAY_ON plus ``overrides``, without the model validator raising first."""
    loaded = make_settings(tmp_path, **{**RELAY_ON, "relay_enabled": False})
    return loaded.model_copy(update={"relay_enabled": True, **overrides})._relay_problems()


def _relay_fields() -> list[str]:
    return [name for name in Settings.model_fields if name.startswith("relay_")]


# ----------------------------------------------------------------------------- feature off
def test_defaults_are_the_spec_values(tmp_path):
    settings = make_settings(tmp_path)
    assert sorted(_relay_fields()) == sorted(SPEC_DEFAULTS)
    for name, value in SPEC_DEFAULTS.items():
        assert getattr(settings, name) == value, name
        assert type(getattr(settings, name)) is type(value), name
        assert Settings.model_fields[name].default == value, name


def test_feature_off_adds_no_requirements(tmp_path):
    # SMTP transport, no WhatsApp ids, no senders and every relay value broken: still loads while off.
    broken: dict[str, Any] = {
        "wa_access_token": "",
        "wa_phone_number_id": "",
        "wa_waba_id": "",
        "relay_enabled": False,
        "relay_mailbox": "not-an-address",
        "relay_allowed_senders": "nobody",
        "relay_test_numbers": "12345, 0044 20 7946 0958",
        "relay_poll_seconds": 1,
        "relay_template_check_seconds": 1,
        "relay_max_message_chars": 0,
        "relay_max_name_chars": 0,
        "relay_name_fallback": "",
        "relay_max_age_hours": 0,
        "relay_folder": "",
        "relay_template_language": "english",
        "relay_template_name": "",
        "relay_booking_template_name": "Bad Name",
        "relay_document_template_name": "x",
        "relay_document_template_params": "message,message",
    }
    settings = make_settings(tmp_path, **broken)
    assert settings.relay_enabled is False
    assert settings.mail_transport == "smtp"
    # the checks exist, they are just not applied while the feature is off
    assert len(settings._relay_problems()) >= 15


def test_plain_test_settings_still_load(tmp_path):
    assert make_settings(tmp_path).relay_enabled is False


# ----------------------------------------------------------------------------- feature on
def test_complete_configuration_loads(tmp_path):
    settings = relay_settings(tmp_path)
    assert settings.relay_enabled is True
    assert settings._relay_problems() == []
    assert relay_problems(tmp_path) == []


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        (
            {"mail_transport": "smtp"},
            "RELAY_ENABLED=true needs MAIL_TRANSPORT=graph (the Inbox is read through Graph)",
        ),
        ({"wa_access_token": ""}, "WA_ACCESS_TOKEN is required when RELAY_ENABLED=true"),
        (
            {"wa_phone_number_id": ""},
            "RELAY_PHONE_NUMBER_ID (or WA_PHONE_NUMBER_ID) is required when RELAY_ENABLED=true: the business"
            " number the updates are sent from",
        ),
        (
            {"relay_phone_number_id": "12x"},
            "RELAY_PHONE_NUMBER_ID must be the numeric id from WhatsApp Manager",
        ),
        (
            {"relay_phone_number_id": "999"},
            "RELAY_PHONE_NUMBER_ID differs from WA_PHONE_NUMBER_ID, so customers' answers to the updates"
            " would not be forwarded (WA_PHONE_NUMBER_ID forwards only that number's messages)",
        ),
        (
            {"relay_default_region": "us"},
            "RELAY_DEFAULT_REGION 'us' is not a country code such as US (two upper-case letters)",
        ),
        ({"wa_waba_id": ""}, "WA_WABA_ID is required when RELAY_ENABLED=true"),
        (
            {"wa_phone_number_id": "CHANGE_ME"},
            "WA_PHONE_NUMBER_ID must be the numeric id from WhatsApp Manager",
        ),
        ({"wa_waba_id": "<WABA ID>"}, "WA_WABA_ID must be the numeric id from WhatsApp Manager"),
        ({"relay_mailbox": "whatsapp"}, "RELAY_MAILBOX (or GRAPH_SENDER) must be an email address"),
        (
            {"relay_allowed_senders": ""},
            "RELAY_ALLOWED_SENDERS must list at least one address when RELAY_ENABLED=true",
        ),
        (
            {"relay_allowed_senders": " ;, "},
            "RELAY_ALLOWED_SENDERS must list at least one address when RELAY_ENABLED=true",
        ),
        (
            {"relay_allowed_senders": "elizabeth@vship2000.com, Elizabeth"},
            "RELAY_ALLOWED_SENDERS holds entries that are not email addresses: elizabeth",
        ),
        (
            {"relay_allowed_senders": "Elizabeth Smith <elizabeth@vship2000.com>"},
            "RELAY_ALLOWED_SENDERS holds entries that are not email addresses: elizabeth smith"
            " <elizabeth@vship2000.com>",
        ),
        (
            {"relay_allowed_senders": "elizabeth@vship2000.com, WhatsApp@VShip2000.com"},
            "RELAY_ALLOWED_SENDERS must not contain the relay mailbox itself",
        ),
        (
            {"relay_mailbox": "Updates@vship2000.com", "relay_allowed_senders": "updates@VSHIP2000.com"},
            "RELAY_ALLOWED_SENDERS must not contain the relay mailbox itself",
        ),
        (
            {"relay_test_numbers": "650-555-1234, 12345"},
            "RELAY_TEST_NUMBERS entry '12345' is not usable: The phone number '12345' is not a 10-digit US"
            " number. Numbers in other countries must be written with + and the country code, for example"
            " +44 20 7946 0958.",
        ),
        (
            {"relay_test_numbers": "0044 20 7946 0958"},
            "RELAY_TEST_NUMBERS entry '0044 20 7946 0958' is not usable: The phone number '0044 20 7946 0958'"
            " starts with an international dialling prefix. Write it with + and the country code instead,"
            " for example +44 20 7946 0958.",
        ),
        (
            {"relay_test_numbers": "+1 650 555 12345"},
            "RELAY_TEST_NUMBERS entry '+1 650 555 12345' is not usable: The phone number '+1 650 555 12345'"
            " is not a valid phone number. Check it for a typing mistake.",
        ),
        (
            {"relay_test_numbers": "650-555-1234 x12"},
            "RELAY_TEST_NUMBERS entry '650-555-1234 x12' is not usable: The phone number '650-555-1234 x12'"
            " holds characters other than digits (an extension or letters?). Write only the number, for"
            " example 650-555-1234 or +44 20 7946 0958.",
        ),
        ({"relay_poll_seconds": 9.5}, "RELAY_POLL_SECONDS must be at least 10"),
        ({"relay_template_check_seconds": 59}, "RELAY_TEMPLATE_CHECK_SECONDS must be at least 60"),
        ({"relay_max_message_chars": 0}, "RELAY_MAX_MESSAGE_CHARS must be between 1 and 30000"),
        ({"relay_max_message_chars": 30001}, "RELAY_MAX_MESSAGE_CHARS must be between 1 and 30000"),
        ({"relay_max_name_chars": 0}, "RELAY_MAX_NAME_CHARS must be between 1 and 200"),
        ({"relay_max_name_chars": 201}, "RELAY_MAX_NAME_CHARS must be between 1 and 200"),
        (
            {"relay_name_fallback": "   "},
            "RELAY_NAME_FALLBACK must not be empty (WhatsApp refuses empty values)",
        ),
        ({"relay_max_age_hours": 0.0}, "RELAY_MAX_AGE_HOURS must be greater than 0"),
        ({"relay_max_age_hours": -1.0}, "RELAY_MAX_AGE_HOURS must be greater than 0"),
        ({"relay_folder": " "}, "RELAY_FOLDER must not be empty"),
        ({"relay_template_language": "en-US"}, "RELAY_TEMPLATE_LANGUAGE must look like en_US or en"),
        ({"relay_template_language": "EN_us"}, "RELAY_TEMPLATE_LANGUAGE must look like en_US or en"),
        ({"relay_template_language": "english"}, "RELAY_TEMPLATE_LANGUAGE must look like en_US or en"),
        ({"relay_template_language": ""}, "RELAY_TEMPLATE_LANGUAGE must look like en_US or en"),
        ({"relay_template_name": ""}, "RELAY_TEMPLATE_NAME is required when RELAY_ENABLED=true"),
        (
            {"relay_template_name": "VShip-Update"},
            "RELAY_TEMPLATE_NAME may hold only lower-case letters, digits and underscores",
        ),
        (
            {"relay_template_name": "a" * 513},
            "RELAY_TEMPLATE_NAME may hold only lower-case letters, digits and underscores",
        ),
        (
            {"relay_template_params": "name,message,phone"},
            "RELAY_TEMPLATE_PARAMS holds unknown entries phone (allowed: name, message, booking)",
        ),
        (
            {"relay_template_params": "name,message,filename"},
            "RELAY_TEMPLATE_PARAMS holds unknown entries filename (allowed: name, message, booking)",
        ),
        ({"relay_template_params": "name,message,name"}, "RELAY_TEMPLATE_PARAMS names an entry twice"),
        ({"relay_template_params": "Name, NAME, message"}, "RELAY_TEMPLATE_PARAMS names an entry twice"),
        ({"relay_template_params": "message"}, "RELAY_TEMPLATE_PARAMS must include name"),
        ({"relay_template_params": "name,booking"}, "RELAY_TEMPLATE_PARAMS must include message"),
        ({"relay_template_params": ""}, "RELAY_TEMPLATE_PARAMS must include name, message"),
        (
            {"relay_booking_template_name": "vship_booking_update"},
            "RELAY_BOOKING_TEMPLATE_PARAMS must include name, message, booking",
        ),
        (
            {
                "relay_booking_template_name": "vship_booking_update",
                "relay_booking_template_params": "name,message",
            },
            "RELAY_BOOKING_TEMPLATE_PARAMS must include booking",
        ),
        (
            {
                "relay_booking_template_name": "Booking Update",
                "relay_booking_template_params": "name,message,booking",
            },
            "RELAY_BOOKING_TEMPLATE_NAME may hold only lower-case letters, digits and underscores",
        ),
        (
            {
                "relay_booking_template_name": "vship_booking_update",
                "relay_booking_template_params": "name,message,booking,filename",
            },
            "RELAY_BOOKING_TEMPLATE_PARAMS holds unknown entries filename (allowed: name, message, booking)",
        ),
        (
            {
                "relay_booking_template_name": "vship_booking_update",
                "relay_booking_template_params": "booking,name,message,booking",
            },
            "RELAY_BOOKING_TEMPLATE_PARAMS names an entry twice",
        ),
        (
            {
                "relay_document_template_name": "vship_document",
                "relay_document_template_params": "name,message",
            },
            "RELAY_DOCUMENT_TEMPLATE_PARAMS holds unknown entries message (allowed: name, booking, filename)",
        ),
        (
            {
                "relay_document_template_name": "vship_document",
                "relay_document_template_params": "filename,filename",
            },
            "RELAY_DOCUMENT_TEMPLATE_PARAMS names an entry twice",
        ),
        (
            {"relay_document_template_name": "vship-document"},
            "RELAY_DOCUMENT_TEMPLATE_NAME may hold only lower-case letters, digits and underscores",
        ),
    ],
)
def test_each_problem_is_reported_on_its_own(tmp_path, overrides, problem):
    assert relay_problems(tmp_path, **overrides) == [problem]
    # A relay problem never stops the settings from loading (and so never stops the forwarder): the
    # lifespan leaves only the relay off and reports why.
    loaded = relay_settings(tmp_path, **overrides)
    assert loaded.relay_problems() == [problem]


def test_placeholder_access_token_is_refused(tmp_path):
    with pytest.raises(ValidationError) as exc:
        relay_settings(tmp_path, wa_access_token="CHANGE_ME")
    assert "WA_ACCESS_TOKEN still holds a placeholder value" in str(exc.value)


def test_core_problems_still_fail_but_relay_problems_never_do(tmp_path):
    with pytest.raises(ValidationError) as exc:
        relay_settings(tmp_path, relay_poll_seconds=1, relay_folder="", mail_to="")
    message = str(exc.value)
    assert "MAIL_TO must list at least one recipient" in message
    assert "RELAY_" not in message
    loaded = relay_settings(tmp_path, relay_poll_seconds=1, relay_folder="")
    assert loaded.relay_problems() == [
        "RELAY_POLL_SECONDS must be at least 10",
        "RELAY_FOLDER must not be empty",
    ]


def test_relay_problems_are_empty_while_the_feature_is_off(tmp_path):
    loaded = relay_settings(tmp_path, relay_enabled=False, relay_poll_seconds=1)
    assert loaded.relay_problems() == []
    assert "RELAY_POLL_SECONDS must be at least 10" in loaded._relay_problems()


# ----------------------------------------------------------------------------- values that cannot be read
UNREADABLE = [
    ("RELAY_ENABLED", "flase", "RELAY_ENABLED must be true or false, not 'flase'"),
    (
        "RELAY_ENABLED",
        "",
        "RELAY_ENABLED is empty; it must be true or false (or remove the line to use the default, false)",
    ),
    (
        "RELAY_REQUIRE_INTERNAL_AUTH",
        "yes please",
        "RELAY_REQUIRE_INTERNAL_AUTH must be true or false, not 'yes please'",
    ),
    ("RELAY_DRY_RUN", "ture", "RELAY_DRY_RUN must be true or false, not 'ture'"),
    (
        "RELAY_DRY_RUN",
        "",
        "RELAY_DRY_RUN is empty; it must be true or false (or remove the line to use the default, true)",
    ),
    ("RELAY_POLL_SECONDS", "30s", "RELAY_POLL_SECONDS must be a number of seconds, not '30s'"),
    (
        "RELAY_POLL_SECONDS",
        "",
        "RELAY_POLL_SECONDS is empty; it must be a number of seconds (or remove the line to use the default,"
        " 30.0)",
    ),
    (
        "RELAY_TEMPLATE_CHECK_SECONDS",
        "10m",
        "RELAY_TEMPLATE_CHECK_SECONDS must be a whole number of seconds, not '10m'",
    ),
    (
        "RELAY_MAX_MESSAGE_CHARS",
        "1,000",
        "RELAY_MAX_MESSAGE_CHARS must be a whole number of characters, not '1,000'",
    ),
    (
        "RELAY_MAX_MESSAGE_CHARS",
        "",
        "RELAY_MAX_MESSAGE_CHARS is empty; it must be a whole number of characters (or remove the line to use"
        " the default, 820)",
    ),
    (
        "RELAY_MAX_NAME_CHARS",
        "x" * 50,
        "RELAY_MAX_NAME_CHARS must be a whole number of characters, not '" + "x" * 40 + "...'",
    ),
    ("RELAY_MAX_AGE_HOURS", "24h", "RELAY_MAX_AGE_HOURS must be a number of hours, not '24h'"),
]


@pytest.mark.parametrize(("name", "value", "problem"), UNREADABLE)
def test_an_unreadable_relay_value_never_stops_the_settings_from_loading(
    tmp_path, monkeypatch, name, value, problem
):
    """A typing mistake in a RELAY_* line must not stop the service (exit 78, not restarted by systemd)."""
    monkeypatch.setenv(name, value)
    settings = make_settings(tmp_path)  # loads: before the fix this raised a ValidationError
    assert getattr(settings, name.lower()) == Settings.model_fields[name.lower()].default
    assert settings.relay_enabled is False
    # reported even though the feature is off: a mistyped RELAY_ENABLED is what keeps it off
    assert settings.relay_problems() == [problem]
    assert settings._relay_problems()[0] == problem


@pytest.mark.parametrize(
    ("overrides", "value", "problem"),
    [
        (
            {"relay_dry_run": "ture"},
            ("relay_dry_run", True),
            "RELAY_DRY_RUN must be true or false, not 'ture'",
        ),
        (
            {"relay_require_internal_auth": "flase"},
            ("relay_require_internal_auth", True),
            "RELAY_REQUIRE_INTERNAL_AUTH must be true or false, not 'flase'",
        ),
        (
            {"relay_poll_seconds": "30 s"},
            ("relay_poll_seconds", 30.0),
            "RELAY_POLL_SECONDS must be a number of seconds, not '30 s'",
        ),
    ],
)
def test_with_the_feature_on_an_unreadable_value_leaves_only_the_feature_off(
    tmp_path, overrides, value, problem
):
    # The safe default is kept, and the lifespan does not start the relay while relay_problems() lists it:
    # an operator who wrote RELAY_DRY_RUN=flase to go live never gets a silent dry run (or the reverse).
    settings = relay_settings(tmp_path, **overrides)
    assert settings.relay_enabled is True
    assert getattr(settings, value[0]) == value[1]
    assert settings.relay_problems() == [problem]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("RELAY_ENABLED", "1", True),
        ("RELAY_ENABLED", "TRUE", True),
        ("RELAY_ENABLED", "off", False),
        ("RELAY_DRY_RUN", "no", False),
        ("RELAY_REQUIRE_INTERNAL_AUTH", "False", False),
        ("RELAY_POLL_SECONDS", "12.5", 12.5),
        ("RELAY_TEMPLATE_CHECK_SECONDS", "900", 900),
        ("RELAY_MAX_MESSAGE_CHARS", "500", 500),
        ("RELAY_MAX_NAME_CHARS", "40", 40),
        ("RELAY_MAX_AGE_HOURS", "6", 6.0),
    ],
)
def test_values_the_settings_accept_are_read_as_before(tmp_path, monkeypatch, name, value, expected):
    monkeypatch.setenv(name, value)
    settings = make_settings(tmp_path, **{k: v for k, v in RELAY_ON.items() if k != "relay_enabled"})
    assert getattr(settings, name.lower()) == expected
    assert settings._unreadable_relay_problems() == []


def test_every_relay_setting_that_is_not_text_is_read_leniently():
    """A new RELAY_* setting that is not text must be added to app.config._RELAY_TYPED (with its wording)."""
    typed = {
        name
        for name, field in Settings.model_fields.items()
        if name.startswith("relay_") and field.annotation is not str
    }
    assert typed == set(_RELAY_TYPED)
    assert len(typed) == 8


def test_settings_other_than_relay_ones_are_still_read_strictly(tmp_path):
    with pytest.raises(ValidationError, match="worker_enabled"):  # unchanged since 570fd03
        make_settings(tmp_path, worker_enabled="flase")


@pytest.fixture
def cli_logging():
    """cli.main configures logging for the command line; put the level back afterwards."""
    root = logging.getLogger()
    level = root.level
    yield
    root.setLevel(level)


def _env_file(tmp_path: Path, *lines: str) -> Path:
    env = tmp_path / "service.env"
    base = [
        "WA_VERIFY_TOKEN=verify-me-verify-me",
        "WA_APP_SECRET=app-secret-app-secret",
        "CRM_ENABLED=false",
        "MAIL_TRANSPORT=smtp",
        "SMTP_HOST=127.0.0.1",
        "SMTP_SECURITY=none",
        "SMTP_AUTH=none",
        "MAIL_FROM=whatsapp@example.com",
        "MAIL_TO=ops@example.com",
        f"STORE_PATH={tmp_path / 'queue.db'}",
        f"SPOOL_DIR={tmp_path / 'spool'}",
    ]
    env.write_text("\n".join([*base, *lines]) + "\n", encoding="utf-8")
    return env


@pytest.fixture
def clean_environment(monkeypatch):
    """Real environment variables win over the env file: keep the test machine's own out of the way."""
    for key in list(os.environ):
        if key.startswith(("RELAY_", "WA_", "MAIL_", "CRM_", "SMTP_", "WORKER_", "STORE_", "SPOOL_")):
            monkeypatch.delenv(key)
    # cli.main --env-file sets this variable; setting it here first makes monkeypatch put it back afterwards
    monkeypatch.setenv("WHATSAPP_WEBHOOK_ENV_FILE", "")


def test_check_config_warns_about_unreadable_relay_values_and_passes(
    tmp_path, capsys, cli_logging, clean_environment
):
    # install.sh and the documented restart steps restart only when check-config passes; `run` (systemd's
    # ExecStart) loads the settings the same way, so it starts too.
    env = _env_file(tmp_path, "RELAY_ENABLED=flase", "RELAY_POLL_SECONDS=30s", "RELAY_MAX_MESSAGE_CHARS=")
    assert cli.main(["--env-file", str(env), "check-config"]) == 0
    captured = capsys.readouterr()
    assert '"relay_enabled": false' in captured.out
    warning = "WARNING: WhatsApp updates from email will NOT start until this is fixed: "
    assert captured.err.splitlines() == [
        warning + "RELAY_ENABLED must be true or false, not 'flase'",
        warning + "RELAY_POLL_SECONDS must be a number of seconds, not '30s'",
        warning
        + "RELAY_MAX_MESSAGE_CHARS is empty; it must be a whole number of characters (or remove the line"
        " to use the default, 820)",
    ]
    # a broken setting the forwarder itself needs is still a configuration error, exactly as before
    broken = _env_file(tmp_path, "WORKER_ENABLED=flase")
    assert cli.main(["--env-file", str(broken), "check-config"]) == cli.EX_CONFIG == 78


def test_missing_graph_sender_and_mailbox(tmp_path):
    problems = relay_problems(tmp_path, mail_transport="smtp", graph_sender="", relay_mailbox="")
    assert problems == [
        "RELAY_ENABLED=true needs MAIL_TRANSPORT=graph (the Inbox is read through Graph)",
        "RELAY_MAILBOX (or GRAPH_SENDER) must be an email address",
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"relay_poll_seconds": 10},
        {"relay_template_check_seconds": 60},
        {"relay_max_message_chars": 1},
        {"relay_max_message_chars": 30000},
        {"relay_max_name_chars": 1},
        {"relay_max_name_chars": 200},
        {"relay_max_age_hours": 0.01},
        {"relay_template_language": "en"},
        {"relay_template_language": "fil"},
        {"relay_template_language": "pt_BR"},
        {"relay_template_name": "a" * 512},
        {"relay_template_name": "vship_update_2"},
        {"relay_template_params": "Message, NAME"},
        {"relay_template_params": "booking;message;name"},
        {
            "relay_booking_template_name": "vship_booking",
            "relay_booking_template_params": "booking,message,name",
        },
        {"relay_booking_template_params": "anything at all"},  # ignored while no booking template is named
        {"relay_document_template_name": "vship_document"},  # its params may be empty
        {"relay_document_template_name": "vship_document", "relay_document_template_params": "Filename"},
        {
            "relay_document_template_name": "vship_document",
            "relay_document_template_params": "name,booking,filename",
        },
        {"relay_document_template_params": "message,message"},  # ignored while no document template is named
        {"relay_mailbox": "updates@vship2000.com"},
        {"relay_allowed_senders": "Elizabeth@VShip2000.com;ops@vship2000.com"},
        {"relay_test_numbers": "650-555-1234; +1 (650) 555-1234, +44 20 7946 0958"},
        {"relay_require_internal_auth": False, "relay_dry_run": False},
    ],
)
def test_accepted_values(tmp_path, overrides):
    assert relay_problems(tmp_path, **overrides) == []
    relay_settings(tmp_path, **overrides)


# ----------------------------------------------------------------------------- properties
def test_relay_mailbox_address(tmp_path):
    assert make_settings(tmp_path, graph_sender=" WhatsApp@VShip2000.com ").relay_mailbox_address == (
        "whatsapp@vship2000.com"
    )
    settings = make_settings(
        tmp_path, graph_sender="whatsapp@vship2000.com", relay_mailbox=" Updates@VShip.com "
    )
    assert settings.relay_mailbox_address == "updates@vship.com"
    assert make_settings(tmp_path).relay_mailbox_address == ""


def test_relay_allowed_sender_list(tmp_path):
    settings = make_settings(
        tmp_path,
        relay_allowed_senders="Elizabeth@VShip2000.com; ops@vship2000.com,, elizabeth@vship2000.com , ",
    )
    assert settings.relay_allowed_sender_list == ["elizabeth@vship2000.com", "ops@vship2000.com"]
    assert make_settings(tmp_path).relay_allowed_sender_list == []


def test_relay_test_number_list(tmp_path):
    settings = relay_settings(
        tmp_path, relay_test_numbers="650-555-1234, +1 (650) 555-1234; 16505551234,+44 20 7946 0958"
    )
    assert settings.relay_test_number_list == ["+16505551234", "+442079460958"]
    assert relay_settings(tmp_path).relay_test_number_list == []
    # while the feature is off nothing is checked; the list simply leaves out what is not a number
    off = make_settings(tmp_path, relay_test_numbers="12345, (212) 555-0100, 0044 20 7946 0958")
    assert off.relay_test_number_list == ["+12125550100"]


def test_test_numbers_use_the_default_region(tmp_path):
    # 10 digits are read as a number of RELAY_DEFAULT_REGION
    settings = relay_settings(tmp_path, relay_default_region="GB", relay_test_numbers="20 7946 0958")
    assert settings.relay_test_number_list == ["+442079460958"]
    settings = relay_settings(tmp_path, relay_default_region="US", relay_test_numbers="20 7946 0958")
    assert settings.relay_test_number_list == ["+12079460958"]


def test_relay_param_list():
    assert Settings.relay_param_list(" Name , MESSAGE;booking ") == ("name", "message", "booking")
    assert Settings.relay_param_list("") == ()
    assert Settings.relay_param_list(" , ;") == ()
    assert Settings.relay_param_list("name,name") == ("name", "name")  # duplicates are the validator's job


def test_redacted_shows_relay_settings_and_still_hides_secrets(tmp_path):
    settings = relay_settings(tmp_path, relay_test_numbers="650-555-1234")
    out = settings.redacted()
    for name in _relay_fields():
        assert out[name] == getattr(settings, name), name
    assert out["relay_allowed_senders"] == "elizabeth@vship2000.com, ops@vship2000.com"
    assert out["relay_test_numbers"] == "650-555-1234"
    assert out["wa_access_token"] == "<set>"
    assert out["oauth_client_secret"] == "<set>"


# ----------------------------------------------------------------------------- environment names
def test_settings_are_read_from_the_documented_environment_names(tmp_path, monkeypatch):
    env = {
        "RELAY_ENABLED": "true",
        "RELAY_MAILBOX": "Updates@VShip2000.com",
        "RELAY_PHONE_NUMBER_ID": "106540352242922",
        "RELAY_ALLOWED_SENDERS": "elizabeth@vship2000.com;ops@vship2000.com",
        "RELAY_REQUIRE_INTERNAL_AUTH": "false",
        "RELAY_DRY_RUN": "false",
        "RELAY_TEST_NUMBERS": "650-555-1234",
        "RELAY_POLL_SECONDS": "45",
        "RELAY_DEFAULT_REGION": "CA",
        "RELAY_TEMPLATE_NAME": "vship_update_v2",
        "RELAY_TEMPLATE_LANGUAGE": "en",
        "RELAY_TEMPLATE_PARAMS": "message,name",
        "RELAY_BOOKING_TEMPLATE_NAME": "vship_booking_update",
        "RELAY_BOOKING_TEMPLATE_PARAMS": "name,booking,message",
        "RELAY_DOCUMENT_TEMPLATE_NAME": "vship_document",
        "RELAY_DOCUMENT_TEMPLATE_PARAMS": "filename",
        "RELAY_TEMPLATE_CHECK_SECONDS": "900",
        "RELAY_MAX_MESSAGE_CHARS": "500",
        "RELAY_MAX_NAME_CHARS": "40",
        "RELAY_NAME_FALLBACK": "friend",
        "RELAY_MAX_AGE_HOURS": "12.5",
        "RELAY_FOLDER": "WA handled",
    }
    assert sorted(name.lower() for name in env) == sorted(_relay_fields())
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    settings = make_settings(tmp_path, **{k: v for k, v in RELAY_ON.items() if not k.startswith("relay_")})
    assert settings.relay_enabled is True
    assert settings.relay_mailbox_address == "updates@vship2000.com"
    assert settings.relay_sending_number_id == "106540352242922"
    assert settings.relay_allowed_sender_list == ["elizabeth@vship2000.com", "ops@vship2000.com"]
    assert settings.relay_require_internal_auth is False
    assert settings.relay_dry_run is False
    assert settings.relay_test_number_list == ["+16505551234"]
    assert settings.relay_poll_seconds == 45.0
    assert settings.relay_default_region == "CA"
    assert (settings.relay_template_name, settings.relay_template_language) == ("vship_update_v2", "en")
    assert Settings.relay_param_list(settings.relay_template_params) == ("message", "name")
    assert settings.relay_booking_template_name == "vship_booking_update"
    assert Settings.relay_param_list(settings.relay_booking_template_params) == ("name", "booking", "message")
    assert settings.relay_document_template_name == "vship_document"
    assert Settings.relay_param_list(settings.relay_document_template_params) == ("filename",)
    assert settings.relay_template_check_seconds == 900
    assert (settings.relay_max_message_chars, settings.relay_max_name_chars) == (500, 40)
    assert settings.relay_name_fallback == "friend"
    assert settings.relay_max_age_hours == 12.5
    assert settings.relay_folder == "WA handled"


# ----------------------------------------------------------------------------- .env.example
def test_env_example_lists_every_relay_setting_with_its_default(tmp_path):
    values = dotenv_values(ENV_EXAMPLE)
    relay = {key: value for key, value in values.items() if key.startswith("RELAY_")}
    assert sorted(relay) == sorted(name.upper() for name in _relay_fields())
    loaded = make_settings(tmp_path, **{key.lower(): value for key, value in relay.items()})
    for name in _relay_fields():
        assert getattr(loaded, name) == Settings.model_fields[name].default, name


def test_env_example_explains_each_relay_setting():
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith("RELAY_"):
            assert lines[index - 1].startswith("# "), f"{line} has no comment above it"
    text = "\n".join(lines)
    assert (
        "# ---------------------------------------------------------------- WhatsApp updates from email"
        in text
    )
    above = lines[lines.index("WA_WABA_ID=") - 1]
    assert above.startswith("# Required when RELAY_ENABLED=true"), "WA_WABA_ID"
    # WA_PHONE_NUMBER_ID keeps its forwarding meaning; the sending number has its own setting
    comment = lines[lines.index("RELAY_PHONE_NUMBER_ID=") - 1]
    assert "would also filter which messages are forwarded" in comment


def test_the_sending_number_never_switches_on_the_forwarding_filter(tmp_path):
    """RELAY_PHONE_NUMBER_ID alone is enough; WA_PHONE_NUMBER_ID (the forwarding filter) can stay empty."""
    settings = relay_settings(tmp_path, wa_phone_number_id="", relay_phone_number_id="106540352242922")
    assert settings.relay_sending_number_id == "106540352242922"
    assert settings.wa_phone_number_id == ""


def test_the_sending_number_falls_back_to_the_forwarding_number(tmp_path):
    settings = relay_settings(tmp_path)
    assert settings.relay_sending_number_id == "106540352242922"


# ----------------------------------------------------------------------------- deploy/install.sh
# The feature adds modules that the forwarder imports at start-up. install.sh must not put a copy of the code
# in place that lacks one of them (deploy/gcp/deploy.sh packs only the files git tracks), and when
# check-config cannot run at all it must not send the operator off to fix the env file and restart.
REPO = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO / "deploy" / "install.sh"


def _copy_check(source: Path) -> list[str]:
    """Run install.sh's check of the copy (its embedded Python program) on ``source``."""
    program = re.search(r"<<'PY'\n(.*?)\nPY\n", INSTALL_SH.read_text(encoding="utf-8"), re.DOTALL)
    assert program, "install.sh no longer holds the copy check"
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-", str(source)],
        input=program.group(1),
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return [line for line in done.stdout.splitlines() if line]


def _copy_of_app(tmp_path: Path, *leave_out: str) -> Path:
    shutil.copytree(REPO / "app", tmp_path / "app", ignore=shutil.ignore_patterns("__pycache__"))
    for name in leave_out:
        target = tmp_path / name
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    return tmp_path


def test_install_accepts_the_complete_repository():
    assert _copy_check(REPO) == []


@pytest.mark.parametrize(
    ("leave_out", "reported"),
    [
        # what deploy.sh shipped before app/relay was added to git: the forwarder could not start any more
        (("app/relay",), "app/main.py imports app.relay.mailbox, which is not in the copy"),
        (("app/relay/processor.py",), "app/main.py imports app.relay.processor, which is not in the copy"),
        (("app/whatsapp/send.py",), "app/worker.py imports app.whatsapp.send, which is not in the copy"),
        (("app/whatsapp/admin.py",), "app/cli.py imports app.whatsapp.admin, which is not in the copy"),
    ],
)
def test_install_refuses_a_copy_with_a_module_missing(tmp_path, leave_out, reported):
    assert reported in _copy_check(_copy_of_app(tmp_path, *leave_out))


@pytest.fixture
def bash() -> str:
    found = shutil.which("bash")
    if found is None or (sys.platform == "win32" and "system32" in found.lower()):  # WSL's launcher
        pytest.skip("bash is not available")
    return found


@pytest.mark.parametrize("leave_out", [(), ("app/relay",)])
def test_install_stops_before_replacing_the_code_when_the_copy_is_incomplete(tmp_path, bash, leave_out):
    text = INSTALL_SH.read_text(encoding="utf-8")
    start = text.index('log "Checking that the copy of the code is complete"')
    end = text.index('log "Copying application code to $APP_DIR"')
    source = _copy_of_app(tmp_path / "source", *leave_out)
    script = "\n".join(
        [
            "set -euo pipefail",
            f"SRC='{source.as_posix()}'",
            "APP_DIR=/opt/whatsapp-webhook",
            "log() { :; }",
            f"python3() {{ '{Path(sys.executable).as_posix()}' \"$@\"; }}",
            text[start:end],
            "echo 'the code would be copied now'",
        ]
    )
    done = subprocess.run(  # noqa: S603
        [bash, "-c", script], capture_output=True, text=True, timeout=120, check=False
    )
    if not leave_out:
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "the code would be copied now" and done.stderr == ""
    else:
        assert done.returncode == 1 and "copied" not in done.stdout
        assert "is incomplete, so /opt/whatsapp-webhook was not changed" in done.stderr
        assert "app/main.py imports app.relay.mailbox, which is not in the copy" in done.stderr


DEPLOY_SH = REPO / "deploy" / "gcp" / "deploy.sh"


@pytest.mark.parametrize("untracked", [(), ("app/relay/processor.py", "app/whatsapp/send.py")])
def test_deploy_stops_before_uploading_when_a_file_the_service_needs_is_not_in_git(tmp_path, bash, untracked):
    """deploy.sh packs only what git tracks; a file never added to git must stop it on the admin's PC."""
    text = DEPLOY_SH.read_text(encoding="utf-8")
    start = text.index('echo "==> Checking that every file the service needs is tracked by git"')
    end = text.index('echo "==> Packaging tracked files from $REPO_ROOT"')
    listing, calls = tmp_path / "untracked.txt", tmp_path / "git-calls.txt"
    listing.write_text("".join(f"{name}\n" for name in untracked), encoding="utf-8")
    script = "\n".join(
        [
            "set -euo pipefail",
            "REPO_ROOT=/home/admin/repo",
            f"git() {{ echo \"$*\" >> '{calls.as_posix()}'; cat '{listing.as_posix()}'; }}",
            text[start:end],
            "echo 'the files would be packed now'",
        ]
    )
    done = subprocess.run(  # noqa: S603
        [bash, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert calls.read_text(encoding="utf-8").split() == [
        "-C",
        "/home/admin/repo",
        "ls-files",
        "--others",
        "--exclude-standard",
        "--",
        "app",
        "deploy",
        "requirements.txt",
        "pyproject.toml",
    ]
    if not untracked:
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip().endswith("the files would be packed now") and done.stderr == ""
    else:
        assert done.returncode == 1 and "packed" not in done.stdout
        assert "not tracked by git, so they would be left out of the deploy" in done.stderr
        assert all(name in done.stderr for name in untracked) and "Nothing was uploaded." in done.stderr


@pytest.mark.parametrize("exit_code", [0, 78, 1])
def test_install_restarts_only_when_check_config_passes_and_says_what_failed(tmp_path, bash, exit_code):
    text = INSTALL_SH.read_text(encoding="utf-8")
    start, end = text.index('log "Checking configuration"'), text.index("\ncat <<EOF\n\nDone.")
    env = tmp_path / "env"
    env.write_text("MAIL_TO=ops@example.com\n", encoding="utf-8")
    script = "\n".join(
        [
            "set -euo pipefail",
            f"ENV_FILE='{env.as_posix()}'",
            "APP_DIR=/opt/whatsapp-webhook",
            "log() { :; }",
            'systemctl() { echo "systemctl $*"; }',
            "sleep() { :; }",
            f'fake_cli() {{ echo "the error" >&2; return {exit_code}; }}',
            text[start:end].replace("/usr/local/bin/whatsapp-webhook", "fake_cli"),
        ]
    )
    done = subprocess.run(  # noqa: S603
        [bash, "-c", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    restarted = "systemctl restart whatsapp-webhook" in done.stdout
    advice = f"fix {env.as_posix()} and run: systemctl restart whatsapp-webhook"
    if exit_code == 0:
        assert restarted and advice not in done.stderr
    elif exit_code == 78:  # a configuration error: the env file is what needs fixing
        assert not restarted and advice in done.stderr
    else:  # check-config could not even start: restarting would stop the forwarder
        assert not restarted and advice not in done.stderr
        assert "could not run (exit code 1" in done.stderr and "NOT restarted" in done.stderr
    assert subprocess.run([bash, "-n", str(INSTALL_SH)], check=False, timeout=60).returncode == 0  # noqa: S603
