from __future__ import annotations

import datetime
import re
from collections.abc import Iterable
from dataclasses import dataclass

import pytz

from kaiano import config
from kaiano import logger as log

log = log.get_logger()


@dataclass(frozen=True)
class M3UEntry:
    """A single extracted history entry."""

    dt: str  # YYYY-MM-DD HH:MM (local tz, monotonic)
    title: str
    artist: str
    length: str = ""
    last_play: str = ""

    def dedup_key(self) -> str:
        return "||".join(
            [
                self.dt.strip().lower(),
                self.title.strip().lower(),
                self.artist.strip().lower(),
            ]
        )


class ParseFacade:
    """Pure parsing helpers (no Drive dependency)."""

    @staticmethod
    def parse_time_str(time_str: str) -> int:
        try:
            h, m = map(int, str(time_str).split(":"))
            return h * 60 + m
        except Exception:
            return 0

    @staticmethod
    def extract_tag_value(line: str, tag: str) -> str:
        match = re.search(rf"<{tag}>(.*?)</{tag}>", line, re.I)
        return match.group(1).strip() if match else ""

    @staticmethod
    def parse_m3u_lines(
        lines: Iterable[str],
        existing_keys: set[str],
        file_date_str: str,
    ) -> list[M3UEntry]:
        tz = pytz.timezone(config.TIMEZONE)
        year, month, day = map(int, file_date_str.split("-"))
        base_date = tz.localize(datetime.datetime(year, month, day, 0, 0))

        prev_assigned_dt: datetime.datetime | None = None
        day_offset = 0
        entries: list[M3UEntry] = []

        def _parse_last_play_datetime(
            last_play_str: str,
        ) -> datetime.datetime | None:
            if not last_play_str:
                return None
            s = str(last_play_str).strip()
            if not s:
                return None

            if s.isdigit():
                try:
                    n = int(s)
                    if n > 10_000_000_000:
                        n = n / 1000.0
                    return datetime.datetime.fromtimestamp(n, tz)
                except Exception:
                    return None

            for fmt in (
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%m/%d/%Y %H:%M",
                "%m/%d/%Y %H:%M:%S",
            ):
                try:
                    dt_naive = datetime.datetime.strptime(s, fmt)
                    return tz.localize(dt_naive)
                except Exception:
                    continue
            return None

        for line in lines:
            if not str(line).strip().lower().startswith("#extvdj:"):
                continue

            time = ParseFacade.extract_tag_value(line, "time")
            title = ParseFacade.extract_tag_value(line, "title")
            artist = ParseFacade.extract_tag_value(line, "artist") or ""
            length = ParseFacade.extract_tag_value(line, "songlength") or ""
            last_play = ParseFacade.extract_tag_value(line, "lastplaytime") or ""

            if not title:
                continue

            if time:
                minutes = ParseFacade.parse_time_str(time)
                assigned_dt = base_date + datetime.timedelta(
                    days=day_offset, minutes=minutes
                )
            else:
                lp_dt = _parse_last_play_datetime(last_play)
                if lp_dt is not None and (
                    prev_assigned_dt is None or lp_dt > prev_assigned_dt
                ):
                    assigned_dt = lp_dt
                else:
                    assigned_dt = (
                        (prev_assigned_dt + datetime.timedelta(minutes=1))
                        if prev_assigned_dt is not None
                        else base_date + datetime.timedelta(days=day_offset)
                    )

            if prev_assigned_dt is not None:
                while assigned_dt <= prev_assigned_dt:
                    assigned_dt += datetime.timedelta(days=1)

            day_offset = (assigned_dt.date() - base_date.date()).days
            prev_assigned_dt = assigned_dt

            full_dt = assigned_dt.strftime("%Y-%m-%d %H:%M")
            entry = M3UEntry(
                dt=full_dt,
                title=title.strip(),
                artist=artist.strip(),
                length=length.strip(),
                last_play=last_play.strip(),
            )
            key = entry.dedup_key()
            if key not in existing_keys:
                entries.append(entry)
                existing_keys.add(key)

        return entries

    @staticmethod
    def parse_m3u(_sheets_service, filepath: str, _spreadsheet_id: str):
        """Back-compat: parse a local .m3u file and return (artist, title, extvdj_line).

        `_spreadsheet_id` is unused (kept only for signature compatibility).
        """
        songs = []
        try:
            with open(filepath, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if line.startswith("#EXTVDJ:"):
                        artist_match = re.search(r"<artist>(.*?)</artist>", line)
                        title_match = re.search(r"<title>(.*?)</title>", line)
                        if artist_match and title_match:
                            songs.append(
                                (
                                    artist_match.group(1).strip(),
                                    title_match.group(1).strip(),
                                    line,
                                )
                            )
        except Exception:
            return []
        return songs


class M3UToolbox:
    """Single entry point to local-only parsing."""

    def __init__(self):
        self.parse = ParseFacade()
