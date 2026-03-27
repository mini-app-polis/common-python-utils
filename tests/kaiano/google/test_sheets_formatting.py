from __future__ import annotations


def test_formatter_batch_update_retries(monkeypatch, as_http_error):
    from kaiano.google import _retry as retry_mod
    from kaiano.google._retry import random as retry_random
    from kaiano.google.sheets_formatting import SheetsFormatter

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

    from kaiano.google.sheets_formatting import SheetsFormatter

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
