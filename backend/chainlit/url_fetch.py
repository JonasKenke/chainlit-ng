import asyncio
import base64
import ipaddress
import math
import os
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import aiohttp

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_BYTES = 50 * 1024 * 1024
_MAX_REDIRECTS = 3
_READ_CHUNK_SIZE = 64 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class URLFetchError(ValueError):
    """Base error for application-mediated URL fetches."""


class URLFetchPolicyError(URLFetchError):
    """The URL or one of its resolved addresses violates fetch policy."""


class URLFetchConfigurationError(URLFetchError):
    """A fetch limit is invalid."""


class URLFetchTimeoutError(URLFetchError, TimeoutError):
    """The fetch exceeded its total timeout."""


class URLFetchTooLargeError(URLFetchError):
    """The response exceeded the configured byte cap."""


class URLFetchHTTPError(URLFetchError):
    """The server returned a non-success status without a redirect."""

    def __init__(self, status: int):
        self.status = status
        super().__init__(f"URL fetch failed with HTTP status {status}")


@dataclass(frozen=True)
class _Target:
    url: str
    host: str
    port: int
    origin: Tuple[str, str, int]
    auth: Optional[str]


class _PinnedResolver:
    """Return only the addresses validated immediately before this request."""

    def __init__(self):
        self._addresses: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}

    @staticmethod
    def _key(host: str, port: int) -> Tuple[str, int]:
        return host.rstrip(".").encode("idna").decode("ascii").lower(), port

    def set_addresses(
        self, host: str, port: int, addresses: List[Dict[str, Any]]
    ) -> None:
        self._addresses[self._key(host, port)] = addresses

    async def resolve(
        self, host: str, port: int, family: int = socket.AF_UNSPEC
    ) -> List[Dict[str, Any]]:
        try:
            addresses = self._addresses[self._key(host, port)]
        except (KeyError, UnicodeError):
            raise URLFetchPolicyError("No validated address is available") from None
        if family == socket.AF_UNSPEC:
            return list(addresses)
        return [address for address in addresses if address["family"] == family]

    async def close(self) -> None:
        pass


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() == "true"


def _timeout_value(timeout: Optional[float]) -> float:
    value = _DEFAULT_TIMEOUT if timeout is None else timeout
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise URLFetchConfigurationError("URL fetch timeout must be a positive number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise URLFetchConfigurationError("URL fetch timeout must be a positive number")
    return value


def _environment_timeout() -> float:
    raw = os.environ.get("CHAINLIT_URL_FETCH_TIMEOUT")
    if raw is None:
        return _DEFAULT_TIMEOUT
    try:
        return _timeout_value(float(raw))
    except (TypeError, ValueError):
        raise URLFetchConfigurationError(
            "CHAINLIT_URL_FETCH_TIMEOUT must be a positive number"
        ) from None


def _max_bytes_value(max_bytes: Optional[int]) -> int:
    if max_bytes is not None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise URLFetchConfigurationError(
                "URL fetch max_bytes must be a positive integer"
            )
        return max_bytes

    raw = os.environ.get("CHAINLIT_URL_FETCH_MAX_BYTES")
    if raw is None:
        return _DEFAULT_MAX_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise URLFetchConfigurationError(
            "CHAINLIT_URL_FETCH_MAX_BYTES must be a positive integer"
        ) from None
    if value <= 0:
        raise URLFetchConfigurationError(
            "CHAINLIT_URL_FETCH_MAX_BYTES must be a positive integer"
        )
    return value


def _parse_target(url: str, allow_userinfo: bool) -> _Target:
    if not isinstance(url, str) or not url or url != url.strip():
        raise URLFetchPolicyError("Invalid URL")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise URLFetchPolicyError("Invalid URL")

    try:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise URLFetchPolicyError("Invalid URL") from None

    if scheme not in {"http", "https"}:
        raise URLFetchPolicyError("Only HTTP and HTTPS URLs are allowed")
    if not host:
        raise URLFetchPolicyError("URL host is required")
    if port is None:
        port = 443 if scheme == "https" else 80
    if not 1 <= port <= 65535:
        raise URLFetchPolicyError("Invalid URL port")

    has_userinfo = parsed.username is not None or parsed.password is not None
    if has_userinfo and not allow_userinfo:
        raise URLFetchPolicyError("URL credentials are not allowed")

    auth = None
    if has_userinfo:
        try:
            login = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            if ":" in login:
                raise ValueError
            credentials = base64.b64encode(f"{login}:{password}".encode())
            auth = f"Basic {credentials.decode('ascii')}"
        except (TypeError, ValueError, UnicodeError):
            raise URLFetchPolicyError("Invalid URL credentials") from None

    host_for_netloc = f"[{host}]" if ":" in host else host
    netloc = f"{host_for_netloc}:{port}" if parsed.port is not None else host_for_netloc
    sanitized = urlunsplit((scheme, netloc, parsed.path, parsed.query, ""))
    return _Target(
        url=sanitized,
        host=host,
        port=port,
        origin=(scheme, host.lower(), port),
        auth=auth,
    )


def _validate_address(
    address: Union[ipaddress.IPv4Address, ipaddress.IPv6Address],
    allow_private: bool,
) -> None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    if allow_private:
        return
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    ):
        raise URLFetchPolicyError("URL resolves to a non-public address")


async def _resolve_and_validate(
    host: str, port: int, allow_private: bool
) -> List[Dict[str, Any]]:
    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
        )
    except (OSError, UnicodeError):
        raise URLFetchError("URL fetch failed during address resolution") from None

    if not records:
        raise URLFetchPolicyError("URL host did not resolve")

    addresses = []
    for family, socktype, protocol, _canonical_name, sockaddr in records:
        raw_host = sockaddr[0]
        if not isinstance(raw_host, str):
            raise URLFetchPolicyError("URL resolved to an invalid address")
        try:
            address = ipaddress.ip_address(raw_host.split("%", 1)[0])
        except (ValueError, IndexError):
            raise URLFetchPolicyError("URL resolved to an invalid address") from None
        _validate_address(address, allow_private)
        addresses.append(
            {
                "hostname": host,
                "host": sockaddr[0],
                "port": sockaddr[1],
                "family": family,
                "proto": protocol,
                "flags": 0,
            }
        )
    return addresses


async def _read_capped(response: aiohttp.ClientResponse, max_bytes: int) -> bytes:
    content_length = getattr(response, "content_length", None)
    if content_length is not None and content_length > max_bytes:
        raise URLFetchTooLargeError("URL response exceeds the byte cap")

    body = bytearray()
    async for chunk in response.content.iter_chunked(_READ_CHUNK_SIZE):
        if len(chunk) > max_bytes - len(body):
            raise URLFetchTooLargeError("URL response exceeds the byte cap")
        body.extend(chunk)
    return bytes(body)


async def _fetch(
    url: str,
    max_bytes: int,
    timeout: float,
    allow_private: bool,
    allow_userinfo: bool,
    resolver: _PinnedResolver,
) -> bytes:
    target = _parse_target(url, allow_userinfo)
    auth = target.auth
    redirects = 0
    client_timeout = aiohttp.ClientTimeout(
        total=timeout,
        connect=timeout,
        sock_read=timeout,
    )

    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(
            resolver=resolver,  # type: ignore[arg-type]
            use_dns_cache=False,
            force_close=True,
        ),
        cookie_jar=aiohttp.DummyCookieJar(),
        timeout=client_timeout,
        trust_env=False,
    ) as session:
        while True:
            addresses = await _resolve_and_validate(
                target.host, target.port, allow_private
            )
            resolver.set_addresses(target.host, target.port, addresses)

            response = await session.get(
                target.url,
                headers={"Authorization": auth} if auth is not None else None,
                allow_redirects=False,
            )
            async with response:
                if response.status == 200:
                    return await _read_capped(response, max_bytes)

                if response.status in _REDIRECT_STATUSES:
                    if redirects >= _MAX_REDIRECTS:
                        raise URLFetchError("URL fetch exceeded the redirect limit")
                    location = response.headers.get("Location")
                    if not location:
                        raise URLFetchPolicyError(
                            "Redirect is missing a Location header"
                        )
                    next_target = _parse_target(
                        urljoin(target.url, location), allow_userinfo
                    )
                    if next_target.origin != target.origin:
                        auth = None
                    elif next_target.auth is not None:
                        auth = next_target.auth
                    target = next_target
                    redirects += 1
                    continue

                raise URLFetchHTTPError(response.status)


async def fetch_url_bytes(
    url: str,
    *,
    max_bytes: Optional[int] = None,
    timeout: Optional[float] = None,
) -> bytes:
    """Fetch an HTTP(S) URL with SSRF, redirect, timeout, and size policies."""

    timeout_value = (
        _environment_timeout() if timeout is None else _timeout_value(timeout)
    )
    max_bytes_value = _max_bytes_value(max_bytes)
    allow_private = _env_true("CHAINLIT_URL_FETCH_ALLOW_PRIVATE")
    allow_userinfo = _env_true("CHAINLIT_URL_FETCH_ALLOW_USERINFO")
    resolver = _PinnedResolver()

    try:
        return await asyncio.wait_for(
            _fetch(
                url,
                max_bytes_value,
                timeout_value,
                allow_private,
                allow_userinfo,
                resolver,
            ),
            timeout=timeout_value,
        )
    except URLFetchError:
        raise
    except asyncio.TimeoutError:
        raise URLFetchTimeoutError("URL fetch exceeded its timeout") from None
    except (aiohttp.ClientError, OSError, ValueError):
        raise URLFetchError("URL fetch failed") from None
