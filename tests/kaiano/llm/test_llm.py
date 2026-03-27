from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kaiano.llm import LLMMessage, LLMResult, build_llm
from kaiano.llm._json import parse_json, validate_json
from kaiano.llm.anthropic_client import AnthropicLLM
from kaiano.llm.base import LLMConfig
from kaiano.llm.errors import LLMError, LLMValidationError
from kaiano.llm.openai_client import OpenAILLM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cfg(provider: str = "openai") -> LLMConfig:
    return LLMConfig(provider=provider, model="test-model", api_key_env="FAKE_KEY")


def _messages() -> list[LLMMessage]:
    return [
        LLMMessage(role="system", content="You output JSON."),
        LLMMessage(role="user", content="Give me the data."),
    ]


_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


# ---------------------------------------------------------------------------
# parse_json / validate_json
# ---------------------------------------------------------------------------

class TestParseJson:
    def test_valid_json(self) -> None:
        result = parse_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(LLMValidationError, match="Failed to parse JSON"):
            parse_json("not json at all")

    def test_json_with_markdown_fence_not_stripped(self) -> None:
        # parse_json itself does NOT strip fences — that's the client's job
        with pytest.raises(LLMValidationError):
            parse_json("```json\n{}\n```")


class TestValidateJson:
    def test_valid_instance(self) -> None:
        validate_json({"name": "Alice"}, _SCHEMA)  # should not raise

    def test_missing_required_field(self) -> None:
        with pytest.raises(LLMValidationError, match="JSON schema validation failed"):
            validate_json({"wrong_key": "value"}, _SCHEMA)

    def test_wrong_type(self) -> None:
        with pytest.raises(LLMValidationError):
            validate_json({"name": 123}, _SCHEMA)


# ---------------------------------------------------------------------------
# build_llm factory
# ---------------------------------------------------------------------------

class TestBuildLlm:
    def test_openai_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with patch("kaiano.llm.anthropic_client.AnthropicLLM.__init__", return_value=None), \
             patch("kaiano.llm.openai_client.OpenAILLM.__init__", return_value=None):
            client = build_llm(provider="openai", model="gpt-4o")
            assert isinstance(client, OpenAILLM)

    def test_anthropic_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch("kaiano.llm.anthropic_client.AnthropicLLM.__init__", return_value=None):
            client = build_llm(provider="anthropic", model="claude-3-5-sonnet-20241022")
            assert isinstance(client, AnthropicLLM)

    def test_claude_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch("kaiano.llm.anthropic_client.AnthropicLLM.__init__", return_value=None):
            client = build_llm(provider="claude", model="claude-3-5-sonnet-20241022")
            assert isinstance(client, AnthropicLLM)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(LLMError, match="Unknown LLM provider"):
            build_llm(provider="gemini", model="gemini-pro")


# ---------------------------------------------------------------------------
# AnthropicLLM
# ---------------------------------------------------------------------------

class TestAnthropicLLM:
    def _make_client(self, monkeypatch: pytest.MonkeyPatch) -> AnthropicLLM:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            client = AnthropicLLM(_cfg("anthropic"))
        client._client = MagicMock()
        return client

    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch.dict("sys.modules", {"anthropic": MagicMock()}):
            with pytest.raises(LLMError, match="Missing env var"):
                AnthropicLLM(_cfg("anthropic"))

    def test_missing_sdk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch.dict("sys.modules", {"anthropic": None}):
            with pytest.raises(LLMError, match="anthropic SDK not installed"):
                AnthropicLLM(_cfg("anthropic"))

    def test_generate_json_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)

        # Build a mock response with a text block
        block = SimpleNamespace(type="text", text='{"name": "Alice"}')
        mock_resp = SimpleNamespace(message=SimpleNamespace(content=[block]))
        client._client.messages.create.return_value = mock_resp

        result = client.generate_json(
            messages=_messages(),
            json_schema=_SCHEMA,
        )
        assert isinstance(result, LLMResult)
        assert result.output_json == {"name": "Alice"}
        assert result.provider == "anthropic"

    def test_generate_json_strips_markdown_fence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)

        block = SimpleNamespace(type="text", text='```json\n{"name": "Bob"}\n```')
        mock_resp = SimpleNamespace(message=SimpleNamespace(content=[block]))
        client._client.messages.create.return_value = mock_resp

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Bob"}

    def test_generate_json_empty_response_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)

        mock_resp = SimpleNamespace(message=SimpleNamespace(content=[]))
        client._client.messages.create.return_value = mock_resp

        with pytest.raises(LLMError):
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)

    def test_generate_json_schema_violation_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)

        block = SimpleNamespace(type="text", text='{"wrong": "field"}')
        mock_resp = SimpleNamespace(message=SimpleNamespace(content=[block]))
        client._client.messages.create.return_value = mock_resp

        with pytest.raises(LLMValidationError):
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)

    def test_requires_non_system_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)
        system_only = [LLMMessage(role="system", content="System prompt.")]

        with pytest.raises(LLMError, match="non-system message"):
            client.generate_json(messages=system_only, json_schema=_SCHEMA)

    def test_api_error_wrapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)
        client._client.messages.create.side_effect = RuntimeError("network timeout")

        with pytest.raises(LLMError, match="Anthropic request failed"):
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)


# ---------------------------------------------------------------------------
# OpenAILLM
# ---------------------------------------------------------------------------

class TestOpenAILLM:
    def _make_client(self, monkeypatch: pytest.MonkeyPatch) -> OpenAILLM:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        mock_openai = MagicMock()
        with patch.dict("sys.modules", {"openai": mock_openai}):
            client = OpenAILLM(_cfg("openai"))
        client._client = MagicMock()
        return client

    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with patch.dict("sys.modules", {"openai": MagicMock()}):
            with pytest.raises(LLMError, match="Missing env var"):
                OpenAILLM(_cfg("openai"))

    def test_missing_sdk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with patch.dict("sys.modules", {"openai": None}):
            with pytest.raises(LLMError, match="openai SDK not installed"):
                OpenAILLM(_cfg("openai"))

    def test_generate_json_structured_output_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)

        mock_resp = SimpleNamespace(output_text='{"name": "Carol"}')
        client._client.responses.create.return_value = mock_resp

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Carol"}
        assert result.provider == "openai"

    def test_generate_json_falls_back_to_chat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)

        # Responses API fails
        client._client.responses.create.side_effect = RuntimeError("not available")

        # Chat completions fallback succeeds
        mock_choice = SimpleNamespace(message=SimpleNamespace(content='{"name": "Dave"}'))
        client._client.chat.completions.create.return_value = SimpleNamespace(choices=[mock_choice])

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Dave"}

    def test_generate_json_schema_violation_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._make_client(monkeypatch)

        mock_resp = SimpleNamespace(output_text='{"wrong": "field"}')
        client._client.responses.create.return_value = mock_resp

        with pytest.raises(LLMValidationError):
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)
