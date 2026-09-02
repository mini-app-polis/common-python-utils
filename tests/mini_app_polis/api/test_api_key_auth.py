"""Named API keys — the credential a cog presents."""

from __future__ import annotations

import pytest

from mini_app_polis.api import KaianoApiClient
from mini_app_polis.api.errors import KaianoApiError


def test_api_key_is_sent_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    """No token exchange: the key is the credential."""
    c = KaianoApiClient(base_url="https://x", api_key="k_deejay")
    assert c._headers()["Authorization"] == "Bearer k_deejay"


def test_key_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KAIANO_API_KEY", "k_from_env")
    c = KaianoApiClient(base_url="https://x")
    assert c._headers()["Authorization"] == "Bearer k_from_env"


def test_no_key_is_an_error_not_a_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """There is no shared credential to fall back to any more.

    A cog without its own key cannot authenticate at all, which is the point:
    the shared secret it would have used made every cog indistinguishable.
    """
    monkeypatch.delenv("KAIANO_API_KEY", raising=False)
    c = KaianoApiClient(base_url="https://x")
    with pytest.raises(KaianoApiError):
        c._headers()


def test_one_cogs_key_is_not_used_by_another(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each cog reads only its own variable, from one shared config."""
    monkeypatch.setenv("DEEJAY_COG_API_KEY", "k_deejay")
    monkeypatch.delenv("EVALUATOR_COG_API_KEY", raising=False)
    monkeypatch.delenv("KAIANO_API_KEY", raising=False)
    c = KaianoApiClient(base_url="https://x", machine_name="evaluator-cog")
    assert c.api_key is None
    with pytest.raises(KaianoApiError):
        c._headers()


# ---------------------------------------------------------------------------
# Per-machine key derivation
# ---------------------------------------------------------------------------


def test_env_var_is_derived_from_the_machine_name() -> None:
    """One convention, shared with the receiving service."""
    from mini_app_polis.api.client import machine_key_env_var

    assert machine_key_env_var("deejay-cog") == "DEEJAY_COG_API_KEY"
    assert machine_key_env_var("wiki-curator-cog") == "WIKI_CURATOR_COG_API_KEY"


def test_named_machine_uses_its_own_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIPTION_COG_API_KEY", "k_transcribe")
    monkeypatch.setenv("KAIANO_API_KEY", "k_generic")
    c = KaianoApiClient(base_url="https://x", machine_name="transcription-cog")
    assert c._headers()["Authorization"] == "Bearer k_transcribe"


def test_from_env_accepts_a_machine_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALUATOR_COG_API_KEY", "k_eval")
    assert KaianoApiClient.from_env("evaluator-cog").api_key == "k_eval"
