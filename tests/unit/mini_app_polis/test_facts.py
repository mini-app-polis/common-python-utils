"""``facts``: figures computed from a repository's own files."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from mini_app_polis import facts

REGISTRY = """\
services:
- {id: a, status: active, host: aws-lambda, type: pipeline-cog}
- {id: b, status: active, host: railway, type: api-service}
- {id: c, status: retired, type: pipeline-cog}
"""

SPEC = """\
repo: sample
facts:
  services:
    description: Services
    count: {file: registry.yaml, path: services}
  active:
    count: {file: registry.yaml, path: services, where: {status: active}}
  by_host:
    count: {file: registry.yaml, path: services, group_by: host}
  adrs:
    files: docs/ADR-*.md
  version:
    value: {file: package.json, path: version}
  machines:
    matches: {file: registry.py, pattern: "\\\\bMachine\\\\("}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "registry.yaml").write_text(REGISTRY)
    (tmp_path / "docs").mkdir()
    for n in (1, 2):
        (tmp_path / "docs" / f"ADR-000{n}-x.md").write_text("x")
    (tmp_path / "package.json").write_text('{"version": "1.2.3"}')
    (tmp_path / "registry.py").write_text("Machine(a)\nMachine(b)\nclass Machine:\n")
    (tmp_path / "facts.yaml").write_text(SPEC)
    return tmp_path


def test_each_kind_computes_from_the_files(repo: Path) -> None:
    spec = facts._load(repo / "facts.yaml")
    doc = facts.build(repo, spec, today=dt.date(2026, 11, 4))
    values = {name: f["value"] for name, f in doc["facts"].items()}
    assert values == {
        "services": 3,
        "active": 2,
        "by_host": {"aws-lambda": 1, "railway": 1, "unset": 1},
        "adrs": 2,
        "version": "1.2.3",
        "machines": 2,
    }
    assert doc["repo"] == "sample"
    assert doc["facts"]["active"]["source"] == (
        "count: registry.yaml services where status=active"
    )


def test_as_of_is_kept_while_a_value_is_unchanged(repo: Path) -> None:
    spec = facts._load(repo / "facts.yaml")
    first = facts.build(repo, spec, today=dt.date(2026, 11, 1))
    again = facts.build(repo, spec, first, today=dt.date(2026, 11, 4))
    assert again["facts"]["services"]["as_of"] == "2026-11-01"
    (repo / "docs" / "ADR-0003-x.md").write_text("x")
    changed = facts.build(repo, spec, first, today=dt.date(2026, 11, 4))
    assert changed["facts"]["adrs"]["as_of"] == "2026-11-04"


def test_generate_then_check_passes(repo: Path, capsys) -> None:
    assert facts.main(["generate", "--root", str(repo)]) == 0
    assert facts.main(["check", "--root", str(repo)]) == 0
    assert "matches (6 facts)" in capsys.readouterr().out


def test_check_fails_when_the_data_moves_on(repo: Path, capsys) -> None:
    facts.main(["generate", "--root", str(repo)])
    (repo / "docs" / "ADR-0003-x.md").write_text("x")
    assert facts.main(["check", "--root", str(repo)]) == 1
    assert "adrs: value is 2, computes to 3" in capsys.readouterr().err


def test_check_fails_when_a_fact_is_edited_by_hand(repo: Path) -> None:
    facts.main(["generate", "--root", str(repo)])
    doc = json.loads((repo / "facts.json").read_text())
    doc["facts"]["services"]["value"] = 99
    (repo / "facts.json").write_text(json.dumps(doc))
    assert facts.main(["check", "--root", str(repo)]) == 1


def test_check_without_facts_json_fails(repo: Path) -> None:
    assert facts.main(["check", "--root", str(repo)]) == 1


def test_a_bad_spec_is_reported_not_raised(repo: Path, capsys) -> None:
    (repo / "facts.yaml").write_text("facts:\n  x:\n    files: nowhere/*.md\n")
    assert facts.main(["generate", "--root", str(repo)]) == 2
    assert "no file matches" in capsys.readouterr().err


def test_a_fact_needs_exactly_one_kind(repo: Path) -> None:
    with pytest.raises(facts.FactsError):
        facts.compute(repo, {"files": "docs/*.md", "value": {"file": "x", "path": "y"}})
