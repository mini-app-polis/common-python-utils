"""Checking a repo against Doppler dev, and writing secrets to Doppler."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from mini_app_polis import doppler
from mini_app_polis.doppler import (
    DopplerClient,
    DopplerError,
    check_keys,
    check_keys_main,
    declared_names,
    doppler_project,
    set_secrets_with_cli,
)

DOPPLER_YAML = "setup:\n  - project: mini-app-polis-ecosystem\n    config: dev\n"


def _cli(monkeypatch: pytest.MonkeyPatch, run) -> list[dict]:
    calls: list[dict] = []

    def fake_run(args, **kwargs):
        calls.append({"args": args, **kwargs})
        return run(args, **kwargs)

    monkeypatch.setattr(doppler.shutil, "which", lambda _n: "/usr/bin/doppler")
    monkeypatch.setattr(doppler.subprocess, "run", fake_run)
    return calls


def _repo(tmp_path: Path, env_example: str) -> Path:
    (tmp_path / ".env.example").write_text(env_example)
    (tmp_path / "doppler.yaml").write_text(DOPPLER_YAML)
    return tmp_path


# -- the contract ------------------------------------------------------------


def test_uncommented_names_are_required_and_commented_ones_optional() -> None:
    text = "# header\nA_KEY=\nB_KEY=value\n# C_KEY=1\n#   D_KEY=\n# prose, not a name\n"

    assert declared_names(text) == (["A_KEY", "B_KEY"], ["C_KEY", "D_KEY"])


def test_the_project_comes_from_doppler_yaml() -> None:
    assert doppler_project(DOPPLER_YAML) == "mini-app-polis-ecosystem"


def test_a_doppler_yaml_without_a_project_is_an_error() -> None:
    with pytest.raises(DopplerError, match="no project"):
        doppler_project("setup: []\n")


# -- check_keys --------------------------------------------------------------


def test_it_reads_names_only_from_dev_and_reports_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _cli(
        monkeypatch,
        lambda args, **_: subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"A_KEY": {}}), stderr=""
        ),
    )
    root = _repo(tmp_path, "A_KEY=\nB_KEY=\n# C_KEY=\n")

    assert check_keys(root) == 1

    args = calls[0]["args"]
    assert "--only-names" in args
    assert args[args.index("--config") + 1] == "dev"
    assert args[args.index("--project") + 1] == "mini-app-polis-ecosystem"
    out = capsys.readouterr().out
    assert "MISSING (required): B_KEY" in out
    assert "Optional, not set (defaults apply): C_KEY" in out


def test_it_passes_when_dev_has_every_required_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cli(
        monkeypatch,
        lambda args, **_: subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"A_KEY": {}, "C_KEY": {}}), stderr=""
        ),
    )

    assert check_keys(_repo(tmp_path, "A_KEY=\n# C_KEY=\n")) == 0
    assert "All required names are present." in capsys.readouterr().out


def test_a_cli_failure_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cli(
        monkeypatch,
        lambda args, **_: subprocess.CompletedProcess(
            args, 1, stdout="", stderr="Unable to authenticate"
        ),
    )

    with pytest.raises(DopplerError, match="Unable to authenticate"):
        check_keys(_repo(tmp_path, "A_KEY=\n"))


def test_output_that_is_not_json_is_an_error_not_a_missing_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cli(
        monkeypatch,
        lambda args, **_: subprocess.CompletedProcess(
            args, 0, stdout="Doppler Error: …", stderr=""
        ),
    )

    with pytest.raises(DopplerError, match="not JSON"):
        check_keys(_repo(tmp_path, "A_KEY=\n"))


def test_without_the_cli_it_says_how_to_install_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doppler.shutil, "which", lambda _n: None)

    with pytest.raises(DopplerError, match="brew install"):
        check_keys(_repo(tmp_path, "A_KEY=\n"))


@pytest.mark.parametrize(
    ("stdout", "code"), [(json.dumps({"A_KEY": {}}), 0), (json.dumps({}), 1)]
)
def test_the_console_script_checks_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, code: int
) -> None:
    _cli(
        monkeypatch,
        lambda args, **_: subprocess.CompletedProcess(
            args, 0, stdout=stdout, stderr=""
        ),
    )
    monkeypatch.chdir(_repo(tmp_path, "A_KEY=\n"))

    with pytest.raises(SystemExit) as exc:
        check_keys_main()
    assert exc.value.code == code


def test_the_console_script_exits_2_outside_a_repo(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc:
        check_keys_main()
    assert exc.value.code == 2
    assert ".env.example" in capsys.readouterr().err


# -- DopplerClient -------------------------------------------------------------


def _client(handler) -> DopplerClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return DopplerClient("dp.st.dev.x", "proj", "prd", http=http)


def test_set_secrets_posts_to_the_config_with_the_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"secrets": {}})

    assert _client(handler).set_secrets({"B": "2", "A": "1"}) == ["A", "B"]

    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == doppler.API_URL
    assert req.headers["Authorization"] == "Bearer dp.st.dev.x"
    assert json.loads(req.content) == {
        "project": "proj",
        "config": "prd",
        "secrets": {"B": "2", "A": "1"},
    }


def test_nothing_to_set_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    assert _client(handler).set_secrets({}) == []


def test_a_refusal_names_the_status_but_never_a_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"echo": "super-secret"})

    with pytest.raises(DopplerError) as exc:
        _client(handler).set_secrets({"A": "super-secret"})
    assert "HTTP 403" in str(exc.value)
    assert "super-secret" not in str(exc.value)


def test_a_transport_failure_is_a_doppler_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(DopplerError, match="unreachable"):
        _client(handler).set_secrets({"A": "1"})


def test_without_a_client_it_opens_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    real = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    monkeypatch.setattr(
        doppler.httpx,
        "Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )

    assert DopplerClient("t", "p", "c").set_secrets({"A": "1"}) == ["A"]


def test_an_empty_token_is_refused() -> None:
    with pytest.raises(DopplerError, match="No Doppler token"):
        DopplerClient("", "p", "c")


def test_from_env_reads_the_names_doppler_syncs_inject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOPPLER_TOKEN", "t")
    monkeypatch.setenv("DOPPLER_PROJECT", "p")
    monkeypatch.setenv("DOPPLER_CONFIG", "c")

    client = DopplerClient.from_env()
    assert (client.project, client.config) == ("p", "c")


def test_from_env_names_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOPPLER_TOKEN", raising=False)

    with pytest.raises(DopplerError, match="DOPPLER_TOKEN is not set"):
        DopplerClient.from_env()


def test_get_secret_reads_one_name_from_the_config() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"name": "A", "value": {"raw": "${B}", "computed": "2026-03-29"}},
        )

    assert _client(handler).get_secret("A") == "2026-03-29"

    req = seen[0]
    assert req.method == "GET"
    assert req.url.copy_with(query=None) == httpx.URL(doppler.SECRET_URL)
    assert dict(req.url.params) == {"project": "proj", "config": "prd", "name": "A"}
    assert req.headers["Authorization"] == "Bearer dp.st.dev.x"


def test_get_secret_falls_back_to_raw() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": {"raw": "1"}})

    assert _client(handler).get_secret("A") == "1"


def test_get_secret_answers_none_for_a_missing_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"messages": ["Could not find secret"]})

    assert _client(handler).get_secret("A") is None


def test_get_secret_refusal_never_carries_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"echo": "super-secret"})

    with pytest.raises(DopplerError) as exc:
        _client(handler).get_secret("A")
    assert "HTTP 401" in str(exc.value)
    assert "reading A" in str(exc.value)
    assert "super-secret" not in str(exc.value)


def test_get_secret_unexpected_body_is_a_doppler_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    with pytest.raises(DopplerError, match="unexpected body"):
        _client(handler).get_secret("A")


def test_get_secret_transport_failure_is_a_doppler_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(DopplerError, match="unreachable reading A"):
        _client(handler).get_secret("A")


# -- set_secrets_with_cli ------------------------------------------------------


def test_the_cli_gets_each_value_on_stdin_never_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _cli(
        monkeypatch,
        lambda args, **_: subprocess.CompletedProcess(args, 0, stdout="", stderr=""),
    )

    written = set_secrets_with_cli(
        {"TOKEN": "s3cret", "ISSUED_AT": "2026-10-09"}, project="p", config="prd"
    )

    assert written == ["TOKEN", "ISSUED_AT"]
    first = calls[0]
    assert first["args"][:4] == ["doppler", "secrets", "set", "TOKEN"]
    assert first["args"][first["args"].index("--config") + 1] == "prd"
    assert first["input"] == "s3cret"
    assert "s3cret" not in first["args"]
    assert first["capture_output"] is True


def test_a_cli_refusal_stops_and_names_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cli(
        monkeypatch,
        lambda args, **_: subprocess.CompletedProcess(args, 1, stdout="", stderr=""),
    )

    with pytest.raises(DopplerError, match="set A failed"):
        set_secrets_with_cli({"A": "1"}, project="p", config="prd")


def test_writing_without_the_cli_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doppler.shutil, "which", lambda _n: None)

    with pytest.raises(DopplerError, match="not installed"):
        set_secrets_with_cli({"A": "1"}, project="p", config="prd")
