"""Proxy-aware browser-origin checks for CHAINLIT_URL deployments."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import chainlit.server as server
from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.user import User

PUBLIC_ORIGIN = "https://public.example"
INTERNAL_HOST = "internal:8000"


@pytest.fixture
def proxy_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHAINLIT_URL", PUBLIC_ORIGIN)
    return monkeypatch


@pytest.fixture
def client():
    return TestClient(server.app, base_url="http://internal:8000")


@pytest.fixture
def authenticated_user():
    previous = server.app.dependency_overrides.get(get_current_user)
    server.app.dependency_overrides[get_current_user] = lambda: User(identifier="alice")
    try:
        yield
    finally:
        if previous is None:
            server.app.dependency_overrides.pop(get_current_user, None)
        else:
            server.app.dependency_overrides[get_current_user] = previous


def post(server_route: str, client: TestClient, origin: str):
    return client.post(
        server_route,
        headers={"Origin": origin, "Host": INTERNAL_HOST},
        json={
            "sessionId": "unknown-session",
            "questionId": "q-1",
            "text": "answer",
            "decisionRequestId": "request-1",
        },
    )


def test_question_accepts_configured_external_origin_behind_proxy(
    client: TestClient, proxy_env, authenticated_user
):
    response = post("/project/question", client, PUBLIC_ORIGIN)
    assert response.status_code != 403
    assert response.status_code == 404


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://public.example",
        "null",
    ],
)
def test_question_rejects_other_or_downgraded_origins_behind_proxy(
    client: TestClient, proxy_env, authenticated_user, origin: str
):
    response = post("/project/question", client, origin)
    assert response.status_code == 403


def test_wildcard_cors_does_not_bypass_the_json_origin_guard(
    client: TestClient, proxy_env, authenticated_user, monkeypatch
):
    monkeypatch.setattr(config.project, "allow_origins", ["*"])
    response = post("/project/question", client, "https://evil.example")
    assert response.status_code == 403


def test_without_chainlit_url_proxy_host_still_requires_an_exact_origin(
    client: TestClient, monkeypatch, authenticated_user
):
    monkeypatch.delenv("CHAINLIT_URL", raising=False)
    response = post("/project/question", client, PUBLIC_ORIGIN)
    assert response.status_code == 403


def test_json_guard_rejects_http_origin_on_https_even_with_http_chainlit_url(
    monkeypatch, authenticated_user
):
    monkeypatch.setenv("CHAINLIT_URL", "http://public.example")
    https_client = TestClient(server.app, base_url="https://internal:8000")
    response = post("/project/question", https_client, "http://public.example")
    assert response.status_code == 403


def test_general_guard_rejects_http_origin_on_https_with_http_allowlist(monkeypatch):
    monkeypatch.setenv("CHAINLIT_URL", "http://public.example")
    monkeypatch.setattr(config.project, "allow_origins", ["http://public.example"])
    https_client = TestClient(server.app, base_url="https://internal:8000")
    response = https_client.post(
        "/set-session-cookie",
        headers={"Origin": "http://public.example", "Host": INTERNAL_HOST},
        json={"session_id": "valid-session"},
    )
    assert response.status_code == 403


def test_approval_uses_the_same_proxy_aware_origin_guard(
    client: TestClient, proxy_env, authenticated_user
):
    response = client.post(
        "/project/approval",
        headers={"Origin": PUBLIC_ORIGIN, "Host": INTERNAL_HOST},
        json={
            "sessionId": "unknown-session",
            "approvalId": "approval-1",
            "decision": "approve",
            "decisionRequestId": "request-1",
        },
    )
    assert response.status_code != 403
    assert response.status_code == 404


def test_set_session_cookie_uses_the_configured_external_origin(
    client: TestClient, proxy_env
):
    response = client.post(
        "/set-session-cookie",
        headers={"Origin": PUBLIC_ORIGIN, "Host": INTERNAL_HOST},
        json={"session_id": "valid-session"},
    )
    assert response.status_code == 200


def test_set_session_cookie_rejects_a_downgraded_configured_origin(
    client: TestClient, proxy_env
):
    response = client.post(
        "/set-session-cookie",
        headers={"Origin": "http://public.example", "Host": INTERNAL_HOST},
        json={"session_id": "valid-session"},
    )
    assert response.status_code == 403


def test_socket_guard_uses_chainlit_url_when_present(monkeypatch):
    monkeypatch.setenv("CHAINLIT_URL", PUBLIC_ORIGIN)
    monkeypatch.setattr(server, "require_login", lambda: True)
    environ = {"HTTP_HOST": INTERNAL_HOST, "wsgi.url_scheme": "https"}
    assert server._socket_origin_allowed(PUBLIC_ORIGIN, environ) is True
    assert server._socket_origin_allowed("http://public.example", environ) is False
