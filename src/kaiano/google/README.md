# Google API Utilities (Unified Interface)

This module provides a **single, stable, high-level interface** for interacting with Google APIs (currently **Sheets** and **Drive**) across all Kaiano projects.

It is designed to:
- Eliminate duplicated Google API boilerplate
- Centralize authentication and retry logic
- Provide a small, predictable API surface
- Remain stable as internal implementations evolve

External projects should **never** import `googleapiclient` directly.  
They should only interact with Google via `GoogleAPI`.

---

## ✨ Key Design Goals

- **One front door**: `GoogleAPI`
- **Centralized retry/backoff** for all Google API calls
- **Small, intentional surface area**
- **Plain Python inputs & outputs**
- **Backwards compatible** with legacy helpers
- **Safe for reuse across many repos**

---

## 📦 Package Structure

```text
google/
├── __init__.py          # exports GoogleAPI
├── api.py               # GoogleAPI (public entry point)
├── _auth.py             # credentials + service builders (private)
├── _retry.py            # retry/backoff logic (private)
├── sheets.py            # SheetsFacade (+ legacy helpers)
├── drive.py             # DriveFacade (+ legacy helpers)
├── types.py             # small dataclasses (e.g. DriveFile)
└── errors.py            # custom exceptions
```

### Public vs Private
- **Public API:** `GoogleAPI`, `SheetsFacade`, `DriveFacade`
- **Private internals:** `_auth.py`, `_retry.py`

Callers should treat anything prefixed with `_` as internal and unstable.

---

## 🚪 The Single Entry Point: `GoogleAPI`

### Import
```python
from kaiano.google import GoogleAPI
```

### Construction

```python
g = GoogleAPI.from_env()
```

Supported construction methods:

| Method | Description |
|------|------------|
| `GoogleAPI.from_env()` | Load credentials from environment (recommended) |
| `GoogleAPI.from_service_account_file(path)` | Load from a service account JSON file |
| `GoogleAPI.from_service_account_info(info)` | Load from a dict (useful for secrets managers) |

---

## 🧠 Architecture Overview

```text
External Repo
    |
    v
GoogleAPI
 ├── sheets  -> SheetsFacade
 └── drive   -> DriveFacade
        |
        v
 Central retry + auth + logging
```

---

## 📊 SheetsFacade Interface

Accessed via:

```python
g.sheets
```

### Read Operations

```python
read_values(spreadsheet_id, a1_range) -> list[list[str]]
get_metadata(spreadsheet_id) -> dict
get_sheet_id(spreadsheet_id, sheet_name) -> int
```

### Write Operations

```python
write_values(spreadsheet_id, a1_range, values, value_input="RAW")
append_values(spreadsheet_id, a1_range, values, value_input="RAW")
clear(spreadsheet_id, a1_range)
```

### Structure / Formatting

```python
ensure_sheet_exists(spreadsheet_id, sheet_name, headers=None)
batch_update(spreadsheet_id, requests)
sort_sheet(spreadsheet_id, sheet_name, column_index, ascending=True, start_row=2, end_row=None)
```

---

## 📁 DriveFacade Interface

Accessed via:

```python
g.drive
```

### File Discovery

```python
list_files(parent_id, mime_type=None, name_contains=None, trashed=False) -> list[DriveFile]
find_first(parent_id, name, mime_type=None) -> DriveFile | None
```

### Folder Management

```python
ensure_folder(parent_id, name) -> folder_id
```

### File Operations

```python
copy_file(file_id, parent_folder_id, name=None) -> new_file_id
move_file(file_id, new_parent_id, remove_from_parents=True)
upload_file(src_path, parent_id, mime_type=None, name=None) -> file_id
download_file(file_id, dst_path)
delete_file(file_id)
```

---

## 🔁 Retry & Backoff Policy

All Google API calls are wrapped in a **single shared retry system**.

### Automatically retried errors
- HTTP **5xx**
- HTTP **429** (Too Many Requests)
- HTTP **408**
- HTTP **403** *only if quota / rate-limit related*

Retry behavior:
- Exponential backoff
- Jitter applied to avoid thundering herds
- Detailed logging on retries

---

## 🔐 Authentication

Authentication is handled centrally in `_auth.py`.

Supported sources:
- Environment variables
- Service account JSON file
- In-memory service account info (dict)

External repos **never** build clients directly.

---

## 🧪 Testing Strategy

- `GoogleAPI` supports injected services for unit tests
- Retry logic can be mocked or bypassed
- No global state required

---

## 🧭 Migration Guide

### Old
```python
from kaiano.google.sheets import get_sheets_service, read_sheet
svc = get_sheets_service()
rows = read_sheet(svc, sid, "Sheet1!A2:C")
```

### New
```python
from kaiano.google import GoogleAPI
g = GoogleAPI.from_env()
rows = g.sheets.read_values(sid, "Sheet1!A2:C")
```

---

## 📦 Stability Contract

- `GoogleAPI`, `SheetsFacade`, and `DriveFacade` are **stable**
- Internal modules may change without notice
- New functionality should be added via facade methods

---

## ✅ Summary

If you need Google access in a Kaiano project, start here:

```python
from kaiano.google import GoogleAPI
```

Everything else is an implementation detail.
