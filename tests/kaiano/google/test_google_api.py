def test_google_api_from_env_builds_facades(monkeypatch):
    from kaiano.google.google import GoogleAPI

    monkeypatch.setattr("kaiano.google.google.load_credentials", lambda _auth: object())
    monkeypatch.setattr(
        "kaiano.google.google.build_sheets_service", lambda _c: "sheets"
    )
    monkeypatch.setattr("kaiano.google.google.build_drive_service", lambda _c: "drive")
    monkeypatch.setattr(
        "kaiano.google.google.build_gspread_client", lambda _c: "gspread"
    )

    g = GoogleAPI.from_env()
    assert g.sheets.service == "sheets"
    assert g.drive.service == "drive"
    assert g.gspread == "gspread"


def test_google_api_from_service_account_file_passes_file(monkeypatch):
    from kaiano.google import GoogleAPI

    captured = {}

    def fake_from_env(*, auth=None, retry=None):
        captured["auth"] = auth
        return "ok"

    monkeypatch.setattr(
        GoogleAPI,
        "from_env",
        classmethod(lambda _cls, **kw: fake_from_env(**kw)),
    )

    out = GoogleAPI.from_service_account_file("creds.json", scopes=("s1",))
    assert out == "ok"
    assert captured["auth"].credentials_file == "creds.json"
    assert captured["auth"].scopes == ("s1",)
