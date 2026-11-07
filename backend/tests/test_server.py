import datetime as dt
import os
import pathlib
import time
import urllib.parse
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest.mock import AsyncMock, Mock, create_autospec, mock_open

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

from chainlit.auth import get_current_user
from chainlit.config import (
    APP_ROOT,
    ChainlitConfig,
    SpontaneousFileUploadFeature,
)
from chainlit.data.base import BaseDataLayer
from chainlit.server import (
    _verify_azure_hf_id_token,
    app,
    get_html_template,
    prefix_asset_url,
)
from chainlit.types import AskFileSpec
from chainlit.user import PersistedUser, User


@pytest.fixture
def test_client():
    return TestClient(app)


@pytest.fixture
def mock_load_translation(test_config: ChainlitConfig, monkeypatch: pytest.MonkeyPatch):
    mock_method = Mock(return_value={"key": "value"})
    monkeypatch.setattr("chainlit.config.ChainlitConfig.load_translation", mock_method)

    return mock_method


def test_project_translations_default_language(
    test_client: TestClient, mock_load_translation: Mock
):
    """Test with default language."""
    response = test_client.get("/project/translations")
    assert response.status_code == 200
    assert "translation" in response.json()
    mock_load_translation.assert_called_once_with("en-US")
    mock_load_translation.reset_mock()


def test_project_translations_specific_language(
    test_client: TestClient, mock_load_translation: Mock
):
    """Test with a specific language."""

    response = test_client.get("/project/translations?language=fr-FR")
    assert response.status_code == 200
    assert "translation" in response.json()
    mock_load_translation.assert_called_once_with("fr-FR")
    mock_load_translation.reset_mock()


def test_project_translations_invalid_language(
    test_client: TestClient, mock_load_translation: Mock
):
    """Test with an invalid language."""

    response = test_client.get("/project/translations?language=invalid")
    assert response.status_code == 422

    assert (
        "translation" not in response.json()
    )  # It should fall back to default translation
    assert not mock_load_translation.called


def test_project_translations_bcp47_language(
    test_client: TestClient, mock_load_translation: Mock
):
    """Regression test for https://github.com/Chainlit/chainlit/issues/1352."""

    response = test_client.get("/project/translations?language=es-419")
    assert response.status_code == 200
    assert "translation" in response.json()
    mock_load_translation.assert_called_once_with("es-419")
    mock_load_translation.reset_mock()


@pytest.fixture
def mock_get_current_user():
    """Override get_current_user() dependency."""

    # Programming sucks!
    # Ref: https://github.com/fastapi/fastapi/issues/3331#issuecomment-1182452859
    app.dependency_overrides[get_current_user] = create_autospec(lambda: None)

    yield app.dependency_overrides[get_current_user]

    del app.dependency_overrides[get_current_user]


async def test_project_settings(test_client: TestClient, mock_get_current_user: Mock):
    """Burn test for project settings."""
    response = test_client.get(
        "/project/settings",
    )

    mock_get_current_user.assert_called_once()

    assert response.status_code == 200, response.json()
    data = response.json()

    assert "ui" in data
    assert "features" in data
    assert "userEnv" in data
    assert "dataPersistence" in data
    assert "threadResumable" in data
    assert "markdown" in data
    assert "debugUrl" not in data
    assert data["chatProfiles"] == []
    assert data["starters"] == []


def test_project_settings_path_traversal(
    test_client: TestClient,
    mock_get_current_user: Mock,
    tmp_path: Path,
    test_config: ChainlitConfig,
):
    """Test to prevent path traversal in project settings."""

    # Create a mock chainlit directory structure
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (tmp_path / "README.md").write_text("This is a secret README")

    # This is required for the exploit to occur.
    chainlit_dir = app_dir / "chainlit_stuff"
    chainlit_dir.mkdir()

    # Mock the config root
    test_config.root = str(app_dir)

    # Attempt to access the file using path traversal
    response = test_client.get(
        "/project/settings", params={"language": "stuff/../../README"}
    )

    # Should not be able to read the file
    assert "This is a secret README" not in response.text

    assert response.status_code == 422

    # The response should not contain the normally expected keys
    data = response.json()
    assert "ui" not in data
    assert "features" not in data
    assert "userEnv" not in data
    assert "dataPersistence" not in data
    assert "threadResumable" not in data
    assert "markdown" not in data
    assert "chatProfiles" not in data
    assert "starters" not in data
    assert "debugUrl" not in data


def test_get_avatar_default(test_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Test with default avatar."""
    response = test_client.get("/avatars/default")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


def test_get_avatar_custom(test_client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Test with custom avatar."""
    custom_avatar_path = os.path.join(
        APP_ROOT, "public", "avatars", "custom_avatar.png"
    )
    os.makedirs(os.path.dirname(custom_avatar_path), exist_ok=True)
    with open(custom_avatar_path, "wb") as f:
        f.write(b"fake image data")

    response = test_client.get("/avatars/custom_avatar")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content == b"fake image data"

    # Clean up
    os.remove(custom_avatar_path)


def test_get_avatar_with_spaces(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Test with custom avatar."""
    custom_avatar_path = os.path.join(APP_ROOT, "public", "avatars", "my_assistant.png")
    os.makedirs(os.path.dirname(custom_avatar_path), exist_ok=True)
    with open(custom_avatar_path, "wb") as f:
        f.write(b"fake image data")

    response = test_client.get("/avatars/My Assistant")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content == b"fake image data"

    # Clean up
    os.remove(custom_avatar_path)


def test_get_avatar_non_existent_favicon(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Test with non-existent avatar (should return favicon)."""
    favicon_response = test_client.get("/favicon")
    assert favicon_response.status_code == 200

    response = test_client.get("/avatars/non_existent")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
    assert response.content == favicon_response.content


def test_avatar_path_traversal(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
):
    """Test to prevent potential path traversal in avatar route on Windows."""

    # Create a Mock object for the glob function
    mock_glob = Mock(return_value=[])
    monkeypatch.setattr("chainlit.server.glob.glob", mock_glob)

    mock_open_inst = mock_open(read_data=b'{"should_not": "Be readable."}')
    monkeypatch.setattr("builtins.open", mock_open_inst)

    # Attempt to access a file using path traversal
    response = test_client.get("/avatars/..%5C..%5Capp")

    # No glob should ever be called
    assert not mock_glob.called

    # Should return an error status
    assert response.status_code == 400


@pytest.fixture
def mock_session_get_by_id_patched(mock_session: Mock, monkeypatch: pytest.MonkeyPatch):
    test_session_id = "test_session_id"

    # Mock the WebsocketSession.get_by_id method to return the mock session
    monkeypatch.setattr(
        "chainlit.session.WebsocketSession.get_by_id",
        lambda session_id: mock_session if session_id == test_session_id else None,
    )

    return mock_session


def test_get_file_success(
    test_client: TestClient,
    mock_session_get_by_id_patched: Mock,
    tmp_path: pathlib.Path,
    mock_get_current_user: Mock,
):
    """
    Test successful retrieval of a file from a session.
    """
    # Set current_user to match session.user
    mock_get_current_user.return_value = mock_session_get_by_id_patched.user

    # Create test data
    test_content = b"Test file content"
    test_file_id = "test_file_id"

    # Create a temporary file with the test content
    test_file = tmp_path / "test_file"
    test_file.write_bytes(test_content)

    mock_session_get_by_id_patched.files = {
        test_file_id: {
            "id": test_file_id,
            "path": test_file,
            "name": "test.txt",
            "type": "text/plain",
            "size": len(test_content),
        }
    }

    # Make the GET request to retrieve the file
    response = test_client.get(
        f"/project/file/{test_file_id}?session_id={mock_session_get_by_id_patched.id}"
    )

    # Verify the response
    assert response.status_code == 200
    assert response.content == test_content
    assert response.headers["content-type"].startswith("text/plain")


def test_get_file_not_existent_file(
    test_client: TestClient,
    mock_session_get_by_id_patched: Mock,
    mock_get_current_user: Mock,
):
    """
    Test retrieval of a non-existing file from a session.
    """
    # Set current_user to match session.user
    mock_get_current_user.return_value = mock_session_get_by_id_patched.user

    # Make the GET request to retrieve the file
    response = test_client.get("/project/file/test_file_id?session_id=test_session_id")

    # Verify the response
    assert response.status_code == 404


def test_get_file_non_existing_session(
    test_client: TestClient,
    tmp_path: pathlib.Path,
    mock_session_get_by_id_patched: Mock,
    mock_session: Mock,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Test that an unauthenticated user cannot retrieve a file uploaded by an authenticated user.
    """

    # Attempt to access the file without authentication by providing an invalid session_id
    response = test_client.get(
        "/project/file/nonexistent?session_id=unauthenticated_session_id"
    )

    # Verify the response
    assert response.status_code == 401  # Unauthorized


def test_upload_file_success(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_session_get_by_id_patched: Mock,
):
    """Test successful file upload."""

    # Prepare the files to upload
    file_content = b"Sample file content"
    files = {
        "file": ("test_upload.txt", file_content, "text/plain"),
    }

    # Mock the persist_file method to return a known value
    expected_file_id = "mocked_file_id"
    mock_session_get_by_id_patched.persist_file = AsyncMock(
        return_value={
            "id": expected_file_id,
            "name": "test_upload.txt",
            "type": "text/plain",
            "size": len(file_content),
        }
    )

    # Make the POST request to upload the file
    response = test_client.post(
        "/project/file",
        files=files,
        params={"session_id": mock_session_get_by_id_patched.id},
    )

    # Verify the response
    assert response.status_code == 200
    response_data = response.json()
    assert "id" in response_data
    assert response_data["id"] == expected_file_id
    assert response_data["name"] == "test_upload.txt"
    assert response_data["type"] == "text/plain"
    assert response_data["size"] == len(file_content)

    # Verify that persist_file was called with the correct arguments
    mock_session_get_by_id_patched.persist_file.assert_called_once_with(
        name="test_upload.txt", content=file_content, mime="text/plain"
    )


def test_file_access_by_different_user(
    test_client: TestClient,
    mock_session_get_by_id_patched: Mock,
    persisted_test_user: PersistedUser,
    tmp_path: pathlib.Path,
    mock_session_factory: Callable[..., Mock],
):
    """Test that a file uploaded by one user cannot be accessed by another user."""

    # Prepare the files to upload
    file_content = b"Sample file content"
    files = {
        "file": ("test_upload.txt", file_content, "text/plain"),
    }

    # Mock the persist_file method to return a known value
    expected_file_id = "mocked_file_id"
    mock_session_get_by_id_patched.persist_file = AsyncMock(
        return_value={
            "id": expected_file_id,
            "name": "test_upload.txt",
            "type": "text/plain",
            "size": len(file_content),
        }
    )

    # Make the POST request to upload the file
    response = test_client.post(
        "/project/file",
        files=files,
        params={"session_id": mock_session_get_by_id_patched.id},
    )

    # Verify the response
    assert response.status_code == 200

    response_data = response.json()
    assert "id" in response_data
    file_id = response_data["id"]

    # Create a second session with a different user
    second_session = mock_session_factory(
        id="another_session_id",
        user=PersistedUser(
            id="another_user_id",
            createdAt=dt.datetime.now().isoformat(),
            identifier="another_user_identifier",
        ),
    )

    # Attempt to access the uploaded file using the second user's session
    response = test_client.get(
        f"/project/file/{file_id}?session_id={second_session.id}"
    )

    # Verify that the access attempt fails
    assert response.status_code == 401  # Unauthorized


def test_upload_file_missing_file(
    test_client: TestClient,
    mock_session: Mock,
):
    """Test file upload with missing file in the request."""

    # Make the POST request without a file
    response = test_client.post(
        "/project/file",
        data={"session_id": mock_session.id},
    )

    # Verify the response
    assert response.status_code == 422  # Unprocessable Entity
    assert "detail" in response.json()


def test_upload_file_invalid_session(
    test_client: TestClient,
):
    """Test file upload with an invalid session."""

    # Prepare the files to upload
    file_content = b"Sample file content"
    files = {
        "file": ("test_upload.txt", file_content, "text/plain"),
    }

    # Make the POST request with an invalid session_id
    response = test_client.post(
        "/project/file",
        files=files,
        data={"session_id": "invalid_session_id"},
    )

    # Verify the response
    assert response.status_code == 422


def test_upload_file_unauthorized(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_session_get_by_id_patched: Mock,
):
    """Test file upload without proper authorization."""

    # Mock the upload_file_session to have no user
    mock_session_get_by_id_patched.user = None

    # Prepare the files to upload
    file_content = b"Sample file content"
    files = {
        "file": ("test_upload.txt", file_content, "text/plain"),
    }

    # Make the POST request to upload the file
    response = test_client.post(
        "/project/file",
        files=files,
        data={"session_id": mock_session_get_by_id_patched.id},
    )

    assert response.status_code == 422


def test_upload_file_disabled(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_session_get_by_id_patched: Mock,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test file upload being disabled by config."""

    # Set accept in config
    monkeypatch.setattr(
        test_config.features,
        "spontaneous_file_upload",
        SpontaneousFileUploadFeature(enabled=False),
    )

    # Prepare the files to upload
    file_content = b"Sample file content"
    files = {
        "file": ("test_upload.txt", file_content, "text/plain"),
    }

    # Make the POST request to upload the file
    response = test_client.post(
        "/project/file",
        files=files,
        params={"session_id": mock_session_get_by_id_patched.id},
    )

    # Verify the response
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("accept_pattern", "mime_type", "expected_status"),
    [
        ({"image/*": [".png", ".gif", ".jpeg", ".jpg"]}, "image/jpeg", 400),
        (["image/*"], "text/plain", 400),
        (["image/*", "application/*"], "text/plain", 400),
        (["image/png", "application/pdf"], "image/jpeg", 400),
        (["text/*"], "text/plain", 200),
        (["application/*"], "application/pdf", 200),
        (["image/*"], "image/jpeg", 200),
        (["image/*", "text/*"], "text/plain", 200),
        (["*/*"], "text/plain", 200),
        (["*/*"], "image/jpeg", 200),
        (["*/*"], "application/pdf", 200),
        (["image/*", "application/*"], "application/pdf", 200),
        (["image/*", "application/*"], "image/jpeg", 200),
        (["image/png", "application/pdf"], "image/png", 200),
        (["image/png", "application/pdf"], "application/pdf", 200),
        ({"image/*": []}, "image/jpeg", 200),
        (
            {"image/*": [".png", ".gif", ".jpeg", ".jpg"]},
            "text/plain",
            400,
        ),  # mime type not allowed
        (
            {"*/*": [".txt", ".gif", ".jpeg", ".jpg"]},
            "text/plain",
            200,
        ),  # extension allowed
        (
            {"*/*": [".gif", ".jpeg", ".jpg"]},
            "text/plain",
            400,
        ),  # extension not allowed
    ],
)
def test_upload_file_mime_type_check(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_session_get_by_id_patched: Mock,
    monkeypatch: pytest.MonkeyPatch,
    accept_pattern: list[str],
    mime_type: str,
    expected_status: int,
):
    """Test check of mime_type."""

    # Set accept in config
    monkeypatch.setattr(
        test_config.features,
        "spontaneous_file_upload",
        SpontaneousFileUploadFeature(enabled=True, accept=accept_pattern),
    )

    # Prepare the files to upload
    file_content = b"Sample file content"
    files = {
        "file": ("test_upload.txt", file_content, mime_type),
    }

    # Mock the persist_file method to return a known value
    expected_file_id = "mocked_file_id"
    mock_session_get_by_id_patched.persist_file = AsyncMock(
        return_value={
            "id": expected_file_id,
            "name": "test_upload.txt",
            "type": "text/plain",
            "size": len(file_content),
        }
    )

    # Make the POST request to upload the file
    response = test_client.post(
        "/project/file",
        files=files,
        params={"session_id": mock_session_get_by_id_patched.id},
    )

    # Verify the response
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    ("file_content", "content_multiplier", "max_size_mb", "expected_status"),
    [
        (b"1", 1, 1, 200),
        (b"11", 1024 * 1024, 1, 400),
    ],
)
def test_upload_file_size_check(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_session_get_by_id_patched: Mock,
    monkeypatch: pytest.MonkeyPatch,
    file_content: bytes,
    content_multiplier: int,
    max_size_mb: int,
    expected_status: int,
):
    """Test check of max_size_mb."""

    file_content = file_content * content_multiplier

    # Set accept in config
    monkeypatch.setattr(
        test_config.features,
        "spontaneous_file_upload",
        SpontaneousFileUploadFeature(max_size_mb=max_size_mb, enabled=True),
    )

    # Prepare the files to upload
    files = {
        "file": ("test_upload.txt", file_content, "text/plain"),
    }

    # Mock the persist_file method to return a known value
    expected_file_id = "mocked_file_id"
    mock_session_get_by_id_patched.persist_file = AsyncMock(
        return_value={
            "id": expected_file_id,
            "name": "test_upload.txt",
            "type": "text/plain",
            "size": len(file_content),
        }
    )

    # Make the POST request to upload the file
    response = test_client.post(
        "/project/file",
        files=files,
        params={"session_id": mock_session_get_by_id_patched.id},
    )

    # Verify the response
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    (
        "file_content",
        "content_multiplier",
        "max_size_mb",
        "parent_id",
        "expected_status",
        "accept",
    ),
    [
        (b"1", 1, 1, "mocked_parent_id", 200, ["text/plain"]),
        (b"11", 1024 * 1024, 1, "mocked_parent_id", 400, ["text/plain"]),
        (b"11", 1, 1, "invalid_parent_id", 404, ["text/plain"]),
        (b"11", 1, 1, "mocked_parent_id", 400, ["image/gif"]),
    ],
)
def test_ask_file_with_spontaneous_upload_disabled(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_session_get_by_id_patched: Mock,
    monkeypatch: pytest.MonkeyPatch,
    file_content: bytes,
    content_multiplier: int,
    max_size_mb: int,
    parent_id: str,
    expected_status: int,
    accept: list[str],
):
    """Test file upload being disabled by config."""

    # Set accept in config
    monkeypatch.setattr(
        test_config.features,
        "spontaneous_file_upload",
        SpontaneousFileUploadFeature(enabled=False),
    )

    # Prepare the files to upload
    file_content = file_content * content_multiplier
    files = {
        "file": ("test_upload.txt", file_content, "text/plain"),
    }

    expected_file_id = "mocked_file_id"
    mock_session_get_by_id_patched.persist_file = AsyncMock(
        return_value={
            "id": expected_file_id,
            "name": "test_upload.txt",
            "type": "text/plain",
            "size": len(file_content),
        }
    )

    mock_session_get_by_id_patched.files_spec = {
        "mocked_parent_id": AskFileSpec(
            step_id="mocked_file_spec",
            timeout=1,
            type="file",
            accept=accept,
            max_files=1,
            max_size_mb=max_size_mb,
        )
    }

    # Make the POST request to upload the file
    response = test_client.post(
        "/project/file",
        files=files,
        params={
            "session_id": mock_session_get_by_id_patched.id,
            "ask_parent_id": parent_id,
        },
    )

    # Verify the response
    assert response.status_code == expected_status


def test_project_translations_file_path_traversal(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Test to prevent file path traversal in project translations."""

    mock_open_inst = mock_open(read_data='{"should_not": "Be readable."}')
    monkeypatch.setattr("builtins.open", mock_open_inst)

    # Attempt to access the file using path traversal
    response = test_client.get(
        "/project/translations", params={"language": "/app/unreadable"}
    )

    # File should never be opened
    assert not mock_open_inst.called

    # Should give error status
    assert response.status_code == 422


def test_project_settings_with_chat_profile_config_overrides(
    test_client: TestClient,
    test_config: ChainlitConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that /project/settings endpoint returns merged configuration when chat_profile is specified."""
    from chainlit.config import (
        ChainlitConfigOverrides,
        FeaturesSettings,
        McpFeature,
        UISettings,
    )
    from chainlit.types import ChatProfile

    # Mock chat profiles with different config overrides
    mock_profiles = [
        ChatProfile(
            name="basic",
            markdown_description="Basic profile without overrides",
            default=True,
        ),
        ChatProfile(
            name="mcp-enabled",
            markdown_description="Profile with MCP enabled",
            config_overrides=ChainlitConfigOverrides(
                features=FeaturesSettings(mcp=McpFeature(enabled=True)),
                ui=UISettings(name="MCP Assistant", default_theme="dark"),
            ),
        ),
        ChatProfile(
            name="light-theme",
            markdown_description="Profile with light theme",
            config_overrides=ChainlitConfigOverrides(
                ui=UISettings(
                    name="Light Theme App",
                    default_theme="light",
                    description="Light theme app",
                )
            ),
        ),
    ]

    # Mock the chat profiles callback
    async def mock_get_chat_profiles(user, language):
        # Use asyncio.sleep to make this truly async
        import asyncio

        await asyncio.sleep(0)
        return mock_profiles

    test_config.code.set_chat_profiles = mock_get_chat_profiles

    # Test 1: Default profile (no overrides)
    response = test_client.get("/project/settings", params={"chat_profile": "basic"})
    assert response.status_code == 200
    config_data = response.json()

    # Should return base configuration without overrides
    assert config_data["ui"]["name"] == test_config.ui.name  # Original name
    assert (
        config_data["features"]["mcp"]["enabled"] == test_config.features.mcp.enabled
    )  # Original MCP setting

    # Test 2: MCP-enabled profile
    response = test_client.get(
        "/project/settings", params={"chat_profile": "mcp-enabled"}
    )
    assert response.status_code == 200
    config_data = response.json()

    # Should return merged configuration with MCP enabled and custom UI
    assert config_data["features"]["mcp"]["enabled"] is True  # Overridden
    assert config_data["ui"]["name"] == "MCP Assistant"  # Overridden
    assert config_data["ui"]["default_theme"] == "dark"  # Overridden

    # Test 3: Light theme profile
    response = test_client.get(
        "/project/settings", params={"chat_profile": "light-theme"}
    )
    assert response.status_code == 200
    config_data = response.json()

    # Should return merged configuration with light theme
    assert config_data["ui"]["default_theme"] == "light"  # Overridden
    assert config_data["ui"]["description"] == "Light theme app"  # Overridden
    assert (
        config_data["features"]["mcp"]["enabled"] == test_config.features.mcp.enabled
    )  # Not overridden

    # Test 4: Non-existent profile (should return base config)
    response = test_client.get(
        "/project/settings", params={"chat_profile": "non-existent"}
    )
    assert response.status_code == 200
    config_data = response.json()

    # Should return base configuration
    assert config_data["ui"]["name"] == test_config.ui.name
    assert config_data["features"]["mcp"]["enabled"] == test_config.features.mcp.enabled

    # Test 5: No profile specified (should return base config)
    response = test_client.get("/project/settings")
    assert response.status_code == 200
    config_data = response.json()

    # Should return base configuration
    assert config_data["ui"]["name"] == test_config.ui.name
    assert config_data["features"]["mcp"]["enabled"] == test_config.features.mcp.enabled


def test_project_settings_redacts_mcp_server_secrets(
    test_client: TestClient,
    test_config: ChainlitConfig,
):
    from chainlit.config import (
        McpFeature,
        McpUserServersFeature,
        SseMcpServer,
        StdioMcpServer,
        StreamableHttpMcpServer,
    )

    test_config.features.mcp = McpFeature(
        enabled=True,
        user_servers=McpUserServersFeature(
            enabled=True, allowed_urls=["https://allowlisted.example.com/api"]
        ),
        servers=[
            StdioMcpServer(
                name="local",
                type="stdio",
                command="npx -y secret-tool",
                env={"GITHUB_TOKEN": "env-secret"},
            ),
            SseMcpServer(
                name="sse",
                type="sse",
                url="https://mcp.example.com/sse",
                headers={"Authorization": "Bearer header-secret"},
            ),
            StreamableHttpMcpServer(
                name="http",
                type="streamable-http",
                url="https://mcp.example.com/mcp?token=query-secret",
                headers={"X-Api-Key": "header-secret-2"},
            ),
        ],
    )

    response = test_client.get("/project/settings")

    assert response.status_code == 200
    mcp = response.json()["features"]["mcp"]
    assert mcp["servers"] == [
        {"name": "local", "type": "stdio"},
        {"name": "sse", "type": "sse"},
        {"name": "http", "type": "streamable-http"},
    ]
    assert mcp["user_servers"] == {"enabled": True}
    for secret in (
        "secret-tool",
        "env-secret",
        "header-secret",
        "query-secret",
        "allowlisted.example.com",
        "GITHUB_TOKEN",
        "Authorization",
        "X-Api-Key",
    ):
        assert secret not in response.text


def test_project_settings_redacts_chat_profile_overridden_mcp_servers(
    test_client: TestClient,
    test_config: ChainlitConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    from chainlit.config import (
        ChainlitConfigOverrides,
        FeaturesSettings,
        McpFeature,
        StdioMcpServer,
    )
    from chainlit.types import ChatProfile

    async def mock_get_chat_profiles(user, language):
        return [
            ChatProfile(
                name="vendored",
                markdown_description="Profile with its own MCP servers",
                config_overrides=ChainlitConfigOverrides(
                    features=FeaturesSettings(
                        mcp=McpFeature(
                            enabled=True,
                            servers=[
                                StdioMcpServer(
                                    name="profile-server",
                                    type="stdio",
                                    command="profile-secret-command",
                                    env={"PROFILE_TOKEN": "profile-env-secret"},
                                )
                            ],
                        )
                    )
                ),
            )
        ]

    test_config.code.set_chat_profiles = mock_get_chat_profiles

    response = test_client.get("/project/settings", params={"chat_profile": "vendored"})

    assert response.status_code == 200
    mcp = response.json()["features"]["mcp"]
    assert mcp["servers"] == [{"name": "profile-server", "type": "stdio"}]
    for secret in ("profile-secret-command", "profile-env-secret", "PROFILE_TOKEN"):
        assert secret not in response.text


def test_project_settings_config_overrides_serialization(
    test_client: TestClient,
    test_config: ChainlitConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that config_overrides field is not included in serialized chat profiles."""
    from chainlit.config import ChainlitConfigOverrides, FeaturesSettings, McpFeature
    from chainlit.types import ChatProfile

    # Mock chat profile with config overrides
    mock_profile = ChatProfile(
        name="test-profile",
        markdown_description="Test profile",
        config_overrides=ChainlitConfigOverrides(
            features=FeaturesSettings(mcp=McpFeature(enabled=True))
        ),
    )

    async def mock_get_chat_profiles(user, language):
        # Use asyncio.sleep to make this truly async
        import asyncio

        await asyncio.sleep(0)
        return [mock_profile]

    test_config.code.set_chat_profiles = mock_get_chat_profiles

    # Get the project settings
    response = test_client.get(
        "/project/settings", params={"chat_profile": "test-profile"}
    )
    assert response.status_code == 200
    config_data = response.json()

    # Check that chatProfiles are included in the response
    assert "chatProfiles" in config_data
    assert len(config_data["chatProfiles"]) == 1

    # Check that config_overrides is NOT included in the serialized profile
    profile_data = config_data["chatProfiles"][0]
    assert "config_overrides" not in profile_data
    assert profile_data["name"] == "test-profile"
    assert profile_data["markdown_description"] == "Test profile"


def test_project_settings_config_overrides_language(
    test_client: TestClient,
    test_config: ChainlitConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that localized chat profiles use the right config overrides."""
    from chainlit.config import ChainlitConfigOverrides, FeaturesSettings, McpFeature
    from chainlit.types import ChatProfile

    # Mock chat profile with config overrides
    mock_profile_fr = ChatProfile(
        name="test-profile-fr",
        markdown_description="Test profil",
        config_overrides=ChainlitConfigOverrides(
            features=FeaturesSettings(mcp=McpFeature(enabled=True))
        ),
    )

    mock_profile_en = ChatProfile(
        name="test-profile",
        markdown_description="Test profile",
        config_overrides=ChainlitConfigOverrides(
            features=FeaturesSettings(mcp=McpFeature(enabled=False))
        ),
    )

    async def mock_get_chat_profiles(user, language):
        # Use asyncio.sleep to make this truly async
        import asyncio

        await asyncio.sleep(0)
        if language == "fr-CA":
            return [mock_profile_fr]

        return [mock_profile_en]

    test_config.code.set_chat_profiles = mock_get_chat_profiles

    # Get the project settings in French
    response = test_client.get(
        "/project/settings",
        params={"language": "fr-CA", "chat_profile": "test-profile-fr"},
    )
    assert response.status_code == 200
    config_data = response.json()

    # Check that chatProfiles are included in the response
    assert "chatProfiles" in config_data
    assert len(config_data["chatProfiles"]) == 1

    # Check that config_overrides is NOT included in the serialized profile
    assert config_data["features"]["mcp"]["enabled"] is True  # Overridden

    # Check that the profile_data matches the selected profile.
    profile_data = config_data["chatProfiles"][0]
    assert "config_overrides" not in profile_data
    assert profile_data["name"] == "test-profile-fr"
    assert profile_data["markdown_description"] == "Test profil"

    # Get the project settings in English
    response = test_client.get(
        "/project/settings",
        params={"language": "en-US", "chat_profile": "test-profile"},
    )
    assert response.status_code == 200
    config_data = response.json()

    # Check that chatProfiles are included in the response
    assert "chatProfiles" in config_data
    assert len(config_data["chatProfiles"]) == 1

    # Check that config_overrides is NOT included in the serialized profile
    assert config_data["features"]["mcp"]["enabled"] is False  # Overridden

    # Check that the profile_data matches the selected profile.
    profile_data = config_data["chatProfiles"][0]
    assert "config_overrides" not in profile_data
    assert profile_data["name"] == "test-profile"
    assert profile_data["markdown_description"] == "Test profile"


def test_project_settings_thread_sharing_flag(
    test_client: TestClient,
    test_config: ChainlitConfig,
    monkeypatch: pytest.MonkeyPatch,
):
    # Start with both disabled
    test_config.features.allow_thread_sharing = False
    test_config.code.on_shared_thread_view = None
    resp = test_client.get("/project/settings")
    assert resp.status_code == 200
    assert resp.json().get("threadSharing") is False

    # Enable flag only -> still False (callback missing)
    test_config.features.allow_thread_sharing = True
    test_config.code.on_shared_thread_view = None
    resp = test_client.get("/project/settings")
    assert resp.status_code == 200
    assert resp.json().get("threadSharing") is False

    # Enable callback only -> still False (flag disabled)
    test_config.features.allow_thread_sharing = False

    def dummy_cb(*args, **kwargs):
        return True

    test_config.code.on_shared_thread_view = dummy_cb
    resp = test_client.get("/project/settings")
    assert resp.status_code == 200
    assert resp.json().get("threadSharing") is False

    # Enable both -> True
    test_config.features.allow_thread_sharing = True
    test_config.code.on_shared_thread_view = dummy_cb
    resp = test_client.get("/project/settings")
    assert resp.status_code == 200
    assert resp.json().get("threadSharing") is True


def test_project_settings_forwards_chat_profile_to_starter_categories(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_get_current_user: Mock,
):
    """Test that /project/settings forwards chat_profile to set_starter_categories."""
    from chainlit.types import Starter, StarterCategory
    from chainlit.utils import wrap_user_function

    async def _get_starter_categories(user, language, chat_profile):
        if chat_profile == "creative":
            return [
                StarterCategory(
                    label="Writing",
                    starters=[Starter(label="Write a poem", message="poem")],
                ),
            ]
        return [
            StarterCategory(
                label="General",
                starters=[Starter(label="Hello", message="hello")],
            ),
        ]

    test_config.code.set_starter_categories = wrap_user_function(
        _get_starter_categories
    )

    response = test_client.get("/project/settings", params={"chat_profile": "creative"})
    assert response.status_code == 200
    categories = response.json()["starterCategories"]
    assert len(categories) == 1
    assert categories[0]["label"] == "Writing"
    assert categories[0]["starters"][0]["label"] == "Write a poem"

    response = test_client.get("/project/settings")
    assert response.status_code == 200
    categories = response.json()["starterCategories"]
    assert len(categories) == 1
    assert categories[0]["label"] == "General"
    assert categories[0]["starters"][0]["label"] == "Hello"


def test_share_thread_endpoint_sets_flags(
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
):
    # Override current user to match thread author
    from chainlit.server import app as _app, get_current_user as _get_current_user

    author = PersistedUser(
        id="u1",
        createdAt=dt.datetime.now().isoformat(),
        identifier="author",
    )

    _app.dependency_overrides[_get_current_user] = lambda: author

    # Mock data layer
    from unittest.mock import AsyncMock

    dl = AsyncMock()
    dl.get_thread.return_value = {
        "id": "t1",
        "name": "Thread 1",
        "userIdentifier": "author",
        "metadata": {"other": True},
    }
    dl.get_thread_author.return_value = "author"

    # Ensure data layer is initialized for both server routes and ACL checks
    import chainlit.data as data_mod

    data_mod._data_layer = dl
    data_mod._data_layer_initialized = True

    # Share
    r = test_client.put(
        "/project/thread/share", json={"threadId": "t1", "isShared": True}
    )
    assert r.status_code == 200

    # Validate metadata passed to update_thread includes is_shared and shared_at
    assert dl.update_thread.await_count >= 1
    _, kwargs = dl.update_thread.await_args
    assert kwargs.get("thread_id") == "t1"
    meta = kwargs.get("metadata") or {}
    assert meta.get("is_shared") is True
    assert isinstance(meta.get("shared_at"), str)

    # Unshare
    r = test_client.put(
        "/project/thread/share", json={"threadId": "t1", "isShared": False}
    )
    assert r.status_code == 200
    _, kwargs = dl.update_thread.await_args
    meta = kwargs.get("metadata") or {}
    assert meta.get("is_shared") is False
    assert "shared_at" not in meta

    # Cleanup override and data layer
    del _app.dependency_overrides[_get_current_user]
    data_mod._data_layer = None
    data_mod._data_layer_initialized = False


def test_health_check(test_client: TestClient):
    response = test_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


AZURE_TENANT_ID = "11111111-1111-1111-1111-111111111111"
AZURE_ISSUER = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/v2.0"

OAUTH_COOKIES = {"oauth_state": "valid_state", "oauth_nonce": "login_nonce"}
GENERIC_CALLBACK = "/auth/oauth/generic/callback"
AZURE_CALLBACK = "/auth/oauth/azure-ad-hybrid/callback"


def _assert_oauth_cookies_cleared(response, *, secure=None):
    headers = response.headers.get_list("set-cookie")
    deleted = {}
    for header in headers:
        name = header.split("=", 1)[0].strip().lower()
        if name in ("oauth_state", "oauth_nonce"):
            deleted[name] = header

    for name in ("oauth_state", "oauth_nonce"):
        assert name in deleted, headers
        lowered = deleted[name].lower()
        assert "max-age=0" in lowered, deleted[name]
        assert "path=" in lowered, deleted[name]
        if secure is not None:
            assert (
                "secure" in [part.strip() for part in lowered.split(";")]
            ) is secure, deleted[name]


@pytest.fixture
def mock_generic_oauth_provider(monkeypatch: pytest.MonkeyPatch):
    provider = Mock()
    provider.id = "generic"
    provider.get_token = AsyncMock(return_value="access-token")
    provider.get_user_info = AsyncMock(
        return_value=(
            {"sub": "user@example.com"},
            User(identifier="user@example.com"),
        )
    )
    monkeypatch.setattr("chainlit.server.get_oauth_provider", lambda _: provider)
    return provider


def test_oauth_callback_provider_error_clears_cookies(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
):
    test_config.code.oauth_callback = AsyncMock()

    response = test_client.get(
        GENERIC_CALLBACK,
        params={"error": "access_denied"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert "error=access_denied" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


@pytest.mark.parametrize("params", [{}, {"code": "code"}, {"state": "valid_state"}])
def test_oauth_callback_missing_code_or_state_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
    params: dict,
):
    callback = AsyncMock()
    test_config.code.oauth_callback = callback

    response = test_client.get(
        GENERIC_CALLBACK,
        params=params,
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_not_awaited()


def test_oauth_callback_invalid_state_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
):
    callback = AsyncMock()
    test_config.code.oauth_callback = callback

    response = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "wrong_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_not_awaited()


def test_oauth_callback_token_error_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
):
    mock_generic_oauth_provider.get_token.side_effect = RuntimeError("token exchange")
    test_config.code.oauth_callback = AsyncMock()

    response = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "valid_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


def test_oauth_callback_user_info_error_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
):
    mock_generic_oauth_provider.get_user_info.side_effect = RuntimeError("user info")
    test_config.code.oauth_callback = AsyncMock()

    response = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "valid_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


def test_oauth_callback_app_error_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
):
    callback = AsyncMock(side_effect=ValueError("app callback secret"))
    test_config.code.oauth_callback = callback

    response = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "valid_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_awaited_once()


def test_oauth_callback_no_user_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
):
    test_config.code.oauth_callback = AsyncMock(return_value=None)

    response = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "valid_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


def test_oauth_callback_success_clears_state_and_nonce(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "CHAINLIT_AUTH_SECRET", "test-secret-test-secret-test-secret-1234"
    )
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)
    test_config.code.oauth_callback = AsyncMock(
        return_value=User(identifier="user@example.com")
    )

    response = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "valid_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "/login/callback?" in response.headers["location"]
    auth_headers = [
        header
        for header in response.headers.get_list("set-cookie")
        if header.startswith("access_token=")
    ]
    assert auth_headers
    assert "max-age=0" not in auth_headers[0].lower()
    _assert_oauth_cookies_cleared(response)


@pytest.mark.parametrize("secure", [False, True])
def test_oauth_callback_cookie_deletions_carry_secure(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_generic_oauth_provider: Mock,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
):
    import chainlit.auth.cookie as cookie_module

    monkeypatch.setattr(cookie_module, "_cookie_secure", secure)
    monkeypatch.setattr(
        "chainlit.server._authenticate_user",
        AsyncMock(return_value=RedirectResponse("/", status_code=302)),
    )
    test_config.code.oauth_callback = AsyncMock(
        return_value=User(identifier="user@example.com")
    )

    success = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "valid_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )
    assert success.status_code == 302
    _assert_oauth_cookies_cleared(success, secure=secure)

    failure = test_client.get(
        GENERIC_CALLBACK,
        params={"code": "code", "state": "wrong_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )
    assert failure.status_code == 303
    _assert_oauth_cookies_cleared(failure, secure=secure)


@pytest.fixture
def mock_azure_hf_verify(monkeypatch: pytest.MonkeyPatch):
    verify = AsyncMock(return_value={"upn": "user@example.com"})
    monkeypatch.setattr("chainlit.server._verify_azure_hf_id_token", verify)
    return verify


@pytest.fixture
def mock_oauth_provider(monkeypatch: pytest.MonkeyPatch):
    provider = Mock()
    provider.id = "azure-ad-hybrid"
    provider.client_id = "test-client"
    provider.authorize_url = (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    )
    provider.authorize_params = {
        "tenant": "common",
        "response_type": "code id_token",
        "scope": "openid",
        "response_mode": "form_post",
    }
    provider.uses_per_login_nonce = True
    provider.get_token = AsyncMock(return_value="access-token")
    provider.get_user_info = AsyncMock(
        return_value=(
            {"upn": "user@example.com"},
            User(identifier="user@example.com"),
        )
    )
    monkeypatch.setattr("chainlit.server.get_oauth_provider", lambda _: provider)
    return provider


def test_azure_hf_callback_missing_state_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
):
    callback = AsyncMock()
    test_config.code.oauth_callback = callback

    response = test_client.post(
        "/auth/oauth/azure-ad-hybrid/callback",
        data={"code": "fake_code", "id_token": "a.b.c"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_not_awaited()


def test_azure_hf_callback_missing_id_token_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
):
    callback = AsyncMock()
    test_config.code.oauth_callback = callback

    response = test_client.post(
        "/auth/oauth/azure-ad-hybrid/callback",
        data={"code": "fake_code", "state": "valid_state"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_not_awaited()


def test_azure_hf_callback_rejects_forged_id_token(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
    monkeypatch: pytest.MonkeyPatch,
):
    verify = AsyncMock(side_effect=ValueError("signature verification failed"))
    monkeypatch.setattr("chainlit.server._verify_azure_hf_id_token", verify)
    callback = AsyncMock()
    test_config.code.oauth_callback = callback

    response = test_client.post(
        "/auth/oauth/azure-ad-hybrid/callback",
        data={"code": "fake_code", "state": "valid_state", "id_token": "forged"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    verify.assert_awaited_once()
    callback.assert_not_awaited()
    assert "access_token" not in response.cookies


def test_azure_hf_callback_verifies_token_with_login_nonce(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
    monkeypatch: pytest.MonkeyPatch,
):
    verify = AsyncMock(return_value={"upn": "user@example.com"})
    monkeypatch.setattr("chainlit.server._verify_azure_hf_id_token", verify)
    monkeypatch.setattr(
        "chainlit.server._authenticate_user",
        AsyncMock(return_value=RedirectResponse("/", status_code=303)),
    )
    callback = AsyncMock(return_value=User(identifier="user@example.com"))
    test_config.code.oauth_callback = callback

    response = test_client.post(
        "/auth/oauth/azure-ad-hybrid/callback",
        data={"code": "fake_code", "state": "valid_state", "id_token": "good"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" not in response.headers.get("location", "")
    _assert_oauth_cookies_cleared(response)
    verify.assert_awaited_once_with(mock_oauth_provider, "good", "login_nonce")
    callback.assert_awaited_once()


def test_azure_hf_login_sets_a_fresh_nonce_cookie(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
):
    test_config.code.oauth_callback = AsyncMock()

    response = test_client.get("/auth/oauth/azure-ad-hybrid", follow_redirects=False)

    assert response.status_code == 307
    params = urllib.parse.parse_qs(
        urllib.parse.urlparse(response.headers["location"]).query
    )
    assert params["nonce"]
    parsed_cookie = SimpleCookie()
    parsed_cookie.load(f"oauth_nonce={response.cookies['oauth_nonce']}")
    assert parsed_cookie["oauth_nonce"].value == params["nonce"][0]
    assert "nonce" not in mock_oauth_provider.authorize_params


def test_azure_hf_provider_error_clears_cookies(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
):
    test_config.code.oauth_callback = AsyncMock()

    response = test_client.post(
        AZURE_CALLBACK,
        data={"error": "access_denied"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert "error=access_denied" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


def test_azure_hf_callback_missing_code_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
):
    callback = AsyncMock()
    test_config.code.oauth_callback = callback

    response = test_client.post(
        AZURE_CALLBACK,
        data={"state": "valid_state", "id_token": "a.b.c"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_not_awaited()


def test_azure_hf_callback_invalid_state_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
):
    callback = AsyncMock()
    test_config.code.oauth_callback = callback

    response = test_client.post(
        AZURE_CALLBACK,
        data={"code": "code", "state": "wrong_state", "id_token": "a.b.c"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_not_awaited()


def test_azure_hf_callback_token_error_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
    mock_azure_hf_verify: AsyncMock,
):
    mock_oauth_provider.get_token.side_effect = RuntimeError("token exchange")
    test_config.code.oauth_callback = AsyncMock()

    response = test_client.post(
        AZURE_CALLBACK,
        data={"code": "code", "state": "valid_state", "id_token": "good"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


def test_azure_hf_callback_user_info_error_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
    mock_azure_hf_verify: AsyncMock,
):
    mock_oauth_provider.get_user_info.side_effect = RuntimeError("user info")
    test_config.code.oauth_callback = AsyncMock()

    response = test_client.post(
        AZURE_CALLBACK,
        data={"code": "code", "state": "valid_state", "id_token": "good"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


def test_azure_hf_callback_app_error_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
    mock_azure_hf_verify: AsyncMock,
):
    callback = AsyncMock(side_effect=ValueError("app callback secret"))
    test_config.code.oauth_callback = callback

    response = test_client.post(
        AZURE_CALLBACK,
        data={"code": "code", "state": "valid_state", "id_token": "good"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)
    callback.assert_awaited_once()


def test_azure_hf_callback_no_user_redirects(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
    mock_azure_hf_verify: AsyncMock,
):
    test_config.code.oauth_callback = AsyncMock(return_value=None)

    response = test_client.post(
        AZURE_CALLBACK,
        data={"code": "code", "state": "valid_state", "id_token": "good"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "error=oauthSignin" in response.headers["location"]
    _assert_oauth_cookies_cleared(response)


@pytest.mark.parametrize("secure", [False, True])
def test_azure_hf_cookie_deletions_carry_secure(
    test_client: TestClient,
    test_config: ChainlitConfig,
    mock_oauth_provider: Mock,
    mock_azure_hf_verify: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
):
    import chainlit.auth.cookie as cookie_module

    monkeypatch.setattr(cookie_module, "_cookie_secure", secure)
    monkeypatch.setattr(
        "chainlit.server._authenticate_user",
        AsyncMock(return_value=RedirectResponse("/", status_code=303)),
    )
    test_config.code.oauth_callback = AsyncMock(
        return_value=User(identifier="user@example.com")
    )

    success = test_client.post(
        AZURE_CALLBACK,
        data={"code": "code", "state": "valid_state", "id_token": "good"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )
    assert success.status_code == 303
    assert "error=oauthSignin" not in success.headers["location"]
    _assert_oauth_cookies_cleared(success, secure=secure)

    failure = test_client.post(
        AZURE_CALLBACK,
        data={"code": "code", "state": "wrong_state", "id_token": "good"},
        cookies=OAUTH_COOKIES,
        follow_redirects=False,
    )
    assert failure.status_code == 303
    _assert_oauth_cookies_cleared(failure, secure=secure)


@pytest.fixture(scope="module")
def rsa_key_pair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _encode_id_token(private_key, **overrides):
    now = int(time.time())
    claims = {
        "aud": "test-client",
        "iss": AZURE_ISSUER,
        "tid": AZURE_TENANT_ID,
        "exp": now + 300,
        "iat": now,
        "nonce": "login-nonce",
        "upn": "user@example.com",
    }
    claims.update(overrides)
    return jwt.encode(
        claims, private_key, algorithm="RS256", headers={"kid": "test-key"}
    )


def _use_local_jwks(monkeypatch: pytest.MonkeyPatch, public_key):
    class FakeJWKS:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=public_key)

    monkeypatch.setattr("chainlit.server._azure_hf_jwks_client", lambda _: FakeJWKS())


@pytest.mark.asyncio
async def test_verify_azure_hf_token_multi_tenant(
    monkeypatch: pytest.MonkeyPatch, rsa_key_pair
):
    private_key, public_key = rsa_key_pair
    monkeypatch.delenv("OAUTH_AZURE_AD_HYBRID_ENABLE_SINGLE_TENANT", raising=False)
    monkeypatch.setenv("OAUTH_AZURE_AD_HYBRID_TENANT_ID", "common")
    _use_local_jwks(monkeypatch, public_key)

    token = _encode_id_token(private_key)
    decoded = await _verify_azure_hf_id_token(
        SimpleNamespace(client_id="test-client"), token, "login-nonce"
    )

    assert decoded["upn"] == "user@example.com"


@pytest.mark.asyncio
async def test_verify_azure_hf_token_rejects_bad_nonce(
    monkeypatch: pytest.MonkeyPatch, rsa_key_pair
):
    private_key, public_key = rsa_key_pair
    monkeypatch.delenv("OAUTH_AZURE_AD_HYBRID_ENABLE_SINGLE_TENANT", raising=False)
    monkeypatch.setenv("OAUTH_AZURE_AD_HYBRID_TENANT_ID", "common")
    _use_local_jwks(monkeypatch, public_key)

    token = _encode_id_token(private_key, nonce="other-nonce")

    with pytest.raises(ValueError, match="nonce"):
        await _verify_azure_hf_id_token(
            SimpleNamespace(client_id="test-client"), token, "login-nonce"
        )


@pytest.mark.asyncio
async def test_verify_azure_hf_token_rejects_unexpected_issuer(
    monkeypatch: pytest.MonkeyPatch, rsa_key_pair
):
    private_key, public_key = rsa_key_pair
    monkeypatch.delenv("OAUTH_AZURE_AD_HYBRID_ENABLE_SINGLE_TENANT", raising=False)
    monkeypatch.setenv("OAUTH_AZURE_AD_HYBRID_TENANT_ID", "common")
    _use_local_jwks(monkeypatch, public_key)

    token = _encode_id_token(private_key, iss="https://evil.example.com/tenant/v2.0")

    with pytest.raises(ValueError, match="issuer"):
        await _verify_azure_hf_id_token(
            SimpleNamespace(client_id="test-client"), token, "login-nonce"
        )


@pytest.mark.asyncio
async def test_verify_azure_hf_token_rejects_wrong_audience(
    monkeypatch: pytest.MonkeyPatch, rsa_key_pair
):
    private_key, public_key = rsa_key_pair
    monkeypatch.delenv("OAUTH_AZURE_AD_HYBRID_ENABLE_SINGLE_TENANT", raising=False)
    monkeypatch.setenv("OAUTH_AZURE_AD_HYBRID_TENANT_ID", "common")
    _use_local_jwks(monkeypatch, public_key)

    token = _encode_id_token(private_key, aud="other-client")

    with pytest.raises(jwt.InvalidAudienceError):
        await _verify_azure_hf_id_token(
            SimpleNamespace(client_id="test-client"), token, "login-nonce"
        )


@pytest.mark.asyncio
async def test_verify_azure_hf_token_single_tenant_issuer(
    monkeypatch: pytest.MonkeyPatch, rsa_key_pair
):
    private_key, public_key = rsa_key_pair
    monkeypatch.setenv("OAUTH_AZURE_AD_HYBRID_ENABLE_SINGLE_TENANT", "1")
    monkeypatch.setenv("OAUTH_AZURE_AD_HYBRID_TENANT_ID", AZURE_TENANT_ID)
    _use_local_jwks(monkeypatch, public_key)

    token = _encode_id_token(private_key)
    decoded = await _verify_azure_hf_id_token(
        SimpleNamespace(client_id="test-client"), token, "login-nonce"
    )

    assert decoded["tid"] == AZURE_TENANT_ID

    wrong_token = _encode_id_token(
        private_key,
        iss="https://login.microsoftonline.com/22222222-2222-2222-2222-222222222222/v2.0",
    )
    with pytest.raises(jwt.InvalidIssuerError):
        await _verify_azure_hf_id_token(
            SimpleNamespace(client_id="test-client"),
            wrong_token,
            "login-nonce",
        )


@pytest.mark.parametrize(
    ("url", "root_path", "expected"),
    [
        ("/public/test.css", "/app", "/app/public/test.css"),
        ("/public/test.css", "/app/", "/app/public/test.css"),
        ("/public/test.css", "", "/public/test.css"),
        (
            "https://cdn.example.com/test.css",
            "/app",
            "https://cdn.example.com/test.css",
        ),
        ("http://cdn.example.com/test.css", "/app", "http://cdn.example.com/test.css"),
        ("//cdn.example.com/test.css", "/app", "//cdn.example.com/test.css"),
        ("data:text/css,body{}", "/app", "data:text/css,body{}"),
        (None, "/app", None),
    ],
)
def test_prefix_asset_url(url: str | None, root_path: str, expected: str | None):
    assert prefix_asset_url(url, root_path) == expected


@pytest.fixture
def html_build_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "index.html").write_text(
        """<!doctype html>
<html>
  <head>
    <!-- TAG INJECTION PLACEHOLDER -->
    <!-- FONT START --><!-- FONT END -->
    <!-- JS INJECTION PLACEHOLDER -->
    <!-- CSS INJECTION PLACEHOLDER -->
    <script type="module" src="/assets/app.js"></script>
    <link rel="stylesheet" href="/assets/app.css">
  </head>
</html>""",
        encoding="utf-8",
    )
    public_dir = tmp_path / "public"
    public_dir.mkdir()
    monkeypatch.setattr("chainlit.server.build_dir", str(build_dir))
    monkeypatch.setattr("chainlit.server.public_dir", str(public_dir))


def test_get_html_template_prefixes_configured_assets(
    html_build_dirs, test_config: ChainlitConfig
):
    test_config.ui.custom_css = "/public/custom.css"
    test_config.ui.custom_js = "/public/custom.js"
    test_config.ui.custom_meta_image_url = "/public/banner.png"

    html = get_html_template("/app")

    assert 'href="/app/public/custom.css"' in html
    assert 'src="/app/public/custom.js"' in html
    assert 'content="/app/public/banner.png"' in html
    assert 'href="/app/favicon"' in html
    assert 'src="/app/assets/app.js"' in html
    assert 'href="/app/assets/app.css"' in html


def test_get_html_template_without_root_path_keeps_configured_assets(
    html_build_dirs, test_config: ChainlitConfig
):
    test_config.ui.custom_css = "/public/custom.css"
    test_config.ui.custom_js = "/public/custom.js"
    test_config.ui.custom_meta_image_url = "/public/banner.png"

    html = get_html_template("")

    assert 'href="/public/custom.css"' in html
    assert 'src="/public/custom.js"' in html
    assert 'content="/public/banner.png"' in html
    assert 'href="/favicon"' in html
    assert 'src="/assets/app.js"' in html


def test_get_html_template_keeps_external_assets(
    html_build_dirs, test_config: ChainlitConfig
):
    test_config.ui.custom_css = "https://cdn.example.com/custom.css"
    test_config.ui.custom_js = "//cdn.example.com/custom.js"
    test_config.ui.custom_meta_image_url = "https://cdn.example.com/banner.png"

    html = get_html_template("/app")

    assert 'href="https://cdn.example.com/custom.css"' in html
    assert 'src="//cdn.example.com/custom.js"' in html
    assert 'content="https://cdn.example.com/banner.png"' in html


# ---- Clear all threads ----


class _BulkDeleteDataLayer:
    """Minimal data layer stand-in for the clear-all-threads endpoint."""

    def __init__(self, threads: dict[str, list[str]]):
        self.threads = threads
        self.calls: list[str] = []

    async def delete_all_threads(self, user_id: str) -> int:
        self.calls.append(user_id)
        return len(self.threads.pop(user_id, []))


class _NoBulkDeleteDataLayer:
    """Layer that inherits, rather than overrides, the base bulk delete method."""

    delete_all_threads = BaseDataLayer.delete_all_threads


@pytest.fixture
def as_user():
    """Override the authenticated user for the duration of a test."""

    def _as_user(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _as_user

    app.dependency_overrides.pop(get_current_user, None)


def test_supports_delete_all_threads_only_for_overrides():
    from chainlit.server import _supports_delete_all_threads

    assert _supports_delete_all_threads(None) is False
    assert _supports_delete_all_threads(_NoBulkDeleteDataLayer()) is False
    assert _supports_delete_all_threads(_BulkDeleteDataLayer({})) is True


def test_delete_all_threads_requires_authentication(
    test_client: TestClient,
):
    response = test_client.delete("/project/threads")

    assert response.status_code == 401


def test_delete_all_threads_requires_data_layer(
    test_client: TestClient, as_user, monkeypatch: pytest.MonkeyPatch
):
    as_user(PersistedUser(id="user-a", identifier="a@example.com", createdAt="now"))
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)

    response = test_client.delete("/project/threads")

    assert response.status_code == 400
    assert response.json()["detail"] == "Data persistence is not enabled"


def test_delete_all_threads_unsupported_layer_returns_501(
    test_client: TestClient, as_user, monkeypatch: pytest.MonkeyPatch
):
    class _UnsupportedLayer:
        async def delete_all_threads(self, user_id: str) -> int:
            raise NotImplementedError("Bulk thread deletion is not implemented")

    as_user(PersistedUser(id="user-a", identifier="a@example.com", createdAt="now"))
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: _UnsupportedLayer())

    response = test_client.delete("/project/threads")

    assert response.status_code == 501


def test_delete_all_threads_returns_deleted_count(
    test_client: TestClient, as_user, monkeypatch: pytest.MonkeyPatch
):
    data_layer = _BulkDeleteDataLayer({"user-a": ["t1", "t2", "t3"]})
    as_user(PersistedUser(id="user-a", identifier="a@example.com", createdAt="now"))
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)

    response = test_client.delete("/project/threads")

    assert response.status_code == 200
    assert response.json() == {"success": True, "deleted": 3}
    assert data_layer.calls == ["user-a"]


def test_delete_all_threads_only_deletes_current_user_threads(
    test_client: TestClient, as_user, monkeypatch: pytest.MonkeyPatch
):
    data_layer = _BulkDeleteDataLayer(
        {"user-a": ["a-thread-1", "a-thread-2"], "user-b": ["b-thread-1"]}
    )
    as_user(PersistedUser(id="user-a", identifier="a@example.com", createdAt="now"))
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)

    response = test_client.request(
        "DELETE",
        "/project/threads",
        json={"userId": "user-b", "threadIds": ["b-thread-1"]},
    )

    assert response.status_code == 200
    assert response.json() == {"success": True, "deleted": 2}
    # Only the authenticated user's id reaches the data layer.
    assert data_layer.calls == ["user-a"]
    assert data_layer.threads == {"user-b": ["b-thread-1"]}


def test_delete_all_threads_logs_identifier_and_count_only(
    test_client: TestClient,
    as_user,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    data_layer = _BulkDeleteDataLayer({"user-a": ["secret-thread-name"]})
    as_user(PersistedUser(id="user-a", identifier="a@example.com", createdAt="now"))
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: data_layer)

    with caplog.at_level("INFO", logger="chainlit"):
        response = test_client.delete("/project/threads")

    assert response.status_code == 200
    assert "Deleted 1 threads for user a@example.com" in caplog.text
    assert "secret-thread-name" not in caplog.text


def test_thread_clear_all_capability_flag(
    test_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        "chainlit.server.get_data_layer", lambda: _NoBulkDeleteDataLayer()
    )
    response = test_client.get("/project/settings")
    assert response.status_code == 200
    assert response.json()["threadClearAll"] is False

    monkeypatch.setattr(
        "chainlit.server.get_data_layer", lambda: _BulkDeleteDataLayer({})
    )
    response = test_client.get("/project/settings")
    assert response.status_code == 200
    assert response.json()["threadClearAll"] is True
