"""The route ledger (TEST-018): every route an API registers is called by
some test in its integration suite, or is exempt with a written reason.

Every request the suite makes is recorded by its route template — the path
as registered, router and mount prefixes included, never the raw path — by
wrapping ``APIRoute.handle``, so every test client is covered, whichever one
made the request. When a run that covers the whole suite ends, the run fails
on:

- a route ``app.routes`` registers that no test called and no exemption
  names;
- an exemption for a route a test now calls;
- an exemption for a route the app no longer registers;

so a new endpoint cannot merge without a test or a reason, and an exemption
cannot outlive its reason. A run of part of the suite checks nothing, nor
does a run that has already failed.

The API supplies its own app; nothing here imports one. Register the ledger
from the integration suite's ``conftest.py``::

    from pathlib import Path

    from mini_app_polis.testing import RouteLedger

    from app.main import app


    def pytest_configure(config):
        RouteLedger(app, suite_dir=Path(__file__).parent).register(config)

Recording happens in the process that runs the tests, so under pytest-xdist
the ledger checks nothing; run the integration suite in one process.

Requires FastAPI (the ``testing`` extra) and pytest, which every API's test
environment already has.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import fastapi.routing
import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

#: ``(method, route template)``, e.g. ``("GET", "/v1/sets/{set_id}")``.
Route = tuple[str, str]

#: The name the ledger is registered under with pytest's plugin manager.
PLUGIN_NAME = "mini_app_polis_route_ledger"

_original_handle = APIRoute.handle
_recorders: list[set[Route]] = []


async def _recording_handle(
    self: APIRoute, scope: Any, receive: Any, send: Any
) -> None:
    for called in _recorders:
        called.add((scope["method"], _template(self, scope)))
    await _original_handle(self, scope, receive, send)


def _template(route: APIRoute, scope: Any) -> str:
    # FastAPI from 0.16x keeps an included router's routes unprefixed and
    # hands the prefixed path in the request's effective route context;
    # before that, include_router copied each route with its prefix.
    context = scope.get("fastapi", {}).get("effective_route_context")
    path = getattr(context, "path", None) or route.path
    # A mounted sub-app's route sees its mount prefix in root_path, not in
    # its own path; app_root_path is the client's root_path, which is not
    # part of the template.
    root = scope.get("root_path", "")
    return root[len(scope.get("app_root_path", "")) :] + path


def registered(app: Any) -> set[Route]:
    """``(method, template)`` for every API route the app registers,
    router prefixes included, and those of mounted sub-apps under their
    mount prefix."""
    return _registered(getattr(app, "routes", []), "")


def _registered(routes: Iterable[Any], prefix: str) -> set[Route]:
    out: set[Route] = set()
    for route, path in _with_paths(routes):
        if isinstance(route, APIRoute):
            out |= {(method, prefix + path) for method in route.methods or ()}
        elif isinstance(route, Mount):
            out |= _registered(route.routes, prefix + path)
    return out


def _with_paths(routes: Iterable[Any]) -> Iterable[tuple[Any, str]]:
    """Each route with its full path. FastAPI's ``iter_route_contexts``
    (0.16x on) resolves included routers; before it, routes were flat."""
    iter_route_contexts = getattr(fastapi.routing, "iter_route_contexts", None)
    if iter_route_contexts is None:
        return [(route, route.path) for route in routes]
    return [
        (context.original_route, context.path)
        for context in iter_route_contexts(list(routes))
    ]


def problems(
    routes: set[Route], called: set[Route], exempt: Mapping[Route, str]
) -> list[str]:
    """What the ledger refuses, one line each; empty when it is satisfied."""
    out = [
        f"not called by any test: {method} {path}"
        for method, path in sorted(routes - called - exempt.keys())
    ]
    out += [
        f"exempt but called by a test; remove its exemption: {method} {path}"
        for method, path in sorted(exempt.keys() & called)
    ]
    out += [
        f"exempt but not a registered route; remove its exemption: {method} {path}"
        for method, path in sorted(exempt.keys() - routes)
    ]
    return out


class RouteLedger:
    """A pytest plugin holding one app's route ledger.

    ``app`` is the API's app object (anything with FastAPI's ``routes``).
    ``exempt`` maps ``(method, template)`` to the reason no test can call
    that route; every reason must say something.

    Whether the whole suite ran is ``whole_suite(session)`` when given.
    Otherwise it is decided from ``suite_dir``, the integration suite's
    directory: the run is partial if a test there was deselected (``-k``,
    ``-m``, ``--deselect``), if an argument names a test (``::``), a file
    or a subdirectory inside it, or if no test from it was collected.
    Naming the suite directory itself, or any directory above it, is a
    whole run.
    """

    def __init__(
        self,
        app: Any,
        *,
        exempt: Mapping[Route, str] | None = None,
        suite_dir: Path | str | None = None,
        whole_suite: Callable[[pytest.Session], bool] | None = None,
    ) -> None:
        if (suite_dir is None) == (whole_suite is None):
            raise ValueError("give exactly one of suite_dir and whole_suite")
        self.app = app
        self.exempt = dict(exempt or {})
        blank = sorted(
            f"{m} {p}" for (m, p), why in self.exempt.items() if not why.strip()
        )
        if blank:
            raise ValueError(f"route exemptions need a reason: {', '.join(blank)}")
        self.suite_dir = Path(suite_dir).resolve() if suite_dir is not None else None
        self.whole_suite = whole_suite
        self.called: set[Route] = set()
        self._deselected_in_suite = False

    # --- registration ------------------------------------------------------

    def register(self, config: pytest.Config) -> None:
        """Register with pytest and start recording; call from
        ``pytest_configure`` in the suite's conftest."""
        config.pluginmanager.register(self, PLUGIN_NAME)
        self.start()

    def start(self) -> None:
        """Record requests into this ledger."""
        if not any(c is self.called for c in _recorders):
            _recorders.append(self.called)
        APIRoute.handle = _recording_handle  # type: ignore[method-assign]

    def stop(self) -> None:
        """Stop recording; the original handler returns with the last ledger."""
        _recorders[:] = [c for c in _recorders if c is not self.called]
        if not _recorders:
            APIRoute.handle = _original_handle  # type: ignore[method-assign]

    def problems(self) -> list[str]:
        """What the ledger refuses now, one line each."""
        return problems(registered(self.app), self.called, self.exempt)

    # --- deciding whether the whole suite ran ------------------------------

    def _in_suite(self, path: Path) -> bool:
        assert self.suite_dir is not None
        return path.resolve().is_relative_to(self.suite_dir)

    def _ran_whole_suite(self, session: pytest.Session) -> bool:
        if self.whole_suite is not None:
            return self.whole_suite(session)
        assert self.suite_dir is not None
        if self._deselected_in_suite:
            return False
        for arg in session.config.args:
            path, _, node = arg.partition("::")
            target = Path(path).resolve()
            if self._in_suite(target) and (node or target != self.suite_dir):
                return False
        return any(self._in_suite(item.path) for item in session.items)

    # --- pytest hooks --------------------------------------------------------

    def pytest_deselected(self, items: list[pytest.Item]) -> None:
        if self.suite_dir is not None and any(self._in_suite(i.path) for i in items):
            self._deselected_in_suite = True

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        # An xdist worker saw only its share of the requests.
        worker = hasattr(session.config, "workerinput")
        if exitstatus != 0 or worker or not self._ran_whole_suite(session):
            return
        found = self.problems()
        if not found:
            return
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("=", "route ledger (TEST-018)", red=True)
            for line in found:
                reporter.write_line(line)
        session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_unconfigure(self) -> None:
        self.stop()
