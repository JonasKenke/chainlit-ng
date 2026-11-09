import json
from unittest.mock import patch

import pytest

from chainlit.thread_metadata import public_thread


def test_public_thread_filters_metadata():
    thread = {
        "id": "thread-1",
        "metadata": {
            "is_shared": True,
            "viewer_read_only": True,
            "chat_profile": "profile",
            "chat_settings": {"model": "gpt"},
            "env": {"SECRET": "value"},
            "custom": "hidden",
        },
    }

    result = public_thread(thread)

    assert result["id"] == "thread-1"
    assert result["metadata"] == {
        "is_shared": True,
        "viewer_read_only": True,
    }
    assert thread["metadata"]["custom"] == "hidden"


def test_public_thread_includes_settings_for_owner():
    thread = {
        "id": "thread-1",
        "metadata": {
            "is_shared": False,
            "chat_profile": "profile",
            "chat_settings": {"model": "gpt"},
            "custom": "hidden",
        },
    }

    result = public_thread(thread, include_settings=True)

    assert result["metadata"] == {
        "is_shared": False,
        "chat_profile": "profile",
        "chat_settings": {"model": "gpt"},
    }


def test_public_thread_parses_json_metadata():
    thread = {"id": "thread-1", "metadata": '{"is_shared": true, "custom": "hidden"}'}

    result = public_thread(thread)

    assert result["metadata"] == {"is_shared": True}


def test_public_thread_parses_json_step_metadata():
    thread = {
        "id": "thread-1",
        "steps": [{"id": "step-1", "metadata": '{"elementsPosition": "above"}'}],
    }

    assert public_thread(thread)["steps"][0]["metadata"] == {
        "elementsPosition": "above"
    }
    assert public_thread(thread, include_settings=True)["steps"][0]["metadata"] == {
        "elementsPosition": "above"
    }


def test_public_thread_handles_missing_or_invalid_metadata():
    assert public_thread(None) is None
    assert public_thread({"id": "thread-1"})["metadata"] == {}
    assert public_thread({"id": "thread-1", "metadata": "not-json"})["metadata"] == {}
    assert (
        public_thread({"id": "thread-1", "metadata": ["not", "a", "dict"]})["metadata"]
        == {}
    )


def _approval_thread():
    return {
        "id": "thread-1",
        "metadata": {"is_shared": True},
        "steps": [
            {
                "id": "step-1",
                "name": "tool",
                "input": json.dumps(
                    {
                        "approval": {
                            "tool": "dangerous_tool",
                            "argumentsPreview": {
                                "amount": 2,
                                "recipient": "bob@example.com",
                            },
                            "edited": True,
                            "redacted": True,
                        }
                    }
                ),
                "metadata": {
                    "approval": {
                        "version": 1,
                        "revision": 2,
                        "id": "approval-1",
                        "stepId": "step-1",
                        "threadId": "thread-1",
                        "tool": "dangerous_tool",
                        "title": "Send money",
                        "status": "edited",
                        "requestedBy": "user-1",
                        "decidedBy": "user-1",
                        "decidedAt": "2026-01-01T00:00:00Z",
                        "decision": "edit",
                        "editAllowed": True,
                        "digestSalt": "salt",
                        "argumentsDigest": "hmac-sha256:original",
                        "argumentsPreview": {"apiKey": "[redacted]", "amount": 1},
                        "editedArgumentsPreview": {
                            "apiKey": "[redacted]",
                            "amount": 2,
                        },
                        "editedArgumentsDigest": "hmac-sha256:edited",
                        "terminalReason": None,
                    }
                },
            }
        ],
    }


def test_shared_thread_exposes_only_the_approval_whitelist():
    thread = _approval_thread()

    result = public_thread(thread)

    approval = result["steps"][0]["metadata"]["approval"]
    assert approval == {
        "tool": "dangerous_tool",
        "status": "edited",
        "decision": "edit",
        "decidedAt": "2026-01-01T00:00:00Z",
        "terminalReason": None,
    }
    # The M11 input trace bypasses the metadata whitelist; it must not be
    # served to share-link viewers either.
    assert "input" not in result["steps"][0]
    assert "bob@example.com" not in json.dumps(result)
    for hidden in (
        "argumentsPreview",
        "editedArgumentsPreview",
        "argumentsDigest",
        "editedArgumentsDigest",
        "digestSalt",
        "requestedBy",
        "decidedBy",
    ):
        assert hidden not in approval
        assert hidden not in json.dumps(result)

    # The stored thread is not mutated.
    assert thread["steps"][0]["metadata"]["approval"]["requestedBy"] == "user-1"
    assert "bob@example.com" in thread["steps"][0]["input"]


def test_owner_view_keeps_the_full_approval_record():
    thread = _approval_thread()

    result = public_thread(thread, include_settings=True)

    approval = result["steps"][0]["metadata"]["approval"]
    assert approval["requestedBy"] == "user-1"
    assert approval["editedArgumentsPreview"] == {"apiKey": "[redacted]", "amount": 2}
    assert result["steps"][0]["input"] == thread["steps"][0]["input"]


def _question_record(**overrides):
    record = {
        "version": 1,
        "revision": 2,
        "kind": "question",
        "id": "question-1",
        "stepId": "step-1",
        "threadId": "thread-1",
        "status": "answered",
        "prompt": "Which environment?",
        "options": [
            {"id": "staging", "label": "Staging", "description": None},
        ],
        "allowFreeText": True,
        "requestedBy": "user-1",
        "requestedAt": "2026-01-01T00:00:00Z",
        "expiresAt": "2026-01-01T00:05:00Z",
        "answer": {"text": "secret-value"},
        "decidedBy": "user-1",
        "decidedAt": "2026-01-01T00:00:05Z",
        "decision": None,
        "decisionRequestId": "request-1",
        "terminalReason": None,
        "terminalAt": "2026-01-01T00:00:05Z",
    }
    record.update(overrides)
    return record


def _question_thread(record=None, metadata=None):
    step = {
        "id": "step-1",
        "name": "question-step",
        "type": "tool",
        "input": json.dumps({"approval": {"answer": {"text": "secret-value"}}}),
        "metadata": metadata
        if metadata is not None
        else {"keep": "step-meta", "approval": record or _question_record()},
    }
    return {"id": "thread-1", "metadata": {"is_shared": True}, "steps": [step]}


def test_shared_thread_redacts_a_free_text_question_answer():
    thread = _question_thread()

    result = public_thread(thread)

    approval = result["steps"][0]["metadata"]["approval"]
    assert set(approval) == {
        "kind",
        "id",
        "stepId",
        "threadId",
        "version",
        "status",
        "prompt",
        "options",
        "allowFreeText",
        "answer",
        "decidedAt",
        "terminalReason",
        "terminalAt",
    }
    assert approval["kind"] == "question"
    assert approval["threadId"] == "thread-1"
    assert approval["answer"] == {"text": None, "redacted": True}
    assert approval["prompt"] == "Which environment?"
    assert approval["options"] == [
        {"id": "staging", "label": "Staging", "description": None}
    ]
    assert "input" not in result["steps"][0]
    for hidden in (
        "secret-value",
        "user-1",
        "request-1",
        "expiresAt",
        "revision",
        "requestedBy",
        "decidedBy",
        "decisionRequestId",
    ):
        assert hidden not in json.dumps(result)
    # Projection only: the stored step still has the answer and the owner view
    # keeps it.
    assert thread["steps"][0]["metadata"]["approval"]["answer"] == {
        "text": "secret-value"
    }
    owner = public_thread(thread, include_settings=True)
    assert owner["steps"][0]["metadata"]["approval"]["answer"] == {
        "text": "secret-value"
    }
    assert owner["steps"][0]["metadata"]["approval"]["requestedBy"] == "user-1"


def test_shared_thread_keeps_an_option_question_answer():
    thread = _question_thread(
        _question_record(answer={"optionId": "staging", "label": "Staging"})
    )

    approval = public_thread(thread)["steps"][0]["metadata"]["approval"]

    assert approval["answer"] == {"optionId": "staging", "label": "Staging"}


@pytest.mark.parametrize(
    "answer",
    [
        {"optionId": "staging", "label": "Staging", "text": "secret-value"},
        {"text": "secret-value"},
        "secret-value",
        ["secret-value"],
        {},
    ],
)
def test_shared_thread_redacts_unexpected_answer_shapes(answer):
    thread = _question_thread(_question_record(answer=answer))

    result = public_thread(thread)

    approval = result["steps"][0]["metadata"]["approval"]
    assert approval["answer"] == {"text": None, "redacted": True}
    assert "secret-value" not in json.dumps(result)


@pytest.mark.parametrize("live", [True, False])
def test_pending_question_is_orphaned_only_without_a_live_registry_entry(live):
    thread = _question_thread(
        _question_record(
            status="pending",
            answer=None,
            decidedBy=None,
            decidedAt=None,
            terminalAt=None,
        )
    )

    with patch("chainlit.thread_metadata.has_live_interaction", return_value=live):
        approval = public_thread(thread)["steps"][0]["metadata"]["approval"]

    assert approval["status"] == ("pending" if live else "orphaned")
    assert thread["steps"][0]["metadata"]["approval"]["status"] == "pending"


def test_owner_view_projects_an_orphaned_pending_question_without_filtering():
    thread = _question_thread(
        _question_record(
            status="pending",
            answer=None,
            decidedBy=None,
            decidedAt=None,
            terminalAt=None,
        )
    )

    with patch("chainlit.thread_metadata.has_live_interaction", return_value=False):
        result = public_thread(thread, include_settings=True)

    approval = result["steps"][0]["metadata"]["approval"]
    assert approval["status"] == "orphaned"
    assert approval["requestedBy"] == "user-1"
    assert approval["expiresAt"] == "2026-01-01T00:05:00Z"
    assert thread["steps"][0]["metadata"]["approval"]["status"] == "pending"


@pytest.mark.parametrize(
    ("metadata", "expected_text"),
    [
        ({"approval": _question_record()}, "secret-value"),
        (json.dumps({"approval": _question_record()}), "secret-value"),
    ],
)
def test_share_json_and_dict_metadata_are_redacted(metadata, expected_text):
    thread = _question_thread(metadata=metadata)

    result = public_thread(thread)

    approval = result["steps"][0]["metadata"]["approval"]
    assert approval["answer"] == {"text": None, "redacted": True}
    assert expected_text not in json.dumps(result)


@pytest.mark.parametrize(
    "metadata",
    [
        "not-json",
        {"approval": "secret-value"},
        {"approval": ["secret-value"]},
        {"approval": {"kind": "question", "id": "q", "answer": "secret-value"}},
        {"approval": {**_question_record(), "kind": "unexpected"}},
    ],
)
def test_share_fails_closed_on_unexpected_question_metadata(metadata):
    thread = _question_thread(metadata=metadata)

    result = public_thread(thread)

    step = result["steps"][0]
    assert "approval" not in step["metadata"]
    assert "secret-value" not in json.dumps(result)


@pytest.mark.parametrize(
    "metadata",
    [
        {"approval": {"tool": "dangerous", "requestedBy": "secret-value"}},
        json.dumps({"approval": {"tool": "dangerous", "requestedBy": "secret-value"}}),
        {"approval": "secret-value"},
        {"approval": ["secret-value"]},
        {"approval": {"kind": "unexpected", "requestedBy": "secret-value"}},
    ],
)
def test_share_fails_closed_for_approval_shapes(metadata):
    thread = _question_thread(metadata=metadata)

    result = public_thread(thread)

    step = result["steps"][0]
    assert (
        "approval" not in step["metadata"]
        or "requestedBy" not in step["metadata"]["approval"]
    )
    assert "secret-value" not in json.dumps(result)


def test_share_fails_closed_on_non_list_steps_and_non_dict_steps():
    assert (
        public_thread(
            {"id": "thread-1", "metadata": {"is_shared": True}, "steps": "secret"}
        )["steps"]
        == []
    )
    result = public_thread(
        {
            "id": "thread-1",
            "metadata": {"is_shared": True},
            "steps": ["secret", {"id": "step-1", "metadata": "secret"}],
        }
    )
    assert len(result["steps"]) == 1
    assert result["steps"][0]["metadata"] == {}
    assert "secret" not in json.dumps(result)
