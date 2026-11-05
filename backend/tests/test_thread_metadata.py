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


def test_public_thread_handles_missing_or_invalid_metadata():
    assert public_thread(None) is None
    assert public_thread({"id": "thread-1"})["metadata"] == {}
    assert public_thread({"id": "thread-1", "metadata": "not-json"})["metadata"] == {}
    assert (
        public_thread({"id": "thread-1", "metadata": ["not", "a", "dict"]})["metadata"]
        == {}
    )
