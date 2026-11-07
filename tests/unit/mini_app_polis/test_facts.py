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


def test_matches_sums_over_a_glob(repo: Path) -> None:
    (repo / "more.py").write_text("Machine(c)\n")
    value, source = facts.compute(
        repo, {"matches": {"file": "*.py", "pattern": "Machine\\("}}
    )
    assert value == 3
    assert source == "matches: *.py /Machine\\(/"


def test_matches_within_regions_counts_each_distinct_match_once(
    repo: Path,
) -> None:
    (repo / "checks").mkdir()
    (repo / "checks" / "a.py").write_text(
        'CHECK_ID = "PY-1"\n_mark_checked("PY-1", "PY-2")\nprint("PY-9")\n'
    )
    (repo / "checks" / "b.py").write_text('CHECK_ID = "PY-3"\n_mark_checked("PY-2")\n')
    spec = {
        "file": "checks/*.py",
        "pattern": '"([A-Z]+-[0-9]+)"',
        "within": ['CHECK_ID = "[^"]+"', "_mark_checked\\([^)]*\\)"],
    }
    total, _ = facts.compute(repo, {"matches": spec})
    distinct, source = facts.compute(repo, {"matches": {**spec, "distinct": True}})
    assert total == 5
    assert distinct == 3
    assert source.endswith(" distinct")
    assert "within" in source


def test_a_single_within_pattern_may_be_a_string(repo: Path) -> None:
    value, _ = facts.compute(
        repo,
        {
            "matches": {
                "file": "registry.py",
                "pattern": "Machine",
                "within": "Machine\\(a\\)",
            }
        },
    )
    assert value == 1


#: A test function, as a test_functions fact counts one.
TEST_DEF = "(?m)^\\s*(?:async\\s+)?def test_\\w+"


def test_double_star_reaches_every_depth_but_not_hidden_directories(
    repo: Path,
) -> None:
    (repo / "tests" / "unit" / "deep").mkdir(parents=True)
    (repo / "tests" / "test_top.py").write_text("def test_a():\n    pass\n")
    (repo / "tests" / "unit" / "deep" / "test_deep.py").write_text(
        "class TestX:\n    def test_b(self):\n        pass\n\n"
        "async def test_c():\n    pass\n\ndef helper_test_d():\n    pass\n"
    )
    (repo / "tests" / ".cache").mkdir()
    (repo / "tests" / ".cache" / "test_hidden.py").write_text("def test_z(): ...\n")
    value, source = facts.compute(
        repo, {"matches": {"file": "tests/**/test_*.py", "pattern": TEST_DEF}}
    )
    assert value == 3
    assert source == f"matches: tests/**/test_*.py /{TEST_DEF}/"


def test_file_may_be_a_list_of_globs_each_file_counted_once(repo: Path) -> None:
    (repo / "src").mkdir()
    (repo / "src" / "a.test.ts").write_text("it('a', () => {})\n")
    (repo / "src" / "b.test.tsx").write_text("it('b', () => {})\nit('c', () => {})\n")
    value, source = facts.compute(
        repo,
        {
            "matches": {
                "file": ["src/**/*.test.ts", "src/**/*.test.tsx", "src/a.test.ts"],
                "pattern": "(?m)^\\s*it\\(",
            }
        },
    )
    assert value == 3
    assert source.startswith(
        "matches: src/**/*.test.ts, src/**/*.test.tsx, src/a.test.ts /"
    )


def test_each_glob_in_a_list_must_match_a_file(repo: Path) -> None:
    with pytest.raises(facts.FactsError, match="nothing/\\*.py"):
        facts.compute(repo, {"files": ["docs/ADR-*.md", "nothing/*.py"]})


def _git(repo: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def git_repo(repo: Path) -> Path:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    return repo


def test_generate_stage_adds_facts_json_when_it_changed(git_repo: Path) -> None:
    assert facts.main(["generate", "--root", str(git_repo), "--stage"]) == 0
    assert _git(git_repo, "diff", "--cached", "--name-only").split() == ["facts.json"]


def test_generate_stage_stages_nothing_else(git_repo: Path) -> None:
    (git_repo / "other.txt").write_text("not mine\n")
    assert facts.main(["generate", "--root", str(git_repo), "--stage"]) == 0
    assert "other.txt" not in _git(git_repo, "diff", "--cached", "--name-only")


def test_generate_stage_does_nothing_when_facts_json_is_current(
    git_repo: Path, capsys
) -> None:
    facts.main(["generate", "--root", str(git_repo)])
    capsys.readouterr()
    assert facts.main(["generate", "--root", str(git_repo), "--stage"]) == 0
    assert "staged" not in capsys.readouterr().out
    assert _git(git_repo, "diff", "--cached", "--name-only") == ""


def test_generate_stage_outside_a_git_repository_fails(repo: Path, capsys) -> None:
    assert facts.main(["generate", "--root", str(repo), "--stage"]) == 1
    assert "could not stage" in capsys.readouterr().err
