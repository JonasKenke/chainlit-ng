import asyncio
from collections.abc import Sequence

import httpx2
import pytest
from mcp.client.auth import OAuthClientProvider
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
)
from mcp.shared._httpx_utils import RedirectAwareAuth
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthToken,
)
from mcp.shared.auth_utils import resource_url_from_server_url

from chainlit import mcp_oauth
from chainlit.mcp_oauth import (
    OAUTH_ROLE,
    OAUTH_ROLE_EXTENSION,
    TRANSPORT_ROLE,
    MarkedOAuthAuth,
    McpAuthorizationError,
    McpAuthorizationTimeout,
    McpOAuthOriginError,
    McpOAuthTokenStore,
    McpTooManyFlows,
    Origin,
    OriginAllowlist,
    PendingAuthorizations,
    ScopedTokenStorage,
    build_oauth_provider,
    canonical_server_key,
    extract_state,
    normalize_origin,
    oauth_request_guard,
    prm_urls,
)

SERVER_KEY = "https://mcp.example.com/mcp"
REDIRECT_URI = "https://app.example.com/mcp/oauth/callback"


def make_authorization_url(state: str = "sdk-state") -> str:
    return (
        "https://auth.example.com/authorize?response_type=code"
        f"&state={state}&code_challenge=abc&code_challenge_method=S256"
    )


async def join_timer(handle: mcp_oauth.PendingAuthorization) -> None:
    """Join a flow's timer so a leak assertion cannot race the event loop."""
    timer = handle._timer
    assert timer is not None
    await asyncio.wait({timer}, timeout=1)
    assert timer.done()


def expiry_tasks() -> list[asyncio.Task]:
    return [
        task for task in asyncio.all_tasks() if task.get_name() == "mcp-oauth-expiry"
    ]


def make_tokens(access: str = "access-token", refresh: str = "refresh-token"):
    return OAuthToken(access_token=access, refresh_token=refresh)


def make_client_info(
    client_id: str = "client-id", client_secret: str = "client-secret"
):
    return OAuthClientInformationFull(client_id=client_id, client_secret=client_secret)


@pytest.fixture
def store() -> McpOAuthTokenStore:
    return McpOAuthTokenStore()


@pytest.fixture
async def pending():
    flows = PendingAuthorizations()
    yield flows
    # Every registered flow owns a timer task; leave none behind for the next
    # test or for the loop teardown.
    handles = list(flows._pending.values())
    for handle in handles:
        flows.cancel(handle.state)
    for handle in handles:
        await join_timer(handle)


@pytest.fixture
def failures() -> list[mcp_oauth.McpOAuthError]:
    return []


@pytest.fixture
def flow(store, pending, failures):
    return build_oauth_provider(
        storage=store.scoped("alice", SERVER_KEY),
        pending=pending,
        user_identifier="alice",
        server_url=SERVER_KEY,
        session_id="session-1",
        name="weather",
        redirect_uri=REDIRECT_URI,
        on_failure=failures.append,
    )


class TestNormalizeOrigin:
    def test_lowercases_scheme_and_host_and_drops_default_ports(self):
        assert normalize_origin("HTTPS://MCP.Example.COM:443/mcp") == Origin(
            "https", "mcp.example.com", None
        )
        assert normalize_origin("http://localhost:80/mcp") == Origin(
            "http", "localhost", None
        )

    def test_keeps_non_default_port(self):
        assert normalize_origin("https://mcp.example.com:8443/mcp") == Origin(
            "https", "mcp.example.com", 8443
        )

    def test_trailing_dot_and_idna_spellings_match(self):
        assert normalize_origin("https://mcp.example.com./") == normalize_origin(
            "https://MCP.EXAMPLE.COM/"
        )
        assert normalize_origin("https://bücher.example/") == normalize_origin(
            "https://xn--bcher-kva.example/"
        )

    def test_ip_literals_are_canonicalized(self):
        assert normalize_origin("http://127.0.0.1:8000") == Origin(
            "http", "127.0.0.1", 8000
        )
        assert normalize_origin("http://[0:0:0:0:0:0:0:1]:8000") == Origin(
            "http", "::1", 8000
        )

    def test_rejects_userinfo_without_echoing_it(self):
        with pytest.raises(ValueError, match="userinfo") as caught:
            normalize_origin("https://alice:super-secret@mcp.example.com/mcp")
        assert "super-secret" not in str(caught.value)

    def test_rejects_non_http_schemes(self):
        for url in ("file:///etc/passwd", "ftp://mcp.example.com/mcp"):
            with pytest.raises(ValueError, match="only http and https"):
                normalize_origin(url)

    def test_plain_http_is_loopback_only(self):
        assert normalize_origin("http://localhost:8000") == Origin(
            "http", "localhost", 8000
        )
        assert normalize_origin("http://[::1]") == Origin("http", "::1", None)
        with pytest.raises(ValueError, match="plain http"):
            normalize_origin("http://10.0.0.5/mcp")
        with pytest.raises(ValueError, match="plain http"):
            normalize_origin("http://mcp.example.com/mcp")


class TestCanonicalServerKey:
    def test_lowercases_scheme_and_host(self):
        assert (
            canonical_server_key("HTTPS://MCP.Example.COM/mcp")
            == "https://mcp.example.com/mcp"
        )

    def test_drops_explicit_default_port(self):
        assert canonical_server_key("https://mcp.example.com:443/mcp") == (
            canonical_server_key("https://mcp.example.com/mcp")
        )
        assert canonical_server_key("http://localhost:80/mcp") == "http://localhost/mcp"

    def test_keeps_non_default_port(self):
        assert (
            canonical_server_key("https://mcp.example.com:8443/mcp")
            == "https://mcp.example.com:8443/mcp"
        )

    def test_empty_path_becomes_slash(self):
        assert canonical_server_key("https://mcp.example.com") == (
            "https://mcp.example.com/"
        )
        assert canonical_server_key("https://mcp.example.com#fragment") == (
            "https://mcp.example.com/"
        )

    def test_trailing_slash_and_query_are_significant(self):
        base = "https://mcp.example.com"
        keys = {
            canonical_server_key(f"{base}/mcp"),
            canonical_server_key(f"{base}/mcp/"),
            canonical_server_key(f"{base}/mcp?tenant=b"),
        }
        assert keys == {
            "https://mcp.example.com/mcp",
            "https://mcp.example.com/mcp/",
            "https://mcp.example.com/mcp?tenant=b",
        }

    def test_query_is_kept_and_fragment_dropped(self):
        assert canonical_server_key(
            "https://mcp.example.com/mcp?tenant=b#fragment"
        ) == ("https://mcp.example.com/mcp?tenant=b")

    def test_userinfo_raises_instead_of_merging_tenants(self):
        for user in ("alice", "bob"):
            with pytest.raises(ValueError, match="userinfo"):
                canonical_server_key(f"https://{user}:pw@mcp.example.com/mcp")

    def test_diverges_from_sdk_resource_url_on_default_port(self):
        url = "https://mcp.example.com:443/mcp"
        assert resource_url_from_server_url(url) == "https://mcp.example.com:443/mcp"
        assert canonical_server_key(url) == "https://mcp.example.com/mcp"


class TestStoreIsolation:
    async def test_separate_entries_per_user_and_server(self, store):
        alice_mcp = store.scoped("alice", "https://mcp.example.com/mcp")
        alice_other = store.scoped("alice", "https://other.example.com/mcp")
        bob_mcp = store.scoped("bob", "https://mcp.example.com/mcp")

        await alice_mcp.set_tokens(make_tokens("alice-mcp"))
        await alice_other.set_tokens(make_tokens("alice-other"))
        await bob_mcp.set_tokens(make_tokens("bob-mcp"))

        assert (await alice_mcp.get_tokens()).access_token == "alice-mcp"
        assert (await alice_other.get_tokens()).access_token == "alice-other"
        assert (await bob_mcp.get_tokens()).access_token == "bob-mcp"

    async def test_forget_user_drops_only_that_user(self, store):
        alice = store.scoped("alice", "https://mcp.example.com/mcp")
        bob = store.scoped("bob", "https://mcp.example.com/mcp")
        await alice.set_tokens(make_tokens())
        await bob.set_tokens(make_tokens())

        store.forget_user("alice")

        assert await alice.get_tokens() is None
        assert await bob.get_tokens() is not None

    async def test_forget_server_drops_only_that_server(self, store):
        alice_mcp = store.scoped("alice", "https://mcp.example.com/mcp")
        alice_other = store.scoped("alice", "https://other.example.com/mcp")
        await alice_mcp.set_tokens(make_tokens())
        await alice_other.set_tokens(make_tokens())

        store.forget_server("alice", "https://mcp.example.com/mcp")

        assert await alice_mcp.get_tokens() is None
        assert await alice_other.get_tokens() is not None

    def test_empty_user_identifier_raises(self, store):
        for identifier in ("", "   "):
            with pytest.raises(ValueError, match="non-empty"):
                store.scoped(identifier, "https://mcp.example.com/mcp")
            with pytest.raises(ValueError, match="non-empty"):
                ScopedTokenStorage(store, identifier, "https://mcp.example.com/mcp")
            with pytest.raises(ValueError, match="non-empty"):
                store.forget_user(identifier)
            with pytest.raises(ValueError, match="non-empty"):
                store.forget_server(identifier, "https://mcp.example.com/mcp")


class TestStoreCaps:
    async def test_per_user_cap_evicts_least_recently_used(self, store):
        scopes = {}
        for index in range(mcp_oauth.MAX_ENTRIES_PER_USER):
            key = f"https://mcp{index}.example.com/mcp"
            scopes[key] = store.scoped("alice", key)
            await scopes[key].set_client_info(make_client_info(f"client-{index}"))

        oldest = "https://mcp0.example.com/mcp"
        second = "https://mcp1.example.com/mcp"
        # Reading an entry makes it recent, so the second entry is now the LRU.
        await scopes[oldest].get_client_info()

        store.evict_for_new_flow("alice")

        assert await scopes[oldest].get_client_info() is not None
        assert await scopes[second].get_client_info() is None
        assert len(store) == mcp_oauth.MAX_ENTRIES_PER_USER - 1

    async def test_total_cap_evicts_global_lru_on_write(self, store):
        per_user = mcp_oauth.MAX_ENTRIES_PER_USER
        users = mcp_oauth.MAX_ENTRIES_TOTAL // per_user
        oldest_scope = None
        newest_scope = None
        for user_index in range(users):
            for entry_index in range(per_user):
                key = f"https://mcp{entry_index}.example.com/mcp"
                scope = store.scoped(f"user-{user_index}", key)
                await scope.set_client_info(
                    make_client_info(f"client-{user_index}-{entry_index}")
                )
                if user_index == 0 and entry_index == 0:
                    oldest_scope = scope
                newest_scope = scope

        assert len(store) == mcp_oauth.MAX_ENTRIES_TOTAL
        extra = store.scoped("user-extra", "https://extra.example.com/mcp")
        await extra.set_client_info(make_client_info("client-extra"))

        assert len(store) == mcp_oauth.MAX_ENTRIES_TOTAL
        assert await oldest_scope.get_client_info() is None
        assert await newest_scope.get_client_info() is not None
        assert await extra.get_client_info() is not None

    async def test_client_info_only_entry_expires(self, store, monkeypatch):
        monkeypatch.setattr(mcp_oauth, "_CLIENT_INFO_TTL", 0.05)
        scoped = store.scoped("alice", "https://mcp.example.com/mcp")
        await scoped.set_client_info(make_client_info())

        await asyncio.sleep(0.06)

        assert await scoped.get_client_info() is None
        assert len(store) == 0

    async def test_entry_with_tokens_does_not_expire(self, store, monkeypatch):
        monkeypatch.setattr(mcp_oauth, "_CLIENT_INFO_TTL", 0.05)
        scoped = store.scoped("alice", "https://mcp.example.com/mcp")
        await scoped.set_client_info(make_client_info())
        await scoped.set_tokens(make_tokens())

        await asyncio.sleep(0.06)

        assert await scoped.get_client_info() is not None
        assert await scoped.get_tokens() is not None
        assert len(store) == 1


class TestStoreForgetClearsClientInfo:
    async def test_forget_user_clears_tokens_and_client_info(self, store):
        scoped = store.scoped("alice", "https://mcp.example.com/mcp")
        await scoped.set_tokens(make_tokens())
        await scoped.set_client_info(make_client_info())

        store.forget_user("alice")

        assert await scoped.get_tokens() is None
        assert await scoped.get_client_info() is None
        assert len(store) == 0

    async def test_forget_server_clears_tokens_and_client_info(self, store):
        scoped = store.scoped("alice", "https://mcp.example.com/mcp")
        other = store.scoped("alice", "https://other.example.com/mcp")
        await scoped.set_tokens(make_tokens())
        await scoped.set_client_info(make_client_info())
        await other.set_client_info(make_client_info("other-client"))

        store.forget_server("alice", "https://mcp.example.com/mcp")

        assert await scoped.get_tokens() is None
        assert await scoped.get_client_info() is None
        assert await other.get_client_info() is not None

    async def test_repr_does_not_contain_secrets(self, store):
        scoped = store.scoped("alice", "https://mcp.example.com/mcp")
        await scoped.set_tokens(
            make_tokens(access="secret-access", refresh="secret-refresh")
        )
        await scoped.set_client_info(
            make_client_info("client-id", "secret-client-secret")
        )
        entry = store._entries[("alice", "https://mcp.example.com/mcp")]

        for rendered in (repr(store), repr(scoped), repr(entry)):
            assert "secret-access" not in rendered
            assert "secret-refresh" not in rendered
            assert "secret-client-secret" not in rendered


class TestGenerations:
    async def test_counter_is_store_wide_and_monotonic(self, store):
        alice = store.scoped("alice", "https://mcp.example.com/mcp")
        bob = store.scoped("bob", "https://mcp.example.com/mcp")

        await alice.set_tokens(make_tokens("first"))
        assert store.generation("alice", "https://mcp.example.com/mcp") == 1

        await bob.set_tokens(make_tokens("second"))
        assert store.generation("bob", "https://mcp.example.com/mcp") == 2

        await alice.set_tokens(make_tokens("third"))
        assert store.generation("alice", "https://mcp.example.com/mcp") == 3

    async def test_client_info_write_does_not_change_generation(self, store):
        scoped = store.scoped("alice", "https://mcp.example.com/mcp")
        await scoped.set_tokens(make_tokens())
        generation = store.generation("alice", "https://mcp.example.com/mcp")

        await scoped.set_client_info(make_client_info())

        assert store.generation("alice", "https://mcp.example.com/mcp") == generation

    async def test_failed_refresh_does_not_delete_concurrent_token(self, store):
        first = store.scoped("alice", "https://mcp.example.com/mcp")
        second = store.scoped("alice", "https://mcp.example.com/mcp")

        await first.set_tokens(make_tokens("old"))
        assert (await first.get_tokens()).access_token == "old"
        assert (await second.get_tokens()).access_token == "old"

        await second.set_tokens(make_tokens("new"))
        assert first.delete_failed_token() is False

        assert (await first.get_tokens()).access_token == "new"

    async def test_failed_refresh_deletes_unchanged_token(self, store):
        scoped = store.scoped("alice", "https://mcp.example.com/mcp")
        await scoped.set_tokens(make_tokens())
        await scoped.set_client_info(make_client_info())
        assert (await scoped.get_tokens()) is not None

        assert scoped.delete_failed_token() is True

        assert await scoped.get_tokens() is None
        assert await scoped.get_client_info() is not None

    async def test_stale_generation_cannot_delete_after_lru_recreate(self, store):
        key = "https://mcp0.example.com/mcp"
        stale = store.scoped("alice", key)
        await stale.set_tokens(make_tokens("old"))
        old_generation = store.generation("alice", key)
        assert (await stale.get_tokens()).access_token == "old"

        # Fill the user's cap and read the newer entries so the token's key is
        # the least recently used entry and is evicted at the next flow start.
        scopes = {}
        for index in range(1, mcp_oauth.MAX_ENTRIES_PER_USER):
            other_key = f"https://mcp{index}.example.com/mcp"
            scopes[other_key] = store.scoped("alice", other_key)
            await scopes[other_key].set_client_info(make_client_info(f"client-{index}"))
        store.evict_for_new_flow("alice")
        assert (await stale.get_tokens()) is None

        fresh = store.scoped("alice", key)
        await fresh.set_tokens(make_tokens("new"))
        new_generation = store.generation("alice", key)
        assert new_generation > old_generation

        assert store.delete_if_unchanged("alice", key, old_generation) is False
        assert (await fresh.get_tokens()).access_token == "new"


class TestDiscardClientInfo:
    async def test_abandoned_flow_cannot_discard_newer_registration(self, store):
        key = "https://mcp.example.com/mcp"
        abandoned = store.scoped("alice", key)
        concurrent = store.scoped("alice", key)

        await abandoned.set_client_info(make_client_info("client-a"))
        abandoned_client = await abandoned.get_client_info()
        assert abandoned_client is not None
        await concurrent.set_client_info(make_client_info("client-b"))

        assert (
            store.discard_client_info("alice", key, abandoned_client.client_id) is False
        )
        current = await concurrent.get_client_info()
        assert current is not None
        assert current.client_id == "client-b"

    async def test_discard_removes_the_flow_own_registration(self, store):
        key = "https://mcp.example.com/mcp"
        scoped = store.scoped("alice", key)
        await scoped.set_client_info(make_client_info("client-a"))
        client = await scoped.get_client_info()

        assert store.discard_client_info("alice", key, client.client_id) is True
        assert await scoped.get_client_info() is None
        assert len(store) == 0

    async def test_discard_keeps_registration_next_to_a_token(self, store):
        key = "https://mcp.example.com/mcp"
        scoped = store.scoped("alice", key)
        await scoped.set_client_info(make_client_info("client-a"))
        await scoped.set_tokens(make_tokens())

        assert store.discard_client_info("alice", key, "client-a") is False
        assert await scoped.get_client_info() is not None


class TestExtractState:
    def test_reads_the_state_from_an_authorization_url(self):
        assert extract_state(make_authorization_url("abc123")) == "abc123"

    def test_returns_none_when_absent_or_blank(self):
        assert extract_state("https://auth.example.com/authorize?code=x") is None
        assert extract_state("https://auth.example.com/authorize?state=") is None


class TestPendingAuthorizations:
    async def test_resolve_delivers_the_code_to_a_parked_waiter(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0)
        pending.resolve("state", "alice", "the-code")

        result = await waiter
        assert isinstance(result, AuthorizationCodeResult)
        assert result.code == "the-code"
        assert result.state == "state"
        assert len(pending) == 0
        await join_timer(handle)
        assert expiry_tasks() == []

    async def test_code_that_lands_before_the_waiter_is_not_lost(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)
        pending.resolve("state", "alice", "the-code")

        assert (await handle.wait()).code == "the-code"
        await join_timer(handle)
        assert expiry_tasks() == []

    async def test_state_is_single_use(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)
        pending.resolve("state", "alice", "first")

        with pytest.raises(KeyError):
            pending.resolve("state", "alice", "second")
        assert (await handle.wait()).code == "first"
        await join_timer(handle)

    async def test_resolve_by_another_user_does_not_consume_the_entry(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)

        with pytest.raises(PermissionError):
            pending.resolve("state", "bob", "stolen")
        assert len(pending) == 1

        pending.resolve("state", "alice", "the-code")
        assert (await handle.wait()).code == "the-code"
        await join_timer(handle)

    async def test_expiry_wakes_the_waiter_and_leaves_nothing_behind(self):
        flows = PendingAuthorizations(ttl_seconds=0.05)
        handle = flows.register("state", "alice", SERVER_KEY)
        waiter = asyncio.create_task(handle.wait())

        with pytest.raises(McpAuthorizationTimeout):
            await waiter

        assert len(flows) == 0
        assert handle._future.done()
        await join_timer(handle)
        assert expiry_tasks() == []
        with pytest.raises(KeyError):
            flows.resolve("state", "alice", "late")

    async def test_resolve_after_the_deadline_raises_timeout(self):
        flows = PendingAuthorizations(ttl_seconds=0)
        handle = flows.register("state", "alice", SERVER_KEY)

        with pytest.raises(McpAuthorizationTimeout):
            flows.resolve("state", "alice", "too-late")

        assert len(flows) == 0
        with pytest.raises(McpAuthorizationTimeout):
            await handle.wait()
        await join_timer(handle)
        assert expiry_tasks() == []

    async def test_abandon_wakes_the_waiter_and_releases_the_entry(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0)

        assert pending.abandon("state", "alice") is True
        with pytest.raises(McpAuthorizationError):
            await waiter
        assert len(pending) == 0
        await join_timer(handle)

    async def test_abandon_by_another_user_does_not_consume_the_entry(self, pending):
        pending.register("state", "alice", SERVER_KEY)

        assert pending.abandon("state", "bob") is False
        assert len(pending) == 1
        assert pending.cancel("state") is True

    async def test_cancel_wakes_the_waiter_once_and_cannot_be_repeated(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0)

        assert pending.cancel("state") is True
        assert pending.cancel("state") is False
        with pytest.raises(McpAuthorizationError):
            await waiter
        assert pending.abandon("state", "alice") is False
        assert len(pending) == 0
        await join_timer(handle)

    async def test_resolve_wins_over_abandon(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0)

        pending.resolve("state", "alice", "the-code")
        assert pending.abandon("state", "alice") is False
        assert (await waiter).code == "the-code"
        await join_timer(handle)

    async def test_abandon_wins_over_resolve(self, pending):
        handle = pending.register("state", "alice", SERVER_KEY)
        waiter = asyncio.create_task(handle.wait())
        await asyncio.sleep(0)

        assert pending.abandon("state", "alice") is True
        with pytest.raises(KeyError):
            pending.resolve("state", "alice", "the-code")
        with pytest.raises(McpAuthorizationError):
            await waiter
        await join_timer(handle)


class TestPendingCaps:
    async def test_fourth_flow_is_refused_and_resolve_frees_the_slot(self, pending):
        handles = [
            pending.register(f"state-{index}", "alice", f"{SERVER_KEY}/{index}")
            for index in range(mcp_oauth.MAX_PENDING_PER_USER)
        ]
        with pytest.raises(McpTooManyFlows):
            pending.register("state-3", "alice", f"{SERVER_KEY}/3")

        pending.resolve("state-0", "alice", "the-code")
        replacement = pending.register("state-3", "alice", f"{SERVER_KEY}/3")
        assert len(pending) == 3

        pending.cancel_user("alice")
        for handle in [*handles, replacement]:
            await join_timer(handle)

    async def test_abandon_frees_the_slot(self, pending):
        handles = [
            pending.register(f"state-{index}", "alice", f"{SERVER_KEY}/{index}")
            for index in range(mcp_oauth.MAX_PENDING_PER_USER)
        ]
        assert pending.abandon("state-0", "alice") is True

        replacement = pending.register("state-3", "alice", f"{SERVER_KEY}/3")
        assert len(pending) == 3
        pending.cancel_user("alice")
        for handle in [*handles, replacement]:
            await join_timer(handle)

    async def test_cancel_frees_the_slot(self, pending):
        handles = [
            pending.register(f"state-{index}", "alice", f"{SERVER_KEY}/{index}")
            for index in range(mcp_oauth.MAX_PENDING_PER_USER)
        ]
        assert pending.cancel("state-0") is True

        replacement = pending.register("state-3", "alice", f"{SERVER_KEY}/3")
        assert len(pending) == 3
        pending.cancel_user("alice")
        for handle in [*handles, replacement]:
            await join_timer(handle)

    async def test_timeout_frees_the_slot(self):
        flows = PendingAuthorizations(ttl_seconds=0.02)
        handles = [
            flows.register(f"state-{index}", "alice", f"{SERVER_KEY}/{index}")
            for index in range(mcp_oauth.MAX_PENDING_PER_USER)
        ]
        with pytest.raises(McpTooManyFlows):
            flows.register("state-3", "alice", f"{SERVER_KEY}/3")

        with pytest.raises(McpAuthorizationTimeout):
            await handles[0].wait()
        for handle in handles:
            await join_timer(handle)

        replacement = flows.register("state-3", "alice", f"{SERVER_KEY}/3")
        flows.cancel_user("alice")
        await join_timer(replacement)

    async def test_the_cap_is_per_user(self, pending):
        handles = [
            pending.register(f"state-{index}", "alice", f"{SERVER_KEY}/{index}")
            for index in range(mcp_oauth.MAX_PENDING_PER_USER)
        ]
        other = pending.register("bob-state", "bob", SERVER_KEY)
        assert len(pending) == 4

        pending.cancel_user("alice")
        pending.cancel("bob-state")
        for handle in [*handles, other]:
            await join_timer(handle)


class TestCancelScopes:
    async def test_cancel_session_drops_only_that_session(self, pending):
        one = pending.register("a1", "alice", SERVER_KEY, session_id="a", name="one")
        two = pending.register("a2", "alice", SERVER_KEY, session_id="a", name="two")
        other = pending.register("b1", "alice", SERVER_KEY, session_id="b", name="one")

        assert pending.cancel_session("a") == 2
        assert len(pending) == 1
        with pytest.raises(KeyError):
            pending.resolve("a1", "alice", "code")
        with pytest.raises(KeyError):
            pending.resolve("a2", "alice", "code")
        pending.resolve("b1", "alice", "code")

        for handle in (one, two, other):
            await join_timer(handle)

    async def test_cancel_name_drops_only_that_connection(self, pending):
        one = pending.register("a1", "alice", SERVER_KEY, session_id="a", name="one")
        two = pending.register("a2", "alice", SERVER_KEY, session_id="a", name="two")
        other = pending.register("b1", "alice", SERVER_KEY, session_id="b", name="one")

        assert pending.cancel_name("a", "one") == 1
        with pytest.raises(KeyError):
            pending.resolve("a1", "alice", "code")
        pending.resolve("a2", "alice", "code")
        pending.resolve("b1", "alice", "code")

        for handle in (one, two, other):
            await join_timer(handle)

    async def test_cancel_user_drops_only_that_user(self, pending):
        alice = pending.register("a1", "alice", SERVER_KEY)
        bob = pending.register("b1", "bob", SERVER_KEY)

        assert pending.cancel_user("alice") == 1
        with pytest.raises(KeyError):
            pending.resolve("a1", "alice", "code")
        pending.resolve("b1", "bob", "code")

        for handle in (alice, bob):
            await join_timer(handle)


class TestBuildOAuthProvider:
    def test_client_metadata_is_a_web_application(self, flow):
        provider = flow.provider
        assert isinstance(provider, OAuthClientProvider)

        metadata = provider.context.client_metadata
        assert metadata.application_type == "web"
        assert [str(uri) for uri in metadata.redirect_uris] == [REDIRECT_URI]
        assert metadata.grant_types == ["authorization_code", "refresh_token"]
        assert metadata.response_types == ["code"]

    async def test_storage_is_scoped_to_the_user_and_server(self, flow, store):
        storage = flow.provider.context.storage
        await storage.set_tokens(make_tokens("alice-token"))
        await storage.set_client_info(make_client_info("alice-client"))

        alice = store.scoped("alice", SERVER_KEY)
        assert (await alice.get_tokens()).access_token == "alice-token"
        assert (await alice.get_client_info()).client_id == "alice-client"
        assert await store.scoped("bob", SERVER_KEY).get_tokens() is None
        assert (
            await store.scoped("alice", "https://other.example.com/mcp").get_tokens()
            is None
        )

    async def test_redirect_registers_the_state_and_publishes_the_url(
        self, flow, pending
    ):
        url = make_authorization_url("sdk-state")
        await flow.provider.context.redirect_handler(url)

        assert len(pending) == 1
        assert await flow.wait_for_authorization_url(timeout=0.1) == url
        pending.cancel("sdk-state")

    async def test_a_second_redirect_replaces_the_first_flow(self, store, pending):
        urls = []

        async def record(url: str) -> None:
            urls.append(url)

        flow = build_oauth_provider(
            storage=store.scoped("alice", SERVER_KEY),
            pending=pending,
            user_identifier="alice",
            server_url=SERVER_KEY,
            session_id="session-1",
            name="weather",
            redirect_uri=REDIRECT_URI,
            on_redirect=record,
        )
        first = make_authorization_url("state-1")
        second = make_authorization_url("state-2")

        await flow.provider.context.redirect_handler(first)
        await flow.provider.context.redirect_handler(second)

        assert urls == [first, second]
        assert len(pending) == 1
        with pytest.raises(KeyError):
            pending.resolve("state-1", "alice", "old-code")
        pending.resolve("state-2", "alice", "new-code")

    async def test_callback_handler_returns_an_authorization_code_result(
        self, flow, pending
    ):
        await flow.provider.context.redirect_handler(
            make_authorization_url("sdk-state")
        )
        pending.resolve("sdk-state", "alice", "the-code")

        result = await flow.provider.context.callback_handler()
        assert isinstance(result, AuthorizationCodeResult)
        assert not isinstance(result, tuple)
        assert result == AuthorizationCodeResult(code="the-code", state="sdk-state")

    async def test_callback_handler_is_bounded_by_the_flow_ttl(self, store, failures):
        flows = PendingAuthorizations(ttl_seconds=0.02)
        flow = build_oauth_provider(
            storage=store.scoped("alice", SERVER_KEY),
            pending=flows,
            user_identifier="alice",
            server_url=SERVER_KEY,
            session_id="session-1",
            name="weather",
            redirect_uri=REDIRECT_URI,
            on_failure=failures.append,
        )
        await flow.provider.context.redirect_handler(
            make_authorization_url("sdk-state")
        )

        with pytest.raises(McpAuthorizationTimeout):
            await flow.provider.context.callback_handler()

        assert len(flows) == 0
        assert isinstance(failures[-1], McpAuthorizationTimeout)

    async def test_cancelled_callback_drops_the_pending_flow(self, flow, pending):
        await flow.provider.context.redirect_handler(
            make_authorization_url("sdk-state")
        )
        callback = asyncio.create_task(flow.provider.context.callback_handler())
        await asyncio.sleep(0)

        callback.cancel()
        with pytest.raises(asyncio.CancelledError):
            await callback

        assert len(pending) == 0

    async def test_flow_cap_is_reported_to_the_caller(self, store, failures):
        flows = PendingAuthorizations()
        created = []
        for index in range(mcp_oauth.MAX_PENDING_PER_USER):
            flow = build_oauth_provider(
                storage=store.scoped("alice", SERVER_KEY),
                pending=flows,
                user_identifier="alice",
                server_url=SERVER_KEY,
                session_id="session-1",
                name=f"weather-{index}",
                redirect_uri=REDIRECT_URI,
            )
            await flow.provider.context.redirect_handler(
                make_authorization_url(f"state-{index}")
            )
            created.append(flow)

        refused = build_oauth_provider(
            storage=store.scoped("alice", SERVER_KEY),
            pending=flows,
            user_identifier="alice",
            server_url=SERVER_KEY,
            session_id="session-1",
            name="weather-4",
            redirect_uri=REDIRECT_URI,
            on_failure=failures.append,
        )
        with pytest.raises(McpTooManyFlows):
            await refused.provider.context.redirect_handler(
                make_authorization_url("state-4")
            )

        assert isinstance(failures[-1], McpTooManyFlows)
        assert len(flows) == mcp_oauth.MAX_PENDING_PER_USER
        handles = [handle._authorization for handle in created if handle._authorization]
        flows.cancel_user("alice")
        for handle in handles:
            await join_timer(handle)

    async def test_redirect_without_a_state_fails_the_flow(
        self, flow, pending, failures
    ):
        with pytest.raises(McpAuthorizationError):
            await flow.provider.context.redirect_handler(
                "https://auth.example.com/authorize?code=abc"
            )

        assert len(pending) == 0
        assert len(failures) == 1
        assert isinstance(failures[0], McpAuthorizationError)

    async def test_authorization_url_wait_is_bounded(self, flow):
        with pytest.raises(McpAuthorizationError):
            await flow.wait_for_authorization_url(timeout=0.01)


class TestRefreshFailure:
    async def test_a_failed_refresh_deletes_the_token_it_read(self, store, pending):
        storage = store.scoped("alice", SERVER_KEY)
        await storage.set_tokens(make_tokens("access-1", "refresh-1"))
        # The provider reads the token, and its generation, before refreshing.
        await storage.get_tokens()
        flow = build_oauth_provider(
            storage=storage,
            pending=pending,
            user_identifier="alice",
            server_url=SERVER_KEY,
            session_id="session-1",
            name="weather",
            redirect_uri=REDIRECT_URI,
        )

        failed = await flow.provider._handle_refresh_response(httpx2.Response(400))

        assert failed is False
        assert await storage.get_tokens() is None

    async def test_a_concurrent_successful_refresh_survives(self, store, pending):
        storage = store.scoped("alice", SERVER_KEY)
        await storage.set_tokens(make_tokens("access-1", "refresh-1"))
        await storage.get_tokens()
        flow = build_oauth_provider(
            storage=storage,
            pending=pending,
            user_identifier="alice",
            server_url=SERVER_KEY,
            session_id="session-1",
            name="weather",
            redirect_uri=REDIRECT_URI,
        )
        # Another tab rotates the token between this provider's read and its
        # failed refresh; the loser must not delete the winner's token.
        await store.scoped("alice", SERVER_KEY).set_tokens(
            make_tokens("access-2", "refresh-2")
        )

        failed = await flow.provider._handle_refresh_response(httpx2.Response(400))

        assert failed is False
        surviving = await store.scoped("alice", SERVER_KEY).get_tokens()
        assert surviving is not None
        assert surviving.access_token == "access-2"


# Origin normalization and the request-role guard (S4a).
SERVER_ORIGIN = "https://mcp.example.com"
ALLOWED_ORIGIN = "https://allowed.example"
PRM_PATH = "/.well-known/oauth-protected-resource"


class ScriptedOAuth(RedirectAwareAuth):
    """A stand-in for the SDK provider that plays a fixed list of requests.

    The SDK's own ``RedirectAwareAuth`` drives it, so redirect handling matches
    the real flow: only a redirect within the origin of the request just sent is
    followed.
    """

    def __init__(self, script: Sequence[httpx2.Request] = ()) -> None:
        self.script = list(script)

    async def _auth_flow(self, request):
        yield request
        for scripted in self.script:
            yield scripted


def mark(request: httpx2.Request, role: str) -> httpx2.Request:
    request.extensions[OAUTH_ROLE_EXTENSION] = role
    return request


def build_guard(entries=(ALLOWED_ORIGIN,), server_url: str = SERVER_KEY):
    return oauth_request_guard(server_url, OriginAllowlist(entries))


async def assert_refused(guard, request: httpx2.Request) -> None:
    with pytest.raises(McpOAuthOriginError):
        await guard(request)


def mock_handler(sent: list[httpx2.Request], routes: dict | None = None):
    """A ``MockTransport`` handler that records every request it receives."""

    async def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        response = (routes or {}).get((request.method, str(request.url)))
        if response is None:
            return httpx2.Response(404, request=request)
        if callable(response):
            return response(request)
        return response

    return handler


class TestOriginAllowlist:
    def test_equivalent_spellings_compare_equal(self):
        allowed = OriginAllowlist(["https://Allowed.Example:443"])
        for url in (
            "https://allowed.example/",
            "HTTPS://ALLOWED.EXAMPLE",
            "https://allowed.example.:443/mcp",
            "https://allowed.example",
        ):
            assert normalize_origin(url) in allowed

    def test_an_idna_spelling_matches_its_punycode_form(self):
        allowed = OriginAllowlist(["https://bücher.example"])
        assert normalize_origin("https://xn--bcher-kva.example/") in allowed

    def test_a_different_scheme_or_port_is_a_different_origin(self):
        allowed = OriginAllowlist([ALLOWED_ORIGIN])
        assert normalize_origin(ALLOWED_ORIGIN) in allowed
        assert normalize_origin("https://allowed.example:8443") not in allowed
        assert normalize_origin("https://allowed.example:444") not in allowed
        assert normalize_origin("https://allowed.example.evil.example") not in allowed

    def test_entries_must_be_bare_origins(self):
        for entry in (
            "https://allowed.example/mcp",
            "https://allowed.example?tenant=a",
            "https://allowed.example/mcp?tenant=a",
            "https://allowed.example#fragment",
        ):
            with pytest.raises(ValueError, match="bare origin") as caught:
                OriginAllowlist([entry])
            assert "allowed.example" not in str(caught.value)

    def test_a_userinfo_entry_is_rejected_instead_of_reduced(self):
        with pytest.raises(ValueError, match="userinfo") as caught:
            OriginAllowlist(["https://alice:super-secret@allowed.example"])
        assert "super-secret" not in str(caught.value)

    def test_a_non_http_entry_is_rejected(self):
        with pytest.raises(ValueError, match="only http and https"):
            OriginAllowlist(["ftp://allowed.example"])

    def test_plain_http_is_loopback_only(self):
        with pytest.raises(ValueError, match="plain http"):
            OriginAllowlist(["http://10.0.0.5"])
        with pytest.raises(ValueError, match="plain http"):
            OriginAllowlist(["https://allowed.example".replace("https", "http")])
        for entry in (
            "http://localhost:8000",
            "http://127.0.0.1:8000",
            "http://[::1]:8000",
        ):
            assert normalize_origin(entry) in OriginAllowlist([entry])

    def test_an_empty_allowlist_is_falsy(self):
        allowed = OriginAllowlist()
        assert not allowed
        assert len(allowed) == 0
        assert list(allowed) == []


class TestPrmUrls:
    def test_it_is_the_sdk_discovery_urls_plus_a_trailing_slash(self):
        assert prm_urls(SERVER_KEY) == {
            f"{SERVER_ORIGIN}{PRM_PATH}/mcp",
            f"{SERVER_ORIGIN}{PRM_PATH}/mcp/",
            f"{SERVER_ORIGIN}{PRM_PATH}",
            f"{SERVER_ORIGIN}{PRM_PATH}/",
        }

    def test_it_canonicalizes_the_server_url(self):
        assert prm_urls("https://MCP.Example.COM:443/mcp") == prm_urls(SERVER_KEY)
        assert prm_urls("https://mcp.example.com/mcp?tenant=b") == prm_urls(SERVER_KEY)

    def test_a_root_server_url_has_only_the_root_urls(self):
        assert prm_urls(SERVER_ORIGIN) == {
            f"{SERVER_ORIGIN}{PRM_PATH}",
            f"{SERVER_ORIGIN}{PRM_PATH}/",
        }


class TestOAuthRequestGuard:
    async def test_the_transport_request_may_use_any_path_on_the_server_origin(self):
        guard = build_guard()
        for method, url in (
            ("POST", SERVER_KEY),
            ("GET", f"{SERVER_ORIGIN}/other/path"),
            ("QUERY", SERVER_ORIGIN),
        ):
            await guard(mark(httpx2.Request(method, url), TRANSPORT_ROLE))

    async def test_a_transport_request_cannot_leave_the_server_origin(self):
        guard = build_guard()
        for url in (f"{ALLOWED_ORIGIN}/mcp", "https://evil.example/mcp"):
            await assert_refused(
                guard, mark(httpx2.Request("GET", url), TRANSPORT_ROLE)
            )

    async def test_an_oauth_request_may_reach_a_listed_origin(self):
        guard = build_guard()
        for method, url in (
            ("GET", f"{ALLOWED_ORIGIN}/.well-known/oauth-authorization-server"),
            ("POST", f"{ALLOWED_ORIGIN}/register"),
            ("POST", f"{ALLOWED_ORIGIN}/token"),
        ):
            await guard(mark(httpx2.Request(method, url), OAUTH_ROLE))

    async def test_an_oauth_request_cannot_reach_an_unlisted_origin(self):
        guard = build_guard()
        await assert_refused(
            guard, mark(httpx2.Request("GET", "https://evil.example/token"), OAUTH_ROLE)
        )

    async def test_the_exact_prm_urls_pass_on_the_server_origin(self):
        guard = build_guard()
        for url in sorted(prm_urls(SERVER_KEY)):
            await guard(mark(httpx2.Request("GET", url), OAUTH_ROLE))

    async def test_a_prm_request_must_be_a_get_without_a_query(self):
        guard = build_guard()
        prm = f"{SERVER_ORIGIN}{PRM_PATH}"
        await assert_refused(guard, mark(httpx2.Request("POST", prm), OAUTH_ROLE))
        await assert_refused(
            guard, mark(httpx2.Request("GET", f"{prm}?scope=a"), OAUTH_ROLE)
        )

    async def test_no_other_prm_path_passes_on_the_server_origin(self):
        guard = build_guard()
        for url in (
            f"{SERVER_ORIGIN}{PRM_PATH}/foo",
            f"{SERVER_ORIGIN}{PRM_PATH}/mcp/extra",
            f"{SERVER_ORIGIN}{PRM_PATH}/mcp//extra",
        ):
            await assert_refused(guard, mark(httpx2.Request("GET", url), OAUTH_ROLE))

    async def test_a_request_without_a_role_is_refused(self):
        guard = build_guard()
        for url in (
            SERVER_KEY,
            f"{ALLOWED_ORIGIN}/token",
            f"{SERVER_ORIGIN}{PRM_PATH}",
        ):
            await assert_refused(guard, httpx2.Request("GET", url))

    async def test_the_server_origin_is_not_an_authorization_server(self):
        guard = build_guard()
        for method, url in (
            ("GET", f"{SERVER_ORIGIN}/.well-known/oauth-authorization-server"),
            ("GET", f"{SERVER_ORIGIN}/.well-known/openid-configuration"),
            ("POST", f"{SERVER_ORIGIN}/register"),
            ("POST", f"{SERVER_ORIGIN}/token"),
            ("POST", SERVER_KEY),
            ("GET", SERVER_KEY),
        ):
            await assert_refused(guard, mark(httpx2.Request(method, url), OAUTH_ROLE))

        listed = build_guard(entries=(SERVER_ORIGIN,))
        await listed(mark(httpx2.Request("POST", f"{SERVER_ORIGIN}/token"), OAUTH_ROLE))

    async def test_the_adversarial_matrix_is_refused(self):
        guard = build_guard()
        for url in (
            "https://allowed.example@evil.example/token",
            "https://allowed.example.evil.example/token",
            "https://evil.example/token",
            "http://169.254.169.254/latest/meta-data",
            "file:///etc/passwd",
            "https://allowed.example:444/token",
        ):
            for role in (OAUTH_ROLE, TRANSPORT_ROLE):
                await assert_refused(guard, mark(httpx2.Request("GET", url), role))

    async def test_no_discovery_candidate_can_be_an_exact_prm_url(self):
        guard = build_guard()
        candidates = build_oauth_authorization_server_metadata_discovery_urls(
            f"{SERVER_ORIGIN}{PRM_PATH}/foo", SERVER_KEY
        )
        assert len(candidates) == 3
        for url in candidates:
            await assert_refused(guard, mark(httpx2.Request("GET", url), OAUTH_ROLE))

    async def test_a_rejection_logs_the_origin_only(self, caplog):
        guard = build_guard()
        with caplog.at_level("WARNING", logger="chainlit.mcp_oauth"):
            await assert_refused(
                guard,
                mark(
                    httpx2.Request(
                        "GET", "https://evil.example:8443/steal?token=secret"
                    ),
                    OAUTH_ROLE,
                ),
            )
        assert "https://evil.example:8443" in caplog.text
        assert "/steal" not in caplog.text
        assert "secret" not in caplog.text

    async def test_an_unusable_url_is_refused_without_echoing_it(self, caplog):
        guard = build_guard()
        with caplog.at_level("WARNING", logger="chainlit.mcp_oauth"):
            await assert_refused(
                guard,
                mark(
                    httpx2.Request("GET", "https://alice:pw@evil.example/token"),
                    OAUTH_ROLE,
                ),
            )
        assert "pw" not in caplog.text


class TestMarkedOAuthAuth:
    def test_it_keeps_the_sdk_need_for_response_bodies(self):
        assert MarkedOAuthAuth(ScriptedOAuth()).requires_response_body is True

    async def test_the_transport_request_is_marked_and_sdk_requests_are_not(self):
        request = httpx2.Request("POST", SERVER_KEY)
        prm = httpx2.Request("GET", f"{SERVER_ORIGIN}{PRM_PATH}")
        flow = MarkedOAuthAuth(ScriptedOAuth([prm]))
        outgoing = flow.async_auth_flow(request)

        first = await outgoing.__anext__()
        assert first is request
        assert request.extensions[OAUTH_ROLE_EXTENSION] == TRANSPORT_ROLE

        second = await outgoing.asend(httpx2.Response(401, request=first))
        assert second is prm
        assert prm.extensions[OAUTH_ROLE_EXTENSION] == OAUTH_ROLE

        with pytest.raises(StopAsyncIteration):
            await outgoing.asend(httpx2.Response(200, request=second))
        await outgoing.aclose()

    async def test_a_retried_transport_request_keeps_its_role(self):
        request = httpx2.Request("POST", SERVER_KEY)
        flow = MarkedOAuthAuth(ScriptedOAuth([request]))
        outgoing = flow.async_auth_flow(request)
        await outgoing.__anext__()

        retried = await outgoing.asend(httpx2.Response(401, request=request))

        assert retried is request
        assert request.extensions[OAUTH_ROLE_EXTENSION] == TRANSPORT_ROLE
        with pytest.raises(StopAsyncIteration):
            await outgoing.asend(httpx2.Response(200, request=retried))
        await outgoing.aclose()


class TestGuardOnTheWire:
    async def test_the_hook_sees_every_request_of_the_flow(self):
        sent: list[httpx2.Request] = []
        seen: list[tuple[str, object]] = []
        prm = httpx2.Request("GET", f"{SERVER_ORIGIN}{PRM_PATH}")
        as_metadata = httpx2.Request(
            "GET", f"{ALLOWED_ORIGIN}/.well-known/oauth-authorization-server"
        )
        token = httpx2.Request(
            "POST", f"{ALLOWED_ORIGIN}/token", data={"grant_type": "authorization_code"}
        )
        guard = build_guard()

        async def spy(request: httpx2.Request) -> None:
            seen.append(
                (str(request.url), request.extensions.get(OAUTH_ROLE_EXTENSION))
            )
            await guard(request)

        routes = {
            ("POST", SERVER_KEY): httpx2.Response(401),
            ("GET", str(prm.url)): httpx2.Response(200, json={}),
            ("GET", str(as_metadata.url)): httpx2.Response(200, json={}),
            ("POST", str(token.url)): httpx2.Response(200, json={}),
        }
        client = httpx2.AsyncClient(
            auth=MarkedOAuthAuth(ScriptedOAuth([prm, as_metadata, token])),
            event_hooks={"request": [spy]},
            transport=httpx2.MockTransport(mock_handler(sent, routes)),
        )
        async with client:
            await client.post(SERVER_KEY, json={"mcp": True})

        assert [str(request.url) for request in sent] == [
            SERVER_KEY,
            str(prm.url),
            str(as_metadata.url),
            str(token.url),
        ]
        assert seen == [
            (SERVER_KEY, TRANSPORT_ROLE),
            (str(prm.url), OAUTH_ROLE),
            (str(as_metadata.url), OAUTH_ROLE),
            (str(token.url), OAUTH_ROLE),
        ]

    async def test_an_unmarked_request_is_refused_before_it_is_sent(self):
        sent: list[httpx2.Request] = []
        client = httpx2.AsyncClient(
            event_hooks={"request": [build_guard()]},
            transport=httpx2.MockTransport(mock_handler(sent)),
        )
        async with client:
            with pytest.raises(McpOAuthOriginError):
                await client.get(f"{ALLOWED_ORIGIN}/token")

        assert sent == []

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("GET", f"{SERVER_ORIGIN}/.well-known/oauth-authorization-server"),
            ("GET", f"{SERVER_ORIGIN}/.well-known/openid-configuration"),
            ("POST", f"{SERVER_ORIGIN}/register"),
            ("POST", f"{SERVER_ORIGIN}/token"),
            ("POST", SERVER_KEY),
            ("GET", SERVER_KEY),
            ("GET", f"{SERVER_ORIGIN}/.well-known/oauth-protected-resource/foo"),
        ],
    )
    async def test_a_same_origin_authorization_request_is_refused(self, method, url):
        sent: list[httpx2.Request] = []
        refused_request = httpx2.Request(method, url)
        routes = {("POST", SERVER_KEY): httpx2.Response(401)}
        client = httpx2.AsyncClient(
            auth=MarkedOAuthAuth(ScriptedOAuth([refused_request])),
            event_hooks={"request": [build_guard()]},
            transport=httpx2.MockTransport(mock_handler(sent, routes)),
        )
        async with client:
            with pytest.raises(McpOAuthOriginError):
                await client.post(SERVER_KEY, json={})

        assert [str(request.url) for request in sent] == [SERVER_KEY]

    async def test_a_path_bearing_authorization_server_is_refused(self, store, pending):
        sent: list[httpx2.Request] = []
        prm = f"{SERVER_ORIGIN}{PRM_PATH}"
        payload = {
            "resource": SERVER_KEY,
            "authorization_servers": [f"{prm}/foo"],
        }
        routes = {
            ("POST", SERVER_KEY): httpx2.Response(401),
            ("GET", f"{prm}/mcp"): httpx2.Response(404),
            ("GET", prm): httpx2.Response(200, json=payload),
        }
        flow = build_oauth_provider(
            storage=store.scoped("alice", SERVER_KEY),
            pending=pending,
            user_identifier="alice",
            server_url=SERVER_KEY,
            session_id="session-1",
            name="weather",
            redirect_uri=REDIRECT_URI,
        )
        client = httpx2.AsyncClient(
            auth=MarkedOAuthAuth(flow.provider),
            event_hooks={"request": [build_guard()]},
            transport=httpx2.MockTransport(mock_handler(sent, routes)),
        )
        async with client:
            with pytest.raises(McpOAuthOriginError):
                await client.post(SERVER_KEY, json={})

        assert [str(request.url) for request in sent] == [
            SERVER_KEY,
            f"{prm}/mcp",
            prm,
        ]
        assert [request.method for request in sent] == ["POST", "GET", "GET"]

    async def test_a_redirect_to_another_origin_is_refused(self):
        sent: list[httpx2.Request] = []

        async def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            return httpx2.Response(
                307, headers={"Location": "https://evil.example/steal"}
            )

        client = httpx2.AsyncClient(
            auth=MarkedOAuthAuth(ScriptedOAuth()),
            event_hooks={"request": [build_guard()]},
            transport=httpx2.MockTransport(handler),
            follow_redirects=True,
        )
        async with client:
            with pytest.raises(McpOAuthOriginError):
                await client.get(SERVER_KEY)

        assert [str(request.url) for request in sent] == [SERVER_KEY]

    async def test_a_prm_redirect_to_another_origin_is_refused(self):
        sent: list[httpx2.Request] = []
        prm = f"{SERVER_ORIGIN}{PRM_PATH}"

        async def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            if str(request.url) == SERVER_KEY:
                return httpx2.Response(401, request=request)
            return httpx2.Response(
                307, headers={"Location": "https://evil.example/steal"}
            )

        client = httpx2.AsyncClient(
            auth=MarkedOAuthAuth(ScriptedOAuth([httpx2.Request("GET", prm)])),
            event_hooks={"request": [build_guard()]},
            transport=httpx2.MockTransport(handler),
            follow_redirects=True,
        )
        async with client:
            with pytest.raises(McpOAuthOriginError):
                await client.get(SERVER_KEY)

        assert [str(request.url) for request in sent] == [SERVER_KEY, prm]

    async def test_a_same_origin_prm_redirect_to_the_trailing_slash_passes(self):
        sent: list[httpx2.Request] = []
        prm = f"{SERVER_ORIGIN}{PRM_PATH}"
        payload = {"resource": SERVER_KEY, "authorization_servers": [ALLOWED_ORIGIN]}
        routes = {
            ("GET", SERVER_KEY): httpx2.Response(401),
            ("GET", prm): httpx2.Response(307, headers={"Location": f"{prm}/"}),
            ("GET", f"{prm}/"): httpx2.Response(200, json=payload),
        }
        client = httpx2.AsyncClient(
            auth=MarkedOAuthAuth(ScriptedOAuth([httpx2.Request("GET", prm)])),
            event_hooks={"request": [build_guard()]},
            transport=httpx2.MockTransport(mock_handler(sent, routes)),
        )
        async with client:
            response = await client.get(SERVER_KEY)

        assert response.status_code == 200
        assert [str(request.url) for request in sent] == [SERVER_KEY, prm, f"{prm}/"]
