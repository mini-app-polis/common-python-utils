from __future__ import annotations


def test_retry_execute_with_retry_zero_retries_is_clamped_to_one_attempt():
    from kaiano.google._retry import RetryConfig, execute_with_retry

    # RetryConfig defensively clamps max_retries to at least 1.
    retry = RetryConfig(max_retries=0)
    assert retry.max_retries == 1

    # With one attempt, the function should run normally.
    assert execute_with_retry(lambda: 1, context="unit", retry=retry) == 1


def test_errors_module_smoke():
    from kaiano.google.errors import GoogleAPIError, NotFoundError

    assert issubclass(NotFoundError, GoogleAPIError)


def test_sheets_facade_exercises_write_append_clear_insert_sort(monkeypatch):
    # Reuse FakeSheetsService from the other test module (pytest loads tests as top-level modules)
    import importlib

    from kaiano.google.sheets import SheetsFacade

    FakeSheetsService = importlib.import_module("test_sheets_facade").FakeSheetsService

    svc = FakeSheetsService()
    sheets = SheetsFacade(svc)

    sheets.write_values("ssid", "Sheet1!A1", [["a"]])
    sheets.append_values("ssid", "Sheet1!A1", [["b"]])
    sheets.clear("ssid", "Sheet1!A1")
    sheets.insert_rows("ssid", "Inserted", [["h"], ["1"]])
    sheets.sort_sheet("ssid", "Sheet1", 0, ascending=False, start_row=1, end_row=10)

    ops = [c[0] for c in svc.calls]
    assert "values.update" in ops
    assert "values.append" in ops
    assert "values.clear" in ops
    assert "batchUpdate" in ops


def test_drive_facade_exercises_remaining_helpers(monkeypatch, tmp_path):
    import importlib

    from kaiano.google.drive import DriveFacade
    from kaiano.google.types import DriveFile

    FakeDriveService = importlib.import_module("test_drive_facade").FakeDriveService

    svc = FakeDriveService()

    # Force Drive "find" query to return no existing spreadsheets so create path is taken.
    original_files = svc.files

    class _Exec:
        def __init__(self, fn):
            self._fn = fn

        def execute(self):
            return self._fn()

    def files_empty_list():
        f = original_files()

        def list(**kwargs):
            return _Exec(lambda: {"files": []})

        f.list = list  # type: ignore[attr-defined]
        return f

    svc.files = files_empty_list  # type: ignore[assignment]
    drive = DriveFacade(svc)

    # find_or_create_spreadsheet -> create path
    monkeypatch.setattr(
        drive,
        "create_spreadsheet_in_folder",
        lambda _name, _folder_id: "new_sheet",
    )
    assert (
        drive.find_or_create_spreadsheet(parent_folder_id="p", name="X") == "new_sheet"
    )

    # get_all_subfolders / get_files_in_folder just call list_files
    monkeypatch.setattr(
        drive,
        "list_files",
        lambda *_a, **_k: [DriveFile(id="f", name="n")],
    )
    assert drive.get_all_subfolders("p")[0].id == "f"
    assert drive.get_files_in_folder("p")[0].name == "n"

    # upload_csv_as_google_sheet + create_spreadsheet_in_folder cover media upload mimeType
    p = tmp_path / "x.csv"
    p.write_text("a,b")
    drive.upload_csv_as_google_sheet(str(p), parent_id="p", dest_name="Y")
    drive.create_spreadsheet_in_folder("S", "p")
