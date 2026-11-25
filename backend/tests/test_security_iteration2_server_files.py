"""Expected-behavior tests for iteration-2 server file findings.

These tests intentionally describe the desired behavior before the server.py
remediation lands. They do not modify product code or existing tests.
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from chainlit import config as config_module, server as server_module
from chainlit.config import SpontaneousFileUploadFeature
from chainlit.session import WebsocketSession, ws_sessions_id
from chainlit.types import AskFileSpec


def _session(session_id: str) -> WebsocketSession:
    session = WebsocketSession.__new__(WebsocketSession)
    session.id = session_id
    session.files = {}
    session.files_spec = {}
    session.uploaded_file_counts = {}
    session.thread_id = "thread-1"
    return session


def _fake_upload(*, size: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        filename="upload.bin",
        content_type="application/octet-stream",
        size=size,
        file=io.BytesIO(b"x"),
        read=AsyncMock(return_value=b"x"),
        close=AsyncMock(),
    )


@pytest.fixture
def upload_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "files"
    root.mkdir()
    monkeypatch.setattr(config_module, "FILES_DIRECTORY", root)
    session = _session("default-upload-session")
    ws_sessions_id[session.id] = session
    try:
        yield session, root
    finally:
        ws_sessions_id.pop(session.id, None)


def test_missing_spontaneous_config_uses_conservative_default_limits(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(config_module.config.features, "spontaneous_file_upload", None)

    assert server_module.get_max_upload_files(None) == 20
    assert server_module.get_max_upload_size_bytes(None) == 500 * 1024 * 1024


def test_explicit_none_limits_remain_unlimited(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        config_module.config.features,
        "spontaneous_file_upload",
        SpontaneousFileUploadFeature(
            enabled=True,
            max_files=None,
            max_size_mb=None,
        ),
    )

    assert server_module.get_max_upload_files(None) is None
    assert server_module.get_max_upload_size_bytes(None) is None


def test_configured_and_ask_limits_remain_unchanged(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        config_module.config.features,
        "spontaneous_file_upload",
        SpontaneousFileUploadFeature(
            enabled=True,
            max_files=3,
            max_size_mb=7,
        ),
    )
    ask = AskFileSpec(
        type="file",
        step_id="step-1",
        timeout=30,
        accept=["*/*"],
        max_files=2,
        max_size_mb=1,
    )

    assert server_module.get_max_upload_files(None) == 3
    assert server_module.get_max_upload_size_bytes(None) == 7 * 1024 * 1024
    assert server_module.get_max_upload_files(ask) == 2
    assert server_module.get_max_upload_size_bytes(ask) == 1024 * 1024


async def test_upload_route_rejects_declared_default_size_over_limit_without_large_body(
    upload_session,
    monkeypatch: pytest.MonkeyPatch,
):
    session, root = upload_session
    monkeypatch.setattr(config_module.config.features, "spontaneous_file_upload", None)
    upload = _fake_upload(size=500 * 1024 * 1024 + 1)

    with pytest.raises(HTTPException) as exc_info:
        await server_module.upload_file(
            current_user=None,
            session_id=session.id,
            file=cast(Any, upload),
            ask_parent_id=None,
        )

    assert exc_info.value.status_code == 400
    assert "File size too large" in exc_info.value.detail
    assert session.files == {}
    assert session.uploaded_file_counts.get(None, 0) == 0
    assert not list(root.rglob("*.bin"))


async def test_upload_route_enforces_default_file_count(upload_session, monkeypatch):
    session, root = upload_session
    monkeypatch.setattr(config_module.config.features, "spontaneous_file_upload", None)
    session.uploaded_file_counts[None] = 20

    with pytest.raises(HTTPException) as exc_info:
        await server_module.upload_file(
            current_user=None,
            session_id=session.id,
            file=_fake_upload(),
            ask_parent_id=None,
        )

    assert exc_info.value.status_code == 400
    assert "File upload limit reached" in exc_info.value.detail
    assert session.files == {}
    assert not list(root.rglob("*.bin"))


@pytest.fixture
def branding_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app_root = tmp_path / "app"
    public = app_root / "public"
    assets = app_root / "build" / "assets"
    public.mkdir(parents=True)
    assets.mkdir(parents=True)
    monkeypatch.setattr(server_module, "APP_ROOT", str(app_root))
    monkeypatch.setattr(server_module, "public_dir", str(public))
    monkeypatch.setattr(server_module, "build_dir", str(app_root / "build"))
    return public, assets


@pytest.mark.parametrize(
    ("route", "filename"),
    [("/favicon", "favicon.png"), ("/logo?theme=light", "logo_light.svg")],
)
def test_branding_rejects_public_symlink_outside_root(
    branding_paths,
    tmp_path: Path,
    route: str,
    filename: str,
):
    public, _ = branding_paths
    outside = tmp_path / "outside-branding.bin"
    outside.write_bytes(b"OUTSIDE-BRANDING-SECRET")
    (public / filename).symlink_to(outside)

    response = TestClient(server_module.app).get(route)

    assert response.status_code == 400
    assert b"OUTSIDE-BRANDING-SECRET" not in response.content


@pytest.mark.parametrize(
    ("route", "relative_path"),
    [
        ("/favicon", Path("favicon.svg")),
        ("/logo?theme=light", Path("assets/logo_light.svg")),
    ],
)
def test_branding_rejects_build_symlink_outside_root(
    branding_paths,
    tmp_path: Path,
    route: str,
    relative_path: Path,
):
    _public, assets = branding_paths
    build = assets.parent
    outside = tmp_path / "outside-branding.bin"
    outside.write_bytes(b"OUTSIDE-BRANDING-SECRET")
    (build / relative_path).symlink_to(outside)

    response = TestClient(server_module.app).get(route)

    assert response.status_code == 400
    assert b"OUTSIDE-BRANDING-SECRET" not in response.content


@pytest.mark.parametrize(
    ("route", "filename"),
    [("/favicon", "favicon.png"), ("/logo?theme=light", "logo_light.svg")],
)
def test_branding_serves_ordinary_files_and_symlinks_inside_root(
    branding_paths,
    route: str,
    filename: str,
):
    public, _ = branding_paths
    selected = public / filename
    selected.write_bytes(b"BRANDING-OK")
    client = TestClient(server_module.app)

    ordinary = client.get(route)
    assert ordinary.status_code == 200
    assert ordinary.content == b"BRANDING-OK"

    selected.unlink()
    inside_target = public / "inside-branding.bin"
    inside_target.write_bytes(b"BRANDING-OK")
    selected.symlink_to(inside_target)

    inside_link = client.get(route)
    assert inside_link.status_code == 200
    assert inside_link.content == b"BRANDING-OK"


def test_logo_uses_packaged_fallback_when_custom_file_is_missing(branding_paths):
    response = TestClient(server_module.app).get("/logo?theme=light")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")


@pytest.mark.parametrize("timestamp", ["not-a-number", "1.2", "-", ""])
def test_slack_malformed_timestamp_is_rejected_before_bolt(timestamp: str):
    with pytest.raises(HTTPException) as raised:
        server_module._validate_slack_timestamp(timestamp)
    assert raised.value.status_code == 401


def test_slack_missing_or_numeric_timestamp_passes_guard():
    server_module._validate_slack_timestamp(None)
    server_module._validate_slack_timestamp("1700000000")
