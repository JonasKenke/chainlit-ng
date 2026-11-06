import importlib

import pytest
from fastapi import FastAPI, Form
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

import chainlit.auth.cookie as cookie_module
from chainlit.auth import (
    clear_auth_cookie,
    get_token_from_cookies,
    set_auth_cookie,
)
from chainlit.auth.cookie import (
    clear_oauth_cookies,
    set_oauth_nonce_cookie,
    set_oauth_state_cookie,
)


@pytest.fixture
def test_app():
    app = FastAPI()

    @app.post("/set-cookie")
    async def set_cookie_endpoint(request: Request, token: str = Form()):
        response = Response()
        set_auth_cookie(request, response, token)
        return response

    @app.get("/get-token")
    async def get_token_endpoint(request: Request):
        token = get_token_from_cookies(request.cookies)
        return {"token": token}

    @app.delete("/clear-cookie")
    async def clear_cookie_endpoint(request: Request):
        response = Response()
        clear_auth_cookie(request, response)
        return response

    return app


@pytest.fixture
def client(test_app):
    return TestClient(test_app)


def test_short_token(client):
    """Test with a <3000 shorter token."""

    # Set a short token
    short_token = "x" * 1000
    set_response = client.post("/set-cookie", data={"token": short_token})
    assert set_response.status_code == 200

    # Verify cookies were set
    cookies = set_response.cookies
    assert cookies, "No cookies set"
    assert "access_token" in cookies, f"No chunking for short cookies: {cookies}"

    # Read back the token using client's cookie jar
    get_response = client.get("/get-token")
    assert get_response.status_code == 200
    assert get_response.json()["token"] == short_token


def test_set_and_read_4kb_token(client):
    """Test full cookie lifecycle using actual client cookie handling."""

    # Set a 4KB token
    token_4kb = "x" * 4000
    set_response = client.post("/set-cookie", data={"token": token_4kb})
    assert set_response.status_code == 200

    # Verify cookies were set
    cookies = set_response.cookies
    assert f"{cookies.keys()} should contain chunked cookies", any(
        key.startswith("access_token_") for key in cookies.keys()
    )

    # Read back the token using client's cookie jar
    get_response = client.get("/get-token")
    assert get_response.status_code == 200

    response_token = get_response.json()["token"]
    assert len(response_token) == len(token_4kb)
    assert response_token == token_4kb


def test_overwrite_shorter_token_chunked(client):
    """Test cookie chunk cleanup when replacing a large token with a smaller one."""
    # Set initial long token
    long_token = "LONG" * 2000  # 8000 characters
    client.post("/set-cookie", data={"token": long_token})

    # Verify initial chunks exist
    first_cookies = client.cookies
    assert len([k for k in first_cookies if k.startswith("access_token_")]) > 1

    # Set shorter token (should clear previous chunks)
    short_token = "SHORT" * 1000  # 4000 characters
    client.post("/set-cookie", data={"token": short_token})

    # Verify new cookie state
    final_response = client.get("/get-token")
    assert final_response.json()["token"] == short_token

    # Verify only two chunks remain
    final_cookies = client.cookies
    chunk_cookies = [k for k in final_cookies if k.startswith("access_token_")]
    assert len(chunk_cookies) == 2, f"Found {len(chunk_cookies)} residual cookies"


def test_overwrite_shorter_token_unchunked(client):
    """Test cookie chunk cleanup when replacing a large token with a smaller one."""
    # Set initial long token
    long_token = "LONG" * 1000  # 4000 characters
    client.post("/set-cookie", data={"token": long_token})

    # Verify initial chunks exist
    first_cookies = client.cookies
    assert len([k for k in first_cookies if k.startswith("access_token_")]) > 1

    # Set shorter token (should clear previous chunks)
    short_token = "SHORT"
    client.post("/set-cookie", data={"token": short_token})

    # Verify new cookie state
    final_response = client.get("/get-token")
    assert final_response.json()["token"] == short_token

    # Verify no chunks remain
    final_cookies = client.cookies
    chunk_cookies = [k for k in final_cookies if k.startswith("access_token_")]
    assert len(chunk_cookies) == 0, f"Found {len(chunk_cookies)} residual cookies"


def test_state_cookie_lifetime_default(monkeypatch):
    """Test that _state_cookie_lifetime defaults to 180 seconds (3 minutes)."""
    monkeypatch.delenv("CHAINLIT_STATE_COOKIE_LIFETIME", raising=False)
    importlib.reload(cookie_module)
    assert cookie_module._state_cookie_lifetime == 180


def test_state_cookie_lifetime_custom(monkeypatch):
    """Test that _state_cookie_lifetime can be set via environment variable."""
    monkeypatch.setenv("CHAINLIT_STATE_COOKIE_LIFETIME", "600")
    importlib.reload(cookie_module)
    assert cookie_module._state_cookie_lifetime == 600


def test_cookie_path_uses_chainlit_root_path_value(monkeypatch):
    monkeypatch.setenv("CHAINLIT_ROOT_PATH", "/myapp")
    monkeypatch.delenv("CHAINLIT_AUTH_COOKIE_PATH", raising=False)
    try:
        importlib.reload(cookie_module)
        assert cookie_module._cookie_path == "/myapp"
    finally:
        monkeypatch.undo()
        importlib.reload(cookie_module)


@pytest.mark.parametrize("token", ["short", "x" * 3001])
def test_auth_cookie_uses_configured_path(client, monkeypatch, token):
    monkeypatch.setattr(cookie_module, "_cookie_path", "/myapp")

    response = client.post("/set-cookie", data={"token": token})

    set_cookies = response.headers.get_list("set-cookie")
    assert set_cookies
    assert all("path=/myapp" in cookie.lower() for cookie in set_cookies)


@pytest.mark.parametrize("cookie_value", [None, "other"])
def test_validate_oauth_state_cookie_rejects_missing_or_mismatched_state(
    cookie_value,
):
    headers = []
    if cookie_value is not None:
        headers.append((b"cookie", f"oauth_state={cookie_value}".encode()))
    request = Request(
        {
            "type": "http",
            "headers": headers,
            "query_string": b"",
            "method": "GET",
            "path": "/",
        }
    )

    with pytest.raises(Exception, match="oauth state does not correspond"):
        cookie_module.validate_oauth_state_cookie(request, "expected")


def test_validate_oauth_state_cookie_accepts_matching_state():
    request = Request(
        {
            "type": "http",
            "headers": [(b"cookie", b"oauth_state=expected")],
            "query_string": b"",
            "method": "GET",
            "path": "/",
        }
    )

    cookie_module.validate_oauth_state_cookie(request, "expected")


def test_clear_auth_cookie(client):
    """Test cookie clearing removes all chunks."""
    # Set initial token
    client.post("/set-cookie", data={"token": "x" * 4000})

    # Verify cookies exist
    assert len(client.cookies) > 0

    # Clear cookies
    clear_response = client.delete("/clear-cookie")
    assert clear_response.status_code == 200

    # Verify cookies were cleared
    assert len(clear_response.cookies) == 0
    final_response = client.get("/get-token")
    assert final_response.json()["token"] is None


COOKIE_SECURE_MATRIX = [
    ("lax", None, False),
    ("lax", "true", True),
    ("lax", " TRUE ", True),
    ("lax", "1", True),
    ("lax", "false", False),
    ("lax", "0", False),
    ("none", None, True),
    ("none", "true", True),
    ("none", "false", True),
    ("none", "0", True),
]


def _make_request(cookie_header: str = "") -> Request:
    headers = [(b"cookie", cookie_header.encode())] if cookie_header else []
    return Request(
        {
            "type": "http",
            "headers": headers,
            "query_string": b"",
            "method": "GET",
            "path": "/",
        }
    )


def _assert_cookie_attributes(response, *, secure, samesite, max_age=None):
    raw_headers = response.headers
    headers = (
        raw_headers.getlist("set-cookie")
        if hasattr(raw_headers, "getlist")
        else raw_headers.get_list("set-cookie")
    )
    assert headers
    for header in headers:
        lowered = header.lower()
        assert ("secure" in [part.strip() for part in lowered.split(";")]) is secure, (
            header
        )
        assert f"samesite={samesite}" in lowered, header
        if max_age is not None:
            assert f"max-age={max_age}" in lowered, header
    return headers


@pytest.mark.parametrize(("samesite", "flag", "expected_secure"), COOKIE_SECURE_MATRIX)
def test_cookie_secure_matrix(monkeypatch, samesite, flag, expected_secure):
    monkeypatch.setenv("CHAINLIT_COOKIE_SAMESITE", samesite)
    if flag is None:
        monkeypatch.delenv("CHAINLIT_COOKIE_SECURE", raising=False)
    else:
        monkeypatch.setenv("CHAINLIT_COOKIE_SECURE", flag)
    try:
        importlib.reload(cookie_module)
        assert cookie_module._cookie_secure is expected_secure
    finally:
        monkeypatch.undo()
        importlib.reload(cookie_module)


@pytest.mark.parametrize("value", ["yes", "", "true-ish"])
def test_cookie_secure_invalid_value_fails_at_import(monkeypatch, value):
    monkeypatch.setenv("CHAINLIT_COOKIE_SECURE", value)
    try:
        with pytest.raises(ValueError, match="CHAINLIT_COOKIE_SECURE"):
            importlib.reload(cookie_module)
    finally:
        monkeypatch.undo()
        importlib.reload(cookie_module)


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("samesite", ["lax", "none"])
@pytest.mark.parametrize("token", ["short", "x" * 3001])
def test_auth_cookie_secure_flag(monkeypatch, secure, samesite, token):
    monkeypatch.setattr(cookie_module, "_cookie_secure", secure)
    monkeypatch.setattr(cookie_module, "_cookie_samesite", samesite)

    response = Response()
    set_auth_cookie(_make_request(), response, token)

    headers = _assert_cookie_attributes(response, secure=secure, samesite=samesite)
    if len(token) > 3000:
        assert len(headers) > 1
    else:
        assert len(headers) == 1

    clear_response = Response()
    clear_auth_cookie(
        _make_request("access_token=a; access_token_0=b; access_token_1=c"),
        clear_response,
    )
    headers = _assert_cookie_attributes(
        clear_response, secure=secure, samesite=samesite, max_age=0
    )
    assert len(headers) == 3


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("samesite", ["lax", "none"])
def test_oauth_state_and_nonce_cookie_attributes(monkeypatch, secure, samesite):
    monkeypatch.setattr(cookie_module, "_cookie_secure", secure)
    monkeypatch.setattr(cookie_module, "_cookie_samesite", samesite)

    response = Response()
    set_oauth_state_cookie(response, "state")
    set_oauth_nonce_cookie(response, "nonce")

    headers = _assert_cookie_attributes(response, secure=secure, samesite=samesite)
    names = {header.split("=", 1)[0].lower() for header in headers}
    assert names == {"oauth_state", "oauth_nonce"}
    assert all("httponly" in header.lower() for header in headers)


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("samesite", ["lax", "none"])
def test_delete_cookie_attributes(monkeypatch, secure, samesite):
    monkeypatch.setattr(cookie_module, "_cookie_secure", secure)
    monkeypatch.setattr(cookie_module, "_cookie_samesite", samesite)

    response = Response()
    clear_oauth_cookies(response)

    headers = _assert_cookie_attributes(
        response, secure=secure, samesite=samesite, max_age=0
    )
    names = {header.split("=", 1)[0].lower() for header in headers}
    assert names == {"oauth_state", "oauth_nonce"}
