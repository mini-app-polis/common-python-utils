"""Named API keys — the credential a cog presents."""

from __future__ import annotations

import pytest

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.errors import KaianoApiError


def test_api_key_is_sent_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token exchange: the key is the credential."""
    monkeypatch.setattr(
        "mini_app_polis.api.client._get_m2m_token",
        lambda _s: pytest.fail("must not mint a token when a key is present"),
    )
    c = KaianoApiClient(base_url="https://x", api_key="k_deejay")
    assert c._headers()["Authorization"] == "Bearer k_deejay"


def test_api_key_wins_over_the_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cog with its own key must never fall back to the shared identity."""
    monkeypatch.setattr(
        "mini_app_polis.api.client._get_m2m_token",
        lambda _s: pytest.fail("must not mint a token when a key is present"),
    )
    c = KaianoApiClient(base_url="https://x", api_key="k_deejay", machine_secret="sk")
    assert c._headers()["Authorization"] == "Bearer k_deejay"


def test_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIANO_API_KEY", "k_from_env")
    c = KaianoApiClient(base_url="https://x")
    assert c._headers()["Authorization"] == "Bearer k_from_env"


def test_legacy_clerk_path_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cogs not yet given a key keep working on the shared machine secret."""
    monkeypatch.delenv("KAIANO_API_KEY", raising=False)
    monkeypatch.setattr("mini_app_polis.api.client._get_m2m_token", lambda _s: "tok")
    c = KaianoApiClient(base_url="https://x", machine_secret="sk")
    assert c._headers()["Authorization"] == "Bearer tok"


def test_no_credential_at_all_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KAIANO_API_KEY", raising=False)
    monkeypatch.delenv("KAIANO_API_CLERK_MACHINE_SECRET", raising=False)
    c = KaianoApiClient(base_url="https://x")
    with pytest.raises(KaianoApiError):
        c._headers()
