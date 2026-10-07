from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from mini_app_polis.llm import LLMMessage, LLMResult, build_llm
from mini_app_polis.llm._json import parse_json, validate_json
from mini_app_polis.llm.anthropic_client import AnthropicLLM
from mini_app_polis.llm.base import LLMConfig
from mini_app_polis.llm.errors import LLMError, LLMTruncationError, LLMValidationError
from mini_app_polis.llm.openai_client import OpenAILLM

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cfg(provider: str = "openai") -> LLMConfig:
    api_key_env = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    return LLMConfig(provider=provider, model="test-model", api_key_env=api_key_env)


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
        with (
            patch(
                "mini_app_polis.llm.anthropic_client.AnthropicLLM.__init__",
                return_value=None,
            ),
            patch(
                "mini_app_polis.llm.openai_client.OpenAILLM.__init__", return_value=None
            ),
        ):
            client = build_llm(provider="openai", model="gpt-4o")
            assert isinstance(client, OpenAILLM)

    def test_anthropic_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch(
            "mini_app_polis.llm.anthropic_client.AnthropicLLM.__init__",
            return_value=None,
        ):
            client = build_llm(provider="anthropic", model="claude-3-5-sonnet-20241022")
            assert isinstance(client, AnthropicLLM)

    def test_claude_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with patch(
            "mini_app_polis.llm.anthropic_client.AnthropicLLM.__init__",
            return_value=None,
        ):
            client = build_llm(provider="claude", model="claude-3-5-sonnet-20241022")
            assert isinstance(client, AnthropicLLM)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(LLMError, match="Unknown LLM provider"):
            build_llm(provider="gemini", model="gemini-pro")

    def test_the_defaults_are_the_configs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        captured: list[LLMConfig] = []
        with patch(
            "mini_app_polis.llm.factory.AnthropicLLM",
            side_effect=lambda cfg: captured.append(cfg),
        ):
            build_llm(provider="anthropic", model="claude")
        (cfg,) = captured
        assert cfg.timeout_s == LLMConfig("p", "m", "E").timeout_s
        assert cfg.max_retries is None

    @pytest.mark.parametrize("provider", ["anthropic", "openai"])
    def test_a_caller_with_a_deadline_can_size_the_call(
        self, monkeypatch: pytest.MonkeyPatch, provider: str
    ) -> None:
        """timeout_s x (1 + max_retries) is what has to fit in the deadline."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        target = "AnthropicLLM" if provider == "anthropic" else "OpenAILLM"
        captured: list[LLMConfig] = []
        with patch(
            f"mini_app_polis.llm.factory.{target}",
            side_effect=lambda cfg: captured.append(cfg),
        ):
            build_llm(provider=provider, model="m", timeout_s=600, max_retries=0)
        (cfg,) = captured
        assert cfg.timeout_s == 600
        assert cfg.max_retries == 0


# ---------------------------------------------------------------------------
# AnthropicLLM
# ---------------------------------------------------------------------------


class TestAnthropicLLM:
    def test_the_sdk_client_is_given_the_configured_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """It used to be declared and ignored, leaving the SDK's 600s default."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_anthropic = MagicMock()
        cfg = LLMConfig(
            provider="anthropic",
            model="test-model",
            api_key_env="ANTHROPIC_API_KEY",
            timeout_s=42.0,
            max_retries=0,
        )
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            AnthropicLLM(cfg)

        mock_anthropic.Anthropic.assert_called_once()
        kwargs = mock_anthropic.Anthropic.call_args.kwargs
        assert kwargs["timeout"] == 42.0
        assert kwargs["max_retries"] == 0

    def test_the_sdk_keeps_its_own_retry_default_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            AnthropicLLM(_cfg("anthropic"))

        assert "max_retries" not in mock_anthropic.Anthropic.call_args.kwargs

    def _make_client(self, monkeypatch: pytest.MonkeyPatch) -> AnthropicLLM:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            client = AnthropicLLM(_cfg("anthropic"))
        client._client = MagicMock()
        return client

    def test_missing_api_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with (
            patch.dict("sys.modules", {"anthropic": MagicMock()}),
            pytest.raises(LLMError, match="Missing env var"),
        ):
            AnthropicLLM(_cfg("anthropic"))

    def test_missing_sdk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        with (
            patch.dict("sys.modules", {"anthropic": None}),
            pytest.raises(LLMError, match="anthropic SDK not installed"),
        ):
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

    def test_generate_json_strips_markdown_fence(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._make_client(monkeypatch)

        block = SimpleNamespace(type="text", text='```json\n{"name": "Bob"}\n```')
        mock_resp = SimpleNamespace(message=SimpleNamespace(content=[block]))
        client._client.messages.create.return_value = mock_resp

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Bob"}

    def test_generate_json_empty_response_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._make_client(monkeypatch)

        mock_resp = SimpleNamespace(message=SimpleNamespace(content=[]))
        client._client.messages.create.return_value = mock_resp

        with pytest.raises(LLMError):
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)

    def test_generate_json_schema_violation_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

    def test_max_tokens_from_config_passed_to_sdk(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        cfg = LLMConfig(
            provider="anthropic",
            model="claude-test",
            api_key_env="ANTHROPIC_API_KEY",
            max_tokens=32768,
        )
        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            client = AnthropicLLM(cfg)
        client._client = MagicMock()

        block = SimpleNamespace(type="text", text='{"name": "Alice"}')
        mock_resp = SimpleNamespace(
            stop_reason="end_turn",
            message=SimpleNamespace(content=[block]),
        )
        client._client.messages.create.return_value = mock_resp

        client.generate_json(messages=_messages(), json_schema=_SCHEMA)

        assert client._client.messages.create.call_args.kwargs["max_tokens"] == 32768

    def test_truncation_raises_llm_truncation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        cfg = LLMConfig(
            provider="anthropic",
            model="claude-test",
            api_key_env="ANTHROPIC_API_KEY",
            max_tokens=32768,
        )
        mock_anthropic = MagicMock()
        with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
            client = AnthropicLLM(cfg)
        client._client = MagicMock()

        block = SimpleNamespace(type="text", text='{"name": "partial')
        mock_resp = SimpleNamespace(
            stop_reason="max_tokens",
            message=SimpleNamespace(content=[block]),
        )
        client._client.messages.create.return_value = mock_resp

        with pytest.raises(LLMTruncationError) as exc_info:
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)

        assert "32768" in str(exc_info.value)
        assert "claude-test" in str(exc_info.value)

    def test_end_turn_does_not_raise_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._make_client(monkeypatch)

        block = SimpleNamespace(type="text", text='{"name": "Alice"}')
        mock_resp = SimpleNamespace(
            stop_reason="end_turn",
            message=SimpleNamespace(content=[block]),
        )
        client._client.messages.create.return_value = mock_resp

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Alice"}


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
        with (
            patch.dict("sys.modules", {"openai": MagicMock()}),
            pytest.raises(LLMError, match="Missing env var"),
        ):
            OpenAILLM(_cfg("openai"))

    def test_missing_sdk_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        with (
            patch.dict("sys.modules", {"openai": None}),
            pytest.raises(LLMError, match="openai SDK not installed"),
        ):
            OpenAILLM(_cfg("openai"))

    def test_generate_json_structured_output_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._make_client(monkeypatch)

        mock_resp = SimpleNamespace(output_text='{"name": "Carol"}')
        client._client.responses.create.return_value = mock_resp

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Carol"}
        assert result.provider == "openai"

    def test_generate_json_falls_back_to_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._make_client(monkeypatch)

        # Responses API fails
        client._client.responses.create.side_effect = RuntimeError("not available")

        # Chat completions fallback succeeds
        mock_choice = SimpleNamespace(
            message=SimpleNamespace(content='{"name": "Dave"}')
        )
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[mock_choice]
        )

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Dave"}

    def test_generate_json_schema_violation_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._make_client(monkeypatch)

        mock_resp = SimpleNamespace(output_text='{"wrong": "field"}')
        client._client.responses.create.return_value = mock_resp

        with pytest.raises(LLMValidationError):
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)

    def test_max_tokens_from_config_passed_to_responses_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        cfg = LLMConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="OPENAI_API_KEY",
            max_tokens=32768,
        )
        mock_openai = MagicMock()
        with patch.dict("sys.modules", {"openai": mock_openai}):
            client = OpenAILLM(cfg)
        client._client = MagicMock()

        mock_resp = SimpleNamespace(output_text='{"name": "Carol"}', status="completed")
        client._client.responses.create.return_value = mock_resp

        client.generate_json(messages=_messages(), json_schema=_SCHEMA)

        assert (
            client._client.responses.create.call_args.kwargs["max_output_tokens"]
            == 32768
        )

    def test_truncation_raises_llm_truncation_error_on_responses_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        cfg = LLMConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="OPENAI_API_KEY",
            max_tokens=32768,
        )
        mock_openai = MagicMock()
        with patch.dict("sys.modules", {"openai": mock_openai}):
            client = OpenAILLM(cfg)
        client._client = MagicMock()

        mock_resp = SimpleNamespace(
            output_text='{"name": "partial',
            status="incomplete",
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        )
        client._client.responses.create.return_value = mock_resp

        with pytest.raises(LLMTruncationError) as exc_info:
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)

        assert "32768" in str(exc_info.value)
        assert "gpt-test" in str(exc_info.value)

    def test_completed_response_does_not_raise_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._make_client(monkeypatch)

        mock_resp = SimpleNamespace(output_text='{"name": "Carol"}', status="completed")
        client._client.responses.create.return_value = mock_resp

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)
        assert result.output_json == {"name": "Carol"}

    def test_truncation_raises_on_chat_fallback_length(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        cfg = LLMConfig(
            provider="openai",
            model="gpt-test",
            api_key_env="OPENAI_API_KEY",
            max_tokens=32768,
        )
        mock_openai = MagicMock()
        with patch.dict("sys.modules", {"openai": mock_openai}):
            client = OpenAILLM(cfg)
        client._client = MagicMock()

        client._client.responses.create.side_effect = RuntimeError("not available")
        mock_choice = SimpleNamespace(
            message=SimpleNamespace(content='{"name": "partial'),
            finish_reason="length",
        )
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[mock_choice]
        )

        with pytest.raises(LLMTruncationError) as exc_info:
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)

        assert "32768" in str(exc_info.value)
        assert "gpt-test" in str(exc_info.value)
        assert (
            client._client.chat.completions.create.call_args.kwargs[
                "max_completion_tokens"
            ]
            == 32768
        )


# ---------------------------------------------------------------------------
# Construction and response-shape handling
# ---------------------------------------------------------------------------


def _anthropic(monkeypatch: pytest.MonkeyPatch) -> AnthropicLLM:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with patch.dict("sys.modules", {"anthropic": MagicMock()}):
        client = AnthropicLLM(_cfg("anthropic"))
    client._client = MagicMock()
    return client


def _openai(monkeypatch: pytest.MonkeyPatch) -> OpenAILLM:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with patch.dict("sys.modules", {"openai": MagicMock()}):
        client = OpenAILLM(_cfg("openai"))
    client._client = MagicMock()
    return client


class _Block:
    """A content block that exposes text only by subscription."""

    type = "text"
    text = None

    def __init__(self, text: object) -> None:
        self._text = text

    def __getitem__(self, key: str) -> object:
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


class TestAnthropicRequestShape:
    def test_system_messages_are_joined_into_the_system_field(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _anthropic(monkeypatch)
        client._client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"name": "A"}')]
        )

        client.generate_json(
            messages=[
                LLMMessage(role="system", content="Rule one."),
                LLMMessage(role="user", content="Hi"),
                LLMMessage(role="system", content="Rule two."),
                LLMMessage(role="assistant", content="Hello"),
            ],
            json_schema=_SCHEMA,
        )

        kwargs = client._client.messages.create.call_args.kwargs
        assert kwargs == {
            "model": "test-model",
            "max_tokens": 16384,
            "messages": [
                {"role": "user", "content": "Hi"},
                {"role": "assistant", "content": "Hello"},
            ],
            "system": "Rule one.\n\nRule two.",
        }

    def test_no_system_field_without_system_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _anthropic(monkeypatch)
        client._client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"name": "A"}')]
        )

        result = client.generate_json(
            messages=[LLMMessage(role="user", content="Hi")], json_schema=_SCHEMA
        )

        assert "system" not in client._client.messages.create.call_args.kwargs
        assert result.raw_text == '{"name": "A"}'
        assert result.model == "test-model"


class TestAnthropicExtractOutputText:
    def test_prefers_get_final_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _anthropic(monkeypatch)
        resp = MagicMock()
        resp.get_final_text.return_value = '  {"name": "F"}  '

        assert client._extract_output_text(resp) == '{"name": "F"}'

    @pytest.mark.parametrize(
        "final", [RuntimeError("stream not finished"), "   ", None]
    )
    def test_falls_back_to_content_when_get_final_text_is_unusable(
        self, monkeypatch: pytest.MonkeyPatch, final: object
    ) -> None:
        client = _anthropic(monkeypatch)
        resp = MagicMock()
        if isinstance(final, Exception):
            resp.get_final_text.side_effect = final
        else:
            resp.get_final_text.return_value = final
        resp.message = None
        resp.content = [SimpleNamespace(type="text", text="from content")]

        assert client._extract_output_text(resp) == "from content"

    def test_joins_dict_and_object_text_blocks_skipping_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _anthropic(monkeypatch)
        resp = SimpleNamespace(
            content=[
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "part one"},
                {"type": "text", "text": "   "},
                {"type": "text", "text": None},
                SimpleNamespace(type="tool_use", text="not text"),
                SimpleNamespace(type="text", text="part two"),
                _Block("part three"),
                _Block(KeyError("text")),
            ]
        )

        assert client._extract_output_text(resp) == "part one\npart two\npart three"

    def test_reports_block_types_when_no_text_is_found(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _anthropic(monkeypatch)
        resp = SimpleNamespace(
            content=[
                {"type": "tool_use"},
                {"no_type": True},
                SimpleNamespace(type="thinking"),
                object(),
            ]
        )

        with pytest.raises(LLMError) as ei:
            client._extract_output_text(resp)

        assert "content has 4 blocks" in str(ei.value)
        assert "'tool_use', 'dict', 'thinking', 'object'" in str(ei.value)

    def test_fence_without_closing_line_is_still_stripped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _anthropic(monkeypatch)
        client._client.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type="text", text='```json\n{"name": "Eve"}')]
        )

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)

        assert result.output_json == {"name": "Eve"}
        # raw_text keeps exactly what the model said.
        assert result.raw_text.startswith("```json")


class TestOpenAIConstructionAndRequests:
    def test_sdk_client_gets_max_retries_only_when_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        sdk = MagicMock()
        cfg = LLMConfig(
            provider="openai", model="m", api_key_env="OPENAI_API_KEY", max_retries=0
        )
        with patch.dict("sys.modules", {"openai": sdk}):
            OpenAILLM(cfg)
            OpenAILLM(_cfg("openai"))

        first, second = sdk.OpenAI.call_args_list
        assert first.kwargs == {"api_key": "sk-test", "max_retries": 0}
        # timeout is per request, not on the client
        assert second.kwargs == {"api_key": "sk-test"}

    def test_responses_request_carries_a_strict_schema_and_the_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _openai(monkeypatch)
        client._client.responses.create.return_value = SimpleNamespace(
            output_text='{"name": "A"}'
        )

        client.generate_json(
            messages=_messages(), json_schema=_SCHEMA, schema_name="person"
        )

        kwargs = client._client.responses.create.call_args.kwargs
        assert kwargs["input"] == [
            {"role": "system", "content": "You output JSON."},
            {"role": "user", "content": "Give me the data."},
        ]
        assert kwargs["timeout"] == 60.0
        assert kwargs["text"]["format"]["name"] == "person"
        assert kwargs["text"]["format"]["strict"] is True
        assert kwargs["text"]["format"]["schema"]["additionalProperties"] is False
        # The caller's schema is not mutated by the strict copy.
        assert "additionalProperties" not in _SCHEMA

    def test_chat_fallback_appends_a_json_only_instruction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _openai(monkeypatch)
        client._client.responses.create.side_effect = RuntimeError("no responses")
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=' {"name": "Z"} '),
                    finish_reason="stop",
                )
            ]
        )

        result = client.generate_json(messages=_messages(), json_schema=_SCHEMA)

        kwargs = client._client.chat.completions.create.call_args.kwargs
        assert kwargs["messages"][-1]["role"] == "system"
        assert "ONLY valid JSON" in kwargs["messages"][-1]["content"]
        assert kwargs["messages"][:-1] == [
            {"role": "system", "content": "You output JSON."},
            {"role": "user", "content": "Give me the data."},
        ]
        assert kwargs["temperature"] == 0.2
        assert kwargs["timeout"] == 60.0
        assert result.raw_text == '{"name": "Z"}'

    def test_schema_invalid_structured_output_falls_back_to_chat(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _openai(monkeypatch)
        client._client.responses.create.return_value = SimpleNamespace(
            output_text='{"wrong": 1}'
        )
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"name": "ok"}'))]
        )

        assert client.generate_json(
            messages=_messages(), json_schema=_SCHEMA
        ).output_json == {"name": "ok"}

    def test_fallback_with_empty_content_raises_a_validation_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _openai(monkeypatch)
        client._client.responses.create.side_effect = RuntimeError("no responses")
        client._client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))]
        )

        with pytest.raises(LLMValidationError, match="Failed to parse JSON"):
            client.generate_json(messages=_messages(), json_schema=_SCHEMA)


class TestOpenAIResponseHandling:
    def test_extracts_text_from_output_items_when_output_text_is_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _openai(monkeypatch)
        resp = SimpleNamespace(
            output_text="  ",
            output=[
                SimpleNamespace(content=None),
                SimpleNamespace(
                    content=[
                        SimpleNamespace(type="refusal", text="no"),
                        SimpleNamespace(type="output_text", text="   "),
                        SimpleNamespace(type="text", text=' {"a": 1} '),
                    ]
                ),
            ],
        )

        assert client._extract_output_text(resp) == '{"a": 1}'

    @pytest.mark.parametrize(
        "resp",
        [
            SimpleNamespace(),
            SimpleNamespace(
                output=[SimpleNamespace(content=[SimpleNamespace(type="refusal")])]
            ),
            SimpleNamespace(
                output=42
            ),  # not iterable: swallowed, then raised as LLMError
        ],
        ids=["empty", "no-text-items", "malformed"],
    )
    def test_raises_when_no_text_can_be_found(
        self, monkeypatch: pytest.MonkeyPatch, resp: object
    ) -> None:
        client = _openai(monkeypatch)

        with pytest.raises(LLMError, match="Unable to extract text from OpenAI"):
            client._extract_output_text(resp)

    @pytest.mark.parametrize(
        "resp",
        [
            SimpleNamespace(status="incomplete", incomplete_details=None),
            SimpleNamespace(
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="content_filter"),
            ),
        ],
        ids=["no-details", "other-reason"],
    )
    def test_incomplete_for_another_reason_is_not_truncation(
        self, monkeypatch: pytest.MonkeyPatch, resp: object
    ) -> None:
        client = _openai(monkeypatch)

        client._raise_if_truncated_response(resp)  # does not raise

    def test_chat_response_without_choices_is_not_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _openai(monkeypatch)

        client._raise_if_truncated_chat(SimpleNamespace(choices=[]))
        client._raise_if_truncated_chat(SimpleNamespace())


class TestSchemaStrictForApi:
    def test_every_object_is_closed_and_fully_required(self) -> None:
        from mini_app_polis.llm.openai_client import _schema_strict_for_api

        schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "address": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string"},
                        "zip": {"type": "string"},
                    },
                    "required": ["city"],
                },
                "tags": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"k": {"type": "string"}},
                    },
                },
            },
            "required": ["name"],
        }

        out = _schema_strict_for_api(schema)

        assert out["additionalProperties"] is False
        assert out["required"] == ["name", "address", "tags"]
        assert out["properties"]["address"]["required"] == ["city", "zip"]
        assert out["properties"]["address"]["additionalProperties"] is False
        item = out["properties"]["tags"]["items"]
        assert item == {
            "type": "object",
            "properties": {"k": {"type": "string"}},
            "additionalProperties": False,
            "required": ["k"],
        }
        # Validation still uses the caller's original schema.
        assert schema["required"] == ["name"]
        assert "additionalProperties" not in schema["properties"]["address"]

    def test_one_of_is_replaced_by_its_first_branch(self) -> None:
        from mini_app_polis.llm.openai_client import _schema_strict_for_api

        schema = {
            "type": "object",
            "properties": {
                "value": {
                    "oneOf": [
                        {"type": "object", "properties": {"n": {"type": "number"}}},
                        {"type": "string"},
                    ]
                },
                "empty": {"oneOf": []},
            },
        }

        out = _schema_strict_for_api(schema)

        assert out["properties"]["value"] == {
            "type": "object",
            "properties": {"n": {"type": "number"}},
            "additionalProperties": False,
            "required": ["n"],
        }
        # An empty oneOf has nothing to choose; it is left as is.
        assert out["properties"]["empty"] == {"oneOf": []}
