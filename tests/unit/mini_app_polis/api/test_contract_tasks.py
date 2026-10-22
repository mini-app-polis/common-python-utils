"""The task-request contract: what a source may hand the task manager."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mini_app_polis.api.contract import TaskCreateRequest, TaskDraft


def _request(**overrides) -> dict:
    base = {
        "source": "voicenote",
        "external_id": "voicenote.file-1",
        "draft": {"title": "Call the venue"},
    }
    base.update(overrides)
    return base


def test_a_minted_source_needs_no_id() -> None:
    request = TaskCreateRequest.model_validate(_request())
    assert request.id is None
    assert request.draft.title == "Call the venue"


def test_a_minted_source_refuses_an_id() -> None:
    with pytest.raises(ValidationError, match="minted"):
        TaskCreateRequest.model_validate(_request(id="VN-4"))


def test_a_given_id_source_needs_its_id() -> None:
    with pytest.raises(ValidationError, match="needs an id"):
        TaskCreateRequest.model_validate(
            _request(source="github-issue", external_id="github-issue.o/r#1")
        )
    request = TaskCreateRequest.model_validate(
        _request(source="github-issue", external_id="github-issue.o/r#1", id="r#1")
    )
    assert request.id == "r#1"


def test_the_external_id_carries_its_source() -> None:
    with pytest.raises(ValidationError, match="must start with voicenote."):
        TaskCreateRequest.model_validate(_request(external_id="file-1"))


def test_an_unknown_source_is_refused() -> None:
    with pytest.raises(ValidationError):
        TaskCreateRequest.model_validate(
            _request(source="discord", external_id="discord.1")
        )


def test_a_blank_title_is_allowed() -> None:
    """The task manager names it for review rather than losing the work."""
    assert TaskDraft(title="").title == ""


def test_links_must_be_https() -> None:
    with pytest.raises(ValidationError):
        TaskDraft(links=[{"text": "audio", "url": "http://example.com/a"}])


@pytest.mark.parametrize("label", ["", "   ", "x" * 51])
def test_labels_are_tag_names(label: str) -> None:
    with pytest.raises(ValidationError, match="tag name"):
        TaskDraft(labels=[label])


def test_unknown_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        TaskDraft.model_validate({"title": "t", "priority": "high"})


@pytest.mark.parametrize("source", ["ci", "cog-run", "conformance", "api-fault"])
def test_detected_problems_are_minted_sources(source: str) -> None:
    request = TaskCreateRequest.model_validate(
        _request(source=source, external_id=f"{source}.some/key#1")
    )
    assert request.id is None


def test_template_fields_are_optional_and_blank_by_default() -> None:
    draft = TaskDraft(title="t")
    assert (draft.done_when, draft.constraints, draft.effort) == (None, None, None)


def test_a_source_that_knows_the_template_fields_can_set_them() -> None:
    draft = TaskDraft(
        title="t", done_when="CI passes on dev", constraints="None", effort="S"
    )
    assert (draft.done_when, draft.constraints, draft.effort) == (
        "CI passes on dev",
        "None",
        "S",
    )


@pytest.mark.parametrize("effort", ["XS", "s", "XL", ""])
def test_effort_is_one_of_the_board_sizes(effort: str) -> None:
    with pytest.raises(ValidationError):
        TaskDraft(title="t", effort=effort)
