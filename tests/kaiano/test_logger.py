from __future__ import annotations

import datetime
import importlib
import logging
import sys


def _load_real_logger_module():
    # tests/kaiano/google/conftest.py stubs kaiano.logger for the whole session;
    # load the real implementation for this test module.
    sys.modules.pop("kaiano.logger", None)
    importlib.invalidate_caches()
    return importlib.import_module("kaiano.logger")


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
