"""Regression tests for iteration-2 stateful feature authorization fixes."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

import chainlit.server as server_mod
from chainlit.auth import get_current_user
from chainlit.config import config
from chainlit.context import context_var
from chainlit.session import WebsocketSession, ws_sessions_id, ws_sessions_sid
from chainlit.state_store import LockNotAcquired
from chainlit.state_store.memory import MemoryStateStore
from chainlit.types import ThreadGrant, ThreadOwner
from chainlit.user import PersistedUser, User

OWNER = PersistedUser(
    id="owner-id",
    identifier="owner@example.test",
    createdAt="2026-01-01T00:00:00Z",
)
READER = PersistedUser(
    id="reader-id",
    identifier="reader@example.test",
    createdAt="2026-01-01T00:00:00Z",
)
RECYCLED = PersistedUser(
    id="recycled-id",
    identifier=OWNER.identifier,
    createdAt="2026-02-01T00:00:00Z",
)
THREAD_ID = "11111111-1111-4111-8111-111111111111"


class RecordingStore(MemoryStateStore):
    def __init__(self) -> None:
        super().__init__(prefix=f"iteration2-{uuid.uuid4().hex}")
        self.keys: list[str] = []

    def lock(self, key: str, *, ttl: float = 10, wait: float = 0):
        self.keys.append(key)
        return super().lock(key, ttl=ttl, wait=wait)


class FeatureLayer:
    thread_grants_supported = True
    feedback_authorization_supported = True

    def __init__(self) -> None:
        self.prompts: dict[str, dict[str, Any]] = {}
        self.skills: dict[str, dict[str, Any]] = {}
        self.grants: list[ThreadGrant] = []
        self.get_prompt_entered: asyncio.Event | None = None
        self.get_prompt_release: asyncio.Event | None = None
        self.get_skill_entered: asyncio.Event | None = None
        self.get_skill_release: asyncio.Event | None = None
        self.create_grant_entered: asyncio.Event | None = None
        self.create_grant_release: asyncio.Event | None = None

    def add_prompt(self, prompt_id: str = "prompt-1", **changes: Any) -> dict[str, Any]:
        row = {
            "id": prompt_id,
            "title": "Prompt",
            "content": "secret",
            "userId": OWNER.id,
            "isShared": True,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "version": 1,
            "contentHash": "sha256:prompt",
            **changes,
        }
        self.prompts[prompt_id] = row
        return deepcopy(row)

    def add_skill(self, skill_id: str = "skill-1", **changes: Any) -> dict[str, Any]:
        row = {
            "id": skill_id,
            "title": "audit-skill",
            "description": "audit",
            "content": "skill secret",
            "userId": OWNER.id,
            "isShared": True,
            "kind": "skill",
            "enabled": False,
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
            "version": 1,
            "contentHash": "sha256:skill",
            **changes,
        }
        self.skills[skill_id] = row
        return deepcopy(row)

    async def get_prompt(self, prompt_id: str):
        row = deepcopy(self.prompts.get(prompt_id))
        if row is not None and self.get_prompt_entered is not None:
            self.get_prompt_entered.set()
            assert self.get_prompt_release is not None
            await self.get_prompt_release.wait()
        return row

    async def update_prompt(self, prompt):
        self.prompts[prompt["id"]] = deepcopy(prompt)
        return deepcopy(prompt)

    async def delete_prompt(self, prompt_id: str, user_id: str) -> bool:
        row = self.prompts.pop(prompt_id, None)
        return bool(row and row["userId"] == user_id)

    async def list_prompts(self, user_id: str):
        return [
            deepcopy(row) for row in self.prompts.values() if row["userId"] == user_id
        ]

    async def list_prompt_versions(self, prompt_id: str):
        return []

    async def get_prompt_version(self, prompt_id: str, version_id: str):
        return None

    async def list_prompt_updates(self, user_id: str):
        return []

    async def ack_prompt_update(self, user_id: str, prompt_id: str, version: int):
        return version

    async def get_skill(self, skill_id: str, user_id: str):
        row = deepcopy(self.skills.get(skill_id))
        if (
            row is not None
            and row["userId"] == user_id
            and self.get_skill_entered is not None
        ):
            self.get_skill_entered.set()
            assert self.get_skill_release is not None
            await self.get_skill_release.wait()
        return row

    async def create_skill(self, skill):
        self.skills[skill["id"]] = deepcopy(skill)
        return deepcopy(skill)

    async def update_skill(self, skill):
        self.skills[skill["id"]] = deepcopy(skill)
        return deepcopy(skill)

    async def delete_skill(self, skill_id: str, user_id: str) -> bool:
        row = self.skills.pop(skill_id, None)
        return bool(row and row["userId"] == user_id)

    async def list_skills(self, user_id: str):
        return [
            deepcopy(row) for row in self.skills.values() if row["userId"] == user_id
        ]

    async def get_shared_skill(self, skill_id: str):
        row = deepcopy(self.skills.get(skill_id))
        return row if row and row.get("isShared") else None

    async def list_skill_updates(self, user_id: str):
        return []

    async def ack_skill_update(self, user_id: str, skill_id: str, version: int):
        return version

    async def get_thread_owner(self, thread_id: str):
        if thread_id != THREAD_ID:
            return None
        return ThreadOwner(user_id=OWNER.id, identifier=OWNER.identifier)

    async def get_user_for_acl(self, identifier: str):
        return READER if identifier == READER.identifier else None

    async def list_thread_grants(self, thread_id: str):
        return [grant for grant in self.grants if grant.thread_id == thread_id]

    async def create_thread_grant(self, grant: ThreadGrant) -> None:
        if self.create_grant_entered is not None:
            self.create_grant_entered.set()
            assert self.create_grant_release is not None
            await self.create_grant_release.wait()
        if not any(
            item.grantee_user_id == grant.grantee_user_id for item in self.grants
        ):
            self.grants.append(grant)

    async def delete_thread_grant(self, thread_id: str, grantee_user_id: str) -> bool:
        before = len(self.grants)
        self.grants = [
            grant
            for grant in self.grants
            if not (
                grant.thread_id == thread_id
                and grant.grantee_user_id == grantee_user_id
            )
        ]
        return len(self.grants) != before

    async def delete_all_threads(self, user_id: str) -> int:
        self.grants = [
            grant for grant in self.grants if grant.granted_by_user_id != user_id
        ]
        return 1


@pytest.fixture
async def store(monkeypatch):
    value = RecordingStore()
    monkeypatch.setattr(server_mod, "get_state_store", lambda: value, raising=False)
    yield value
    await value.close()


@pytest.fixture
async def api(monkeypatch, store):
    app = FastAPI()
    app.include_router(server_mod.skills_router)
    app.include_router(server_mod.router)
    token = context_var.set(object())  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield app, client
    finally:
        context_var.reset(token)
        app.dependency_overrides.pop(get_current_user, None)


@contextmanager
def acting_as(app: FastAPI, user: PersistedUser | None):
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


@pytest.fixture
def feature_config(monkeypatch):
    monkeypatch.setattr(config.features, "prompt_gallery", True)
    monkeypatch.setattr(config.features, "skills", True)
    monkeypatch.setattr(config.features, "thread_grants", True)
    monkeypatch.setattr(server_mod, "require_login", lambda: True)
    monkeypatch.setattr(config.code, "action_callbacks", {})


def use_layer(monkeypatch, layer: FeatureLayer) -> None:
    monkeypatch.setattr(server_mod, "get_data_layer", lambda: layer)
    monkeypatch.setattr("chainlit.data.acl.get_data_layer", lambda: layer)


@pytest.mark.asyncio
async def test_prompt_unshare_waits_for_stale_update(
    api, store, monkeypatch, feature_config
):
    _, client = api
    layer = FeatureLayer()
    layer.add_prompt()
    use_layer(monkeypatch, layer)
    layer.get_prompt_entered = asyncio.Event()
    layer.get_prompt_release = asyncio.Event()

    with acting_as(api[0], OWNER):
        update_task = asyncio.create_task(
            client.put("/project/prompts/prompt-1", json={"title": "stale edit"})
        )
        await layer.get_prompt_entered.wait()
        unshare_task = asyncio.create_task(
            client.post("/project/prompts/prompt-1/share", json={"isShared": False})
        )
        try:
            await asyncio.sleep(0.03)
            unshare_waited = not unshare_task.done()
        finally:
            layer.get_prompt_release.set()
        update_response, unshare_response = await asyncio.gather(
            update_task, unshare_task
        )

    assert unshare_waited
    assert update_response.status_code == 200
    assert unshare_response.status_code == 200
    assert layer.prompts["prompt-1"]["isShared"] is False
    assert store.keys.count("prompt-mutation:prompt-1") == 2


@pytest.mark.asyncio
async def test_skill_unshare_waits_for_stale_update(
    api, store, monkeypatch, feature_config
):
    _, client = api
    layer = FeatureLayer()
    layer.add_skill()
    use_layer(monkeypatch, layer)
    layer.get_skill_entered = asyncio.Event()
    layer.get_skill_release = asyncio.Event()

    with acting_as(api[0], OWNER):
        update_task = asyncio.create_task(
            client.put("/project/skills/skill-1", json={"description": "stale edit"})
        )
        await layer.get_skill_entered.wait()
        unshare_task = asyncio.create_task(
            client.post("/project/skills/skill-1/share", json={"isShared": False})
        )
        try:
            await asyncio.sleep(0.03)
            unshare_waited = not unshare_task.done()
        finally:
            layer.get_skill_release.set()
        update_response, unshare_response = await asyncio.gather(
            update_task, unshare_task
        )

    assert unshare_waited
    assert update_response.status_code == 200
    assert unshare_response.status_code == 200
    assert layer.skills["skill-1"]["isShared"] is False
    assert store.keys.count("skill-mutation:skill-1") == 2


@pytest.mark.asyncio
async def test_grant_revoke_waits_for_inflight_create(
    api, store, monkeypatch, feature_config
):
    _, client = api
    layer = FeatureLayer()
    use_layer(monkeypatch, layer)
    layer.create_grant_entered = asyncio.Event()
    layer.create_grant_release = asyncio.Event()

    with acting_as(api[0], OWNER):
        grant_task = asyncio.create_task(
            client.post(
                f"/project/thread/{THREAD_ID}/grants",
                json={"grantee": READER.identifier},
            )
        )
        await layer.create_grant_entered.wait()
        revoke_task = asyncio.create_task(
            client.delete(f"/project/thread/{THREAD_ID}/grants/{READER.id}")
        )
        try:
            await asyncio.sleep(0.03)
            revoke_waited = not revoke_task.done()
        finally:
            layer.create_grant_release.set()
        grant_response, revoke_response = await asyncio.gather(grant_task, revoke_task)

    assert revoke_waited
    assert grant_response.status_code == 200
    assert revoke_response.status_code == 200
    assert layer.grants == []
    assert store.keys.count(f"thread-grants:{THREAD_ID}") == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "payload", "lock_key"),
    [
        (
            "PUT",
            "/project/prompts/prompt-1",
            {"title": "edit"},
            "prompt-mutation:prompt-1",
        ),
        (
            "POST",
            "/project/prompts/prompt-1/share",
            {"isShared": False},
            "prompt-mutation:prompt-1",
        ),
        ("DELETE", "/project/prompts/prompt-1", None, "prompt-mutation:prompt-1"),
        ("POST", "/project/prompts/prompt-1/sync", {}, "prompt-mutation:prompt-1"),
        ("POST", "/project/prompts/prompt-1/detach", None, "prompt-mutation:prompt-1"),
        (
            "PUT",
            "/project/skills/skill-1",
            {"description": "edit"},
            "skill-mutation:skill-1",
        ),
        (
            "POST",
            "/project/skills/skill-1/share",
            {"isShared": False},
            "skill-mutation:skill-1",
        ),
        ("DELETE", "/project/skills/skill-1", None, "skill-mutation:skill-1"),
        (
            "POST",
            "/project/skills/skill-1/update-from-source",
            {},
            "skill-mutation:skill-1",
        ),
        ("POST", "/project/skills/skill-1/detach", None, "skill-mutation:skill-1"),
        (
            "POST",
            "/project/skills/skill-1/updates/seen",
            {"version": 1},
            "skill-mutation:skill-1",
        ),
    ],
)
async def test_prompt_and_skill_mutations_acquire_resource_lock(
    api, store, monkeypatch, feature_config, method, path, payload, lock_key
):
    _, client = api
    layer = FeatureLayer()
    layer.add_prompt()
    layer.add_skill()
    use_layer(monkeypatch, layer)
    with acting_as(api[0], OWNER):
        response = await client.request(method, path, json=payload)
    assert response.status_code in (200, 409)
    if response.status_code == 200:
        # A non-pinned sync/update returns 409 after the pre-lock source check;
        # no resource mutation happens in that case.
        assert lock_key in store.keys


@pytest.mark.asyncio
async def test_lock_conflict_returns_409(api, monkeypatch, feature_config):
    _, client = api
    layer = FeatureLayer()
    layer.add_prompt()
    use_layer(monkeypatch, layer)

    class ConflictStore:
        def lock(self, key: str, *, ttl: float = 10, wait: float = 0):
            raise LockNotAcquired("busy")

    monkeypatch.setattr(
        server_mod, "get_state_store", lambda: ConflictStore(), raising=False
    )
    with acting_as(api[0], OWNER):
        response = await client.put("/project/prompts/prompt-1", json={"title": "edit"})
    assert response.status_code == 409
    assert "busy" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_feature_disabled_and_missing_layer_do_not_lock(
    api, store, monkeypatch, feature_config
):
    _, client = api
    layer = FeatureLayer()
    layer.add_prompt()
    use_layer(monkeypatch, layer)
    monkeypatch.setattr(config.features, "prompt_gallery", False)
    with acting_as(api[0], OWNER):
        disabled = await client.put("/project/prompts/prompt-1", json={"title": "edit"})
    assert disabled.status_code == 400
    assert store.keys == []

    monkeypatch.setattr(config.features, "prompt_gallery", True)
    monkeypatch.setattr(server_mod, "get_data_layer", lambda: None)
    with acting_as(api[0], OWNER):
        missing = await client.put("/project/prompts/prompt-1", json={"title": "edit"})
    assert missing.status_code == 400
    assert store.keys == []

    use_layer(monkeypatch, layer)
    monkeypatch.setattr(config.features, "thread_grants", False)
    with acting_as(api[0], OWNER):
        grants_off = await client.post(
            f"/project/thread/{THREAD_ID}/grants", json={"grantee": READER.identifier}
        )
    assert grants_off.status_code == 404
    assert store.keys == []


def make_session(session_id: str, user: PersistedUser) -> WebsocketSession:
    session = WebsocketSession(
        id=session_id,
        socket_id=f"socket-{session_id}",
        emit=AsyncMock(),
        emit_call=AsyncMock(),
        user_env={},
        client_type="webapp",
        thread_id=THREAD_ID,
        user=user,
    )
    session.has_first_interaction = True
    return session


@pytest.fixture
def ws_session():
    sessions: list[WebsocketSession] = []

    def create(session_id: str, user: PersistedUser) -> WebsocketSession:
        session = make_session(session_id, user)
        sessions.append(session)
        return session

    yield create
    for session in sessions:
        ws_sessions_id.pop(session.id, None)
        ws_sessions_sid.pop(session.socket_id, None)


@pytest.mark.asyncio
async def test_action_rejects_recycled_generation_and_allows_same_generation(
    api, ws_session, feature_config
):
    _, client = api
    session = ws_session("action-session", OWNER)
    calls: list[str] = []

    async def callback(action):
        calls.append(action.payload["value"])
        return {"ok": True}

    config.code.action_callbacks = {"audit": callback}
    payload = {
        "sessionId": session.id,
        "action": {"name": "audit", "payload": {"value": "called"}},
    }
    with acting_as(api[0], RECYCLED):
        denied = await client.post("/project/action", json=payload)
    assert denied.status_code == 401
    assert calls == []

    with acting_as(api[0], OWNER):
        allowed = await client.post("/project/action", json=payload)
    assert allowed.status_code == 200
    assert calls == ["called"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "/project/approval",
            {
                "sessionId": "interaction-session",
                "approvalId": "approval-1",
                "decision": "approve",
                "decisionRequestId": "request-1",
            },
        ),
        (
            "/project/question",
            {
                "sessionId": "interaction-session",
                "questionId": "question-1",
                "text": "answer",
                "decisionRequestId": "request-1",
            },
        ),
    ],
)
async def test_interactions_reject_recycled_generation(
    api, ws_session, feature_config, path, body
):
    _, client = api
    ws_session("interaction-session", OWNER)
    with acting_as(api[0], RECYCLED):
        response = await client.post(path, json=body, headers={"Origin": "http://test"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_no_login_action_behavior_is_unchanged(api, ws_session, feature_config):
    _, client = api
    session = ws_session("no-login-session", OWNER)
    calls: list[str] = []

    async def callback(action):
        calls.append(action.payload["value"])
        return {"ok": True}

    config.code.action_callbacks = {"audit": callback}
    with acting_as(api[0], None):
        response = await client.post(
            "/project/action",
            json={
                "sessionId": session.id,
                "action": {"name": "audit", "payload": {"value": "no-login"}},
            },
        )
    assert response.status_code == 200
    assert calls == ["no-login"]


def test_same_principal_keeps_nonpersisted_fallback():
    assert server_mod._same_principal(User(identifier="same"), User(identifier="same"))
    assert not server_mod._same_principal(
        User(identifier="one"), User(identifier="two")
    )


@pytest.mark.asyncio
async def test_bulk_delete_acquires_grant_barrier(
    api, store, monkeypatch, feature_config
):
    _, client = api
    layer = FeatureLayer()
    use_layer(monkeypatch, layer)
    with acting_as(api[0], OWNER):
        response = await client.delete("/project/threads")
    assert response.status_code == 200
    assert "thread-grants:bulk-delete" in store.keys
