"""Regression tests for iteration-2 upload storage path containment."""

from pathlib import Path

import pytest

from chainlit import config as config_module
from chainlit.session import WebsocketSession


def _session(session_id: str) -> WebsocketSession:
    session = WebsocketSession.__new__(WebsocketSession)
    session.id = session_id
    session.files = {}
    session.thread_id = "thread-1"
    return session


def _files_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "files"
    root.mkdir()
    monkeypatch.setattr(config_module, "FILES_DIRECTORY", root)
    return root


async def test_persist_file_creates_missing_session_directory_and_writes_inside_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _files_root(tmp_path, monkeypatch)
    session = _session("normal-session")

    ref = await session.persist_file(
        name="../../escape.txt",
        mime="text/plain",
        content=b"safe upload",
    )

    stored = session.files[ref["id"]]["path"]
    assert stored.parent == root / session.id
    assert stored.name == f"{ref['id']}.txt"
    assert root.resolve() in stored.resolve().parents
    assert stored.read_bytes() == b"safe upload"
    assert not (tmp_path / "escape.txt").exists()


async def test_persist_file_rejects_preexisting_symlinked_session_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _files_root(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    session = _session("linked-session")
    (root / session.id).symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="Invalid session directory"):
        await session.persist_file(
            name="escape.txt",
            mime="text/plain",
            content=b"must not escape",
        )

    assert list(outside.iterdir()) == []
    assert session.files == {}


async def test_persist_file_rejects_resolved_path_outside_files_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _files_root(tmp_path, monkeypatch)
    session = _session("escaped-session")
    outside = tmp_path / "outside"
    outside.mkdir()
    escaped_root = tmp_path / "escaped"
    escaped_root.mkdir()
    escaped_session_dir = outside / ".." / escaped_root.name / session.id
    monkeypatch.setattr(
        WebsocketSession,
        "files_dir",
        property(lambda _self: escaped_session_dir),
    )

    with pytest.raises(ValueError, match="Invalid session directory"):
        await session.persist_file(
            name="escape.txt",
            mime="text/plain",
            content=b"must not escape",
        )

    assert list(escaped_root.iterdir()) == []
    assert list(outside.iterdir()) == []
    assert session.files == {}
