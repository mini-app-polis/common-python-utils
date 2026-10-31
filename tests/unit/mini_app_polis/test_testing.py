"""The route ledger (TEST-018): what it records, what it refuses, and when
it checks."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mini_app_polis import testing
from mini_app_polis.testing import RouteLedger, problems, registered

pytest_plugins = ["pytester"]


def stand_in() -> FastAPI:
    app = FastAPI()
    router = APIRouter(prefix="/v1/things")

    @router.get("/{thing_id}")
    async def thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    @router.post("")
    async def make() -> dict[str, str]:
        return {}

    app.include_router(router)

    sub = FastAPI()

    @sub.get("/ping")
    async def ping() -> dict[str, str]:
        return {}

    app.mount("/admin", sub)
    return app


@pytest.fixture
def ledger(tmp_path: Path):
    ledger = RouteLedger(stand_in(), suite_dir=tmp_path)
    ledger.start()
    yield ledger
    ledger.stop()


def test_routes_are_registered_by_method_and_full_template() -> None:
    assert registered(stand_in()) == {
        ("GET", "/v1/things/{thing_id}"),
        ("POST", "/v1/things"),
        ("GET", "/admin/ping"),
    }


def test_requests_are_recorded_by_template_with_prefixes(
    ledger: RouteLedger,
) -> None:
    client = TestClient(ledger.app)
    assert client.get("/v1/things/42").status_code == 200
    assert client.get("/admin/ping").status_code == 200
    assert client.get("/nowhere").status_code == 404
    assert ledger.called == {("GET", "/v1/things/{thing_id}"), ("GET", "/admin/ping")}
    assert ledger.problems() == ["not called by any test: POST /v1/things"]


def test_a_client_root_path_is_not_part_of_the_template(ledger: RouteLedger) -> None:
    client = TestClient(ledger.app, root_path="/api")
    assert client.get("/api/admin/ping").status_code == 200
    assert ledger.called == {("GET", "/admin/ping")}


def test_stopping_the_last_ledger_restores_the_handler(tmp_path: Path) -> None:
    original = APIRoute.handle
    first = RouteLedger(stand_in(), suite_dir=tmp_path)
    second = RouteLedger(stand_in(), suite_dir=tmp_path)
    first.start()
    first.start()
    second.start()
    first.stop()
    assert APIRoute.handle is not original
    TestClient(second.app).get("/admin/ping")
    assert first.called == set()
    assert second.called == {("GET", "/admin/ping")}
    second.stop()
    assert APIRoute.handle is original


def test_routes_are_read_flat_where_fastapi_does_not_nest_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fastapi.routing

    monkeypatch.delattr(fastapi.routing, "iter_route_contexts", raising=False)
    app = FastAPI()

    @app.delete("/v1/things/{thing_id}")
    async def drop(thing_id: str) -> None:
        return None

    assert registered(app) == {("DELETE", "/v1/things/{thing_id}")}


def test_an_exemption_covers_an_uncalled_route_until_it_is_called() -> None:
    routes = {("GET", "/a"), ("POST", "/a")}
    exempt = {("POST", "/a"): "a reason"}
    assert problems(routes, {("GET", "/a")}, exempt) == []
    assert problems(routes, routes, exempt) == [
        "exempt but called by a test; remove its exemption: POST /a"
    ]


def test_an_exemption_for_an_unregistered_route_is_refused() -> None:
    assert problems({("GET", "/a")}, {("GET", "/a")}, {("GET", "/b"): "gone"}) == [
        "exempt but not a registered route; remove its exemption: GET /b"
    ]


def test_an_exemption_needs_a_reason(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="need a reason: POST /a"):
        RouteLedger(FastAPI(), exempt={("POST", "/a"): " "}, suite_dir=tmp_path)


@pytest.mark.parametrize("given", [{}, {"suite_dir": ".", "whole_suite": bool}])
def test_whole_suite_is_decided_one_way(given: dict) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        RouteLedger(FastAPI(), **given)


# --- as a plugin, in a real pytest run -------------------------------------

APP = """
from fastapi import FastAPI

app = FastAPI()


@app.get("/one")
async def one():
    return {}


@app.get("/two")
async def two():
    return {}
"""

TESTS = """
from fastapi.testclient import TestClient

from app_under_test import app


def test_one():
    TestClient(app).get("/one")


def test_fails():
    assert {fail}
"""


@pytest.fixture
def suite(pytester: pytest.Pytester):
    pytester.makepyfile(app_under_test=APP)
    pytester.syspathinsert()
    integration = pytester.mkpydir("integration")

    def make(*, exempt: str = "{}", whole: str | None = None, fail: bool = False):
        how = f"whole_suite={whole}" if whole else "suite_dir=Path(__file__).parent"
        (integration / "conftest.py").write_text(
            textwrap.dedent(f"""
                from pathlib import Path

                from app_under_test import app
                from mini_app_polis.testing import RouteLedger


                def pytest_configure(config):
                    RouteLedger(app, exempt={exempt}, {how}).register(config)
            """)
        )
        (integration / "test_api.py").write_text(TESTS.format(fail=not fail))
        return integration

    return make


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest("-p", "no:cacheprovider", *args)


def test_a_whole_run_fails_on_an_uncalled_route(suite, pytester) -> None:
    suite()
    result = run(pytester)
    result.stdout.fnmatch_lines(["*route ledger (TEST-018)*", "*: GET /two"])
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_the_run_fails_without_a_terminal_too(suite, pytester) -> None:
    suite()
    assert run(pytester, "-p", "no:terminal").ret == pytest.ExitCode.TESTS_FAILED


def test_naming_the_suite_or_above_is_a_whole_run(suite, pytester) -> None:
    integration = suite()
    assert run(pytester, str(integration)).ret == pytest.ExitCode.TESTS_FAILED
    assert run(pytester, ".").ret == pytest.ExitCode.TESTS_FAILED


def test_an_exempt_route_passes(suite, pytester) -> None:
    suite(exempt="{('GET', '/two'): 'no test can call it'}")
    run(pytester).assert_outcomes(passed=2)
    assert run(pytester).ret == pytest.ExitCode.OK


def test_a_stale_exemption_fails(suite, pytester) -> None:
    suite(exempt="{('GET', '/two'): 'why', ('GET', '/one'): 'why'}")
    result = run(pytester)
    result.stdout.fnmatch_lines(["*called by a test; remove its exemption: GET /one"])
    assert result.ret == pytest.ExitCode.TESTS_FAILED


@pytest.mark.parametrize(
    "args",
    [
        ["-k", "one"],
        ["integration/test_api.py"],
        ["integration/test_api.py::test_one"],
    ],
)
def test_a_partial_run_checks_nothing(suite, pytester, args) -> None:
    suite()
    assert run(pytester, *args).ret == pytest.ExitCode.OK


def test_a_run_without_suite_tests_checks_nothing(suite, pytester) -> None:
    suite()
    pytester.makepyfile(test_elsewhere="def test_x():\n    pass\n")
    assert run(pytester, "test_elsewhere.py").ret == pytest.ExitCode.OK
    assert run(pytester, "test_elsewhere.py", "integration").ret != 0


def test_deselection_outside_the_suite_is_still_a_whole_run(suite, pytester) -> None:
    suite()
    pytester.makepyfile(test_elsewhere="def test_x():\n    pass\n")
    result = run(pytester, "--deselect", "test_elsewhere.py::test_x")
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_an_xdist_worker_checks_nothing(suite, pytester) -> None:
    suite()
    pytester.makeconftest(
        "def pytest_configure(config):\n    config.workerinput = {}\n"
    )
    assert run(pytester).ret == pytest.ExitCode.OK


def test_a_failing_run_is_not_checked_again(suite, pytester) -> None:
    suite(fail=True)
    result = run(pytester)
    assert "route ledger" not in result.stdout.str()
    assert result.ret == pytest.ExitCode.TESTS_FAILED


def test_the_consumer_can_decide_what_a_whole_run_is(suite, pytester) -> None:
    suite(whole="lambda session: False")
    assert run(pytester).ret == pytest.ExitCode.OK
    suite(whole="lambda session: True")
    assert run(pytester, "-k", "one").ret == pytest.ExitCode.TESTS_FAILED


def test_the_module_imports_no_app() -> None:
    assert not any(isinstance(v, FastAPI) for v in vars(testing).values())
