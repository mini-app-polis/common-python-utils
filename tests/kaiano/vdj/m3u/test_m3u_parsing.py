import importlib
import sys


def test_parse_facade_helpers(monkeypatch):
    # Ensure TIMEZONE exists on the stubbed config module installed by google tests.
    cfg = sys.modules.get("kaiano.config")
    if cfg is not None and not hasattr(cfg, "TIMEZONE"):
        cfg.TIMEZONE = "America/Chicago"

    m3u = importlib.reload(importlib.import_module("kaiano.vdj.m3u.m3u"))
    ParseFacade = m3u.ParseFacade

    assert ParseFacade.parse_time_str("12:34") == 754
    assert ParseFacade.parse_time_str("bad") == 0
    assert ParseFacade.extract_tag_value("<title> Song </title>", "title") == "Song"


def test_parse_m3u_lines_rollover_and_lastplay_formats(monkeypatch):
    cfg = sys.modules.get("kaiano.config")
    if cfg is not None:
        cfg.TIMEZONE = "America/Chicago"

    m3u = importlib.reload(importlib.import_module("kaiano.vdj.m3u.m3u"))
    ParseFacade = m3u.ParseFacade

    existing = set()
    lines = [
        "#EXTVDJ:<time>23:59</time><title>T1</title><artist>A</artist>",
        # next day rollover (00:01)
        "#EXTVDJ:<time>00:01</time><title>T2</title><artist>A</artist>",
        # missing <time>, uses lastplaytime epoch ms
        "#EXTVDJ:<lastplaytime>1700000000000</lastplaytime><title>T3</title><artist>A</artist>",
        # missing <time>, uses lastplaytime date string
        "#EXTVDJ:<lastplaytime>2020-01-01 00:00</lastplaytime><title>T4</title><artist>A</artist>",
        # repeated title/artist/time later in file will not dedup because the parser
        # enforces monotonic timestamps (so dt changes).
        "#EXTVDJ:<time>23:59</time><title>T1</title><artist>A</artist>",
    ]

    out = ParseFacade.parse_m3u_lines(lines, existing, "2026-01-19")
    assert [e.title for e in out] == ["T1", "T2", "T3", "T4", "T1"]
    # rollover ensured monotonic dt
    assert out[0].dt < out[1].dt


def test_parse_m3u_backcompat(tmp_path, monkeypatch):
    cfg = sys.modules.get("kaiano.config")
    if cfg is not None:
        cfg.TIMEZONE = "America/Chicago"

    m3u = importlib.reload(importlib.import_module("kaiano.vdj.m3u.m3u"))
    content = "\n".join(
        [
            "#EXTM3U",
            "#EXTVDJ:<artist>A</artist><title>T</title>",
            "not a tag",
        ]
    )
    p = tmp_path / "x.m3u"
    p.write_text(content)
    songs = m3u.ParseFacade.parse_m3u(None, str(p), "unused")
    assert songs == [("A", "T", "#EXTVDJ:<artist>A</artist><title>T</title>")]
