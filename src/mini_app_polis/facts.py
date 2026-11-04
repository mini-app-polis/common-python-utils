"""``facts``: the figures a repository owns, computed from its own files.

A repository that owns some data publishes the figures drawn from it, so a
reader (a resume, a site, a report) points at them instead of recounting.
The repository declares each figure in ``facts.yaml``; this module computes
them and writes ``facts.json``, which is committed. ``facts check`` runs in
CI and fails when the committed file no longer matches what the repository's
data computes, so a published figure cannot drift. Facts are generated,
never edited by hand.

``facts.yaml``::

    repo: ecosystem-standards
    facts:
      services_active:
        description: Active services in the registry
        count: {file: ecosystem.yaml, path: services, where: {status: active}}
      services_by_host:
        description: Services by where they run
        count: {file: ecosystem.yaml, path: services, group_by: host}
      adrs:
        description: Architecture decision records
        files: docs/decisions/ADR-*.md
      rule_count:
        description: A value read from a file
        value: {file: catalog.json, path: rule_count}
      machines:
        description: Declared machine principals
        matches: {file: src/registry.py, pattern: "Machine\\\\("}

Each fact uses one of four kinds:

- ``count``: the entries of a list or mapping at ``path`` (dotted) in a YAML
  or JSON file, summed over every file ``file`` matches as a glob. ``where``
  keeps entries whose fields equal the given values; ``group_by`` counts per
  value of a field instead of in total (an entry without the field counts
  as ``unset``).
- ``files``: the files a glob matches.
- ``value``: the value at ``path`` in a YAML or JSON file.
- ``matches``: the matches of a regular expression in a file.

Paths are relative to the repository root. A fact's ``as_of`` is the day its
value last changed; regenerating without a change keeps it.

    facts generate   compute the facts and write facts.json
    facts check      exit 1 when facts.json differs from what is computed
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

SPEC_FILE = "facts.yaml"
OUT_FILE = "facts.json"
KINDS = ("count", "files", "value", "matches")


class FactsError(ValueError):
    """The spec is invalid, or a fact cannot be computed from the files."""


def _load(path: Path) -> Any:
    text = path.read_text()
    return json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)


def _at(data: Any, path: str) -> Any:
    for key in path.split(".") if path else ():
        if not isinstance(data, Mapping) or key not in data:
            raise FactsError(f"no {path!r}")
        data = data[key]
    return data


def _paths(root: Path, pattern: str) -> list[Path]:
    found = sorted(Path(p) for p in glob.glob(str(root / pattern)))
    if not found:
        raise FactsError(f"no file matches {pattern!r}")
    return found


def _entries(data: Any, path: str) -> list[Any]:
    items = _at(data, path)
    if isinstance(items, Mapping):
        return list(items.values())
    if isinstance(items, list):
        return items
    raise FactsError(f"{path!r} is neither a list nor a mapping")


def _count(root: Path, spec: Mapping[str, Any]) -> int | dict[str, int]:
    where = spec.get("where") or {}
    group_by = spec.get("group_by")
    entries = [
        entry
        for p in _paths(root, spec["file"])
        for entry in _entries(_load(p), spec.get("path", ""))
        if all(
            isinstance(entry, Mapping) and entry.get(k) == v for k, v in where.items()
        )
    ]
    if group_by is None:
        return len(entries)
    counts = Counter(
        str(entry[group_by])
        if isinstance(entry, Mapping) and entry.get(group_by) is not None
        else "unset"
        for entry in entries
    )
    return dict(sorted(counts.items()))


def compute(root: Path, spec: Mapping[str, Any]) -> tuple[Any, str]:
    """One fact's value, and a line saying what it was computed from."""
    kinds = [k for k in KINDS if k in spec]
    if len(kinds) != 1:
        raise FactsError(f"a fact needs exactly one of {', '.join(KINDS)}")
    kind = kinds[0]
    how = spec[kind]
    if kind == "files":
        return len(_paths(root, how)), f"files: {how}"
    if kind == "count":
        source = f"count: {how['file']}" + (
            f" {how['path']}" if how.get("path") else ""
        )
        if how.get("where"):
            source += " where " + ", ".join(f"{k}={v}" for k, v in how["where"].items())
        if how.get("group_by"):
            source += f" by {how['group_by']}"
        return _count(root, how), source
    if kind == "value":
        (path,) = _paths(root, how["file"])[:1]
        return _at(_load(path), how["path"]), f"value: {how['file']} {how['path']}"
    (path,) = _paths(root, how["file"])[:1]
    matches = len(re.findall(how["pattern"], path.read_text()))
    return matches, f"matches: {how['file']} /{how['pattern']}/"


def build(
    root: Path,
    spec: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    *,
    today: dt.date | None = None,
) -> dict[str, Any]:
    """The facts document for ``spec``; ``as_of`` kept where a value is unchanged."""
    today = today or dt.datetime.now(dt.UTC).date()
    before = (previous or {}).get("facts", {})
    facts: dict[str, Any] = {}
    for name, fact in (spec.get("facts") or {}).items():
        value, source = compute(root, fact)
        old = before.get(name, {})
        facts[name] = {
            "value": value,
            "description": fact.get("description", ""),
            "source": source,
            "as_of": old["as_of"]
            if old.get("value") == value and "as_of" in old
            else today.isoformat(),
        }
    return {"repo": spec.get("repo", root.name), "facts": facts}


def differences(committed: Mapping[str, Any], computed: Mapping[str, Any]) -> list[str]:
    """What differs between the committed document and the computed one."""
    old, new = committed.get("facts", {}), computed.get("facts", {})
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in new:
            lines.append(f"{name}: in facts.json but no longer declared")
        elif name not in old:
            lines.append(f"{name}: declared but missing from facts.json")
        else:
            for key in ("value", "source", "description"):
                if old[name].get(key) != new[name].get(key):
                    lines.append(
                        f"{name}: {key} is {old[name].get(key)!r}, "
                        f"computes to {new[name].get(key)!r}"
                    )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command. Returns the exit code."""
    parser = argparse.ArgumentParser(
        prog="facts", description="The figures this repository owns."
    )
    parser.add_argument("command", choices=["generate", "check"])
    parser.add_argument("--root", default=".", help="The repository root.")
    args = parser.parse_args(argv)
    root = Path(args.root)
    out = root / OUT_FILE
    try:
        spec = _load(root / SPEC_FILE) or {}
        previous = json.loads(out.read_text()) if out.exists() else None
        document = build(root, spec, previous)
    except (FactsError, OSError, KeyError, yaml.YAMLError) as exc:
        print(f"facts: {exc}", file=sys.stderr)
        return 2
    if args.command == "generate":
        out.write_text(json.dumps(document, indent=2) + "\n")
        print(f"facts: wrote {len(document['facts'])} facts to {OUT_FILE}")
        return 0
    if previous is None:
        print(f"facts: {OUT_FILE} is missing; run `facts generate`", file=sys.stderr)
        return 1
    diffs = differences(previous, document)
    if diffs:
        print(
            f"facts: {OUT_FILE} is out of date; run `facts generate`", file=sys.stderr
        )
        for line in diffs:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"facts: {OUT_FILE} matches ({len(document['facts'])} facts)")
    return 0


def cli() -> None:
    """The ``facts`` console script."""
    sys.exit(main())
