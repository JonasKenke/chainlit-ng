"""Per-user OAuth state for HTTP MCP connections.

This module currently contains the in-memory token store only. The OAuth flow
wiring (pending authorizations, provider factory, callback route) lands on top of
it; nothing here is reachable from the rest of Chainlit yet.

Tokens and dynamic-client-registration data live in one process-wide
``McpOAuthTokenStore`` keyed by ``(user identifier, canonical server key)``.
Nothing is persisted: a restart forces re-authorization. The SDK's
``TokenStorage`` protocol has no user argument, so isolation comes from handing
the SDK a ``ScopedTokenStorage`` fixed to one key.

Server keys
-----------
``canonical_server_key`` lowercases scheme and host, drops default ports,
maps an empty path to ``/``, keeps the path verbatim (a trailing slash is
significant) and the query, and drops the fragment. URLs carrying userinfo are
rejected rather than reduced: dropping it would merge two tenants that the SDK's
RFC 8707 ``resource`` value still treats as distinct.

This intentionally diverges from the SDK's ``resource_url_from_server_url``: the
SDK keeps an explicit default port (``https://host:443/mcp`` stays as written),
while this store drops it so two spellings of the same server share one token.
The divergence is harmless because the SDK value is only the audience parameter
of the token request, and it is pinned by a test.

Refresh races
-------------
The store keeps one monotonic counter for all keys. Each token write stamps the
entry with the next value; ``set_client_info`` never changes it. A scoped view
remembers the stamp it saw at ``get_tokens``. On refresh failure,
``delete_if_unchanged`` compares the current stamp and does nothing when another
provider has written a newer token in the meantime. Because the counter is
store-wide, an LRU eviction followed by a re-creation of the same key cannot
reuse an older value either.
"""

import ipaddress
import time
from dataclasses import dataclass
from typing import NamedTuple, Optional
from urllib.parse import urlsplit

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

# Per-user entry cap. Enforced when a new flow starts (and on a new-entry write
# as a backstop), evicting that user's least recently used entry.
MAX_ENTRIES_PER_USER = 20
# Store-wide entry cap, enforced on every write by evicting the global LRU.
MAX_ENTRIES_TOTAL = 500
# How long an entry that holds only a dynamic client registration survives.
_CLIENT_INFO_TTL = 3600.0


class Origin(NamedTuple):
    """Normalized ``(scheme, host, port)`` triple used for exact comparison."""

    scheme: str
    host: str
    port: Optional[int]


def _validate_user(identifier: str) -> None:
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("user identifier must be a non-empty string")


def _validate_server_key(server_key: str) -> None:
    if not isinstance(server_key, str) or not server_key.strip():
        raise ValueError("server key must be a non-empty string")


def normalize_origin(url: str) -> Origin:
    """Return the exact origin of an ``http(s)`` URL for comparison.

    The host is lowercased, at most one trailing dot is stripped and
    internationalized names are IDNA-encoded, so equivalent spellings compare
    equal. IP literals are canonicalized with :mod:`ipaddress`. Default ports are
    mapped to ``None``. URLs with userinfo, non-``http(s)`` schemes, and plain
    ``http`` outside loopback are rejected. The error messages never echo the
    URL, so a URL that carries credentials cannot leak through an exception.
    """
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are supported")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs with userinfo are not supported")

    host = parsed.hostname
    if not host:
        raise ValueError("URL must include a host")
    host = host.lower().removesuffix(".")

    is_ip = False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("URL host is not a valid hostname") from exc
    else:
        host = address.compressed
        is_ip = True

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL port is invalid") from exc
    if (scheme, port) in (("http", 80), ("https", 443)):
        port = None

    if scheme == "http" and host != "localhost" and not (is_ip and address.is_loopback):
        raise ValueError("plain http is only supported for loopback hosts")

    return Origin(scheme, host, port)


def _format_origin(origin: Origin) -> str:
    host = f"[{origin.host}]" if ":" in origin.host else origin.host
    return host if origin.port is None else f"{host}:{origin.port}"


def canonical_server_key(url: str) -> str:
    """Return the store key for an MCP server URL.

    The key is the normalized origin plus the path exactly as written (an empty
    path becomes ``/``, a trailing slash stays significant) plus the query, if
    any. The fragment is dropped. A URL with userinfo raises instead of silently
    merging with another tenant.

    Unlike the SDK's ``resource_url_from_server_url``, an explicit default port
    is dropped: ``https://host:443/mcp`` and ``https://host/mcp`` share one key
    here. The SDK keeps it in the RFC 8707 ``resource`` parameter, which is
    harmless; this divergence is pinned by a test.
    """
    origin = normalize_origin(url)
    parsed = urlsplit(url)
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{origin.scheme}://{_format_origin(origin)}{path}{query}"


@dataclass(repr=False)
class _Entry:
    tokens: Optional[OAuthToken] = None
    client_info: Optional[OAuthClientInformationFull] = None
    generation: int = 0
    last_used: int = 0
    client_info_at: Optional[float] = None

    def __repr__(self) -> str:
        # Never let a default dataclass repr leak a token or a client secret.
        return (
            f"_Entry(has_tokens={self.tokens is not None}, "
            f"has_client_info={self.client_info is not None}, "
            f"generation={self.generation})"
        )


class McpOAuthTokenStore:
    """Process-wide, in-memory store for per-user MCP OAuth credentials.

    Entries are reached through :meth:`scoped`, which fixes the ``(user,
    server)`` pair for the SDK. The entry cap and the client-info-only TTL are
    enforced here; eviction order is least recently used, where reads and writes
    both count as use.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._generations = 0
        self._access = 0

    def __repr__(self) -> str:
        return f"McpOAuthTokenStore(entries={len(self._entries)})"

    def __len__(self) -> int:
        return len(self._entries)

    def scoped(self, user_identifier: str, server_key: str) -> "ScopedTokenStorage":
        """Return a ``TokenStorage`` view fixed to one user and one server."""
        _validate_user(user_identifier)
        _validate_server_key(server_key)
        return ScopedTokenStorage(self, user_identifier, server_key)

    def forget_user(self, user_identifier: str) -> None:
        """Drop every entry (tokens and client info) belonging to a user."""
        _validate_user(user_identifier)
        for key in [key for key in self._entries if key[0] == user_identifier]:
            del self._entries[key]

    def forget_server(self, user_identifier: str, server_key: str) -> None:
        """Drop one user's entry for one server, tokens and client info."""
        _validate_user(user_identifier)
        self._entries.pop((user_identifier, server_key), None)

    def evict_for_new_flow(self, user_identifier: str) -> None:
        """Make room for a new flow by evicting the user's LRU entry.

        Called before a flow starts so a user at the cap can always begin a new
        authorization instead of failing with a full store.
        """
        _validate_user(user_identifier)
        self._evict_user_lru(user_identifier)

    def generation(self, user_identifier: str, server_key: str) -> int:
        """Return the token generation stamped on an entry (0 when absent)."""
        _validate_user(user_identifier)
        entry = self._peek(user_identifier, server_key)
        return entry.generation if entry is not None else 0

    def delete_if_unchanged(
        self, user_identifier: str, server_key: str, generation: int
    ) -> bool:
        """Delete the stored token only if its generation is still ``generation``.

        A failed refresh calls this with the generation it read; a newer token
        written by a concurrent successful refresh makes the generations differ,
        so the newer token survives. ``client_info`` is kept: the dynamic client
        registration is still usable and expires on its own TTL once token-less.
        """
        _validate_user(user_identifier)
        entry = self._peek(user_identifier, server_key)
        if entry is None or entry.tokens is None or entry.generation != generation:
            return False
        entry.tokens = None
        entry.generation = 0
        if entry.client_info is None:
            del self._entries[(user_identifier, server_key)]
        return True

    def discard_client_info(
        self, user_identifier: str, server_key: str, client_id: str
    ) -> bool:
        """Drop a client registration only if it is still the one identified.

        An abandoned flow calls this with the ``client_id`` it read before its
        authorization attempt. A concurrent flow that registered a newer client
        between that read and this call keeps its registration. Registrations
        sitting next to a token are kept; the token still needs them.
        """
        _validate_user(user_identifier)
        entry = self._peek(user_identifier, server_key)
        if (
            entry is None
            or entry.tokens is not None
            or entry.client_info is None
            or entry.client_info.client_id != client_id
        ):
            return False
        entry.client_info = None
        entry.client_info_at = None
        del self._entries[(user_identifier, server_key)]
        return True

    def purge_expired(self, now: Optional[float] = None) -> int:
        """Remove client-info-only entries past the TTL and return the count."""
        if now is None:
            now = time.monotonic()
        expired = [
            key for key, entry in self._entries.items() if self._is_expired(entry, now)
        ]
        for key in expired:
            del self._entries[key]
        return len(expired)

    def _peek(self, user_identifier: str, server_key: str) -> Optional[_Entry]:
        key = (user_identifier, server_key)
        entry = self._entries.get(key)
        if entry is not None and self._is_expired(entry, time.monotonic()):
            del self._entries[key]
            return None
        return entry

    def _touch(self, entry: _Entry) -> None:
        self._access += 1
        entry.last_used = self._access

    def _get_entry(self, user_identifier: str, server_key: str) -> Optional[_Entry]:
        entry = self._peek(user_identifier, server_key)
        if entry is not None:
            self._touch(entry)
        return entry

    def _set_entry(self, user_identifier: str, server_key: str) -> _Entry:
        key = (user_identifier, server_key)
        entry = self._peek(user_identifier, server_key)
        if entry is None:
            self._evict_user_lru(user_identifier)
            entry = _Entry()
            self._entries[key] = entry
            self._touch(entry)
            self._evict_global_lru()
        else:
            self._touch(entry)
        return entry

    def _evict_user_lru(self, user_identifier: str) -> None:
        while True:
            candidates = [
                (key, entry)
                for key, entry in self._entries.items()
                if key[0] == user_identifier
            ]
            if len(candidates) < MAX_ENTRIES_PER_USER:
                return
            key, _ = min(candidates, key=lambda item: item[1].last_used)
            del self._entries[key]

    def _evict_global_lru(self) -> None:
        while len(self._entries) > MAX_ENTRIES_TOTAL:
            key, _ = min(self._entries.items(), key=lambda item: item[1].last_used)
            del self._entries[key]

    @staticmethod
    def _is_expired(entry: _Entry, now: float) -> bool:
        return (
            entry.tokens is None
            and entry.client_info is not None
            and entry.client_info_at is not None
            and now - entry.client_info_at >= _CLIENT_INFO_TTL
        )

    def _read_tokens(
        self, user_identifier: str, server_key: str
    ) -> Optional[OAuthToken]:
        entry = self._get_entry(user_identifier, server_key)
        return entry.tokens if entry is not None else None

    def _write_tokens(
        self, user_identifier: str, server_key: str, tokens: OAuthToken
    ) -> int:
        entry = self._set_entry(user_identifier, server_key)
        self._generations += 1
        entry.tokens = tokens
        entry.generation = self._generations
        return entry.generation

    def _read_client_info(
        self, user_identifier: str, server_key: str
    ) -> Optional[OAuthClientInformationFull]:
        entry = self._get_entry(user_identifier, server_key)
        return entry.client_info if entry is not None else None

    def _write_client_info(
        self,
        user_identifier: str,
        server_key: str,
        client_info: OAuthClientInformationFull,
    ) -> None:
        entry = self._set_entry(user_identifier, server_key)
        entry.client_info = client_info
        entry.client_info_at = time.monotonic()


class ScopedTokenStorage:
    """The SDK's ``TokenStorage`` for exactly one ``(user, server)`` pair.

    The SDK never sees the user identifier; it only sees this view, so one
    user's provider cannot read or write another user's credential.
    """

    def __init__(
        self,
        store: McpOAuthTokenStore,
        user_identifier: str,
        server_key: str,
    ) -> None:
        _validate_user(user_identifier)
        _validate_server_key(server_key)
        self._store = store
        self._user_identifier = user_identifier
        self._server_key = server_key
        self._generation = 0

    def __repr__(self) -> str:
        return (
            f"ScopedTokenStorage(user_identifier={self._user_identifier!r}, "
            f"server_key={self._server_key!r})"
        )

    async def get_tokens(self) -> Optional[OAuthToken]:
        tokens = self._store._read_tokens(self._user_identifier, self._server_key)
        self._generation = self._store.generation(
            self._user_identifier, self._server_key
        )
        return tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._generation = self._store._write_tokens(
            self._user_identifier, self._server_key, tokens
        )

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        return self._store._read_client_info(self._user_identifier, self._server_key)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._store._write_client_info(
            self._user_identifier, self._server_key, client_info
        )

    def delete_failed_token(self) -> bool:
        """Delete the token this view read only if nobody replaced it."""
        return self._store.delete_if_unchanged(
            self._user_identifier, self._server_key, self._generation
        )
