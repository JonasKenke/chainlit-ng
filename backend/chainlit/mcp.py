import ipaddress
import os
import re
import socket
from typing import Callable, Dict, Literal, Optional, Union
from urllib.parse import unquote, urlsplit

import anyio
import httpx2
from pydantic import BaseModel


class StdioMcpConnection(BaseModel):
    name: str
    command: str
    args: list[str]
    clientType: Literal["stdio"] = "stdio"


class SseMcpConnection(BaseModel):
    name: str
    url: str
    headers: Optional[Dict[str, str]] = None
    clientType: Literal["sse"] = "sse"


class HttpMcpConnection(BaseModel):
    name: str
    url: str
    headers: Optional[Dict[str, str]] = None
    clientType: Literal["streamable-http"] = "streamable-http"


McpConnection = Union[StdioMcpConnection, SseMcpConnection, HttpMcpConnection]

# Headers that must never be forwarded from the browser to the MCP transport.
#
# Note: ``Authorization`` is deliberately absent — forwarding a user-supplied
# token is the point of user-provided servers. The allowlist scopes where it
# can be sent, and redirects are disabled so that destination cannot move.
_RESTRICTED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "upgrade",
        "proxy-authorization",
        "te",
        "trailer",
        # Session credentials — must not be replayed to a third-party server.
        "cookie",
        # Spoofable client/origin identity, used to bypass upstream ACLs.
        "forwarded",
        "via",
        "x-forwarded-by",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
        "x-forwarded-server",
        "x-forwarded-user",
        "x-forwarded-email",
        "x-forwarded-groups",
        "x-forwarded-prefix",
        "x-forwarded-client-cert",
        "x-original-host",
        "x-real-ip",
        "x-scheme",
        # Routing/method overrides honoured by some proxies and frameworks,
        # which can reach endpoints the literal request line would not.
        "x-http-method-override",
        "x-method-override",
        "x-original-url",
        "x-rewrite-url",
    }
)

# Prefixes for proxy/edge identity headers, matched after folding ``_`` to ``-``
# so underscore spellings cannot slip past the exact-name set above.
_RESTRICTED_HEADER_PREFIXES = ("x-forwarded-", "x-original-", "x-rewrite-")

# POSITIVE path grammar: RFC 3986 pchar plus '/', and percent triplets are
# validated separately below. Enumerating dangerous sequences kept coming up
# short (``..;``, ``..%00``, IIS ``%u002e``), so only characters that cannot
# change path meaning at an origin are accepted. ``;`` (path parameters) and
# controls are deliberately absent.
_PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9\-._~!$&'()*+,=:@/%]*$")
_PCT_RE = re.compile(r"%[0-9A-Fa-f]{2}")


def _effective_port(parsed) -> int:
    """Return the TCP port for a parsed URL, inferring defaults for http/https."""
    if parsed.port is not None:
        return parsed.port
    return 443 if parsed.scheme == "https" else 80


def _path_matches(req_path: str, allowed_path: str) -> bool:
    """True if req_path equals or is a sub-path of allowed_path.

    Normalises allowed_path to end with '/' before the startswith check so
    that '/v1' does not accidentally match '/v1-evil'.

    Note: if allowed_path is empty (i.e. the allowlist entry is an origin with
    no path component, e.g. "https://example.com"), all sub-paths on that host
    are permitted. Use a path-restricted entry (e.g. "https://example.com/api")
    to limit access to a sub-tree.

    A '*' in allowed_path matches one or more characters other than '/', so it
    never moves the match into a different segment. The matched segment's
    sub-tree is still permitted, as for a literal path: '/tenant/*' covers
    '/tenant/alice' and '/tenant/alice/messages', but '/tenant/*/mcp' does not
    cover '/tenant/alice/other/mcp'.
    """
    norm = allowed_path.rstrip("/")
    if "*" in norm:
        regex = re.escape(norm).replace(r"\*", "[^/]+")
        return re.fullmatch(rf"{regex}(?:/.*)?", req_path) is not None
    return req_path == norm or req_path.startswith(norm + "/")


def _host_matches(host: str, allowed_host: str) -> bool:
    """True if host matches an allowlist host, which may start with '*.'.

    The only supported host wildcard is a single leading label: '*.' covers one
    or more subdomain labels (so 'a.b.example.com' matches '*.example.com'), but
    not the apex 'example.com'. The suffix is anchored, so
    'example.com.evil.com' cannot match either.
    """
    if "*" not in allowed_host:
        return host == allowed_host

    # A wildcard is for DNS names; never let it match an IP literal, even when
    # an operator writes a wildcard over an IP-shaped suffix.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False

    suffix = allowed_host[len("*.") :]
    return (
        re.fullmatch(rf"(?:[^.]+\.)+{re.escape(suffix)}", host, re.IGNORECASE)
        is not None
    )


def _has_ambiguous_path(raw_path: str) -> bool:
    """True if raw_path contains constructs that could change meaning in transit.

    We reject rather than normalise. Decoding first would let ``%2f`` create
    path-segment boundaries that were not in the original URI, manufacturing a
    passing path from a malicious one, and re-implementing RFC 3986 dot-segment
    removal would leave us permanently obliged to match httpx2 byte for byte.
    None of these constructs belong in a configured MCP endpoint.

    Deliberately operates on the *raw* percent-encoded path rather than
    ``httpx2.URL(url).path``: httpx2 pre-decodes ``%2e``/``%2f``, which would
    make the marker check below blind to exactly the sequences it exists to
    catch.
    """
    lowered = raw_path.lower()
    # %2e -> '.', %2f -> '/', %5c -> '\', %25 -> a second decoding pass
    # downstream. Backslash is a separator on some origin servers and is left
    # untouched by httpx2.
    if any(marker in lowered for marker in ("%2e", "%2f", "%5c", "%25")):
        return True
    if "\\" in raw_path:
        return True
    if any(segment in (".", "..") for segment in raw_path.split("/")):
        return True

    # Non-ASCII characters are rejected outright, before and after a single
    # decode. Many code points fold to '.', '/' or '\' under the normalisation
    # some origin servers and proxies apply (U+FF0E, U+2024, U+FF0F, U+2215,
    # U+2044, ...), which would reopen traversal at the destination. Enumerating
    # them is a denylist that keeps coming up short, so we require the path to
    # be ASCII instead — a positive rule, and no real MCP endpoint needs more.
    if not raw_path.isascii():
        return True
    try:
        decoded = unquote(raw_path, errors="strict")
    except UnicodeDecodeError:
        return True
    if not decoded.isascii():
        return True

    if not _PATH_CHARS_RE.fullmatch(raw_path):
        return True
    # Every '%' must start exactly one two-hex-digit triplet; leftovers reject
    # IIS/legacy %u escapes and malformed encodings.
    if "%" in _PCT_RE.sub("", raw_path):
        return True
    for triplet in _PCT_RE.findall(raw_path):
        value = int(triplet[1:], 16)
        if value < 0x20 or value == 0x7F or value in (0x3B, 0x5C):
            # controls, ';' (path parameters) and '\\' have all been used to
            # truncate or re-route a path at some origin.
            return True
    return False


def validate_mcp_allowlist_entry(entry: str) -> None:
    """Reject an allowlist entry that cannot be matched safely.

    '*' is supported as a single leading '*.' host label wildcard and anywhere
    in the path, where it matches one or more non-'/' characters. Scheme, port,
    credentials, query and fragment must stay literal; a '*' there is a config
    error rather than a silently dead entry.
    """
    try:
        parsed = urlsplit(entry)
    except ValueError as exc:
        raise ValueError("the URL could not be parsed") from exc

    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http and https URLs are supported")

    if "@" in parsed.netloc:
        userinfo = parsed.netloc.rsplit("@", 1)[0]
        if "*" in userinfo:
            raise ValueError("'*' is not supported in credentials")

    try:
        parsed.port
    except ValueError:
        raise ValueError("the port must be a literal number") from None

    host = parsed.hostname
    if not host:
        raise ValueError("the URL must include a host")
    if "*" in host:
        suffix = host[2:] if host.startswith("*.") else ""
        if (
            "*" in suffix
            or not suffix
            or suffix.startswith(".")
            or suffix.endswith(".")
            or ".." in suffix
        ):
            raise ValueError(
                "host wildcards are only supported as a single leading '*.' label"
            )

    if "*" in parsed.query or "*" in parsed.fragment:
        raise ValueError("'*' is only supported in the host and path")
    if "%2a" in parsed.path.lower():
        raise ValueError(
            "percent-encoded '*' is not a wildcard; use a literal '*' in the path"
        )
    if _has_ambiguous_path(parsed.path):
        raise ValueError("the path must not contain ambiguous segments")


def validate_mcp_url(url: str, allowed_urls: list[str]) -> None:
    """Validate that a user-provided MCP URL is in the allowlist.

    Raises ValueError if the URL is not permitted.
    """
    if not allowed_urls:
        raise ValueError(
            f"URL {url!r} is not in the allowed MCP URL list. "
            "Configure features.mcp.user_servers.allowed_urls in your config."
        )

    # Parse the request URL through httpx2, which is what the MCP transports
    # dispatch with. Validating anything else risks approving a URL that
    # differs from the one that actually goes on the wire.
    try:
        parsed = httpx2.URL(url)
    except Exception as exc:
        raise ValueError(
            f"URL {url!r} is not in the allowed MCP URL list. It could not be parsed."
        ) from exc

    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"URL {url!r} is not in the allowed MCP URL list. "
            "Only http and https URLs are supported."
        )

    # ``urlsplit`` keeps ``;params`` in the path; ``urlparse`` would move that
    # tail into ``.params`` and hide ``/api/x;%5c..%5c..%5cadmin`` from every
    # check below.
    if _has_ambiguous_path(urlsplit(url).path):
        raise ValueError(
            f"URL {url!r} is not in the allowed MCP URL list. Its path must not "
            "contain '.' or '..' segments, encoded separators (%2e, %2f, %5c), "
            "backslashes, double-encoded sequences (%25), or non-ASCII "
            "characters."
        )

    for allowed in allowed_urls:
        try:
            validate_mcp_allowlist_entry(allowed)
        except ValueError as exc:
            raise ValueError(
                f"The MCP URL allowlist contains an invalid entry: {exc}"
            ) from None
        allowed_parsed = urlsplit(allowed)
        if (
            parsed.scheme == allowed_parsed.scheme
            and _host_matches(parsed.host, allowed_parsed.hostname or "")
            and _effective_port(parsed) == _effective_port(allowed_parsed)
            and _path_matches(parsed.path or "/", allowed_parsed.path or "/")
        ):
            return

    raise ValueError(
        f"URL {url!r} is not in the allowed MCP URL list. "
        "Configure features.mcp.user_servers.allowed_urls in your config."
    )


def validate_mcp_headers(
    headers: Optional[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    """Strip restricted headers from a user-supplied headers dict.

    Returns a filtered copy (or None if no headers remain / input is None).
    """
    if not headers:
        return None
    filtered = {}
    for key, value in headers.items():
        normalized = key.lower().replace("_", "-")
        if normalized in _RESTRICTED_HEADERS or normalized.startswith(
            _RESTRICTED_HEADER_PREFIXES
        ):
            continue
        filtered[key] = value
    return filtered or None


class McpDestinationError(Exception):
    """An MCP transport tried to reach a destination outside its grant."""


def _mcp_address_is_public(
    address: Union[ipaddress.IPv4Address, ipaddress.IPv6Address],
) -> bool:
    # Classify the embedded address, not just the IPv6 wrapper. Otherwise an
    # IPv4-mapped loopback/private destination can inherit different flags.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return (
        address.is_global
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_private
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


async def _resolve_mcp_destination(
    url: httpx2.URL, *, allow_private: bool
) -> list[str]:
    """Resolve every A/AAAA answer and reject the whole set if any is unsafe."""
    host = url.raw_host.decode("ascii")
    port = _effective_port(url)
    try:
        answers = await anyio.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise McpDestinationError(
            f"MCP destination host {host!r} could not be resolved."
        ) from exc

    addresses: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in answers:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except (IndexError, TypeError, ValueError) as exc:
            raise McpDestinationError(
                f"MCP destination host {host!r} returned an invalid address."
            ) from exc
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        normalized = str(address)
        if normalized not in addresses:
            addresses.append(normalized)

    if not addresses:
        raise McpDestinationError(
            f"MCP destination host {host!r} returned no addresses."
        )

    if not allow_private:
        unsafe = [
            address
            for address in addresses
            if not _mcp_address_is_public(ipaddress.ip_address(address))
        ]
        if unsafe:
            raise McpDestinationError(
                f"MCP destination host {host!r} resolved to non-public "
                f"address(es): {', '.join(unsafe)}. Set "
                "CHAINLIT_MCP_ALLOW_PRIVATE=true only for intentional internal "
                "MCP deployments."
            )
    return addresses


class _DnsPinnedMcpTransport(httpx2.AsyncBaseTransport):
    """Revalidate DNS per request and connect only to a validated numeric IP.

    httpx2/anyio do not expose a resolved-address connect option. Rewriting the
    transport URL to the validated address closes the DNS-rebinding TOCTOU: the
    socket layer receives an IP literal and cannot perform a second lookup. The
    original authority remains in Host, and HTTPS keeps the original hostname
    for SNI and certificate verification. A separate underlying pool per origin
    prevents TLS connections from being reused across hostnames.

    An explicit transport also bypasses environment proxies, which would
    otherwise resolve the destination outside this policy.
    """

    def __init__(
        self,
        *,
        allow_private: bool,
        on_blocked: Optional[Callable[[McpDestinationError], None]],
    ) -> None:
        self._allow_private = allow_private
        self._on_blocked = on_blocked
        self._pins: dict[tuple[str, bytes, int], str] = {}
        self._transports: dict[tuple[str, bytes, int], httpx2.AsyncHTTPTransport] = {}

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        origin = (
            request.url.scheme,
            request.url.raw_host,
            _effective_port(request.url),
        )
        try:
            addresses = await _resolve_mcp_destination(
                request.url, allow_private=self._allow_private
            )
        except McpDestinationError as exc:
            if self._on_blocked is not None:
                self._on_blocked(exc)
            raise

        transport = self._transports.get(origin)
        if transport is None:
            pinned_address = addresses[0]
            self._pins[origin] = pinned_address
            transport = httpx2.AsyncHTTPTransport()
            self._transports[origin] = transport
        else:
            # Reconnect/retry uses the same address that passed validation,
            # rather than introducing another DNS lookup at connect time.
            pinned_address = self._pins[origin]

        raw_host = request.url.raw_host.decode("ascii")
        authority = f"[{raw_host}]" if ":" in raw_host else raw_host
        if request.url.port is not None:
            authority += f":{request.url.port}"
        headers = request.headers.copy()
        headers["Host"] = authority
        extensions = dict(request.extensions)
        if request.url.scheme == "https":
            extensions["sni_hostname"] = raw_host

        pinned_request = httpx2.Request(
            request.method,
            request.url.copy_with(
                host=pinned_address,
                username=None,
                password=None,
            ),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        return await transport.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        transports = list(self._transports.values())
        self._transports.clear()
        self._pins.clear()
        for transport in transports:
            await transport.aclose()


def _destination_in_allowlist(allowed_urls: list[str]):
    """Permit only destinations the user's allowlist covers."""

    def check(url: str) -> None:
        try:
            validate_mcp_url(url, allowed_urls)
        except ValueError as exc:
            raise McpDestinationError(str(exc)) from None

    return check


def _destination_on_origin(configured_url: str):
    """Permit any path on the configured server's own origin.

    Named servers are pinned to their origin rather than their path subtree:
    the MCP SSE transport routinely advertises a message endpoint on a sibling
    path (``/sse`` handing off to ``/messages/``), so a path constraint would
    reject conforming servers. Cross-origin movement is still refused.
    """
    expected = httpx2.URL(configured_url)

    def check(url: str) -> None:
        actual = httpx2.URL(url)
        if (
            actual.scheme != expected.scheme
            or actual.host != expected.host
            or _effective_port(actual) != _effective_port(expected)
        ):
            raise McpDestinationError(
                f"MCP server tried to reach {actual.scheme}://{actual.netloc.decode()}, "
                "which is not the origin it was configured with."
            )

    return check


McpHttpClientFactory = Callable[..., httpx2.AsyncClient]

# Bounds for how long the /mcp connect handler waits for a connection to
# come up, in server.py. Co-located here (rather than a new McpFeature
# config knob) to avoid adding parsing/validation surface and a footgun —
# too low a value breaks `npx -y` cold starts — in a security release.
_MCP_CONNECT_TIMEOUT_STDIO = 120.0  # npx -y can cold-download on first run
_MCP_CONNECT_TIMEOUT_HTTP = 30.0


def make_mcp_http_client_factory(
    check_destination: Callable[[str], None],
    on_blocked: Optional[Callable[[McpDestinationError], None]] = None,
) -> McpHttpClientFactory:
    """Build the httpx2 client factory used by the SSE and streamable-http transports.

    Mirrors the MCP SDK's ``create_mcp_http_client`` — which sets only
    ``follow_redirects``, ``timeout``, ``headers`` and ``auth`` — with three
    additions, because validating the URL we were *handed* does not constrain
    the requests the transport actually makes:

    - Redirects are not followed at the client level, so a permitted origin
      cannot move the connection and forward headers to another target.
    - Every outgoing request is re-checked against ``check_destination``. The
      SSE transport takes its POST target from the server's ``endpoint`` event,
      and the SDK accepts any same-origin value — including one outside the
      path subtree the allowlist granted. Hooking the client binds the grant to
      the wire rather than to a single string, so this holds for the initial
      GET, the endpoint POSTs, and anything the SDK adds later.
    - Every transport request resolves all A/AAAA answers, rejects the entire
      set if any address is non-public, and pins the connection to a validated
      numeric address. This closes the DNS-rebinding check/connect TOCTOU.
      Redirects and reconnects resolve again; low-level retries reuse the
      already validated pin. Operators with intentional internal MCP servers
      must explicitly set ``CHAINLIT_MCP_ALLOW_PRIVATE=true``.

    ``on_blocked``, if given, is called synchronously with the raised
    ``McpDestinationError`` before it propagates. This is a fail-fast side
    channel: ``mcp/client/sse.py`` and ``mcp/client/streamable_http.py`` wrap
    their send loops in a bare ``except Exception: logger.exception(...)``
    with no re-raise, so the exception itself never reaches
    ``ClientSession.initialize()`` — callers that only awaited the SDK would
    hang forever. The callback lets ``server.py`` unblock its wait
    immediately instead. Kept as a plain callback (not a mutable holder
    threaded through here) so this module stays free of asyncio primitives
    and stays synchronously testable.
    """

    allow_private = (
        os.environ.get("CHAINLIT_MCP_ALLOW_PRIVATE", "").strip().lower() == "true"
    )

    def factory(
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[httpx2.Timeout] = None,
        auth: Optional[httpx2.Auth] = None,
    ) -> httpx2.AsyncClient:
        async def _check_request(request: httpx2.Request) -> None:
            try:
                check_destination(str(request.url))
            except McpDestinationError as exc:
                if on_blocked is not None:
                    on_blocked(exc)
                raise

        return httpx2.AsyncClient(
            follow_redirects=False,
            timeout=timeout if timeout is not None else httpx2.Timeout(30.0),
            headers=headers,
            auth=auth,
            event_hooks={"request": [_check_request]},
            transport=_DnsPinnedMcpTransport(
                allow_private=allow_private,
                on_blocked=on_blocked,
            ),
        )

    return factory
