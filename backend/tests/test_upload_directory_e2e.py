"""End-to-end test for an upload directory configured outside APP_ROOT.

test_config.py covers resolve_files_directory on its own and test_server.py
mocks persist_file, so neither proves that an operator-configured directory is
the one the real upload route writes to and the real download route serves
from. This test drives both routes with a temporary absolute directory and
asserts the legacy ``.files`` directory stays untouched.
"""

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from chainlit import config as chainlit_config
from chainlit.auth import get_current_user
from chainlit.config import (
    ChainlitConfig,
    SpontaneousFileUploadFeature,
    resolve_files_directory,
)
from chainlit.server import app
from chainlit.session import WebsocketSession, ws_sessions_id, ws_sessions_sid
from chainlit.user import User

SESSION_ID = "upload-dir-e2e-session"
USER_IDENTIFIER = "upload-dir-e2e-user"
FILE_CONTENT = b"configured upload directory end to end\n"


@pytest.fixture
def authenticated_client() -> Iterator[TestClient]:
    """TestClient whose requests authenticate as the session's user."""
    client = TestClient(app)
    user = User(identifier=USER_IDENTIFIER)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield client
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def configured_upload_dir(
    tmp_path: Path,
    test_config: ChainlitConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Configure an absolute upload directory outside APP_ROOT.

    APP_ROOT is redirected to an empty directory so the absence of the legacy
    ``.files`` directory can be asserted without depending on whatever the
    repository checkout already contains.
    """
    fake_app_root = tmp_path / "app-root"
    fake_app_root.mkdir()
    external = (tmp_path / "external-uploads").resolve()

    feature = SpontaneousFileUploadFeature(
        enabled=True,
        accept=["*/*"],
        max_files=20,
        max_size_mb=1,
        directory=str(external),
    )
    monkeypatch.setattr(test_config.features, "spontaneous_file_upload", feature)
    monkeypatch.setattr(chainlit_config, "APP_ROOT", str(fake_app_root))
    monkeypatch.setattr(
        chainlit_config,
        "FILES_DIRECTORY",
        resolve_files_directory(feature),
    )

    return external


@pytest.fixture
def live_session(test_config: ChainlitConfig) -> Iterator[WebsocketSession]:
    """A real WebsocketSession, found by the real get_by_id lookup."""
    session = WebsocketSession(
        id=SESSION_ID,
        socket_id="upload-dir-e2e-socket",
        emit=lambda *args: None,
        emit_call=lambda *args: None,
        user_env={},
        client_type="webapp",
        thread_id="upload-dir-e2e-thread",
        user=User(identifier=USER_IDENTIFIER),
        token=None,
    )
    try:
        yield session
    finally:
        ws_sessions_id.pop(SESSION_ID, None)
        ws_sessions_sid.pop("upload-dir-e2e-socket", None)


def test_configured_directory_upload_roundtrip(
    authenticated_client: TestClient,
    configured_upload_dir: Path,
    live_session: WebsocketSession,
    tmp_path: Path,
):
    legacy_directory = tmp_path / "app-root" / ".files"
    assert chainlit_config.FILES_DIRECTORY == configured_upload_dir

    response = authenticated_client.post(
        "/project/file",
        files={"file": ("e2e.txt", FILE_CONTENT, "text/plain")},
        params={"session_id": SESSION_ID},
    )

    assert response.status_code == 200, response.text
    file_id = response.json()["id"]

    # The real persist_file stored the file under the configured directory,
    # inside the per-session subdirectory.
    session_directory = configured_upload_dir / SESSION_ID
    stored_files = [path for path in configured_upload_dir.rglob("*") if path.is_file()]
    assert len(stored_files) == 1
    stored = stored_files[0]
    assert stored.parent == session_directory
    assert stored.name == f"{file_id}.txt"
    assert stored.read_bytes() == FILE_CONTENT
    assert live_session.files[file_id]["path"] == stored

    # Nothing leaked into the legacy default directory.
    assert not legacy_directory.exists()

    served = authenticated_client.get(
        f"/project/file/{file_id}", params={"session_id": SESSION_ID}
    )

    assert served.status_code == 200
    assert served.content == FILE_CONTENT
    assert served.headers["content-type"].startswith("text/plain")
    assert served.headers["content-disposition"].startswith("attachment")
    assert "e2e.txt" in served.headers["content-disposition"]
    assert served.headers["x-content-type-options"] == "nosniff"
