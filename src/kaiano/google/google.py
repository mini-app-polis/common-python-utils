from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kaiano import logger as log

from ._auth import (
    AuthConfig,
    build_drive_service,
    build_gspread_client,
    build_sheets_service,
    load_credentials,
)
from ._retry import RetryConfig
from .drive import DriveFacade
from .sheets import SheetsFacade

log = log.get_logger()


@dataclass
class GoogleAPI:
    """Single, stable entry point for Google APIs.

    External repos should use this class rather than importing googleapiclient
    directly.

    Example:
        from kaiano.api.google import GoogleAPI
        g = GoogleAPI.from_env()
        rows = g.sheets.read_values(spreadsheet_id, "Sheet1!A2:C")
    """

    sheets: SheetsFacade
    drive: DriveFacade
    gspread: Any | None = None

    @classmethod
    def from_env(
        cls,
        *,
        auth: AuthConfig | None = None,
        retry: RetryConfig | None = None,
    ) -> GoogleAPI:
        """Create clients using GOOGLE_CREDENTIALS_JSON (preferred) or credentials.json."""

        auth = auth or AuthConfig()
        retry = retry or RetryConfig()

        # Prefer kaiano._google_credentials if present
        creds = load_credentials(auth)
        sheets_service = build_sheets_service(creds)
        drive_service = build_drive_service(creds)
        gspread_client = build_gspread_client(creds)

        return cls(
            sheets=SheetsFacade(sheets_service, retry=retry),
            drive=DriveFacade(drive_service, retry=retry),
            gspread=gspread_client,
        )

    @classmethod
    def from_service_account_file(
        cls,
        credentials_file: str,
        *,
        scopes: tuple[str, ...] | None = None,
        retry: RetryConfig | None = None,
    ) -> GoogleAPI:
        auth = AuthConfig(
            credentials_file=credentials_file, scopes=scopes or AuthConfig().scopes
        )
        return cls.from_env(auth=auth, retry=retry)
