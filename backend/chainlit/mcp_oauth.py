"""Per-user OAuth state for HTTP MCP connections.

This module holds the in-memory token store, the pending-authorization map that
correlates an OAuth redirect back to the user who started it, and the factory
that adapts the MCP SDK's ``OAuthClientProvider`` to both. The callback route
and logout use the process-wide instances below; the ``/mcp`` wiring will use
the same ones.

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

Pending authorizations
----------------------
The authorization redirect leaves Chainlit and returns on a route every user
shares, so the SDK-minted ``state`` is the only correlation handle. A
``PendingAuthorization`` is single-use, expires after ``AUTHORIZATION_TIMEOUT``
and can only be completed or abandoned by the user it was registered for. One
timer task per entry wakes a parked waiter at the deadline; every terminal
outcome removes the entry, cancels the timer and settles the waiter exactly
once. A user may have at most ``MAX_PENDING_PER_USER`` flows in flight.
"""

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, NamedTuple, Optional
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)
from pydantic import AnyUrl

# Per-user entry cap. Enforced when a new flow starts (and on a new-entry write
# as a backstop), evicting that user's least recently used entry.
MAX_ENTRIES_PER_USER = 20
# Store-wide entry cap, enforced on every write by evicting the global LRU.
MAX_ENTRIES_TOTAL = 500
# How long an entry that holds only a dynamic client registration survives.
_CLIENT_INFO_TTL = 3600.0
# How long a user has to finish authorizing in the browser. It bounds both the
# pending entry and the SDK's wait for the callback.
AUTHORIZATION_TIMEOUT = 300.0
# How long the SDK gets to produce an authorization URL (discovery and dynamic
# client registration included) before the caller reports the failure.
AUTHORIZATION_URL_TIMEOUT = 30.0
# In-flight flows one user may have at once.
MAX_PENDING_PER_USER = 3


class McpOAuthError(Exception):
    """Base class for MCP OAuth flow errors."""


class McpAuthorizationError(McpOAuthError):
    """A user's authorization ended without a code: refused, cancelled or dropped."""


class McpAuthorizationTimeout(McpAuthorizationError):
    """The user did not finish authorizing before the deadline."""


class McpTooManyFlows(McpOAuthError):
    """A user already has the maximum number of flows in flight."""


# Client-visible failure details: the whole set a browser or a socket payload
# may ever see. Exception text can carry a state, a URL or a token, and a state
# mismatch names both states, so every failure maps to one of these strings.
MCP_OAUTH_UNKNOWN_STATE_DETAIL = (
    "This MCP authorization link is unknown or has expired."
)
MCP_OAUTH_OTHER_USER_DETAIL = "This MCP authorization belongs to a different user."
MCP_OAUTH_FAILED_DETAIL = "The MCP authorization failed."
MCP_OAUTH_TIMEOUT_DETAIL = "The MCP authorization was not completed in time."
MCP_OAUTH_TOO_MANY_DETAIL = (
    "Too many MCP authorizations are in progress; finish one first."
)
MCP_OAUTH_DETAILS = frozenset(
    {
        MCP_OAUTH_UNKNOWN_STATE_DETAIL,
        MCP_OAUTH_OTHER_USER_DETAIL,
        MCP_OAUTH_FAILED_DETAIL,
        MCP_OAUTH_TIMEOUT_DETAIL,
        MCP_OAUTH_TOO_MANY_DETAIL,
    }
)


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


def extract_state(authorization_url: str) -> Optional[str]:
    """Return the ``state`` the SDK embedded in an authorization URL."""
    values = parse_qs(urlsplit(authorization_url).query).get("state")
    return values[0] if values else None


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


class ChainlitOAuthProvider(OAuthClientProvider):
    """The SDK provider, plus cleanup of the token whose refresh just failed.

    ``OAuthClientProvider._handle_refresh_response`` clears only the provider's
    in-memory token and never calls ``storage``, so a rejected refresh token
    would stay in the store and every later connect would retry it. The
    override drops the stored token, but only if it is still the one this
    provider read: a token written by a concurrent successful refresh survives.
    """

    def __init__(self, *, storage: ScopedTokenStorage, **kwargs) -> None:
        self._scoped_storage = storage
        super().__init__(storage=storage, **kwargs)

    async def _handle_refresh_response(self, response) -> bool:
        refreshed = await super()._handle_refresh_response(response)
        if not refreshed:
            self._scoped_storage.delete_failed_token()
        return refreshed


class PendingAuthorization:
    """One in-flight authorization, reachable through its SDK-minted ``state``.

    ``PendingAuthorizations.register`` returns the handle, and the provider keeps
    it for the SDK flow. That matters because the callback can land before the
    SDK starts waiting: the code is then already set on ``wait()``'s future, not
    looked up in a map that a ``resolve`` has already emptied.
    """

    def __init__(
        self,
        parent: "PendingAuthorizations",
        state: str,
        user_identifier: str,
        server_key: str,
        session_id: Optional[str],
        name: Optional[str],
        expires_at: float,
        future: "asyncio.Future[AuthorizationCodeResult]",
    ) -> None:
        self.state = state
        self.user_identifier = user_identifier
        self.server_key = server_key
        self.session_id = session_id
        self.name = name
        self.expires_at = expires_at
        self._parent = parent
        self._future = future
        self._timer: Optional[asyncio.Task[None]] = None

    def __repr__(self) -> str:
        # The state is a bearer-like secret: keep it out of logs and tracebacks.
        return (
            f"PendingAuthorization(user_identifier={self.user_identifier!r}, "
            f"server_key={self.server_key!r}, name={self.name!r})"
        )

    async def wait(self) -> AuthorizationCodeResult:
        """Wait for the authorization code, at most until the deadline.

        The wait is bounded by the flow's remaining lifetime, so a waiter parked
        after the deadline wakes immediately and one parked before it wakes at
        the deadline even if the timer task has not run yet.
        """
        remaining = self.expires_at - time.monotonic()
        try:
            return await asyncio.wait_for(self._future, remaining)
        except asyncio.TimeoutError:
            # ``wait_for`` cancelled the future, so this only drops the entry and
            # its timer; the single-use guarantee is the pop inside ``_finish``.
            self._parent._expire(self.state)
            raise McpAuthorizationTimeout(
                "The MCP authorization was not completed in time."
            ) from None

    def _complete(self, result: AuthorizationCodeResult) -> None:
        if not self._future.done():
            self._future.set_result(result)

    def _fail(self, exc: McpAuthorizationError) -> None:
        if not self._future.done():
            self._future.set_exception(exc)
            # Mark the exception retrieved: flows nobody waits on (logout,
            # session teardown, cap cleanup) would otherwise log it at GC.
            # Awaiting the future later still raises it.
            self._future.exception()

    def _stop_timer(self) -> None:
        if self._timer is not None and not self._timer.done():
            self._timer.cancel()


class PendingAuthorizations:
    """Correlates an OAuth redirect back to the user who started it.

    The redirect leaves Chainlit and returns on a route every user shares, so
    the ``state`` the SDK minted is the only link to the originating user.
    Resolving a callback requires the state to be known *and* to belong to the
    user presenting it, otherwise one user could complete another's flow and
    have the resulting token stored against them.

    States are single-use, expire after ``ttl_seconds`` (a timer task wakes the
    parked waiter) and are capped per user, so neither a leaked redirect URL nor
    an abandoned flow accumulates.
    """

    def __init__(
        self,
        ttl_seconds: float = AUTHORIZATION_TIMEOUT,
        max_per_user: int = MAX_PENDING_PER_USER,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_per_user = max_per_user
        self._pending: dict[str, PendingAuthorization] = {}

    def register(
        self,
        state: str,
        user_identifier: str,
        server_key: str,
        *,
        session_id: Optional[str] = None,
        name: Optional[str] = None,
    ) -> PendingAuthorization:
        """Record the owner of an in-flight authorization and start its timer.

        ``state`` is generated by the SDK and already embedded in the
        authorization URL, so it is passed in rather than minted here: the SDK
        compares the returned state against its own and would reject anything
        else. Await the returned handle to get the authorization code.
        """
        if not state:
            raise ValueError("an authorization state must be a non-empty string")
        _validate_user(user_identifier)
        _validate_server_key(server_key)
        self._sweep()
        if state in self._pending:
            raise ValueError("authorization state is already pending")
        if self._flow_count(user_identifier) >= self._max_per_user:
            raise McpTooManyFlows(
                f"MCP OAuth already has {self._max_per_user} flows in flight "
                "for this user; finish or cancel one first."
            )

        expires_at = time.monotonic() + self._ttl
        handle = PendingAuthorization(
            self,
            state,
            user_identifier,
            server_key,
            session_id,
            name,
            expires_at,
            asyncio.get_running_loop().create_future(),
        )
        self._pending[state] = handle
        handle._timer = asyncio.create_task(
            self._expire_after(state, expires_at), name="mcp-oauth-expiry"
        )
        return handle

    def resolve(self, state: str, user_identifier: str, code: str) -> None:
        """Complete the flow named by ``state`` on behalf of ``user_identifier``.

        Raises ``KeyError`` when the state is unknown or already used,
        ``McpAuthorizationTimeout`` when it expired, and ``PermissionError``
        when it belongs to a different user. The entry is only consumed on
        success, so a mistaken or malicious resolve leaves the owner's flow
        intact.
        """
        handle = self._pending.get(state)
        if handle is None:
            raise KeyError("Unknown or expired MCP OAuth state.")
        if handle.expires_at <= time.monotonic():
            exc = McpAuthorizationTimeout("The MCP authorization expired.")
            self._finish(state, exc=exc)
            raise exc
        if handle.user_identifier != user_identifier:
            # Do not consume it: the rightful owner may still complete it.
            raise PermissionError("MCP OAuth state belongs to a different user.")
        self._finish(state, result=AuthorizationCodeResult(code=code, state=state))

    def abandon(
        self,
        state: str,
        user_identifier: str,
        reason: str = "The MCP authorization was not completed.",
    ) -> bool:
        """Drop a flow its owner gave up on, releasing the waiting caller.

        Ownership is checked for the same reason ``resolve`` checks it: without
        it, anyone holding a state could cancel someone else's connection.
        Returns whether anything was dropped.
        """
        handle = self._pending.get(state)
        if handle is None or handle.user_identifier != user_identifier:
            return False
        self._finish(state, exc=McpAuthorizationError(reason))
        return True

    def cancel(
        self, state: str, reason: str = "The MCP authorization was cancelled."
    ) -> bool:
        """Drop one flow without an ownership check, e.g. when its session ends."""
        return self._finish(state, exc=McpAuthorizationError(reason))

    def cancel_session(
        self, session_id: str, reason: str = "The MCP session was closed."
    ) -> int:
        """Drop every flow started by one websocket session."""
        return self._cancel_where(
            lambda handle: handle.session_id == session_id, reason
        )

    def cancel_name(
        self,
        session_id: str,
        name: str,
        reason: str = "The MCP connection was removed.",
    ) -> int:
        """Drop the flow for one connection name of one websocket session."""
        return self._cancel_where(
            lambda handle: handle.session_id == session_id and handle.name == name,
            reason,
        )

    def cancel_user(
        self, user_identifier: str, reason: str = "The user logged out."
    ) -> int:
        """Drop every flow a user has in flight."""
        return self._cancel_where(
            lambda handle: handle.user_identifier == user_identifier, reason
        )

    def __len__(self) -> int:
        self._sweep()
        return len(self._pending)

    def _flow_count(self, user_identifier: str) -> int:
        return sum(
            1
            for handle in self._pending.values()
            if handle.user_identifier == user_identifier
        )

    def _cancel_where(
        self, predicate: Callable[[PendingAuthorization], bool], reason: str
    ) -> int:
        states = [state for state, handle in self._pending.items() if predicate(handle)]
        for state in states:
            self._finish(state, exc=McpAuthorizationError(reason))
        return len(states)

    def _expire(self, state: str) -> bool:
        return self._finish(
            state, exc=McpAuthorizationTimeout("The MCP authorization expired.")
        )

    def _finish(
        self,
        state: str,
        *,
        result: Optional[AuthorizationCodeResult] = None,
        exc: Optional[McpAuthorizationError] = None,
        stop_timer: bool = True,
    ) -> bool:
        """Remove one entry and settle its waiter, at most once.

        The pop is the single-use gate: concurrent resolve, abandon, cancel and
        expiry paths all funnel here, and only the first finds the entry.
        """
        handle = self._pending.pop(state, None)
        if handle is None:
            return False
        if stop_timer:
            handle._stop_timer()
        if exc is not None:
            handle._fail(exc)
        elif result is not None:
            handle._complete(result)
        return True

    def _sweep(self) -> None:
        """Expire entries whose timer task has not run yet."""
        now = time.monotonic()
        for state in [
            state for state, handle in self._pending.items() if handle.expires_at <= now
        ]:
            self._expire(state)

    async def _expire_after(self, state: str, expires_at: float) -> None:
        delay = expires_at - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        # The timer is this task, so do not cancel it from inside itself.
        self._finish(
            state,
            exc=McpAuthorizationTimeout("The MCP authorization expired."),
            stop_timer=False,
        )


class McpOAuthFlow:
    """The SDK OAuth provider for one flow plus the handles Chainlit needs.

    ``provider`` is the ``httpx2.Auth`` the MCP transport is built with. The SDK
    calls ``redirect_handler`` once it has an authorization URL (discovery and
    dynamic client registration are done) and ``callback_handler`` to wait for
    the code; ``wait_for_authorization_url`` lets the caller learn about the URL
    without racing the SDK.
    """

    def __init__(
        self,
        *,
        storage: ScopedTokenStorage,
        pending: PendingAuthorizations,
        user_identifier: str,
        server_url: str,
        session_id: str,
        name: str,
        redirect_uri: str,
        client_name: str = "Chainlit",
        on_redirect: Optional[Callable[[str], Awaitable[None]]] = None,
        on_failure: Optional[Callable[[McpOAuthError], None]] = None,
    ) -> None:
        self._pending = pending
        self._user_identifier = user_identifier
        self._server_key = canonical_server_key(server_url)
        self._session_id = session_id
        self._name = name
        self._on_redirect = on_redirect
        self._on_failure = on_failure
        self._authorization_url: Optional[str] = None
        self._authorization_url_ready = asyncio.Event()
        self._authorization: Optional[PendingAuthorization] = None

        self.provider = ChainlitOAuthProvider(
            server_url=server_url,
            client_metadata=OAuthClientMetadata(
                client_name=client_name,
                redirect_uris=[AnyUrl(redirect_uri)],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                application_type="web",
            ),
            storage=storage,
            redirect_handler=self._redirect,
            callback_handler=self._callback,
        )

    async def _redirect(self, authorization_url: str) -> None:
        state = extract_state(authorization_url)
        if state is None:
            exc = McpAuthorizationError(
                "The MCP authorization URL carries no state parameter."
            )
            self._report_failure(exc)
            raise exc
        # A re-authorization inside one connection replaces its own flow; only
        # one entry per connection may be pending, or repeated failures would
        # eat the user's cap.
        previous = self._authorization
        if previous is not None:
            self._pending.cancel(previous.state, "Replaced by a new MCP authorization.")
            self._authorization = None
        try:
            self._authorization = self._pending.register(
                state,
                self._user_identifier,
                self._server_key,
                session_id=self._session_id,
                name=self._name,
            )
        except McpOAuthError as exc:
            self._report_failure(exc)
            raise
        self._authorization_url = authorization_url
        self._authorization_url_ready.set()
        if self._on_redirect is not None:
            await self._on_redirect(authorization_url)

    async def _callback(self) -> AuthorizationCodeResult:
        authorization = self._authorization
        if authorization is None:  # pragma: no cover - the SDK redirects first
            exc = McpAuthorizationError(
                "MCP authorization was awaited before it started."
            )
            self._report_failure(exc)
            raise exc
        try:
            return await authorization.wait()
        except McpAuthorizationError as exc:
            self._report_failure(exc)
            raise
        except asyncio.CancelledError:
            # The connection gave up first (timeout, teardown). Drop the flow so
            # a late callback is refused instead of completing into nothing.
            self._pending.cancel(
                authorization.state, "The MCP connection was cancelled."
            )
            raise

    async def wait_for_authorization_url(
        self, timeout: float = AUTHORIZATION_URL_TIMEOUT
    ) -> str:
        """Wait for the SDK to produce the sign-in URL, bounded by ``timeout``.

        Discovery and dynamic client registration can stall on a slow server;
        the caller must fail the connection rather than hold it open.
        """
        if self._authorization_url is None:
            try:
                await asyncio.wait_for(self._authorization_url_ready.wait(), timeout)
            except asyncio.TimeoutError:
                raise McpAuthorizationError(
                    "The MCP authorization server did not provide a sign-in URL."
                ) from None
        url = self._authorization_url
        if url is None:  # pragma: no cover - the event is only set with the URL
            raise McpAuthorizationError(
                "The MCP authorization server did not provide a sign-in URL."
            )
        return url

    def _report_failure(self, exc: McpOAuthError) -> None:
        if self._on_failure is not None:
            self._on_failure(exc)


def build_oauth_provider(
    *,
    storage: ScopedTokenStorage,
    pending: PendingAuthorizations,
    user_identifier: str,
    server_url: str,
    session_id: str,
    name: str,
    redirect_uri: str,
    client_name: str = "Chainlit",
    on_redirect: Optional[Callable[[str], Awaitable[None]]] = None,
    on_failure: Optional[Callable[[McpOAuthError], None]] = None,
) -> McpOAuthFlow:
    """Build the SDK OAuth provider for one user's MCP connection.

    The SDK owns discovery, dynamic client registration, PKCE, ``state``
    generation and refresh; this function only supplies what it cannot know:
    which user the flow belongs to, where the browser comes back, and how to
    reach the caller.

    Client metadata is fixed to what a browser-based server needs: ``web`` as
    the application type (the SDK defaults to ``native``), the given
    ``redirect_uri`` verbatim, and the authorization-code and refresh grants.
    The callback handler returns ``AuthorizationCodeResult``, which is the mcp
    2.x contract. ``on_redirect`` is awaited with every authorization URL the
    SDK produces, so the caller can emit it to the browser (a re-authorization
    inside one connection produces another). ``on_failure`` is called when the
    flow ends without a code, because the SDK's transports swallow exceptions
    raised from the auth flow and the connection would otherwise wait out its
    timeout.
    """
    return McpOAuthFlow(
        storage=storage,
        pending=pending,
        user_identifier=user_identifier,
        server_url=server_url,
        session_id=session_id,
        name=name,
        redirect_uri=redirect_uri,
        client_name=client_name,
        on_redirect=on_redirect,
        on_failure=on_failure,
    )


# One store and one pending map per process: nothing is persisted, so a restart
# forces re-authorization. The routes reach them through these getters so the
# callback, logout and the /mcp wiring cannot drift onto different instances.
_token_store = McpOAuthTokenStore()
_pending_authorizations = PendingAuthorizations()


def get_token_store() -> McpOAuthTokenStore:
    return _token_store


def get_pending_authorizations() -> PendingAuthorizations:
    return _pending_authorizations
