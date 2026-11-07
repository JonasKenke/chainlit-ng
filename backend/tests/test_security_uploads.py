"""Upload and file-serving boundary tests.

The route-level happy paths and size/MIME checks already live in
test_server.py. These tests cover what those mocked-route tests cannot:
the real path construction in WebsocketSession.persist_file, session-id
sanitisation, size boundaries, and response-header injection.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, patch

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from chainlit.auth import get_current_user
from chainlit.server import app, validate_file_size
from chainlit.session import WebsocketSession, is_valid_session_id
from chainlit.types import AskFileSpec


def _session(session_id: str = "safe-session") -> WebsocketSession:
    session = WebsocketSession.__new__(WebsocketSession)
    session.id = session_id
    session.files = {}
    session.thread_id = "thread-1"
    return session


class TestSessionIdPathComponent:
    @pytest.mark.parametrize(
        "session_id",
        ["", ".", "..", "../x", "a/b", "a\\b", "a b", "x" * 129],
    )
    def test_rejects_values_that_are_not_one_path_component(self, session_id):
        assert is_valid_session_id(session_id) is False

    @pytest.mark.parametrize(
        "session_id",
        ["abc", "a-b_c", "A1_b-c", "0" * 64],
    )
    def test_accepts_safe_path_components(self, session_id):
        assert is_valid_session_id(session_id) is True

    def test_files_dir_refuses_invalid_id(self):
        session = _session("../../etc")

        with pytest.raises(ValueError, match="Invalid session id"):
            _ = session.files_dir


class TestPersistFilePaths:
    async def test_traversal_filename_is_not_used_as_the_stored_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        files_root = tmp_path / "files"
        files_root.mkdir()
        monkeypatch.setattr("chainlit.config.FILES_DIRECTORY", files_root)
        session = _session("session-1")

        ref = await session.persist_file(
            name="../../escape.html",
            mime="text/html",
            content=b"<script>alert(1)</script>",
        )

        stored = session.files[ref["id"]]["path"]
        assert stored.parent == files_root / "session-1"
        assert stored.name == f"{ref['id']}.html"
        assert not (tmp_path / "escape.html").exists()
        assert not (tmp_path.parent / "escape.html").exists()

    @pytest.mark.parametrize(
        "name",
        ["/etc/passwd", "..\\..\\windows\\win.ini", "....//....//x", "a\x00b"],
    )
    async def test_hostile_name_only_labels_the_file(
        self, name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        files_root = tmp_path / "files"
        files_root.mkdir()
        monkeypatch.setattr("chainlit.config.FILES_DIRECTORY", files_root)
        session = _session("session-2")

        ref = await session.persist_file(name=name, mime="text/plain", content=b"body")

        stored = session.files[ref["id"]]["path"]
        assert stored.parent == files_root / "session-2"
        assert stored.name.startswith(ref["id"])
        assert session.files[ref["id"]]["name"] == name

    async def test_mime_parameters_do_not_add_a_path_suffix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        files_root = tmp_path / "files"
        files_root.mkdir()
        monkeypatch.setattr("chainlit.config.FILES_DIRECTORY", files_root)
        session = _session("session-3")

        ref = await session.persist_file(
            name="x", mime="text/plain; charset=utf-8", content=b"x"
        )

        stored = session.files[ref["id"]]["path"]
        assert stored.parent == files_root / "session-3"


class TestSizeLimitBoundary:
    @pytest.mark.parametrize(
        ("size", "allowed"),
        [
            (0, True),
            (1024 * 1024, True),
            (1024 * 1024 + 1, False),
        ],
    )
    def test_limit_is_inclusive(self, size: int, allowed: bool):
        spec = AskFileSpec(
            accept=["*/*"],
            max_files=1,
            max_size_mb=1,
            timeout=1,
            type="file",
            step_id="step-1",
        )
        upload = cast(UploadFile, SimpleNamespace(size=size))

        if allowed:
            validate_file_size(upload, spec)
        else:
            with pytest.raises(ValueError, match="File size too large"):
                validate_file_size(upload, spec)


@pytest.fixture
def client():
    test_client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: None
    try:
        yield test_client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


class TestFileResponse:
    @pytest.mark.parametrize("file_id", ["%2e%2e", "..%5C..%5Cetc%5Cpasswd", "..%00"])
    def test_path_traversal_file_id_is_not_resolved(
        self, client: TestClient, file_id: str
    ):
        session = Mock(spec=WebsocketSession)
        session.id = "session-1"
        session.user = None
        session.files = {"ok": {"path": "/dev/null", "type": "text/plain"}}

        with patch("chainlit.session.WebsocketSession.get_by_id", return_value=session):
            response = client.get(f"/project/file/{file_id}?session_id=session-1")

        assert response.status_code == 404

    def test_filename_is_not_reflected_into_response_headers(
        self, client: TestClient, tmp_path: Path
    ):
        payload = b"<html><script>alert(1)</script></html>"
        stored = tmp_path / "stored.html"
        stored.write_bytes(payload)
        session = Mock(spec=WebsocketSession)
        session.id = "session-1"
        session.user = None
        session.files = {
            "file-1": {
                "id": "file-1",
                "path": stored,
                "name": "x\r\nX-Injected: yes",
                "type": "text/html",
                "size": len(payload),
            }
        }

        with patch("chainlit.session.WebsocketSession.get_by_id", return_value=session):
            response = client.get("/project/file/file-1?session_id=session-1")

        assert response.status_code == 200
        assert "x-injected" not in {key.lower() for key in response.headers}
        assert response.headers["content-type"].startswith("text/html")
