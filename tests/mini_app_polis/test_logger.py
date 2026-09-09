from __future__ import annotations

import datetime
import importlib
import logging
import sys


def _load_real_logger_module():
    # tests/mini_app_polis/google/conftest.py stubs mini_app_polis.logger for the whole session;
    # load the real implementation for this test module.
    sys.modules.pop("mini_app_polis.logger", None)
    importlib.invalidate_caches()
    return importlib.import_module("mini_app_polis.logger")


def test_log_constants_are_non_empty_strings() -> None:
    logger_mod = _load_real_logger_module()
    for value in (
        logger_mod.LOG_START,
        logger_mod.LOG_SUCCESS,
        logger_mod.LOG_FAILURE,
        logger_mod.LOG_WARNING,
    ):
        assert isinstance(value, str)
        assert value.strip()


def test_with_log_prefix_starts_with_emoji_and_contains_message() -> None:
    logger_mod = _load_real_logger_module()
    out = logger_mod.with_log_prefix(logger_mod.LOG_START, "pipeline started")
    assert out.startswith(logger_mod.LOG_START)
    assert "pipeline started" in out


def test_with_log_prefix_strips_extra_whitespace() -> None:
    logger_mod = _load_real_logger_module()
    out = logger_mod.with_log_prefix(
        f" {logger_mod.LOG_START} ", "  pipeline   started   "
    )
    assert out == f"{logger_mod.LOG_START} pipeline started"


def test_format_date_known_datetime() -> None:
    logger_mod = _load_real_logger_module()
    dt = datetime.datetime(2026, 3, 27, 9, 5)
    assert logger_mod.format_date(dt) == "2026-03-27 09:05"


def test_get_logger_returns_logger_instance() -> None:
    logger_mod = _load_real_logger_module()
    assert isinstance(logger_mod.get_logger(), logging.Logger)


def test_default_level_is_info_not_debug(monkeypatch) -> None:
    """The default reaches every library, because basicConfig configures root.

    It defaulted to DEBUG, which is how ``websockets`` came to log
    ``Authorization: bearer <PREFECT_API_KEY>`` into Railway.
    """
    monkeypatch.delenv("LOGGING_LEVEL", raising=False)
    logger_mod = _load_real_logger_module()
    assert logger_mod._level == "INFO"


def test_credential_bearing_loggers_are_floored_at_import() -> None:
    logger_mod = _load_real_logger_module()
    for name in logger_mod._CREDENTIAL_BEARING_LOGGERS:
        assert logging.getLogger(name).level >= logging.INFO


def test_logging_level_debug_does_not_reopen_the_credential_leak(
    monkeypatch,
) -> None:
    """Debugging your own code must not start printing your secrets."""
    monkeypatch.setenv("LOGGING_LEVEL", "DEBUG")
    logger_mod = _load_real_logger_module()
    assert logger_mod._level == "DEBUG"
    assert logging.getLogger("websockets").level >= logging.INFO


def test_clamp_does_not_lower_a_quieter_setting() -> None:
    """A consumer that already silenced one of these keeps its setting."""
    logger_mod = _load_real_logger_module()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger_mod._clamp_credential_bearing_loggers()
    assert logging.getLogger("httpx").level == logging.WARNING
