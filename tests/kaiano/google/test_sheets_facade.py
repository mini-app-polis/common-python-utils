from __future__ import annotations

from kaiano.google.sheets import SheetsFacade


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeSheetsService:
    def __init__(self):
        self.calls = []
        self._meta = {
            "sheets": [
                {"properties": {"title": "Sheet1", "sheetId": 111}},
                {"properties": {"title": "Other", "sheetId": 222}},
            ]
        }
        self.values_store = {}

    def spreadsheets(self):
        service = self

        class _Spreadsheets:
            def get(self, spreadsheetId, fields=None):
                service.calls.append(("get", spreadsheetId, fields))
                return _Exec(lambda: service._meta)

            def batchUpdate(self, spreadsheetId, body):
                service.calls.append(("batchUpdate", spreadsheetId, body))
                return _Exec(lambda: {"ok": True, "body": body})

            def values(self):
                class _Values:
                    def get(self, spreadsheetId, range):
                        service.calls.append(("values.get", spreadsheetId, range))
                        return _Exec(
                            lambda: {"values": service.values_store.get(range, [])}
                        )

                    def update(self, spreadsheetId, range, valueInputOption, body):
                        service.calls.append(
                            (
                                "values.update",
                                spreadsheetId,
                                range,
                                valueInputOption,
                                body,
                            )
                        )
                        service.values_store[range] = body.get("values", [])
                        return _Exec(lambda: {"updated": True})

                    def append(
                        self,
                        spreadsheetId,
                        range,
                        valueInputOption,
                        insertDataOption,
                        body,
                    ):
                        service.calls.append(
                            (
                                "values.append",
                                spreadsheetId,
                                range,
                                valueInputOption,
                                insertDataOption,
                                body,
                            )
                        )
                        service.values_store.setdefault(range, []).extend(
                            body.get("values", [])
                        )
                        return _Exec(lambda: {"appended": True})

                    def clear(self, spreadsheetId, range, body):
                        _ = body
                        service.calls.append(("values.clear", spreadsheetId, range))
                        service.values_store[range] = []
                        return _Exec(lambda: {"cleared": True})

                return _Values()

        return _Spreadsheets()


def test_read_values_casts_to_strings(monkeypatch):

    svc = FakeSheetsService()
    svc.values_store["Sheet1!A1:C2"] = [[1, None, True]]

    sheets = SheetsFacade(svc)
    out = sheets.read_values("ssid", "Sheet1!A1:C2")
    assert out == [["1", "", "True"]]


def test_ensure_sheet_exists_adds_sheet_and_headers(monkeypatch):

    svc = FakeSheetsService()
    # Remove target sheet from metadata so ensure_sheet_exists creates it
    svc._meta = {"sheets": [{"properties": {"title": "Sheet1", "sheetId": 111}}]}

    sheets = SheetsFacade(svc)
    sheets.ensure_sheet_exists("ssid", "New", headers=["A", "B"])

    # addSheet request + header write
    assert any(c[0] == "batchUpdate" for c in svc.calls)
    assert svc.values_store["New!A1"] == [["A", "B"]]


def test_get_sheet_id_success_and_failure(monkeypatch):

    svc = FakeSheetsService()
    sheets = SheetsFacade(svc)
    assert sheets.get_sheet_id("ssid", "Other") == 222

    try:
        sheets.get_sheet_id("ssid", "Missing")
    except ValueError as e:
        assert "Missing" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_delete_sheet_by_name_ignores_http_error(monkeypatch, as_http_error):
    from kaiano.google._retry import RetryConfig

    # Avoid real sleeps from execute_with_retry() while simulating 5xx
    monkeypatch.setattr("kaiano.google._retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("kaiano.google._retry.random.random", lambda: 0.0)

    svc = FakeSheetsService()

    # Make batchUpdate raise an HttpError, which should be ignored
    original = svc.spreadsheets

    def spreadsheets():
        sp = original()

        def batchUpdate(spreadsheetId, body):
            return _Exec(lambda: (_ for _ in ()).throw(as_http_error(status=500)))

        sp.batchUpdate = batchUpdate  # type: ignore[attr-defined]
        return sp

    svc.spreadsheets = spreadsheets  # type: ignore[assignment]

    sheets = SheetsFacade(
        svc, retry=RetryConfig(max_retries=1, base_delay_s=0.0, max_delay_s=0.0)
    )
    sheets.delete_sheet_by_name("ssid", "Other")  # should not raise


def test_clear_all_except_one_sheet_creates_then_deletes_and_clears(monkeypatch):

    svc = FakeSheetsService()
    # metadata has no 'Keep' sheet
    svc._meta = {"sheets": [{"properties": {"title": "Old", "sheetId": 1}}]}

    sheets = SheetsFacade(svc)
    sheets.clear_all_except_one_sheet("ssid", "Keep")

    # Should have added Keep, deleted Old, and cleared Keep
    batch_calls = [c for c in svc.calls if c[0] == "batchUpdate"]
    assert batch_calls
    assert any("addSheet" in req for req in batch_calls[0][2]["requests"])
    assert any(c[0] == "values.clear" and c[2] == "Keep!A:Z" for c in svc.calls)


def test_get_range_format():

    assert SheetsFacade.get_range_format("A", 5, "D") == "A5:D"
    assert SheetsFacade.get_range_format("A", 5, "D", 10) == "A5:D10"


def test_get_metadata_retries_transient_then_succeeds(monkeypatch, as_http_error):

    monkeypatch.setattr("kaiano.google._retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("kaiano.google._retry.random.random", lambda: 0.0)

    svc = FakeSheetsService()
    calls = {"n": 0}

    original = svc.spreadsheets

    def spreadsheets():
        sp = original()
        old_get = sp.get

        def get(spreadsheetId, fields=None):
            calls["n"] += 1
            if calls["n"] < 3:
                return _Exec(lambda: (_ for _ in ()).throw(as_http_error(status=503)))
            return old_get(spreadsheetId, fields=fields)

        sp.get = get  # type: ignore[attr-defined]
        return sp

    svc.spreadsheets = spreadsheets  # type: ignore[assignment]

    sheets = SheetsFacade(svc)
    meta = sheets.get_metadata("ssid", max_retries=5)
    assert meta["sheets"][0]["properties"]["title"] == "Sheet1"
    assert calls["n"] == 3


def test_get_metadata_exhausts_transient_and_raises(monkeypatch, as_http_error):

    monkeypatch.setattr("kaiano.google._retry.time.sleep", lambda _s: None)
    monkeypatch.setattr("kaiano.google._retry.random.random", lambda: 0.0)

    svc = FakeSheetsService()

    original = svc.spreadsheets

    def spreadsheets():
        sp = original()

        def get(spreadsheetId, fields=None):
            return _Exec(lambda: (_ for _ in ()).throw(as_http_error(status=503)))

        sp.get = get  # type: ignore[attr-defined]
        return sp

    svc.spreadsheets = spreadsheets  # type: ignore[assignment]

    sheets = SheetsFacade(svc)
    try:
        sheets.get_metadata("ssid", max_retries=2)
    except Exception as e:
        assert "HttpError" in str(e)
    else:
        raise AssertionError("Expected HttpError")


def test_get_metadata_falls_back_to_execute_with_retry(monkeypatch):
    """If max_retries is 0, the manual retry loop is skipped."""

    svc = FakeSheetsService()
    sheets = SheetsFacade(svc)
    meta = sheets.get_metadata("ssid", max_retries=0)
    assert meta["sheets"][1]["properties"]["title"] == "Other"


def test_normalize_cell_handles_none_and_whitespace():
    assert SheetsFacade.normalize_cell(None) == ""
    assert SheetsFacade.normalize_cell("") == ""
    assert SheetsFacade.normalize_cell("   ") == ""
    assert SheetsFacade.normalize_cell("  hello  ") == "hello"
    assert SheetsFacade.normalize_cell("\tworld\n") == "world"
    assert SheetsFacade.normalize_cell(123) == "123"


def test_normalize_row_normalizes_each_cell():
    row = [None, "  a ", 5, "\t b\n", "", "  "]
    assert SheetsFacade.normalize_row(row) == [
        "",
        "a",
        "5",
        "b",
        "",
        "",
    ]


def test_normalize_row_preserves_length_and_order():
    row = ["a", None, "b", None]
    result = SheetsFacade.normalize_row(row)

    assert len(result) == 4
    assert result == ["a", "", "b", ""]


def test_get_range_format_without_end_row():
    assert SheetsFacade.get_range_format("A", 1, "D") == "A1:D"
    assert SheetsFacade.get_range_format("B", 5, "Z") == "B5:Z"


def test_get_range_format_with_end_row():
    assert SheetsFacade.get_range_format("A", 1, "D", 10) == "A1:D10"
    assert SheetsFacade.get_range_format("AA", 3, "AB", 7) == "AA3:AB7"
