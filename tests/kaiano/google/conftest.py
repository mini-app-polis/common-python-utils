import json
import sys
import types
from pathlib import Path

import pytest


class DummyLogger:
    """Very small logger stub used by tests."""

    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _log(self, level: str, msg: str):
        self.records.append((level, str(msg)))

    def info(self, msg: str):
        self._log("info", msg)

    def warning(self, msg: str):
        self._log("warning", msg)

    def error(self, msg: str):
        self._log("error", msg)

    def debug(self, msg: str):
        self._log("debug", msg)

    def critical(self, msg: str):
        self._log("critical", msg)


def _install_stub_module(name: str, module: types.ModuleType) -> None:
    sys.modules[name] = module


def pytest_configure():
    """Provide stubs for `kaiano.*` and Google client libs.

    The package under test imports `kaiano.logger`, `kaiano.config`, and several
    Google client libraries. For unit tests we replace them with minimal stubs.
    """

    # Ensure the package under test (src/kaiano) is importable.
    # This repo uses a src/ layout, so when running tests without an editable install,
    # we add <repo>/src to sys.path.
    repo_root = Path(__file__).resolve().parents[3]
    src_path = str(repo_root / "src")
    if src_path not in sys.path:
        sys.path.insert(0, src_path)

    # ------------------------------------------------------------------
    # kaiano stubs (DO NOT overwrite the real `kaiano` package)
    # ------------------------------------------------------------------
    kaiano_logger = types.ModuleType("kaiano.logger")
    kaiano_config = types.ModuleType("kaiano.config")

    _logger = DummyLogger()

    def get_logger():
        return _logger

    kaiano_logger.get_logger = get_logger  # type: ignore[attr-defined]
    # Some modules use `from kaiano import logger as log` and then call
    # `log.warning(...)` directly. Provide those module-level shims.
    kaiano_logger.info = _logger.info  # type: ignore[attr-defined]
    kaiano_logger.warning = _logger.warning  # type: ignore[attr-defined]
    kaiano_logger.error = _logger.error  # type: ignore[attr-defined]
    kaiano_logger.debug = _logger.debug  # type: ignore[attr-defined]
    kaiano_logger.critical = _logger.critical  # type: ignore[attr-defined]
    # set a default (tests will patch as needed)
    kaiano_config.VDJ_HISTORY_FOLDER_ID = None

    _install_stub_module("kaiano.logger", kaiano_logger)
    _install_stub_module("kaiano.config", kaiano_config)

    # ------------------------------------------------------------------
    # googleapiclient stubs (only pieces referenced by the package)
    # ------------------------------------------------------------------
    googleapiclient = types.ModuleType("googleapiclient")
    googleapiclient_discovery = types.ModuleType("googleapiclient.discovery")
    googleapiclient_errors = types.ModuleType("googleapiclient.errors")
    googleapiclient_http = types.ModuleType("googleapiclient.http")

    class HttpError(Exception):
        def __init__(self, resp=None, content=None):
            super().__init__("HttpError")
            self.resp = resp
            self.content = content

        def __str__(self):
            return "HttpError"

    googleapiclient_errors.HttpError = HttpError  # type: ignore[attr-defined]

    def build(serviceName, version, credentials=None):  # noqa: N802
        return {
            "serviceName": serviceName,
            "version": version,
            "credentials": credentials,
        }

    googleapiclient_discovery.build = build  # type: ignore[attr-defined]

    class MediaIoBaseDownload:
        def __init__(self, fh, request):
            self._fh = fh
            self._request = request
            self._done = False

        def next_chunk(self):
            # write once then report done
            if not self._done:
                self._fh.write(self._request.get("data", b""))
                self._done = True
            return None, self._done

    class MediaFileUpload:
        def __init__(self, filename, mimetype=None, resumable=False):
            self.filename = filename
            self.mimetype = mimetype
            self.resumable = resumable

    googleapiclient_http.MediaIoBaseDownload = MediaIoBaseDownload  # type: ignore[attr-defined]
    googleapiclient_http.MediaFileUpload = MediaFileUpload  # type: ignore[attr-defined]

    class MediaIoBaseUpload:  # pragma: no cover
        def __init__(self, fh, mimetype=None, resumable=False, chunksize=None):
            self.fh = fh
            self.mimetype = mimetype
            self.resumable = resumable
            self.chunksize = chunksize

    googleapiclient_http.MediaIoBaseUpload = MediaIoBaseUpload  # type: ignore[attr-defined]

    googleapiclient.discovery = googleapiclient_discovery  # type: ignore[attr-defined]
    googleapiclient.errors = googleapiclient_errors  # type: ignore[attr-defined]
    googleapiclient.http = googleapiclient_http  # type: ignore[attr-defined]

    _install_stub_module("googleapiclient", googleapiclient)
    _install_stub_module("googleapiclient.discovery", googleapiclient_discovery)
    _install_stub_module("googleapiclient.errors", googleapiclient_errors)
    _install_stub_module("googleapiclient.http", googleapiclient_http)

    # ------------------------------------------------------------------
    # google.oauth2.service_account stub
    #
    # IMPORTANT: The package under test is named `google` (it has a real
    # __init__.py), which would normally shadow the upstream `google` namespace.
    # For unit tests we inject ONLY the submodules needed by google._auth
    # without overwriting the top-level `google` package itself.
    # ------------------------------------------------------------------
    oauth2_mod = types.ModuleType("google.oauth2")
    service_account_mod = types.ModuleType("google.oauth2.service_account")

    class _Creds:
        def __init__(self, source: str, payload=None):
            self.source = source
            self.payload = payload

    class Credentials:
        @staticmethod
        def from_service_account_info(info, scopes=None):
            return _Creds("info", payload={"info": info, "scopes": scopes})

        @staticmethod
        def from_service_account_file(filename, scopes=None):
            return _Creds("file", payload={"filename": filename, "scopes": scopes})

    service_account_mod.Credentials = Credentials  # type: ignore[attr-defined]

    oauth2_mod.service_account = service_account_mod  # type: ignore[attr-defined]

    _install_stub_module("google.oauth2", oauth2_mod)
    _install_stub_module("google.oauth2.service_account", service_account_mod)

    # ------------------------------------------------------------------
    # gspread stub
    # ------------------------------------------------------------------
    gspread = types.ModuleType("gspread")

    class Client:
        pass

    def authorize(creds):
        return Client()

    gspread.Client = Client  # type: ignore[attr-defined]
    gspread.authorize = authorize  # type: ignore[attr-defined]
    _install_stub_module("gspread", gspread)


@pytest.fixture
def as_http_error():
    """Fixture: factory to create stub HttpError with resp.status."""

    def _factory(*, status: int, message: str = ""):
        from googleapiclient.errors import HttpError

        class _Resp:
            def __init__(self, status):
                self.status = status

        class _HttpError(HttpError):
            def __str__(self):
                return message or super().__str__()

        return _HttpError(resp=_Resp(status), content=b"")

    return _factory


@pytest.fixture
def json_env_payload():
    """Fixture: factory to json-dump a dict for GOOGLE_CREDENTIALS_JSON."""

    def _factory(payload: dict) -> str:
        return json.dumps(payload)

    return _factory


def _unused_as_http_error(*, status: int, message: str = ""):
    """Helper for tests: create a stub HttpError with a resp.status."""
    from googleapiclient.errors import HttpError

    class _Resp:
        def __init__(self, status):
            self.status = status

    class _HttpError(HttpError):
        def __str__(self):
            return message or super().__str__()

    return _HttpError(resp=_Resp(status), content=b"")
