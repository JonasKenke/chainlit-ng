import json

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
