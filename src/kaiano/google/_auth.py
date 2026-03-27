from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import gspread
from google.oauth2 import service_account
from googleapiclient.discovery import build

from kaiano import logger as log

log = log.get_logger()


DEFAULT_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
)


@dataclass(frozen=True)
class AuthConfig:
    """How Google credentials should be loaded."""

    scopes: tuple[str, ...] = DEFAULT_SCOPES
    credentials_json_env: str = "GOOGLE_CREDENTIALS_JSON"
    credentials_file: str = "credentials.json"


def load_credentials(config: AuthConfig | None = None):
    """Load service account credentials from env or file.

    If env var contains invalid JSON, falls back to credentials.json.
    """

    config = config or AuthConfig()
    creds_json = os.getenv(config.credentials_json_env)

    if creds_json:
        try:
            creds_dict = json.loads(creds_json)
            if not isinstance(creds_dict, dict):
                raise ValueError("Decoded credentials JSON is not a dict")
            return service_account.Credentials.from_service_account_info(
                creds_dict,
                scopes=list(config.scopes),
            )
        except Exception as e:
            log.warning(
                f"Invalid {config.credentials_json_env} ({e}); falling back to {config.credentials_file}"
            )

    return service_account.Credentials.from_service_account_file(
        config.credentials_file,
        scopes=list(config.scopes),
    )


def build_sheets_service(creds) -> Any:
    return build("sheets", "v4", credentials=creds)


def build_drive_service(creds) -> Any:
    return build("drive", "v3", credentials=creds)


def build_gspread_client(creds) -> gspread.Client:
    return gspread.authorize(creds)
