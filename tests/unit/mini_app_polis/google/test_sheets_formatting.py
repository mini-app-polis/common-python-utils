from __future__ import annotations


def test_formatter_batch_update_retries(monkeypatch, as_http_error):
    from mini_app_polis.google import _retry as retry_mod
    from mini_app_polis.google._retry import random as retry_random
    from mini_app_polis.google.sheets_formatting import SheetsFormatter

    # Make retry deterministic and fast
    monkeypatch.setattr(retry_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(retry_random, "random", lambda: 0.0)

    class _Svc:
        def __init__(self):
            self.n = 0

        def spreadsheets(self):
            svc = self

            class _Sheets:
                def batchUpdate(self, spreadsheetId, body):
                    _ = (spreadsheetId, body)

                    def run():
                        svc.n += 1
                        if svc.n < 3:
                            raise as_http_error(status=503)
                        return {"ok": True}

                    class _Exec:
                        def execute(self_inner):
                            return run()

                    return _Exec()

            return _Sheets()

    svc = _Svc()
    fmt = SheetsFormatter(sheets_service=svc)

    class _Spreadsheet:
        id = "ssid"

    class _Sheet:
        spreadsheet = _Spreadsheet()
        id = 123
        col_count = 5

    fmt.apply_sheet_formatting(_Sheet())
    assert svc.n == 3


def test_apply_sheet_formatting_builds_single_batch_update(monkeypatch):
    """Smoke-test that SheetsFormatter.apply_sheet_formatting composes requests and calls batchUpdate."""

    from mini_app_polis.google.sheets_formatting import SheetsFormatter

    captured = {"requests": None, "operation": None}

    fmt = SheetsFormatter(sheets_service=object())

    def capture_batch_update(spreadsheet_id, requests, *, operation, max_attempts=5):
        captured["requests"] = requests
        captured["operation"] = operation

    monkeypatch.setattr(fmt, "_batch_update", capture_batch_update)

    class _Spreadsheet:
        id = "ssid"

    class _Sheet:
        spreadsheet = _Spreadsheet()
        id = 123
        col_count = 5

    fmt.apply_sheet_formatting(_Sheet())

    assert isinstance(captured["requests"], list)
    assert len(captured["requests"]) == 4
    assert captured["operation"] and "format sheetid" in captured["operation"].lower()


# ---------------------------------------------------------------------------
# MagicMock-backed tests: every assertion is on the exact request the Sheets
# API received.
# ---------------------------------------------------------------------------


def _formatter(monkeypatch):
    from unittest.mock import MagicMock

    from mini_app_polis.google import sheets_formatting as fmt_mod

    svc = MagicMock()
    log = MagicMock()
    sleeps: list[float] = []
    monkeypatch.setattr(fmt_mod, "log", log)
    monkeypatch.setattr(fmt_mod, "time", type("T", (), {"sleep": sleeps.append}))
    return fmt_mod, fmt_mod.SheetsFormatter(sheets_service=svc), svc, log, sleeps


def _sent_requests(svc) -> list[list[dict]]:
    batch = svc.spreadsheets.return_value.batchUpdate
    return [c.kwargs["body"]["requests"] for c in batch.call_args_list]


def _sheet_meta(sheet_id, title, columns=None):
    props = {"sheetId": sheet_id, "title": title}
    if columns is not None:
        props["gridProperties"] = {"columnCount": columns}
    return {"properties": props}


def _pixel_meta(sizes_by_sheet: dict[int, list[int | None]]):
    return {
        "sheets": [
            {
                "properties": {"sheetId": sid},
                "data": [{"columnMetadata": [{"pixelSize": s} for s in sizes]}],
            }
            for sid, sizes in sizes_by_sheet.items()
        ]
    }


def test_sheets_service_is_built_lazily_from_auth_once(monkeypatch):
    from mini_app_polis.google import sheets_formatting as fmt_mod

    auth = object()
    loaded, built = [], []
    monkeypatch.setattr(
        fmt_mod, "load_credentials", lambda a: loaded.append(a) or "creds"
    )
    monkeypatch.setattr(
        fmt_mod, "build_sheets_service", lambda c: built.append(c) or "svc"
    )

    fmt = fmt_mod.SheetsFormatter(auth=auth)
    assert loaded == []

    assert fmt.sheets_service == "svc"
    assert fmt.sheets_service == "svc"
    assert loaded == [auth]
    assert built == ["creds"]


def test_apply_sheet_formatting_sends_the_four_requests_for_that_sheet(monkeypatch):
    _, fmt, svc, _, _ = _formatter(monkeypatch)

    class _Sheet:
        spreadsheet = type("S", (), {"id": "ssid"})()
        id = 42
        col_count = 3

    fmt.apply_sheet_formatting(_Sheet())

    batch = svc.spreadsheets.return_value.batchUpdate
    batch.assert_called_once()
    assert batch.call_args.kwargs["spreadsheetId"] == "ssid"
    (reqs,) = _sent_requests(svc)
    assert reqs[0] == {
        "updateSheetProperties": {
            "properties": {"sheetId": 42, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }
    }
    assert reqs[1]["repeatCell"]["range"]["endColumnIndex"] == 3
    # Only fontSize and alignment: replacing textFormat would wipe hyperlinks.
    assert reqs[1]["repeatCell"]["fields"] == (
        "userEnteredFormat.textFormat.fontSize,userEnteredFormat.horizontalAlignment"
    )
    assert reqs[2]["repeatCell"]["range"] == {
        "sheetId": 42,
        "startRowIndex": 0,
        "endRowIndex": 1,
        "startColumnIndex": 0,
        "endColumnIndex": 3,
    }
    assert reqs[3] == {
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": 42,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": 3,
            }
        }
    }


def test_apply_sheet_formatting_defaults_columns_when_count_is_missing_or_zero(
    monkeypatch,
):
    fmt_mod, fmt, svc, _, _ = _formatter(monkeypatch)

    class _Sheet:
        spreadsheet = type("S", (), {"id": "ssid"})()
        id = 1
        col_count = 0

    fmt.apply_sheet_formatting(_Sheet())

    (reqs,) = _sent_requests(svc)
    assert (
        reqs[3]["autoResizeDimensions"]["dimensions"]["endIndex"]
        == fmt_mod.DEFAULT_NUM_COLUMNS
    )


def test_apply_sheet_formatting_logs_and_swallows_a_failure(monkeypatch, as_http_error):
    _, fmt, svc, log, _ = _formatter(monkeypatch)
    svc.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = (
        as_http_error(status=400, message="bad request")
    )

    class _Sheet:
        spreadsheet = type("S", (), {"id": "ssid"})()
        id = 1
        col_count = 2
        title = "Tab A"

    fmt.apply_sheet_formatting(_Sheet())  # does not raise

    # 400 is not retryable: one attempt only.
    batch = svc.spreadsheets.return_value.batchUpdate
    assert batch.return_value.execute.call_count == 1
    (msg,) = log.warning.call_args.args
    assert "Tab A" in msg and "bad request" in msg


def test_apply_formatting_to_sheet_formats_every_sheet_then_buffers_widths(
    monkeypatch,
):
    fmt_mod, fmt, svc, log, sleeps = _formatter(monkeypatch)
    get = svc.spreadsheets.return_value.get
    get.return_value.execute.side_effect = [
        {"sheets": [_sheet_meta(11, "A", columns=4), _sheet_meta(12, "B")]},
        _pixel_meta({11: [100, None, 340, 350, 50], 12: []}),
    ]

    fmt.apply_formatting_to_sheet("ssid")

    assert get.call_args_list[0].kwargs == {"spreadsheetId": "ssid"}
    assert get.call_args_list[1].kwargs == {
        "spreadsheetId": "ssid",
        "includeGridData": True,
        "fields": "sheets(properties(sheetId,title),data(columnMetadata(pixelSize)))",
    }

    format_reqs, width_reqs = _sent_requests(svc)
    assert len(format_reqs) == 8
    assert [
        r["autoResizeDimensions"]["dimensions"]
        for r in format_reqs
        if "autoResizeDimensions" in r
    ] == [
        {"sheetId": 11, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 4},
        # No gridProperties: the default column count is used.
        {
            "sheetId": 12,
            "dimension": "COLUMNS",
            "startIndex": 0,
            "endIndex": fmt_mod.DEFAULT_NUM_COLUMNS,
        },
    ]

    # Column 0 grows by the buffer; column 1 has no size; column 2 is capped;
    # column 3 is already at the cap; column 4 is beyond columnCount.
    assert width_reqs == [
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": 11,
                    "dimension": "COLUMNS",
                    "startIndex": 0,
                    "endIndex": 1,
                },
                "properties": {"pixelSize": 100 + fmt_mod.AUTORESIZE_BUFFER_PX},
                "fields": "pixelSize",
            }
        },
        {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": 11,
                    "dimension": "COLUMNS",
                    "startIndex": 2,
                    "endIndex": 3,
                },
                "properties": {"pixelSize": fmt_mod.AUTORESIZE_MAX_PX},
                "fields": "pixelSize",
            }
        },
    ]
    assert sleeps == []
    log.error.assert_not_called()


def test_apply_formatting_to_sheet_chunks_requests_and_throttles(monkeypatch):
    fmt_mod, fmt, svc, _, sleeps = _formatter(monkeypatch)
    monkeypatch.setattr(fmt_mod, "FORMAT_REQUEST_CHUNK_SIZE", 4)
    monkeypatch.setattr(fmt_mod, "WIDTH_REQUEST_CHUNK_SIZE", 1)
    svc.spreadsheets.return_value.get.return_value.execute.side_effect = [
        {"sheets": [_sheet_meta(1, "A", 2), _sheet_meta(2, "B", 2)]},
        _pixel_meta({1: [10, 20]}),
    ]

    fmt.apply_formatting_to_sheet("ssid")

    sent = _sent_requests(svc)
    # Two format chunks of 4, then two width chunks of 1.
    assert [len(r) for r in sent] == [4, 4, 1, 1]
    # Each format chunk holds one sheet's four requests.
    assert [
        chunk[0]["updateSheetProperties"]["properties"]["sheetId"] for chunk in sent[:2]
    ] == [1, 2]
    # A pause between chunks, never after the last of a pass.
    assert sleeps == [fmt_mod.CHUNK_THROTTLE_S, fmt_mod.CHUNK_THROTTLE_S]


def test_apply_formatting_to_sheet_with_no_sheets_sends_nothing(monkeypatch):
    _, fmt, svc, log, _ = _formatter(monkeypatch)
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {"sheets": []}

    fmt.apply_formatting_to_sheet("ssid")

    svc.spreadsheets.return_value.batchUpdate.assert_not_called()
    log.info.assert_called_once_with("No sheets found to format; nothing to do")


def test_apply_formatting_to_sheet_logs_a_metadata_failure_instead_of_raising(
    monkeypatch, as_http_error
):
    _, fmt, svc, log, _ = _formatter(monkeypatch)
    svc.spreadsheets.return_value.get.return_value.execute.side_effect = as_http_error(
        status=404, message="Requested entity was not found"
    )

    fmt.apply_formatting_to_sheet("missing")

    svc.spreadsheets.return_value.batchUpdate.assert_not_called()
    (msg,) = log.error.call_args.args
    assert "Requested entity was not found" in msg


def test_a_failed_width_buffer_pass_does_not_undo_the_formatting(
    monkeypatch, as_http_error
):
    _, fmt, svc, log, _ = _formatter(monkeypatch)
    svc.spreadsheets.return_value.get.return_value.execute.side_effect = [
        {"sheets": [_sheet_meta(5, "A", 2)]},
        as_http_error(status=400, message="pixel fetch failed"),
    ]

    fmt.apply_formatting_to_sheet("ssid")

    (format_reqs,) = _sent_requests(svc)
    assert len(format_reqs) == 4
    (warning,) = log.warning.call_args.args
    assert "Column width buffer pass failed" in warning
    assert "pixel fetch failed" in warning
    log.error.assert_not_called()
    log.info.assert_called_with("✅ Formatting applied successfully to all sheets")


def test_width_buffer_pass_skips_sheets_without_an_id_or_sizes(monkeypatch):
    _, fmt, svc, _, _ = _formatter(monkeypatch)
    pixel_meta = _pixel_meta({3: [100]})
    # A sheet the grid-data fetch returned no column data for.
    pixel_meta["sheets"].append({"properties": {"sheetId": 4}, "data": []})
    svc.spreadsheets.return_value.get.return_value.execute.return_value = pixel_meta

    fmt._apply_column_width_buffer_pass(
        spreadsheet_id="ssid",
        sheets_metadata=[
            {"properties": {"title": "no id"}},
            _sheet_meta(4, "no sizes", 2),
            _sheet_meta(3, "zero columns", 0),
        ],
    )

    # columnCount 0 falls back to the default, so sheet 3's one column applies.
    (reqs,) = _sent_requests(svc)
    assert [r["updateDimensionProperties"]["range"]["sheetId"] for r in reqs] == [3]


def test_width_buffer_pass_includes_the_first_tab_with_sheet_id_zero(monkeypatch):
    # Google gives a spreadsheet's first tab sheetId 0; it is an id, not a
    # missing one, so its columns must be buffered like any other tab's.
    fmt_mod, fmt, svc, _, _ = _formatter(monkeypatch)
    svc.spreadsheets.return_value.get.return_value.execute.return_value = _pixel_meta(
        {0: [100], 7: [100]}
    )

    assert fmt._get_column_pixel_sizes("ssid") == {0: [100], 7: [100]}

    fmt._apply_column_width_buffer_pass(
        spreadsheet_id="ssid",
        sheets_metadata=[_sheet_meta(0, "First", 1), _sheet_meta(7, "Other", 1)],
    )

    (reqs,) = _sent_requests(svc)
    assert [r["updateDimensionProperties"]["range"]["sheetId"] for r in reqs] == [0, 7]
    assert reqs[0]["updateDimensionProperties"]["properties"] == {
        "pixelSize": 100 + fmt_mod.AUTORESIZE_BUFFER_PX
    }


def test_width_buffer_pass_sends_nothing_when_no_column_can_grow(monkeypatch):
    fmt_mod, fmt, svc, _, _ = _formatter(monkeypatch)
    at_cap = fmt_mod.AUTORESIZE_MAX_PX
    svc.spreadsheets.return_value.get.return_value.execute.return_value = _pixel_meta(
        {3: [at_cap, None, at_cap + 10]}
    )

    fmt._apply_column_width_buffer_pass(
        spreadsheet_id="ssid", sheets_metadata=[_sheet_meta(3, "A", 3)]
    )

    svc.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_set_column_text_formatting_targets_the_named_sheet(monkeypatch):
    _, fmt, svc, _, _ = _formatter(monkeypatch)
    get = svc.spreadsheets.return_value.get
    get.return_value.execute.return_value = {
        "sheets": [_sheet_meta(1, "Other"), _sheet_meta(9, "Data")]
    }

    fmt.set_column_text_formatting("ssid", "Data", [0, "3"])

    get.assert_called_once_with(
        spreadsheetId="ssid", fields="sheets(properties(sheetId,title))"
    )
    (reqs,) = _sent_requests(svc)
    assert [r["repeatCell"]["range"] for r in reqs] == [
        {"sheetId": 9, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
        {"sheetId": 9, "startRowIndex": 1, "startColumnIndex": 3, "endColumnIndex": 4},
    ]
    assert reqs[0]["repeatCell"]["cell"] == {
        "userEnteredFormat": {"numberFormat": {"type": "TEXT", "pattern": "@"}}
    }
    assert reqs[0]["repeatCell"]["fields"] == "userEnteredFormat.numberFormat"


def test_set_column_text_formatting_raises_for_an_unknown_sheet(monkeypatch):
    import pytest

    _, fmt, svc, _, _ = _formatter(monkeypatch)
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [_sheet_meta(1, "Other")]
    }

    with pytest.raises(
        ValueError, match="Sheet named 'Data' not found in spreadsheet ssid"
    ):
        fmt.set_column_text_formatting("ssid", "Data", [0])
    svc.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_set_column_text_formatting_with_no_columns_sends_nothing(monkeypatch):
    _, fmt, svc, _, _ = _formatter(monkeypatch)
    svc.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [_sheet_meta(9, "Data")]
    }

    fmt.set_column_text_formatting("ssid", "Data", [])

    svc.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_reorder_sheets_puts_named_sheets_first_then_the_rest_in_order(monkeypatch):
    _, fmt, svc, _, _ = _formatter(monkeypatch)
    metadata = {
        "sheets": [
            _sheet_meta(1, "A"),
            _sheet_meta(2, "B"),
            _sheet_meta(3, "C"),
            _sheet_meta(4, "D"),
        ]
    }

    fmt.reorder_sheets("ssid", ["C", "Missing", "A"], metadata)

    (reqs,) = _sent_requests(svc)
    assert [
        (
            r["updateSheetProperties"]["properties"]["sheetId"],
            r["updateSheetProperties"]["properties"]["index"],
        )
        for r in reqs
    ] == [(3, 0), (1, 1), (2, 2), (4, 3)]
    assert {r["updateSheetProperties"]["fields"] for r in reqs} == {"index"}
    # Metadata is supplied by the caller; nothing is fetched.
    svc.spreadsheets.return_value.get.assert_not_called()


def test_reorder_sheets_with_no_sheets_sends_nothing(monkeypatch):
    _, fmt, svc, _, _ = _formatter(monkeypatch)

    fmt.reorder_sheets("ssid", ["A"], {})

    svc.spreadsheets.return_value.batchUpdate.assert_not_called()


def test_reorder_sheets_re_raises_an_http_error(monkeypatch, as_http_error):
    import pytest

    _, fmt, svc, log, _ = _formatter(monkeypatch)
    boom = as_http_error(status=400, message="invalid index")
    svc.spreadsheets.return_value.batchUpdate.return_value.execute.side_effect = boom

    with pytest.raises(type(boom)):
        fmt.reorder_sheets("ssid", ["A"], {"sheets": [_sheet_meta(1, "A")]})
    (msg,) = log.error.call_args.args
    assert "reordering sheets" in msg
