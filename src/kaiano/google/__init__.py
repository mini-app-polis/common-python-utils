"""kaiano.google

This package is the single, stable interface layer for Google APIs used across
your projects.

External code should only import :class:`~kaiano.google.GoogleAPI`:

    from kaiano.google import GoogleAPI

    g = GoogleAPI.from_env()
    rows = g.sheets.read_values(spreadsheet_id, "Sheet1!A1:C10")
"""

from .google import GoogleAPI

__all__ = ["GoogleAPI"]
