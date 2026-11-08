"""Negative-path coverage for the stateless JWT auth boundary.

Existing cookie tests cover transport flags; these tests cover what the token
itself may claim and how /auth/jwt rejects malformed credentials.
"""

import time
from unittest.mock import Mock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from chainlit import auth as auth_module
from chainlit.auth import create_jwt, decode_jwt, ensure_jwt_secret
from chainlit.server import app
from chainlit.user import User

SECRET = "security-baseline-test-secret-at-least-32-bytes"


@pytest.fixture(autouse=True)
def jwt_secret(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", SECRET)
    # Keep the JWT boundary self-contained: an ambient DATABASE_URL must not
    # initialize the process-wide data layer as a side effect of /auth/jwt.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr("chainlit.server.get_data_layer", lambda: None)
    monkeypatch.setattr("chainlit.auth.get_data_layer", lambda: None)


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _claims(**overrides):
    claims = {
        "identifier": "alice",
        "metadata": {},
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    return claims


class TestDecodeJwt:
    def test_round_trip_preserves_identity(self):
        token = create_jwt(User(identifier="alice", metadata={"role": "user"}))

        user = decode_jwt(token)

        assert user.identifier == "alice"
        assert user.metadata == {"role": "user"}

    def test_rejects_token_signed_with_another_secret(self):
        token = pyjwt.encode(
            _claims(), "not-the-app-secret-but-at-least-32b", algorithm="HS256"
        )

        with pytest.raises(pyjwt.InvalidSignatureError):
            decode_jwt(token)

    def test_rejects_expired_token(self):
        token = pyjwt.encode(_claims(exp=int(time.time()) - 1), SECRET, "HS256")

        with pytest.raises(pyjwt.ExpiredSignatureError):
            decode_jwt(token)

    def test_rejects_unsigned_alg_none_token(self):
        token = pyjwt.encode(_claims(), None, algorithm="none")

        with pytest.raises(pyjwt.InvalidAlgorithmError):
            decode_jwt(token)

    def test_rejects_algorithm_confusion_with_rs256(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        token = pyjwt.encode(_claims(), private_key, algorithm="RS256")

        with pytest.raises(pyjwt.InvalidAlgorithmError):
            decode_jwt(token)

    def test_rejects_tampered_signature(self):
        token = create_jwt(User(identifier="alice"))
        header, payload, signature = token.split(".")
        # Tamper the first char: the last base64url char carries padding bits,
        # so changing it can decode to the same signature bytes.
        tampered = ".".join(
            [header, payload, ("A" if signature[0] != "A" else "B") + signature[1:]]
        )

        with pytest.raises(pyjwt.InvalidSignatureError):
            decode_jwt(tampered)

    def test_rejects_malformed_token(self):
        with pytest.raises(pyjwt.DecodeError):
            decode_jwt("not-a-jwt")


class TestJwtEndpoint:
    @pytest.mark.parametrize(
        ("headers", "expected_detail"),
        [
            ({}, "Authorization header missing"),
            (
                {"Authorization": "Basic abc"},
                "Invalid authentication scheme. Please use Bearer",
            ),
            ({"Authorization": "Bearer"}, "Invalid authorization header format"),
            ({"Authorization": "Bearer not-a-jwt"}, "Invalid token"),
        ],
    )
    def test_rejects_bad_credentials(
        self, client: TestClient, headers, expected_detail
    ):
        response = client.post("/auth/jwt", headers=headers)

        assert response.status_code == 401
        assert response.json()["detail"] == expected_detail
        assert "access_token" not in response.cookies

    def test_rejects_expired_token(self, client: TestClient):
        token = pyjwt.encode(_claims(exp=int(time.time()) - 1), SECRET, "HS256")

        response = client.post(
            "/auth/jwt", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 401
        assert "access_token" not in response.cookies

    def test_accepts_valid_token_and_sets_httponly_cookie(self, client: TestClient):
        token = create_jwt(User(identifier="alice"))

        response = client.post(
            "/auth/jwt", headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 200
        assert response.json() == {"success": True}
        set_cookie = response.headers["set-cookie"]
        assert "access_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie


class TestJwtSecretConfiguration:
    def test_missing_secret_fails_closed_when_login_is_required(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("CHAINLIT_AUTH_SECRET", raising=False)
        monkeypatch.setattr(auth_module, "require_login", Mock(return_value=True))

        with pytest.raises(ValueError, match="JWT secret"):
            ensure_jwt_secret()

    def test_configured_secret_is_accepted(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(auth_module, "require_login", Mock(return_value=True))

        assert ensure_jwt_secret() is None

    def test_secret_is_not_required_without_login(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("CHAINLIT_AUTH_SECRET", raising=False)
        monkeypatch.setattr(auth_module, "require_login", Mock(return_value=False))

        assert ensure_jwt_secret() is None
