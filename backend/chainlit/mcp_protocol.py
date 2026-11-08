"""MCP prompts, resources, list pagination and version negotiation.

The MCP SDK returns a single page per list call. Chainlit needs the whole
list for the connect payload and for the prompt picker, so every listing
loops over ``nextCursor`` here, under a page cap that is logged when hit.

Everything in this module treats server output as untrusted: sizes are
capped before content is copied into a Chainlit element or the composer, and
callers map errors to fixed details instead of echoing server text.
"""

import base64
import logging
import mimetypes
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from mcp import MCPError, types as mcp_types

logger = logging.getLogger(__name__)

# A well-behaved server pages a handful of times. The cap keeps a malicious
# or broken server (a cursor loop, or one item per page forever) from pinning
# a connect request; when it trips the listing is truncated and logged.
MCP_MAX_LIST_PAGES = 100

# Untrusted remote content becomes a Chainlit element or composer text, so
# both paths share one byte cap.
MCP_CONTENT_MAX_BYTES = 5 * 1024 * 1024

# The SDK has no read timeout unless the session sets one, and a server that
# never answers must not pin the HTTP request that invoked it.
MCP_REQUEST_TIMEOUT_SECONDS = 30.0


class McpPromptError(Exception):
    """Base class for prompt failures the HTTP layer maps to a detail."""


class McpPromptsUnsupported(McpPromptError):
    """The server has no prompts capability."""


class McpPromptNotFound(McpPromptError):
    """No prompt with the requested name is published."""


class McpPromptArgumentsError(McpPromptError):
    """The arguments do not match the prompt's published schema."""

    def __init__(self, message: str, missing: Sequence[str] = (), unknown=()):
        super().__init__(message)
        self.missing = list(missing)
        self.unknown = list(unknown)


class McpContentTooLarge(Exception):
    """Remote content exceeded MCP_CONTENT_MAX_BYTES."""


class McpResourceReadError(Exception):
    """The resource could not be read, or returned nothing usable."""


@dataclass(frozen=True)
class McpResourceContent:
    """One resource's bytes plus the provenance shown on the element."""

    data: bytes
    mime_type: Optional[str]
    name: str


async def _paginate(fetch, attribute: str, label: str) -> List[Any]:
    """Collect every page of one MCP listing.

    ``fetch`` takes the SDK's ``params=`` keyword, so a fake session in tests
    only has to implement the same shape as ``ClientSession.list_*``.
    """
    items: List[Any] = []
    cursor: Optional[str] = None
    for _ in range(MCP_MAX_LIST_PAGES):
        params = mcp_types.PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await fetch(params=params)
        items.extend(getattr(result, attribute))
        cursor = getattr(result, "next_cursor", None)
        if not cursor:
            return items
    logger.warning(
        "MCP %s listing stopped after %d pages; the server has more items "
        "and the list was truncated.",
        label,
        MCP_MAX_LIST_PAGES,
    )
    return items


async def list_all_tools(session) -> List[Any]:
    return await _paginate(session.list_tools, "tools", "tools")


async def list_all_prompts(session) -> List[Any]:
    fetch = getattr(session, "list_prompts", None)
    if fetch is None:
        return []
    return await _paginate(fetch, "prompts", "prompts")


async def list_all_resources(session) -> List[Any]:
    fetch = getattr(session, "list_resources", None)
    if fetch is None:
        return []
    return await _paginate(fetch, "resources", "resources")


async def describe_mcp_server(session) -> Dict[str, Any]:
    """Everything the UI lists for a connected server.

    Tools must list or the connection is useless. Prompts and resources are
    optional capabilities, so a server that rejects those calls still connects
    with an empty list; that is what keeps old servers working.
    """
    payload: Dict[str, Any] = {
        "tools": [{"name": tool.name} for tool in await list_all_tools(session)]
    }
    for key, lister, serialiser in (
        ("prompts", list_all_prompts, prompt_definition),
        ("resources", list_all_resources, resource_definition),
    ):
        try:
            payload[key] = [serialiser(item) for item in await lister(session)]
        except Exception:
            logger.debug("MCP server did not list %s", key, exc_info=True)
            payload[key] = []
    return payload


async def negotiate_protocol(session) -> str:
    """Negotiate with the server, preferring the modern ``server/discover``.

    ``initialize()`` stays the fallback for servers that only speak the
    legacy handshake: the SDK documents METHOD_NOT_FOUND and timeouts from
    ``discover()`` as caller policy. The SDK bound to this release also has
    the call, but older 2.x installs allowed by ``pyproject`` may not.
    """
    discover = getattr(session, "discover", None)
    if discover is not None:
        try:
            await discover()
            return "discover"
        except (MCPError, RuntimeError) as exc:
            logger.debug(
                "MCP server rejected server/discover (%s); falling back to initialize()",
                type(exc).__name__,
            )
    await session.initialize()
    return "initialize"


def render_prompt_messages(messages: Sequence[Any]) -> str:
    """Render prompt messages as the text the composer inserts."""
    parts: List[str] = []
    for message in messages:
        content = getattr(message, "content", None)
        if getattr(content, "type", None) == "text":
            parts.append(str(getattr(content, "text", "")))
        else:
            kind = getattr(content, "type", "unknown")
            parts.append(f"[{kind} content omitted]")
    return "\n\n".join(parts)


async def render_prompt(session, name: str, arguments: Optional[Dict[str, str]]) -> str:
    """Look the prompt up, validate the arguments, and render its messages."""
    arguments = arguments or {}
    try:
        prompts = await list_all_prompts(session)
    except MCPError as exc:
        if exc.code == mcp_types.METHOD_NOT_FOUND:
            raise McpPromptsUnsupported("This MCP server does not support prompts.")
        raise

    prompt = next((p for p in prompts if p.name == name), None)
    if prompt is None:
        raise McpPromptNotFound("This MCP server has no prompt with that name.")

    schema = {argument.name: argument for argument in prompt.arguments or []}
    missing = [
        argument_name
        for argument_name, argument in schema.items()
        if argument.required and not arguments.get(argument_name, "").strip()
    ]
    if missing:
        raise McpPromptArgumentsError(
            f"Missing required prompt argument(s): {', '.join(missing)}",
            missing=missing,
        )
    unknown = [key for key in arguments if key not in schema]
    if unknown:
        raise McpPromptArgumentsError(
            f"Unknown prompt argument(s): {', '.join(unknown)}", unknown=unknown
        )

    result = await session.get_prompt(name, arguments or None)
    if not isinstance(result, mcp_types.GetPromptResult):
        # MRTR is out of scope: never start a second round with the user.
        raise McpPromptError("This MCP prompt requires input and cannot be shown.")

    text = render_prompt_messages(result.messages)
    if len(text.encode("utf-8")) > MCP_CONTENT_MAX_BYTES:
        raise McpContentTooLarge
    return text


def _decode_contents(contents: Sequence[Any]) -> tuple[bytes, Optional[str]]:
    """Join text parts, or take the first blob, under the shared size cap.

    The cap is enforced while decoding so an oversized blob is rejected before
    it is materialised, and applies to every part so a multi-part response
    cannot bypass it.
    """
    texts: List[str] = []
    blob: Optional[bytes] = None
    mime_type: Optional[str] = None
    total = 0
    for item in contents:
        mime_type = mime_type or getattr(item, "mime_type", None)
        raw = getattr(item, "text", None)
        if raw is not None:
            data = str(raw).encode("utf-8")
            total += len(data)
            if total > MCP_CONTENT_MAX_BYTES:
                raise McpContentTooLarge
            texts.append(str(raw))
            continue
        encoded = getattr(item, "blob", "") or ""
        total += len(encoded) * 3 // 4
        if total > MCP_CONTENT_MAX_BYTES:
            raise McpContentTooLarge
        if blob is None:
            try:
                blob = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError):
                raise McpResourceReadError from None
    if texts:
        return "\n\n".join(texts).encode("utf-8"), mime_type
    if blob is not None:
        return blob, mime_type
    raise McpResourceReadError


async def read_resource(
    session, uri: str, name: Optional[str] = None
) -> McpResourceContent:
    """Read one resource and cap its size."""
    try:
        result = await session.read_resource(uri)
    except MCPError:
        raise McpResourceReadError from None
    if not isinstance(result, mcp_types.ReadResourceResult) or not result.contents:
        raise McpResourceReadError

    data, mime_type = _decode_contents(result.contents)
    if not mime_type:
        mime_type = mimetypes.guess_type(uri)[0]
    return McpResourceContent(
        data=data,
        mime_type=mime_type,
        name=name or uri.rstrip("/").rsplit("/", 1)[-1] or uri,
    )


def prompt_definition(prompt) -> Dict[str, Any]:
    """Serialise one prompt for the connect payload and the picker."""
    return {
        "name": prompt.name,
        "description": prompt.description,
        "arguments": [
            {
                "name": argument.name,
                "description": argument.description,
                "required": bool(argument.required),
            }
            for argument in prompt.arguments or []
        ],
    }


def resource_definition(resource) -> Dict[str, Any]:
    """Serialise one resource for the connect payload and the picker."""
    return {
        "uri": resource.uri,
        "name": resource.name,
        "description": resource.description,
        "mimeType": resource.mime_type,
        "size": resource.size,
    }
