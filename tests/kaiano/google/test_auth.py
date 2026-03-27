def test_load_credentials_from_env_valid_json(monkeypatch, json_env_payload):
    from kaiano.google._auth import AuthConfig, load_credentials

    monkeypatch.setenv(
        "GOOGLE_CREDENTIALS_JSON", json_env_payload({"type": "service_account"})
    )
    creds = load_credentials(AuthConfig())
    assert getattr(creds, "source", None) == "info"
    assert creds.payload["info"]["type"] == "service_account"


def test_load_credentials_env_invalid_json_falls_back_to_file(monkeypatch):
    from kaiano.google._auth import AuthConfig, load_credentials

    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "not-json")
    creds = load_credentials(AuthConfig(credentials_file="creds.json"))
    assert getattr(creds, "source", None) == "file"
    assert creds.payload["filename"] == "creds.json"


def test_load_credentials_env_json_not_dict_falls_back(monkeypatch):
    from kaiano.google._auth import AuthConfig, load_credentials

    monkeypatch.setenv("GOOGLE_CREDENTIALS_JSON", "[]")
    creds = load_credentials(AuthConfig(credentials_file="creds.json"))
    assert getattr(creds, "source", None) == "file"


def test_build_services_use_googleapiclient_build():
    from kaiano.google._auth import build_drive_service, build_sheets_service

    c = object()
    sheets = build_sheets_service(c)
    drive = build_drive_service(c)
    assert sheets["serviceName"] == "sheets"
    assert drive["serviceName"] == "drive"


def test_build_gspread_client_authorizes():
    from kaiano.google._auth import build_gspread_client

    c = object()
    client = build_gspread_client(c)
    assert client.__class__.__name__ == "Client"
