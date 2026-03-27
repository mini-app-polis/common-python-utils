import time
from collections.abc import Iterable
from typing import Any

from googleapiclient.errors import HttpError

from kaiano import logger as logger_mod

from ._auth import AuthConfig, build_sheets_service, load_credentials
from ._retry import RetryConfig, execute_with_retry

log = logger_mod.get_logger()

# -----------------------------------------------------------------------------
# Tuning constants (quota-friendly defaults)
# -----------------------------------------------------------------------------

FORMAT_REQUEST_CHUNK_SIZE = 400  # ~100 sheets per call (4 requests per sheet)
WIDTH_REQUEST_CHUNK_SIZE = 500
AUTORESIZE_BUFFER_PX = 20
AUTORESIZE_MAX_PX = 350
CHUNK_THROTTLE_S = 0.5
DEFAULT_NUM_COLUMNS = 26
DEFAULT_FONT_SIZE = 10


class SheetsFormatter:
    """Quota-friendly Google Sheets formatting helper.

    This class holds a single authorized Sheets API service instance so callers
    can batch multiple formatting operations without rebuilding services.
    """

    def __init__(self, *, auth: AuthConfig | None = None, sheets_service=None):
        self._auth = auth
        self._sheets_service = sheets_service

    @property
    def sheets_service(self) -> Any:
        if self._sheets_service is None:
            creds = load_credentials(self._auth)
            self._sheets_service = build_sheets_service(creds)
        return self._sheets_service

    def _batch_update(
        self,
        spreadsheet_id: str,
        requests: list[dict[str, Any]],
        *,
        operation: str,
        max_attempts: int = 5,
    ) -> None:
        def _call() -> Any:
            return (
                self.sheets_service.spreadsheets()
                .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
                .execute()
            )

        retry = RetryConfig(max_attempts=max_attempts, max_delay_s=16.0)
        execute_with_retry(_call, context=operation, retry=retry)

    def _get_column_pixel_sizes(
        self, spreadsheet_id: str
    ) -> dict[int, list[int | None]]:
        """Return {sheetId: [pixelSize,...]} using a minimal includeGridData fetch."""
        meta = execute_with_retry(
            lambda: (
                self.sheets_service.spreadsheets()
                .get(
                    spreadsheetId=spreadsheet_id,
                    includeGridData=True,
                    fields="sheets(properties(sheetId,title),data(columnMetadata(pixelSize)))",
                )
                .execute()
            ),
            context=f"fetching column pixel sizes for {spreadsheet_id}",
            retry=RetryConfig(max_attempts=5, max_delay_s=16.0),
        )
        out: dict[int, list[int | None]] = {}
        for s in meta.get("sheets", []):
            props = s.get("properties", {})
            sid = props.get("sheetId")
            data = s.get("data", []) or []
            if not sid or not data:
                continue
            col_meta = (data[0] or {}).get("columnMetadata", []) or []
            sizes: list[int | None] = []
            for cm in col_meta:
                ps = cm.get("pixelSize")
                sizes.append(int(ps) if ps is not None else None)
            out[int(sid)] = sizes
        return out

    def _apply_column_width_buffer_pass(
        self,
        *,
        spreadsheet_id: str,
        sheets_metadata: list[dict[str, Any]],
    ) -> None:
        """Apply a small pixel buffer to auto-resized columns, capped at a max width."""
        try:
            pixel_sizes_by_sheet = self._get_column_pixel_sizes(spreadsheet_id)
            width_requests: list[dict[str, Any]] = []
            for s in sheets_metadata:
                props = s.get("properties", {})
                sheet_id_raw = props.get("sheetId")
                if sheet_id_raw is None:
                    continue
                sheet_id = int(sheet_id_raw)
                grid = props.get("gridProperties", {})
                num_columns = int(
                    grid.get("columnCount", DEFAULT_NUM_COLUMNS) or DEFAULT_NUM_COLUMNS
                )
                if num_columns < 1:
                    num_columns = DEFAULT_NUM_COLUMNS
                sizes = pixel_sizes_by_sheet.get(sheet_id, [])
                if not sizes:
                    continue
                for col_idx in range(0, min(num_columns, len(sizes))):
                    ps = sizes[col_idx]
                    if ps is None:
                        continue
                    new_ps = min(int(ps) + AUTORESIZE_BUFFER_PX, AUTORESIZE_MAX_PX)
                    if new_ps <= int(ps):
                        continue
                    width_requests.append(_req_set_col_width(sheet_id, col_idx, new_ps))
            if not width_requests:
                return
            log.debug(
                f"Applying column width buffer (+{AUTORESIZE_BUFFER_PX}px, cap {AUTORESIZE_MAX_PX}px). "
                f"Total width updates: {len(width_requests)}"
            )
            for i in range(0, len(width_requests), WIDTH_REQUEST_CHUNK_SIZE):
                chunk = width_requests[i : i + WIDTH_REQUEST_CHUNK_SIZE]
                chunk_num = (i // WIDTH_REQUEST_CHUNK_SIZE) + 1
                total_chunks = (
                    (len(width_requests) - 1) // WIDTH_REQUEST_CHUNK_SIZE
                ) + 1
                self._batch_update(
                    spreadsheet_id,
                    chunk,
                    operation=f"column width buffer (chunk {chunk_num}/{total_chunks})",
                    max_attempts=5,
                )
                if chunk_num < total_chunks:
                    time.sleep(CHUNK_THROTTLE_S)
        except Exception as e:
            log.warning(f"Column width buffer pass failed (continuing without it): {e}")

    def apply_sheet_formatting(self, sheet) -> None:
        """Apply formatting to a single gspread Worksheet-like sheet object."""
        try:
            spreadsheet_id = sheet.spreadsheet.id
            sheet_id = sheet.id
            num_columns = int(
                getattr(sheet, "col_count", DEFAULT_NUM_COLUMNS) or DEFAULT_NUM_COLUMNS
            )
            if num_columns < 1:
                num_columns = DEFAULT_NUM_COLUMNS
            requests: list[dict[str, Any]] = [
                _req_freeze_header(int(sheet_id)),
                _req_body_font_and_left(int(sheet_id), int(num_columns)),
                _req_bold_header(int(sheet_id), int(num_columns)),
                _req_auto_resize_cols(int(sheet_id), int(num_columns)),
            ]
            self._batch_update(
                spreadsheet_id,
                requests,
                operation=f"format sheetId={sheet_id}",
                max_attempts=5,
            )
        except Exception as e:
            log.warning(
                f"Auto formatting failed for sheet '{getattr(sheet, 'title', sheet)}': {e}"
            )

    def apply_formatting_to_sheet(self, spreadsheet_id: str) -> None:
        """Apply formatting to all sheets in a spreadsheet."""
        log.debug(
            f"Applying formatting to all sheets in spreadsheet ID: {spreadsheet_id}"
        )
        try:
            meta = execute_with_retry(
                lambda: (
                    self.sheets_service.spreadsheets()
                    .get(spreadsheetId=spreadsheet_id)
                    .execute()
                ),
                context=f"fetching spreadsheet metadata for {spreadsheet_id}",
                retry=RetryConfig(max_attempts=5, max_delay_s=16.0),
            )
            sheets = meta.get("sheets", [])
            log.debug(f"Found {len(sheets)} sheet(s) to format")
            all_requests: list[dict[str, Any]] = []
            for s in sheets:
                props = s.get("properties", {})
                sheet_id = props.get("sheetId")
                title = props.get("title", "(untitled)")
                grid = props.get("gridProperties", {})
                num_columns = int(
                    grid.get("columnCount", DEFAULT_NUM_COLUMNS) or DEFAULT_NUM_COLUMNS
                )
                if num_columns < 1:
                    num_columns = DEFAULT_NUM_COLUMNS
                log.debug(
                    f"Queueing formatting for sheet: {title} (sheetId={sheet_id}, cols={num_columns})"
                )
                all_requests.extend(
                    [
                        _req_freeze_header(int(sheet_id)),
                        _req_body_font_and_left(int(sheet_id), int(num_columns)),
                        _req_bold_header(int(sheet_id), int(num_columns)),
                        _req_auto_resize_cols(int(sheet_id), int(num_columns)),
                    ]
                )
            if not all_requests:
                log.info("No sheets found to format; nothing to do")
                return
            log.debug(
                f"Sending formatting batchUpdate requests in chunks of {FORMAT_REQUEST_CHUNK_SIZE}. Total requests: {len(all_requests)}"
            )
            for i in range(0, len(all_requests), FORMAT_REQUEST_CHUNK_SIZE):
                chunk = all_requests[i : i + FORMAT_REQUEST_CHUNK_SIZE]
                chunk_num = (i // FORMAT_REQUEST_CHUNK_SIZE) + 1
                total_chunks = (
                    (len(all_requests) - 1) // FORMAT_REQUEST_CHUNK_SIZE
                ) + 1
                self._batch_update(
                    spreadsheet_id,
                    chunk,
                    operation=f"format all sheets (chunk {chunk_num}/{total_chunks})",
                    max_attempts=5,
                )
                if chunk_num < total_chunks:
                    time.sleep(CHUNK_THROTTLE_S)
            self._apply_column_width_buffer_pass(
                spreadsheet_id=spreadsheet_id,
                sheets_metadata=sheets,
            )
            log.info("✅ Formatting applied successfully to all sheets")
        except Exception as e:
            log.error(f"Error applying formatting to sheets: {e}")

    def set_column_text_formatting(
        self,
        spreadsheet_id: str,
        sheet_name: str,
        column_indexes: Iterable[int],
    ) -> None:
        """Set number format to TEXT for specified columns in a sheet."""
        meta = execute_with_retry(
            lambda: (
                self.sheets_service.spreadsheets()
                .get(
                    spreadsheetId=spreadsheet_id,
                    fields="sheets(properties(sheetId,title))",
                )
                .execute()
            ),
            context=f"fetching sheet metadata for set_column_text_formatting ({spreadsheet_id})",
            retry=RetryConfig(max_attempts=5, max_delay_s=16.0),
        )
        sheet = next(
            (
                s
                for s in meta.get("sheets", [])
                if s.get("properties", {}).get("title") == sheet_name
            ),
            None,
        )
        if not sheet:
            raise ValueError(
                f"Sheet named '{sheet_name}' not found in spreadsheet {spreadsheet_id}"
            )
        sheet_id = sheet["properties"]["sheetId"]
        requests = []
        for col in column_indexes:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": int(col),
                            "endColumnIndex": int(col) + 1,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "numberFormat": {
                                    "type": "TEXT",
                                    "pattern": "@",
                                }
                            }
                        },
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
        if requests:
            self._batch_update(
                spreadsheet_id,
                requests,
                operation=f"set_column_text_formatting sheet={sheet_name}",
                max_attempts=5,
            )

    def reorder_sheets(
        self,
        spreadsheet_id: str,
        sheet_names_in_order: list[str],
        spreadsheet_metadata: dict[str, Any],
    ) -> None:
        """Reorder sheets in a spreadsheet to the specified order."""
        log.info(
            f"🔀 Reordering sheets in spreadsheet ID {spreadsheet_id} to order: {sheet_names_in_order}"
        )
        try:
            sheets = spreadsheet_metadata.get("sheets", [])
            title_to_id = {
                sheet["properties"]["title"]: sheet["properties"]["sheetId"]
                for sheet in sheets
            }
            requests = []
            index = 0
            for name in sheet_names_in_order:
                sheet_id = title_to_id.get(name)
                if sheet_id is not None:
                    requests.append(
                        {
                            "updateSheetProperties": {
                                "properties": {"sheetId": sheet_id, "index": index},
                                "fields": "index",
                            }
                        }
                    )
                    index += 1
            remaining_sheets = [
                sheet
                for sheet in sheets
                if sheet["properties"]["title"] not in sheet_names_in_order
            ]
            for sheet in remaining_sheets:
                requests.append(
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": sheet["properties"]["sheetId"],
                                "index": index,
                            },
                            "fields": "index",
                        }
                    }
                )
                index += 1
            if requests:
                self._batch_update(
                    spreadsheet_id,
                    requests,
                    operation="reorder_sheets",
                    max_attempts=5,
                )
                log.info("✅ Sheets reordered successfully")
        except HttpError as error:
            log.error(f"An error occurred while reordering sheets: {error}")
            raise


def _req_freeze_header(sheet_id: int) -> dict[str, Any]:
    return {
        "updateSheetProperties": {
            "properties": {
                "sheetId": sheet_id,
                "gridProperties": {"frozenRowCount": 1},
            },
            "fields": "gridProperties.frozenRowCount",
        }
    }


def _req_body_font_and_left(
    sheet_id: int,
    num_columns: int,
    *,
    font_size: int = DEFAULT_FONT_SIZE,
) -> dict[str, Any]:
    # IMPORTANT: Do NOT set/replace the entire textFormat object, because that can wipe
    # rich-text hyperlink info (textFormat.link / textFormatRuns). Only set fontSize.
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "startColumnIndex": 0,
                "endColumnIndex": num_columns,
            },
            "cell": {
                "userEnteredFormat": {
                    "textFormat": {"fontSize": font_size},
                    "horizontalAlignment": "LEFT",
                }
            },
            "fields": "userEnteredFormat.textFormat.fontSize,userEnteredFormat.horizontalAlignment",
        }
    }


def _req_bold_header(sheet_id: int, num_columns: int) -> dict[str, Any]:
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": 0,
                "endRowIndex": 1,
                "startColumnIndex": 0,
                "endColumnIndex": num_columns,
            },
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat.bold",
        }
    }


def _req_auto_resize_cols(sheet_id: int, num_columns: int) -> dict[str, Any]:
    return {
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": 0,
                "endIndex": num_columns,
            }
        }
    }


def _req_set_col_width(sheet_id: int, col_idx: int, px: int) -> dict[str, Any]:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": col_idx,
                "endIndex": col_idx + 1,
            },
            "properties": {"pixelSize": int(px)},
            "fields": "pixelSize",
        }
    }
