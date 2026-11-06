import asyncio

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.shared.auth_utils import resource_url_from_server_url

from chainlit import mcp_oauth
from chainlit.mcp_oauth import (
    McpOAuthTokenStore,
    Origin,
    ScopedTokenStorage,
    canonical_server_key,
    normalize_origin,
)


def make_tokens(access: str = "access-token", refresh: str = "refresh-token"):
    return OAuthToken(access_token=access, refresh_token=refresh)


def make_client_info(
    client_id: str = "client-id", client_secret: str = "client-secret"
):
    return OAuthClientInformationFull(client_id=client_id, client_secret=client_secret)


@pytest.fixture
def store() -> McpOAuthTokenStore:
    return McpOAuthTokenStore()


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
