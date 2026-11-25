import asyncio
import fnmatch
import glob
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import time
import urllib.parse
import webbrowser
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Union, cast

import socketio
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError
from starlette.datastructures import URL
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Receive, Scope, Send
from typing_extensions import Annotated
from watchfiles import awatch

from chainlit import otel
from chainlit.approval import (
    ApprovalError,
    ApprovalInvalidArguments,
    ApprovalNotPending,
    ApprovalStatus,
    clear_thread_deleted,
    live_interaction_ids,
    managed_thread_ids_for,
    mark_thread_deleted,
    shared_managed_thread_ids,
    supersede_thread_approvals,
    transition,
)
from chainlit.auth import (
    create_jwt,
    decode_jwt,
    get_configuration,
    get_current_user,
    get_current_user_optional,
    require_login,
    resolve_current_user_optional,
)
from chainlit.auth.cookie import (
    clear_auth_cookie,
    clear_oauth_cookies,
    get_oauth_nonce_cookie,
    get_token_from_cookies,
    set_auth_cookie,
    set_oauth_nonce_cookie,
    set_oauth_state_cookie,
    validate_oauth_state_cookie,
)
from chainlit.config import (
    APP_ROOT,
    BACKEND_ROOT,
    DEFAULT_HOST,
    FILES_DIRECTORY,
    PACKAGE_ROOT,
    ChainlitConfig,
    config,
    load_module,
    public_dir,
    reload_config,
)
from chainlit.context import RequestContextMiddleware
from chainlit.data import get_data_layer
from chainlit.data.acl import (
    MAX_THREAD_GRANTS_PER_THREAD,
    ThreadAccess,
    _require_owner_or_404,
    authorize_thread_read,
    is_thread_author,
)
from chainlit.data.base import BaseDataLayer
from chainlit.logger import logger
from chainlit.markdown import get_markdown_str
from chainlit.mcp_protocol import (
    MCP_REQUEST_TIMEOUT_SECONDS,
    describe_mcp_server,
    negotiate_protocol,
    read_resource,
    render_prompt,
)
from chainlit.oauth_providers import get_oauth_provider
from chainlit.prompt_versioning import (
    MAX_DIFF_BYTES,
    MAX_DIFF_ROWS,
    PromptContentTooLarge,
    PromptDiffTooLarge,
    PromptTitleTooLarge,
    PromptValidationError,
    PromptVersionConflict,
    PromptVersionError,
    PromptVersionLimitReached,
    SkillNameConflict,
    build_diff,
    enforce_content_limit,
    enforce_title_limit,
    fingerprint,
    latest_head,
    public_version,
    public_versions,
    validate_unicode_scalar,
)
from chainlit.scheduled_task import (
    next_run_at,
    validate_cron,
    validate_prompt,
    validate_timezone,
)
from chainlit.scheduler import ScheduledTaskWorker
from chainlit.secret import random_secret
from chainlit.session import WebsocketSession
from chainlit.share_author import resolve_shared_author_label
from chainlit.skill import (
    MAX_SKILLS,
    Skill,
    SkillError,
    load_skills,
    parse_skill_markdown,
    skill_content_hash,
    skill_to_markdown,
)
from chainlit.state_store import LockNotAcquired, close_state_store, get_state_store
from chainlit.thread_metadata import public_thread
from chainlit.types import (
    AckPromptUpdateRequest,
    AddSharedPromptRequest,
    AddSharedSkillRequest,
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    AskFileSpec,
    CallActionRequest,
    ConnectMCPRequest,
    CreatePromptRequest,
    CreateScheduledTaskRequest,
    CreateSkillRequest,
    CreateThreadGrantRequest,
    DeleteFeedbackRequest,
    DeleteThreadRequest,
    DisconnectMCPRequest,
    ElementRequest,
    GetThreadsRequest,
    ImportSkillRequest,
    McpPromptRequest,
    McpResourceRequest,
    PromptDict,
    QuestionAnswerRequest,
    QuestionAnswerResponse,
    ScheduledTaskDict,
    SharePromptRequest,
    ShareSkillRequest,
    ShareThreadRequest,
    SkillSeenUpdateRequest,
    SyncPromptRequest,
    Theme,
    ThreadGrant,
    UpdateFeedbackRequest,
    UpdatePromptRequest,
    UpdateScheduledTaskRequest,
    UpdateSkillFromSourceRequest,
    UpdateSkillRequest,
    UpdateThreadRequest,
)
from chainlit.user import PersistedUser, User, redact_sensitive_metadata
from chainlit.utils import utc_now

from ._utils import is_path_inside

if TYPE_CHECKING:
    from chainlit.element import CustomElement, ElementDict
    from chainlit.session import WebsocketSession

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")


def _cleanup_files_directory() -> None:
    """Remove only the legacy default upload directory on shutdown.

    A configured directory may contain operator data, so it is preserved
    exactly like a directory outside APP_ROOT.
    """
    feature = config.features.spontaneous_file_upload
    if feature is not None and feature.directory is not None:
        return
    default_directory = (Path(APP_ROOT) / ".files").resolve()
    if FILES_DIRECTORY.resolve() != default_directory:
        return
    if FILES_DIRECTORY.is_dir():
        shutil.rmtree(FILES_DIRECTORY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Context manager to handle app start and shutdown."""
    load_skills()
    otel.init()

    if config.code.on_app_startup:
        await config.code.on_app_startup()

    host = config.run.host
    port = config.run.port
    root_path = os.getenv("CHAINLIT_ROOT_PATH", "")
    scheme = "https" if config.run.ssl_cert else "http"

    if host == DEFAULT_HOST:
        url = f"{scheme}://localhost:{port}{root_path}"
    else:
        url = f"{scheme}://{host}:{port}{root_path}"

    logger.info(f"Your app is available at {url}")

    if not config.run.headless:
        # Add a delay before opening the browser
        await asyncio.sleep(1)
        webbrowser.open(url)

    watch_task = None
    stop_event = asyncio.Event()

    if config.run.watch:

        async def watch_files_for_changes():
            extensions = [".py"]
            files = ["chainlit.md", "config.toml"]
            async for changes in awatch(config.root, stop_event=stop_event):
                for change_type, file_path in changes:
                    file_name = os.path.basename(file_path)
                    file_ext = os.path.splitext(file_name)[1]

                    if file_ext.lower() in extensions or file_name.lower() in files:
                        logger.info(
                            f"File {change_type.name}: {file_name}. Reloading app..."
                        )

                        try:
                            reload_config()
                        except Exception as e:
                            logger.error(f"Error reloading config: {e}")
                            break

                        # Reload the module if the module name is specified in the config
                        if config.run.module_name:
                            try:
                                load_module(config.run.module_name, force_refresh=True)
                            except Exception as e:
                                logger.error(f"Error reloading module: {e}")

                        await asyncio.sleep(1)
                        await sio.emit("reload", {})

                        break

        watch_task = asyncio.create_task(watch_files_for_changes())

    discord_task = None

    if discord_bot_token := os.environ.get("DISCORD_BOT_TOKEN"):
        from chainlit.discord.app import client

        discord_task = asyncio.create_task(client.start(discord_bot_token))

    slack_task = None
    scheduler: Optional[ScheduledTaskWorker] = None
    scheduler_task = None
    if (
        config.features.prompt_scheduling
        and require_login()
        and config.code.on_message
        and (data_layer := get_data_layer())
        and _supports_scheduled_tasks(data_layer)
    ):
        scheduler = ScheduledTaskWorker(data_layer)
        scheduler_task = asyncio.create_task(scheduler.run())

    # Slack Socket Handler if env variable SLACK_WEBSOCKET_TOKEN is set
    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get("SLACK_WEBSOCKET_TOKEN"):
        from chainlit.slack.app import start_socket_mode

        slack_task = asyncio.create_task(start_socket_mode())

    try:
        yield
    finally:
        try:
            if config.code.on_app_shutdown:
                await config.code.on_app_shutdown()

            if watch_task:
                stop_event.set()
                watch_task.cancel()
                await watch_task

            if discord_task:
                discord_task.cancel()
                await discord_task

            if slack_task:
                slack_task.cancel()
                await slack_task

            if scheduler:
                await scheduler.shutdown()
                if scheduler_task:
                    scheduler_task.cancel()
                    await scheduler_task

            if data_layer := get_data_layer():
                await data_layer.close()

            try:
                await asyncio.wait_for(close_state_store(), timeout=2)
            except asyncio.TimeoutError:
                logger.warning("Timed out while closing the state store")
        except asyncio.exceptions.CancelledError:
            pass

        _cleanup_files_directory()

        try:
            otel.shutdown()
        finally:
            # Force exit the process to avoid potential AnyIO threads still running
            os._exit(0)


def get_build_dir(local_target: str, packaged_target: str) -> str:
    """
    Get the build directory based on the UI build strategy.

    Args:
        local_target (str): The local target directory.
        packaged_target (str): The packaged target directory.

    Returns:
        str: The build directory
    """

    local_build_dir = os.path.join(PACKAGE_ROOT, local_target, "dist")
    packaged_build_dir = os.path.join(BACKEND_ROOT, packaged_target, "dist")

    if config.ui.custom_build and os.path.exists(
        os.path.join(APP_ROOT, config.ui.custom_build)
    ):
        return os.path.join(APP_ROOT, config.ui.custom_build)
    elif os.path.exists(local_build_dir):
        return local_build_dir
    elif os.path.exists(packaged_build_dir):
        return packaged_build_dir
    else:
        raise FileNotFoundError(f"{local_target} built UI dir not found")


build_dir = get_build_dir("frontend", "frontend")
copilot_build_dir = get_build_dir(os.path.join("libs", "copilot"), "copilot")

app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> Response:
    # Rejected input is not echoed back: it can hold lone surrogates that
    # JSONResponse cannot encode. ensure_ascii keeps the body valid even when a
    # field name (loc) carries one.
    errors = [
        {key: value for key, value in error.items() if key not in ("input", "ctx")}
        for error in exc.errors()
    ]
    return Response(
        content=json.dumps({"detail": errors}, ensure_ascii=True),
        status_code=422,
        media_type="application/json",
    )


def _cors_allow_credentials() -> bool:
    """Resolve CORS credentials without reflecting every origin.

    An unset value enables credentials only for an explicit origin allowlist.
    A wildcard allowlist keeps the pre-3.1 behavior only when the deployment
    opts in, because Starlette then reflects any caller's Origin together with
    ``Access-Control-Allow-Credentials: true``.
    """
    configured = config.project.allow_credentials
    if configured is not None:
        return configured
    return "*" not in config.project.allow_origins


def _socket_origin_allowed(origin: Optional[str], environ: Dict[str, Any]) -> bool:
    """Validate a browser Origin for Engine.IO (HTTP polling and WebSocket).

    Same-origin and explicitly configured origins always pass. A wildcard
    allowlist passes only when login is not required: without an identity
    there are no credentials for a cross-site handshake to steal. With login
    enabled a wildcard must not silently authorize cross-site WebSockets.
    """
    if not origin:
        return True

    try:
        parsed = URL(origin)
    except Exception:
        return False

    if not parsed.netloc:
        return False

    chainlit_url = os.environ.get("CHAINLIT_URL")
    effective_scheme = None
    if chainlit_url:
        try:
            effective_scheme = URL(chainlit_url).scheme.lower()
        except Exception:
            pass
    if effective_scheme is None:
        forwarded_proto = str(environ.get("HTTP_X_FORWARDED_PROTO") or "")
        forwarded_scheme = forwarded_proto.split(",", 1)[0].strip().lower()
        if forwarded_scheme in ("http", "https"):
            effective_scheme = forwarded_scheme
    if effective_scheme is None:
        scope = environ.get("asgi.scope")
        if isinstance(scope, dict):
            scope_scheme = str(scope.get("scheme") or "").lower()
            effective_scheme = {"ws": "http", "wss": "https"}.get(
                scope_scheme, scope_scheme
            )
    if effective_scheme is None:
        request_scheme = str(environ.get("wsgi.url_scheme") or "").lower()
        effective_scheme = {"ws": "http", "wss": "https"}.get(
            request_scheme, request_scheme
        )
    if effective_scheme not in ("http", "https"):
        # Preserve compatibility for non-browser/internal transports that do
        # not provide enough scheme information to enforce a comparison.
        effective_scheme = parsed.scheme.lower()

    host = str(environ.get("HTTP_HOST") or "").lower()
    if (
        effective_scheme == parsed.scheme.lower()
        and host
        and parsed.netloc.lower() == host
    ):
        return True

    if chainlit_url:
        try:
            configured = URL(chainlit_url)
            if (
                parsed.scheme.lower() == configured.scheme.lower()
                and parsed.netloc.lower() == configured.netloc.lower()
            ):
                return True
        except Exception:
            pass

    normalized = origin.rstrip("/")
    explicit = {
        candidate.rstrip("/")
        for candidate in config.project.allow_origins
        if candidate != "*"
    }
    if normalized in explicit:
        return True

    if "*" in config.project.allow_origins:
        return not require_login()

    return False


_cors_credentials = _cors_allow_credentials()
if _cors_credentials and "*" in config.project.allow_origins:
    logger.warning(
        "CORS: allow_credentials=true with a wildcard allow_origins list reflects "
        "every Origin. Configure allow_origins explicitly."
    )

# Engine.IO silently drops frames above this limit. The composer enforces the
# same limit (isMessageTooLarge in frontend/src/lib/utils.ts) so an oversized
# message is reported instead of being rendered and lost.
sio = socketio.AsyncServer(
    cors_allowed_origins=_socket_origin_allowed,
    async_mode="asgi",
    max_http_buffer_size=1_000_000,
)

asgi_app = socketio.ASGIApp(socketio_server=sio, socketio_path="")

# config.run.root_path is only set when started with --root-path. Not on submounts.
SOCKET_IO_PATH = f"{config.run.root_path}/ws/socket.io"
app.mount(SOCKET_IO_PATH, asgi_app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.project.allow_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SafariWebSocketsCompatibleGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Prevent gzip compression for HTTP requests to socket.io path due to a bug in Safari
        if URL(scope=scope).path.startswith(SOCKET_IO_PATH):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


app.add_middleware(SafariWebSocketsCompatibleGZipMiddleware)
app.add_middleware(RequestContextMiddleware)

# config.run.root_path is only set when started with --root-path. Not on submounts.
router = APIRouter(prefix=config.run.root_path)


def _resolve_static_file(base_path: Path, filename: str) -> Path:
    """Resolve one request path under base_path, rejecting NUL and escapes.

    ``Path.resolve`` raises ValueError on an embedded NUL; the route used to
    turn that into an unauthenticated 500.
    """
    if "\x00" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    try:
        file_path = (base_path / filename).resolve()
    except (OSError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid filename") from None
    if not is_path_inside(file_path, base_path):
        raise HTTPException(status_code=400, detail="Invalid filename")
    return file_path


def _resolve_selected_static_file(file_path: str, root_path: Path) -> Path:
    """Resolve a glob-selected file while confining it to root_path."""
    try:
        relative_path = Path(file_path).relative_to(root_path)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid filename") from None
    return _resolve_static_file(root_path, str(relative_path))


@router.get("/public/{filename:path}")
async def serve_public_file(
    filename: str,
):
    """Serve a file from public dir."""

    file_path = _resolve_static_file(Path(public_dir), filename)
    if file_path.is_file():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")


@router.get("/assets/{filename:path}")
async def serve_asset_file(
    filename: str,
):
    """Serve a file from assets dir."""

    file_path = _resolve_static_file(Path(os.path.join(build_dir, "assets")), filename)
    if file_path.is_file():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")


@router.get("/copilot/{filename:path}")
async def serve_copilot_file(
    filename: str,
):
    """Serve a file from assets dir."""

    file_path = _resolve_static_file(Path(copilot_build_dir), filename)
    if file_path.is_file():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File not found")


# -------------------------------------------------------------------------------
#                               SLACK HTTP HANDLER
# -------------------------------------------------------------------------------


def _validate_slack_timestamp(timestamp: Optional[str]) -> None:
    """Reject malformed Slack timestamps before Bolt's verifier raises 500."""
    if timestamp is None:
        return
    try:
        int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Unauthorized") from None


if (
    os.environ.get("SLACK_BOT_TOKEN")
    and os.environ.get("SLACK_SIGNING_SECRET")
    and not os.environ.get("SLACK_WEBSOCKET_TOKEN")
):
    from chainlit.slack.app import slack_app_handler

    @router.post("/slack/events")
    async def slack_endpoint(req: Request):
        _validate_slack_timestamp(req.headers.get("X-Slack-Request-Timestamp"))
        return await slack_app_handler.handle(req)


# -------------------------------------------------------------------------------
#                               TEAMS HANDLER
# -------------------------------------------------------------------------------


async def _process_teams_activity(request: Request, adapter=None, bot=None):
    """Validate and dispatch one Teams activity without leaking 500s.

    Bot Framework authentication failures are 401s, malformed activities are
    400s. Keeping this helper independent of the conditional route makes the
    failure mapping testable without a Teams tenant.
    """
    from botbuilder.schema import Activity

    auth_header = request.headers.get("Authorization", "")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if adapter is None or bot is None:
        from chainlit.teams.app import adapter as teams_adapter, bot as teams_bot

        adapter, bot = teams_adapter, teams_bot

    try:
        body = await request.json()
        activity = Activity().deserialize(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid activity") from None

    expected_tenant = os.environ.get("TEAMS_APP_TENANT_ID")
    if expected_tenant:
        conversation = getattr(activity, "conversation", None)
        tenant_id = getattr(conversation, "tenant_id", None)
        if str(tenant_id or "") != expected_tenant:
            raise HTTPException(status_code=403, detail="Unexpected tenant")

    try:
        return await adapter.process_activity(activity, auth_header, bot.on_turn)
    except PermissionError:
        raise HTTPException(status_code=401, detail="Unauthorized") from None
    except Exception:
        logger.warning("Teams activity rejected", exc_info=True)
        raise HTTPException(status_code=400, detail="Invalid activity") from None


if os.environ.get("TEAMS_APP_ID") and os.environ.get("TEAMS_APP_PASSWORD"):

    @router.post("/teams/events")
    async def teams_endpoint(req: Request):
        return await _process_teams_activity(req)


# -------------------------------------------------------------------------------
#                               HTTP HANDLERS
# -------------------------------------------------------------------------------


def replace_between_tags(
    text: str, start_tag: str, end_tag: str, replacement: str
) -> str:
    """Replace text between two tags in a string."""

    pattern = start_tag + ".*?" + end_tag
    return re.sub(pattern, start_tag + replacement + end_tag, text, flags=re.DOTALL)


def prefix_asset_url(url: Optional[str], root_path: str) -> Optional[str]:
    """Prefix a root-relative asset URL with the app root path."""
    if not url or not root_path or not url.startswith("/") or url.startswith("//"):
        return url
    return f"{root_path.rstrip('/')}{url}"


def get_html_template(root_path):
    """
    Get HTML template for the index view.
    """
    root_path = root_path.rstrip("/")  # Avoid duplicated / when joining with root path.

    custom_theme = None
    custom_theme_file_path = Path(public_dir) / "theme.json"
    if (
        is_path_inside(custom_theme_file_path, Path(public_dir))
        and custom_theme_file_path.is_file()
    ):
        custom_theme = json.loads(custom_theme_file_path.read_text(encoding="utf-8"))

    PLACEHOLDER = "<!-- TAG INJECTION PLACEHOLDER -->"
    JS_PLACEHOLDER = "<!-- JS INJECTION PLACEHOLDER -->"
    CSS_PLACEHOLDER = "<!-- CSS INJECTION PLACEHOLDER -->"

    default_url = (
        config.ui.custom_meta_url or "https://github.com/JonasKenke/chainlit-ng"
    )
    default_meta_image_url = (
        "https://chainlit-cloud.s3.eu-west-3.amazonaws.com/logo/chainlit_banner.png"
    )
    meta_image_url = prefix_asset_url(
        config.ui.custom_meta_image_url or default_meta_image_url, root_path
    )
    favicon_path = prefix_asset_url("/favicon", root_path)

    tags = f"""<title>{config.ui.name}</title>
    <link rel="icon" href="{favicon_path}" />
    <meta name="description" content="{config.ui.description}">
    <meta property="og:type" content="website">
    <meta property="og:title" content="{config.ui.name}">
    <meta property="og:description" content="{config.ui.description}">
    <meta property="og:image" content="{meta_image_url}">
    <meta property="og:url" content="{default_url}">
    <meta property="og:root_path" content="{root_path}">"""

    js = f"""<script>
{f"window.theme = {json.dumps(custom_theme.get('variables'))};" if custom_theme and custom_theme.get("variables") else "undefined"}
{f"window.transports = {json.dumps(config.project.transports)};" if config.project.transports else "undefined"}
</script>"""

    css = None
    if config.ui.custom_css:
        css_url = prefix_asset_url(config.ui.custom_css, root_path)
        css = f"""<link rel="stylesheet" type="text/css" href="{css_url}" {config.ui.custom_css_attributes}>"""

    if config.ui.custom_js:
        js_url = prefix_asset_url(config.ui.custom_js, root_path)
        js += f"""<script src="{js_url}" {config.ui.custom_js_attributes}></script>"""

    font = None
    if custom_theme and "custom_fonts" in custom_theme:
        font = "\n".join(
            f"""<link rel="stylesheet" href="{prefix_asset_url(f, root_path)}">"""
            for f in custom_theme["custom_fonts"]
        )

    index_html_file_path = os.path.join(build_dir, "index.html")

    with open(index_html_file_path, encoding="utf-8") as f:
        content = f.read()
        content = content.replace('href="/', f'href="{root_path}/')
        content = content.replace('src="/', f'src="{root_path}/')
        content = content.replace(PLACEHOLDER, tags)
        if js:
            content = content.replace(JS_PLACEHOLDER, js)
        if css:
            content = content.replace(CSS_PLACEHOLDER, css)
        if font is not None:
            content = replace_between_tags(
                content, "<!-- FONT START -->", "<!-- FONT END -->", font
            )
        return content


def get_user_facing_url(url: URL):
    """
    Return the user facing URL for a given URL.
    Handles deployment with proxies (like cloud run).
    """
    chainlit_url = os.environ.get("CHAINLIT_URL")

    # No config, we keep the URL as is
    if not chainlit_url:
        url = url.replace(query="", fragment="")
        return url.__str__()

    config_url = URL(chainlit_url).replace(
        query="",
        fragment="",
    )
    # Remove trailing slash from config URL
    if config_url.path.endswith("/"):
        config_url = config_url.replace(path=config_url.path[:-1])

    return config_url.__str__() + url.path


# The OAuth callback sits on the same router as every other route, so it moves
# with config.run.root_path like the login callback does.
MCP_OAUTH_CALLBACK_PATH = "/mcp/oauth/callback"

MCP_OAUTH_CALLBACK_PAGE = """<!doctype html>
<html lang="en">
  <head><meta charset="utf-8"><title>MCP authorization complete</title></head>
  <body><p>Authorization complete. You can close this window and return to the chat.</p></body>
</html>
"""

# A failed /mcp connect answers with this instead of the exception text: the SDK
# error can quote a URL, a PRM document or a token from the OAuth flow.
MCP_CONNECT_FAILED_DETAIL = "Could not connect to the MCP server."

# P1 (CVE-2026-45018) closes the client-supplied connection path entirely.
# KRE-61 re-enables user-provided SSE/HTTP connections behind an allowlist.
MCP_USER_SERVERS_DISABLED_DETAIL = (
    "User-provided MCP servers are not enabled. "
    "Connect a server configured in features.mcp.servers instead."
)

# Fixed details for the OAuth gates. They are configuration advice, not error
# text: no user input, URL or exception is echoed.
MCP_OAUTH_SSE_UNSUPPORTED_DETAIL = (
    "OAuth is not supported for SSE MCP connections; use streamable HTTP."
)
MCP_OAUTH_URL_REQUIRED_DETAIL = "OAuth requires a user-provided MCP URL."
MCP_OAUTH_DISABLED_DETAIL = (
    "MCP OAuth is disabled. Set features.mcp.user_oauth.enabled = true."
)
MCP_OAUTH_NO_ORIGINS_DETAIL = (
    "MCP OAuth requires an authorization server origin in "
    "features.mcp.oauth_authorization_origins."
)
MCP_OAUTH_CHAINLIT_URL_DETAIL = (
    "OAuth requires an https CHAINLIT_URL (http is only allowed for localhost)."
)
MCP_OAUTH_HEADERS_DETAIL = "OAuth cannot be combined with custom headers."
MCP_OAUTH_URL_DETAIL = (
    "OAuth requires an http(s) MCP URL without userinfo and https outside localhost."
)
MCP_OAUTH_WELL_KNOWN_DETAIL = "OAuth MCP URLs cannot contain /.well-known/."
MCP_OAUTH_RATE_DETAIL = "Too many MCP authorization attempts; wait a minute and retry."

# Prompt/resource failures answer with fixed details: the server's error text
# can quote a URL, a token, or another user's data.
MCP_NOT_CONNECTED_DETAIL = "This MCP server is not connected."
MCP_PROMPT_FAILED_DETAIL = "Could not get the prompt from the MCP server."
MCP_PROMPT_NOT_FOUND_DETAIL = "This MCP server has no prompt with that name."
MCP_PROMPTS_UNSUPPORTED_DETAIL = "This MCP server does not support prompts."
MCP_PROMPT_ARGUMENTS_DETAIL = "This prompt needs different arguments."
MCP_RESOURCE_FAILED_DETAIL = "Could not read the resource from the MCP server."
MCP_RESOURCE_TOO_LARGE_DETAIL = (
    "The resource is larger than the size limit and was not loaded."
)
MCP_PROMPT_TOO_LARGE_DETAIL = (
    "The prompt is larger than the size limit and was not loaded."
)

_MAX_MCP_SESSIONS = int(os.environ.get("CHAINLIT_MAX_MCP_SESSIONS", "16") or 0)


def _mcp_capacity_reached(session, name: str) -> bool:
    """Whether a new (non-replacement) MCP connection exceeds the cap."""
    if _MAX_MCP_SESSIONS <= 0:
        return False
    reservations = getattr(session, "mcp_reservations", None)
    if not isinstance(reservations, dict):
        reservations = {}
    if name in session.mcp_sessions or name in session.mcp_oauth_tasks:
        return False
    if name in reservations:
        return True
    occupied = (
        set(session.mcp_sessions) | set(session.mcp_oauth_tasks) | set(reservations)
    )
    return len(occupied) >= _MAX_MCP_SESSIONS


def get_mcp_oauth_callback_url(request: Request) -> str:
    """Return the absolute URL the authorization server redirects back to.

    Derived like the OAuth login callback: ``get_user_facing_url`` applies
    ``CHAINLIT_URL`` and ``CHAINLIT_ROOT_PATH`` keeps the app prefix in front of
    the route, so the two callbacks always share origin and prefix.
    """
    root_path = os.environ.get("CHAINLIT_ROOT_PATH", "")
    if root_path == "/":
        root_path = ""
    url = request.url.replace(
        path=f"{root_path}{MCP_OAUTH_CALLBACK_PATH}",
        query="",
        fragment="",
    )
    return get_user_facing_url(url)


@router.get("/auth/config")
async def auth(request: Request):
    return get_configuration()


def _get_response_dict(access_token: str) -> dict:
    """Get the response dictionary for the auth response."""

    return {"success": True}


def _get_auth_response(access_token: str, redirect_to_callback: bool) -> Response:
    """Get the redirect params for the OAuth callback."""

    response_dict = _get_response_dict(access_token)

    if redirect_to_callback:
        root_path = os.environ.get("CHAINLIT_ROOT_PATH", "")
        root_path = "" if root_path == "/" else root_path
        redirect_url = (
            f"{root_path}/login/callback?{urllib.parse.urlencode(response_dict)}"
        )

        return RedirectResponse(
            # FIXME: redirect to the right frontend base url to improve the dev environment
            url=redirect_url,
            status_code=302,
        )

    return JSONResponse(response_dict)


def _get_oauth_redirect_error(
    request: Request, error: str, status_code: int = 307
) -> Response:
    """Get the redirect response for an OAuth error."""
    params = urllib.parse.urlencode(
        {
            "error": error,
        }
    )
    # Root-relative: request.url_for() would turn an attacker-influenced Host
    # header into an absolute open redirect.
    root_path = config.run.root_path.rstrip("/")
    response = RedirectResponse(
        url=f"{root_path}/login?{params}",
        status_code=status_code,
    )
    clear_oauth_cookies(response)
    return response


async def _authenticate_user(
    request: Request, user: Optional[User], redirect_to_callback: bool = False
) -> Response:
    """Authenticate a user and return the response."""

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="credentialssignin",
        )

    # If a data layer is defined, attempt to persist user.
    if data_layer := get_data_layer():
        from chainlit.auth import resolve_provider_identity

        user = await resolve_provider_identity(user, data_layer)
        try:
            await data_layer.create_user(user)
        except Exception as e:
            # Catch and log exceptions during user creation.
            # TODO: Make this catch only specific errors and allow others to propagate.
            logger.error(f"Error creating user: {e}")

    access_token = create_jwt(user)

    response = _get_auth_response(access_token, redirect_to_callback)

    set_auth_cookie(request, response, access_token)

    return response


_LOGIN_MAX_ATTEMPTS = int(os.environ.get("CHAINLIT_LOGIN_MAX_ATTEMPTS", "20") or 0)
_LOGIN_WINDOW_SECONDS = max(
    1, int(os.environ.get("CHAINLIT_LOGIN_WINDOW_SECONDS", "60") or 60)
)


def _login_attempt_key(request: Request, username: str) -> str:
    client = request.client.host if request.client else "unknown"
    identity = f"{client}\0{username.lower()}".encode()
    return f"login-attempt:{hashlib.sha256(identity).hexdigest()}"


async def _enforce_login_rate_limit(request: Request, username: str) -> None:
    if _LOGIN_MAX_ATTEMPTS <= 0:
        return
    from chainlit.state_store import get_state_store

    try:
        count = await get_state_store().increment(
            _login_attempt_key(request, username), ttl=_LOGIN_WINDOW_SECONDS
        )
    except Exception:
        # Availability over throttling: never take password login down when
        # the coordination store is unavailable.
        logger.exception("Unable to update login rate limiter")
        return
    if count > _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts",
            headers={"Retry-After": str(_LOGIN_WINDOW_SECONDS)},
        )


async def _clear_login_rate_limit(request: Request, username: str) -> None:
    from chainlit.state_store import get_state_store

    try:
        await get_state_store().delete(_login_attempt_key(request, username))
    except Exception:
        pass


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """
    Login a user using the password auth callback.
    """
    _require_same_origin(request)

    if not config.code.password_auth_callback:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No auth_callback defined"
        )

    await _enforce_login_rate_limit(request, form_data.username)

    user = await config.code.password_auth_callback(
        form_data.username, form_data.password
    )
    if user:
        await _clear_login_rate_limit(request, form_data.username)

    return await _authenticate_user(request, user)


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout the user by calling the on_logout callback.

    Identity is resolved best effort: an absent, expired or invalid token must
    not turn logout into a 401 before the cookie is cleared. When it resolves,
    the user's MCP credentials and in-flight authorizations are dropped before
    ``on_logout`` runs, so a failing callback cannot leave them behind.
    """
    from chainlit.auth.jwt import revoke_token
    from chainlit.mcp_oauth import get_pending_authorizations, get_token_store

    _require_same_origin(request)

    token = get_token_from_cookies(request.cookies)
    if not token:
        authorization = request.headers.get("Authorization", "")
        scheme, _, credentials = authorization.partition(" ")
        if scheme.lower() == "bearer" and credentials:
            token = credentials
    if token:
        await revoke_token(token)

    user = await resolve_current_user_optional(request)
    if user is not None:
        from chainlit.session import WebsocketSession

        get_token_store().forget_user(user.identifier)
        get_pending_authorizations().cancel_user(user.identifier)
        for session in WebsocketSession.sessions_for_user(user.identifier):
            session.to_clear = True
            try:
                await sio.disconnect(session.socket_id)
            except Exception:
                logger.exception("Unable to disconnect a logged-out session")

    clear_auth_cookie(request, response)

    if config.code.on_logout:
        try:
            return await config.code.on_logout(request, response)
        except Exception:
            # Still answer with the response that carries the cleared cookie.
            logger.exception("on_logout callback failed")
            return {"success": True}

    return {"success": True}


@router.post("/auth/jwt")
async def jwt_auth(request: Request):
    """Login a user using a valid jwt."""
    from jwt import InvalidTokenError

    _require_same_origin(request)

    auth_header: Optional[str] = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Authorization header missing")

    # Check if it starts with "Bearer "
    try:
        scheme, token = auth_header.split()
        if scheme.lower() != "bearer":
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication scheme. Please use Bearer",
            )
    except ValueError:
        raise HTTPException(
            status_code=401, detail="Invalid authorization header format"
        )

    try:
        user = decode_jwt(token)
        return await _authenticate_user(request, user)
    except InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.post("/auth/header")
async def header_auth(request: Request):
    """Login a user using the header_auth_callback."""
    _require_same_origin(request)

    if not config.code.header_auth_callback:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No header_auth_callback defined",
        )

    user = await config.code.header_auth_callback(request.headers)

    return await _authenticate_user(request, user)


def _azure_hf_authority() -> str:
    return os.environ.get(
        "OAUTH_AZURE_AD_HYBRID_AUTHORITY",
        "https://login.microsoftonline.com",
    ).rstrip("/")


def _azure_hf_tenant() -> str:
    return os.environ.get("OAUTH_AZURE_AD_HYBRID_TENANT_ID", "common")


def _azure_hf_pinned_issuer() -> Optional[str]:
    if not os.environ.get("OAUTH_AZURE_AD_HYBRID_ENABLE_SINGLE_TENANT"):
        return None
    tenant = _azure_hf_tenant()
    if tenant.lower() in {"common", "organizations", "consumers"}:
        return None
    return f"{_azure_hf_authority()}/{tenant}/v2.0"


def _azure_hf_jwks_url() -> str:
    tenant = _azure_hf_tenant() if _azure_hf_pinned_issuer() else "common"
    return f"{_azure_hf_authority()}/{tenant}/discovery/v2.0/keys"


@lru_cache(maxsize=4)
def _azure_hf_jwks_client(url: str):
    import jwt

    return jwt.PyJWKClient(url, cache_keys=True)


def _azure_hf_issuer_allowed(decoded: dict) -> bool:
    issuer = str(decoded.get("iss") or "")
    parsed = urllib.parse.urlparse(issuer)
    authority = urllib.parse.urlparse(_azure_hf_authority())
    if parsed.scheme != "https" or parsed.netloc != authority.netloc:
        return False
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[1] != "v2.0":
        return False
    tenant_id = parts[0]
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", tenant_id):
        return False
    token_tenant = decoded.get("tid")
    return not token_tenant or str(token_tenant).lower() == tenant_id.lower()


async def _verify_azure_hf_id_token(
    provider, id_token: str, expected_nonce: Optional[str]
) -> dict:
    """Verify the browser-supplied azure-ad-hybrid id_token before trusting it."""
    import jwt

    if not expected_nonce:
        raise ValueError("missing nonce cookie for azure-ad-hybrid login")
    if not provider.client_id:
        raise ValueError("azure-ad-hybrid client id is not configured")

    jwks_client = _azure_hf_jwks_client(_azure_hf_jwks_url())
    signing_key = await asyncio.to_thread(
        jwks_client.get_signing_key_from_jwt, id_token
    )

    decode_kwargs: dict = {
        "algorithms": ["RS256"],
        "audience": provider.client_id,
        "options": {"require": ["exp", "aud", "iss", "nonce"]},
    }
    if pinned_issuer := _azure_hf_pinned_issuer():
        decode_kwargs["issuer"] = pinned_issuer

    decoded = jwt.decode(id_token, signing_key.key, **decode_kwargs)

    if not _azure_hf_pinned_issuer() and not _azure_hf_issuer_allowed(decoded):
        raise ValueError("unexpected id_token issuer")

    token_nonce = decoded.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(
        token_nonce, expected_nonce
    ):
        raise ValueError("id_token nonce does not match the login nonce")

    return decoded


@router.get("/auth/oauth/{provider_id}")
async def oauth_login(provider_id: str, request: Request):
    """Redirect the user to the oauth provider login page."""
    if config.code.oauth_callback is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No oauth_callback defined",
        )

    provider = get_oauth_provider(provider_id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider {provider_id} not found",
        )

    random = random_secret(32)

    authorize_params = dict(provider.authorize_params)
    nonce = None
    if getattr(provider, "uses_per_login_nonce", False):
        nonce = random_secret(16)
        authorize_params["nonce"] = nonce

    params = urllib.parse.urlencode(
        {
            "client_id": provider.client_id,
            "redirect_uri": f"{get_user_facing_url(request.url)}/callback",
            "state": random,
            **authorize_params,
        }
    )
    response = RedirectResponse(
        url=f"{provider.authorize_url}?{params}",
    )

    set_oauth_state_cookie(response, random, request)
    if nonce is not None:
        set_oauth_nonce_cookie(response, nonce, request)

    return response


@router.get("/auth/oauth/{provider_id}/callback")
async def oauth_callback(
    provider_id: str,
    request: Request,
    error: Optional[str] = None,
    code: Optional[str] = None,
    state: Optional[str] = None,
):
    """Handle the oauth callback and login the user."""

    if config.code.oauth_callback is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No oauth_callback defined",
        )

    provider = get_oauth_provider(provider_id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider {provider_id} not found",
        )

    if error:
        return _get_oauth_redirect_error(request, error)

    if not code or not state:
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        validate_oauth_state_cookie(request, state)
    except Exception:
        logger.warning("Unable to validate oauth state for provider %s", provider_id)
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    url = get_user_facing_url(request.url)
    try:
        token = await provider.get_token(code, url)
    except Exception:
        logger.warning(
            "OAuth token exchange failed for provider %s", provider_id, exc_info=True
        )
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        (raw_user_data, default_user) = await provider.get_user_info(token)
    except Exception:
        logger.warning(
            "OAuth user info failed for provider %s", provider_id, exc_info=True
        )
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        user = await config.code.oauth_callback(
            provider_id, token, raw_user_data, default_user
        )
    except Exception as e:
        logger.warning(
            "OAuth callback failed for provider %s: %s", provider_id, type(e).__name__
        )
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        response = await _authenticate_user(request, user, redirect_to_callback=True)
    except Exception:
        logger.warning("OAuth authentication failed for provider %s", provider_id)
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    clear_oauth_cookies(response)

    return response


# specific route for azure ad hybrid flow
@router.post("/auth/oauth/azure-ad-hybrid/callback")
async def oauth_azure_hf_callback(
    request: Request,
    error: Annotated[Optional[str], Form()] = None,
    code: Annotated[Optional[str], Form()] = None,
    state: Annotated[Optional[str], Form()] = None,
    id_token: Annotated[Optional[str], Form()] = None,
):
    """Handle the azure ad hybrid flow callback and login the user."""

    provider_id = "azure-ad-hybrid"
    if config.code.oauth_callback is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No oauth_callback defined",
        )

    provider = get_oauth_provider(provider_id)
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Provider {provider_id} not found",
        )

    if error:
        return _get_oauth_redirect_error(request, error)

    if not code:
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    # The provider posts both code and id_token back to the browser
    # (response_mode=form_post). Validate the state cookie, then verify the
    # id_token before it is trusted or forwarded to the app callback.
    if not state:
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        validate_oauth_state_cookie(request, state)
    except Exception:
        logger.warning("Unable to validate oauth state for provider %s", provider_id)
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    if not id_token:
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        await _verify_azure_hf_id_token(
            provider, id_token, get_oauth_nonce_cookie(request)
        )
    except Exception:
        logger.warning("Unable to verify azure-ad-hybrid id_token", exc_info=True)
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    url = get_user_facing_url(request.url)
    try:
        token = await provider.get_token(code, url)
    except Exception:
        logger.warning(
            "OAuth token exchange failed for provider %s", provider_id, exc_info=True
        )
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        (raw_user_data, default_user) = await provider.get_user_info(token)
    except Exception:
        logger.warning(
            "OAuth user info failed for provider %s", provider_id, exc_info=True
        )
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        user = await config.code.oauth_callback(
            provider_id, token, raw_user_data, default_user, id_token
        )
    except Exception as e:
        logger.warning(
            "OAuth callback failed for provider %s: %s", provider_id, type(e).__name__
        )
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    try:
        response = await _authenticate_user(request, user, redirect_to_callback=True)
    except Exception:
        logger.warning("OAuth authentication failed for provider %s", provider_id)
        return _get_oauth_redirect_error(request, "oauthSignin", status_code=303)

    clear_oauth_cookies(response)

    return response


GenericUser = Union[User, PersistedUser, None]
UserParam = Annotated[GenericUser, Depends(get_current_user)]
OptionalUserParam = Annotated[GenericUser, Depends(get_current_user_optional)]

_SCHEDULED_TASK_METHODS = (
    "create_scheduled_task",
    "list_scheduled_tasks",
    "get_scheduled_task",
    "update_scheduled_task",
    "delete_scheduled_task",
    "request_scheduled_task_run",
    "claim_due_scheduled_tasks",
    "renew_scheduled_task_lease",
    "complete_scheduled_task_run",
)


def _supports_scheduled_tasks(data_layer: Optional[BaseDataLayer]) -> bool:
    if data_layer is None:
        return False
    return all(
        callable(getattr(data_layer, method, None))
        and getattr(type(data_layer), method, None)
        is not getattr(BaseDataLayer, method)
        for method in _SCHEDULED_TASK_METHODS
    )


_PROMPT_GALLERY_METHODS = (
    "create_prompt",
    "list_prompts",
    "get_prompt",
    "update_prompt",
    "delete_prompt",
)


def _overrides_base_method(data_layer: BaseDataLayer, method: str) -> bool:
    """Return whether a layer really implements a base method.

    Check the class, not the instance: mocks carrying a spec expose callable
    attributes but have not implemented anything.
    """
    implementation = getattr(type(data_layer), method, None)
    return callable(implementation) and implementation is not getattr(
        BaseDataLayer, method, None
    )


def _supports_prompt_gallery(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a data layer implements all prompt gallery operations."""
    if data_layer is None:
        return False

    return all(
        _overrides_base_method(data_layer, method) for method in _PROMPT_GALLERY_METHODS
    )


_PROMPT_VERSION_METHODS = (
    "list_prompt_versions",
    "get_prompt_version",
    "list_prompt_updates",
    "ack_prompt_update",
)


def _supports_prompt_versioning(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a data layer stores immutable prompt versions and notices."""
    if data_layer is None:
        return False
    return all(
        _overrides_base_method(data_layer, method) for method in _PROMPT_VERSION_METHODS
    )


_SKILL_LIBRARY_METHODS = (
    "create_skill",
    "list_skills",
    "get_skill",
    "update_skill",
    "delete_skill",
)


def _supports_skill_library(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a data layer implements per-user skill persistence."""
    if data_layer is None:
        return False
    return all(
        _overrides_base_method(data_layer, method) for method in _SKILL_LIBRARY_METHODS
    )


_SKILL_VERSION_METHODS = ("list_prompt_versions", "get_prompt_version")


def _supports_skill_versions(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a layer can read the immutable version store."""
    if data_layer is None:
        return False
    return all(
        _overrides_base_method(data_layer, method) for method in _SKILL_VERSION_METHODS
    )


_SKILL_SHARING_METHODS = (
    "get_shared_skill",
    "list_skill_updates",
    "ack_skill_update",
)


def _supports_skill_sharing(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a layer stores shared skills, versions and notices."""
    if not _supports_skill_library(data_layer) or not _supports_skill_versions(
        data_layer
    ):
        return False
    assert data_layer is not None
    return all(
        _overrides_base_method(data_layer, method) for method in _SKILL_SHARING_METHODS
    )


def _supports_delete_all_threads(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a data layer overrides bulk thread deletion."""
    if data_layer is None:
        return False

    method = getattr(type(data_layer), "delete_all_threads", None)
    return callable(method) and method is not BaseDataLayer.delete_all_threads


@router.get("/user")
async def get_user(current_user: UserParam, response: Response) -> GenericUser:
    response.headers["Cache-Control"] = "private, no-store"
    return redact_sensitive_metadata(current_user)


_language_pattern = (
    "^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,4})?(-[a-zA-Z0-9]{2,8})?(-x-[a-zA-Z0-9]{1,8})?$"
)


@router.post("/set-session-cookie")
async def set_session_cookie(request: Request, response: Response):
    from chainlit.session import is_valid_session_id

    _require_same_origin(request)

    body = await request.json()
    session_id = body.get("session_id")

    if not is_valid_session_id(session_id):
        raise HTTPException(status_code=400, detail="Invalid session id")

    is_local = request.client and request.client.host in ["127.0.0.1", "localhost"]

    response.set_cookie(
        key="X-Chainlit-Session-id",
        value=session_id,
        path="/",
        httponly=True,
        secure=not is_local,
        samesite="lax" if is_local else "none",
    )

    return {"message": "Session cookie set"}


@router.get("/project/translations")
async def project_translations(
    language: str = Query(
        default="en-US", description="Language code", pattern=_language_pattern
    ),
):
    """Return project translations."""

    # Use configured language if set, otherwise use the language from query
    effective_language = config.ui.language or language

    # Load translation based on the effective language
    translation = config.load_translation(effective_language)

    return JSONResponse(
        content={
            "translation": translation,
        }
    )


def _thread_grants_active(data_layer: Optional[BaseDataLayer]) -> bool:
    """Named thread sharing needs the flag, login, and a capable data layer."""
    return bool(
        config.features.thread_grants
        and require_login()
        and data_layer is not None
        and getattr(data_layer, "thread_grants_supported", False)
    )


def _no_store(response: JSONResponse) -> JSONResponse:
    """Caller-varying thread responses must never be cached by a shared cache."""
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _require_same_origin(request: Request) -> None:
    """Reject a present cross-origin Origin header on state-changing routes.

    An absent Origin (CLI/API clients) is allowed. Explicit entries in
    ``project.allow_origins`` are trusted for cross-origin frontends; a
    wildcard is deliberately not, because it would make the check a no-op.
    Scheme is part of the comparison: an HTTP page on the HTTPS host must not
    be able to drive a credentialed login.
    """
    origin = request.headers.get("origin")
    if not origin:
        return

    try:
        parsed = URL(origin)
        origin_scheme = parsed.scheme
        origin_netloc = parsed.netloc
    except Exception:
        origin_scheme = ""
        origin_netloc = ""

    if origin_scheme not in ("http", "https") or not origin_netloc:
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")

    # Keep the pre-CHAINLIT_URL downgrade protection: an HTTPS request must not
    # be driven by an HTTP browser origin, even if the configured external URL
    # or an explicit allowlist says HTTP.
    if request.url.scheme == "https" and origin_scheme.lower() != "https":
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")

    expected = _configured_external_origin(request)
    if expected is not None and (
        origin_scheme.lower() == expected[0] and origin_netloc.lower() == expected[1]
    ):
        return

    for entry in config.project.allow_origins:
        if entry == "*":
            continue
        try:
            allowed_origin = URL(entry)
        except Exception:
            continue
        if (
            allowed_origin.scheme == origin_scheme
            and allowed_origin.netloc == origin_netloc
        ):
            return

    raise HTTPException(status_code=403, detail="Cross-origin request rejected")


def _thread_grants_layer() -> BaseDataLayer:
    """Grant routes: 400 without a layer, 404 while off, 501 without support."""
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    if not config.features.thread_grants:
        raise HTTPException(status_code=404, detail="Thread not found")
    if not getattr(data_layer, "thread_grants_supported", False):
        raise HTTPException(
            status_code=501,
            detail="Thread grants are not supported by the data layer",
        )
    return data_layer


def _feedback_gate_active(data_layer: BaseDataLayer) -> bool:
    """Whether feedback mutations are owner-gated; 403 if the layer cannot.

    With login enabled, a capable data layer always enforces ownership. The
    thread-grants flag no longer decides this: otherwise every authenticated
    caller could update or delete another user's feedback by id. Anonymous
    (no-login) deployments keep the legacy behavior because they have no
    principal to compare.
    """
    if not getattr(data_layer, "feedback_authorization_supported", False):
        if _thread_grants_active(data_layer):
            raise HTTPException(
                status_code=403,
                detail="Feedback authorization is not supported by the data layer",
            )
        # Legacy/custom layers: still gate authenticated callers through the
        # thread-author check. Existing feedback ids fail closed in
        # _authorize_feedback_update because the row lookup is unsupported.
        return require_login()
    return require_login()


def _same_principal(one: Optional[User], other: Optional[User]) -> bool:
    """Compare the persisted generation when both sides know it."""
    if one is None or other is None:
        return False
    if isinstance(one, PersistedUser) and isinstance(other, PersistedUser):
        return one.id == other.id and one.createdAt == other.createdAt
    return one.identifier == other.identifier


@asynccontextmanager
async def _mutation_lock(key: str, label: str):
    """Serialize one stateful HTTP mutation and map contention to HTTP 409."""
    try:
        async with get_state_store().lock(key, ttl=10, wait=5):
            yield
    except LockNotAcquired as exc:
        raise HTTPException(status_code=409, detail=f"{label} is busy; retry") from exc


@asynccontextmanager
async def _mutation_locks(keys: List[str], label: str):
    """Acquire related mutation locks in a stable order across workers."""
    async with AsyncExitStack() as stack:
        for key in sorted(set(keys)):
            await stack.enter_async_context(_mutation_lock(key, label))
        yield


@asynccontextmanager
async def _locked_pinned_source(
    data_layer: BaseDataLayer,
    user_id: str,
    copy_id: str,
    *,
    skill: bool,
):
    """Re-read a pinned copy and its source while both mutation locks are held."""
    label = "Skill" if skill else "Prompt"
    prefix = "skill-mutation" if skill else "prompt-mutation"
    while True:
        if skill:
            initial = await _dl_skill(data_layer.get_skill(copy_id, user_id))
        else:
            initial = await _dl_prompt(data_layer.get_prompt(copy_id))
        if not initial or initial["userId"] != user_id:
            raise HTTPException(status_code=404, detail=f"{label} not found")
        initial_source_id = initial.get("sourcePromptId")
        if not initial_source_id:
            raise _prompt_error(
                409, "not_pinned", f"This {label.lower()} is not pinned to a source"
            )

        async with _mutation_locks(
            [f"{prefix}:{copy_id}", f"{prefix}:{initial_source_id}"], label
        ):
            if skill:
                copy = await _dl_skill(data_layer.get_skill(copy_id, user_id))
            else:
                copy = await _dl_prompt(data_layer.get_prompt(copy_id))
            if not copy or copy["userId"] != user_id:
                raise HTTPException(status_code=404, detail=f"{label} not found")
            source_id = copy.get("sourcePromptId")
            if not source_id:
                raise _prompt_error(
                    409, "not_pinned", f"This {label.lower()} is not pinned to a source"
                )
            if source_id != initial_source_id:
                continue

            if skill:
                source = await _dl_skill(data_layer.get_shared_skill(source_id))
                available = bool(source)
            else:
                source = await _dl_prompt(data_layer.get_prompt(source_id))
                available = bool(source and source.get("isShared"))
            if not available:
                raise _prompt_error(
                    409,
                    "source_unavailable",
                    f"The source {label.lower()} is no longer available",
                )
            yield copy, source
        return


def _require_session_principal(
    session: "WebsocketSession",
    current_user: Optional[User],
    *,
    status_code: int = 401,
    detail: str = "You are not authorized to use this session",
) -> None:
    """Bind authenticated HTTP calls to the session's persisted generation."""
    if current_user is None:
        return
    if not _same_principal(session.user, current_user):
        raise HTTPException(status_code=status_code, detail=detail)


async def _authorize_feedback_update(
    update: UpdateFeedbackRequest,
    current_user: Optional[User],
    data_layer: BaseDataLayer,
) -> None:
    """Owner-gate a feedback write by the feedback's own thread and step."""
    session = WebsocketSession.get_by_id(update.sessionId)
    if session is None or not _same_principal(session.user, current_user):
        raise HTTPException(status_code=403, detail="Session does not match the caller")

    feedback = update.feedback
    if feedback.threadId is None:
        raise HTTPException(status_code=404, detail="Thread not found")

    access = await authorize_thread_read(
        feedback.threadId, current_user, data_layer=data_layer
    )
    if access != ThreadAccess.OWNER:
        raise HTTPException(status_code=404, detail="Thread not found")

    try:
        belongs = await data_layer.step_belongs_to_thread(
            feedback.threadId, feedback.forId
        )
    except Exception:
        belongs = False
    if not belongs:
        raise HTTPException(status_code=404, detail="Thread not found")

    if feedback.id is None:
        return

    try:
        lookup = await data_layer.get_feedback_thread_id(feedback.id)
    except Exception:
        raise HTTPException(status_code=404, detail="Thread not found")
    if lookup is not None and lookup.status == "not_found":
        return
    if (
        lookup is not None
        and lookup.status == "found"
        and lookup.thread_id
        and lookup.thread_id == feedback.threadId
    ):
        return
    raise HTTPException(status_code=404, detail="Thread not found")


async def _can_delete_feedback(
    feedback_id: str,
    current_user: Optional[User],
    data_layer: BaseDataLayer,
) -> bool:
    """Delete only a row whose thread resolves and whose owner is the caller."""
    try:
        lookup = await data_layer.get_feedback_thread_id(feedback_id)
    except Exception:
        return False
    if lookup is None or lookup.status != "found" or not lookup.thread_id:
        return False
    return (
        await authorize_thread_read(
            lookup.thread_id, current_user, data_layer=data_layer
        )
        == ThreadAccess.OWNER
    )


@router.get("/project/settings")
async def project_settings(
    current_user: UserParam,
    language: str = Query(
        default="en-US", description="Language code", pattern=_language_pattern
    ),
    chat_profile: Optional[str] = Query(
        default=None, description="Current chat profile name"
    ),
):
    """Return project settings. This is called by the UI before the establishing the websocket connection."""

    # Use configured language if set, otherwise use the language from query
    effective_language = config.ui.language or language

    # Load the markdown file based on the provided language
    markdown = get_markdown_str(config.root, effective_language)

    chat_profiles = []
    profiles: list[dict] = []
    if config.code.set_chat_profiles:
        chat_profiles = await config.code.set_chat_profiles(
            current_user, effective_language
        )
        if chat_profiles:
            for p in chat_profiles:
                d = p.to_dict()
                d.pop("config_overrides", None)
                profiles.append(d)

    scheduled_models: list[str] = []
    if config.code.set_scheduled_task_models:
        scheduled_models = list(
            dict.fromkeys(
                await config.code.set_scheduled_task_models(
                    current_user, effective_language
                )
            )
        )

    starters = []
    if config.code.set_starters:
        s = await config.code.set_starters(current_user, effective_language)
        if s:
            starters = [it.to_dict() for it in s]

    starter_categories = []
    if config.code.set_starter_categories:
        sc = await config.code.set_starter_categories(
            current_user, effective_language, chat_profile
        )
        if sc:
            starter_categories = [it.to_dict() for it in sc]

    data_layer = get_data_layer()

    cfg = config
    if chat_profile and chat_profiles:
        current_profile = next(
            (p for p in chat_profiles if p.name == chat_profile), None
        )
        if current_profile and getattr(current_profile, "config_overrides", None):
            cfg = config.with_overrides(current_profile.config_overrides)

    scheduled_hooks = [
        {"name": name, "default": bool(value.get("default", True))}
        for name, value in config.code.scheduled_task_after_hooks.items()
    ]
    scheduled_user = False
    if current_user is not None and data_layer:
        persisted = await data_layer.get_user(current_user.identifier)
        scheduled_user = bool(
            persisted
            and (
                not isinstance(current_user, PersistedUser)
                or persisted.id == current_user.id
            )
        )

    features_dict = cfg.features.model_dump()
    # Strip sensitive details from MCP server configs — only expose name and
    # type to clients. Read from the *effective* (profile-overridden) cfg:
    # a chat profile can set features.mcp.servers itself, and redacting the
    # base config would serve that profile's command/env/headers verbatim.
    if "mcp" in features_dict and isinstance(features_dict["mcp"].get("servers"), list):
        features_dict["mcp"]["servers"] = [
            {"name": s.name, "type": s.type} for s in cfg.features.mcp.servers
        ]
    # Clients only need to know whether user-provided servers are enabled.
    if "mcp" in features_dict and isinstance(
        features_dict["mcp"].get("user_servers"), dict
    ):
        features_dict["mcp"]["user_servers"] = {
            "enabled": features_dict["mcp"]["user_servers"].get("enabled", False)
        }

    return JSONResponse(
        headers={"Cache-Control": "private, no-store"},
        content={
            "ui": cfg.ui.model_dump(),
            # Explicit origins are the only cross-origin window-message peers
            # the UI will trust. A wildcard is served as-is but is not matched
            # against event.origin by the client.
            "allowOrigins": list(config.project.allow_origins),
            "features": features_dict,
            "userEnv": cfg.project.user_env,
            "maskUserEnv": cfg.project.mask_user_env,
            "dataPersistence": data_layer is not None,
            "threadResumable": bool(config.code.on_chat_resume),
            # Expose whether shared threads feature is enabled (flag + app callback)
            "threadSharing": bool(
                getattr(cfg.features, "allow_thread_sharing", False)
                and getattr(config.code, "on_shared_thread_view", None)
            ),
            "threadGrants": _thread_grants_active(data_layer),
            "promptGallery": bool(
                getattr(cfg.features, "prompt_gallery", False)
                and _supports_prompt_gallery(data_layer)
            ),
            "promptVersioning": bool(
                getattr(cfg.features, "prompt_gallery", False)
                and _supports_prompt_gallery(data_layer)
                and _supports_prompt_versioning(data_layer)
            ),
            "skillsLibrary": bool(
                getattr(cfg.features, "skills", False)
                and _supports_skill_library(data_layer)
            ),
            "skillsSharing": bool(
                getattr(cfg.features, "skills", False)
                and _supports_skill_sharing(data_layer)
            ),
            "threadClearAll": _supports_delete_all_threads(data_layer),
            "promptScheduling": bool(
                getattr(cfg.features, "prompt_scheduling", False)
                and scheduled_user
                and _supports_scheduled_tasks(data_layer)
                and config.code.on_message is not None
            ),
            "scheduledTaskAfterHooks": scheduled_hooks,
            "markdown": markdown,
            "chatProfiles": profiles,
            "scheduledTaskModels": scheduled_models,
            "starters": starters,
            "starterCategories": starter_categories,
        },
    )


@router.put("/feedback")
async def update_feedback(
    request: Request,
    update: UpdateFeedbackRequest,
    current_user: UserParam,
):
    """Update the human feedback for a particular message."""
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=500, detail="Data persistence is not enabled")

    if _feedback_gate_active(data_layer):
        await _authorize_feedback_update(update, current_user, data_layer)

    try:
        feedback_id = await data_layer.upsert_feedback(feedback=update.feedback)

        if config.code.on_feedback:
            try:
                from chainlit.context import init_ws_context
                from chainlit.session import WebsocketSession

                session = WebsocketSession.get_by_id(update.sessionId)
                init_ws_context(session)

                await config.code.on_feedback(update.feedback)
            except Exception as callback_error:
                logger.error(
                    f"Error in user-provided on_feedback callback: {callback_error}"
                )
                # Optionally, you could continue without raising an exception to avoid disrupting the endpoint.
    except Exception:
        logger.exception("Failed to update feedback")
        raise HTTPException(
            status_code=500, detail="Failed to update feedback"
        ) from None

    return JSONResponse(content={"success": True, "feedbackId": feedback_id})


@router.delete("/feedback")
async def delete_feedback(
    request: Request,
    payload: DeleteFeedbackRequest,
    current_user: UserParam,
):
    """Delete a feedback."""

    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    feedback_id = payload.feedbackId

    if _feedback_gate_active(data_layer):
        if not current_user:
            raise HTTPException(status_code=401, detail="Unauthorized")
        if not await _can_delete_feedback(feedback_id, current_user, data_layer):
            return JSONResponse(content={"success": True})

    await data_layer.delete_feedback(feedback_id)
    return JSONResponse(content={"success": True})


@router.post("/project/threads")
async def get_user_threads(
    request: Request,
    payload: GetThreadsRequest,
    current_user: UserParam,
):
    """Get the threads page by page."""

    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    if not isinstance(current_user, PersistedUser):
        persisted_user = await data_layer.get_user(identifier=current_user.identifier)
        if not persisted_user:
            raise HTTPException(status_code=404, detail="User not found")
    else:
        persisted_user = current_user
    payload.filter.userId = persisted_user.id

    grants_active = _thread_grants_active(data_layer)
    if grants_active:
        payload.filter.sharedWithUserId = persisted_user.id
        payload.filter.sharedWithUserCreatedAt = persisted_user.createdAt
    else:
        # M7: a client-crafted filter can neither add rows nor survive the off path.
        payload.filter.sharedWithUserId = None
        payload.filter.sharedWithUserCreatedAt = None

    res = await data_layer.list_threads(payload.pagination, payload.filter)
    response = res.to_dict()
    live_ids = await live_interaction_ids(response["data"])
    if not grants_active:
        response["data"] = [
            public_thread(thread, live_ids=live_ids) for thread in response["data"]
        ]
        return JSONResponse(content=response)

    visible_threads = []
    visible_ids = []
    for thread in response["data"]:
        # L2: decide without the row, so an owner-only Dynamo list row is not
        # mistaken for an ownerless thread; this check can only remove rows.
        access = await authorize_thread_read(
            thread["id"], persisted_user, data_layer=data_layer
        )
        if access not in (ThreadAccess.OWNER, ThreadAccess.SHARED):
            continue
        visible_ids.append(thread["id"])
        visible_threads.append(
            public_thread(
                thread,
                include_owner=access == ThreadAccess.OWNER,
                live_ids=live_ids,
            )
        )
    response["data"] = visible_threads
    response["pageInfo"]["startCursor"] = visible_ids[0] if visible_ids else None
    response["pageInfo"]["endCursor"] = visible_ids[-1] if visible_ids else None
    return JSONResponse(content=response)


@router.get("/project/thread/{thread_id}")
async def get_thread(
    request: Request,
    thread_id: str,
    current_user: UserParam,
):
    """Get a specific thread."""
    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    access = await authorize_thread_read(thread_id, current_user, data_layer=data_layer)
    if access in (ThreadAccess.MISSING, ThreadAccess.OWNERLESS):
        raise HTTPException(status_code=404, detail="Thread not found")
    if access != ThreadAccess.OWNER:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        res = await data_layer.get_thread(thread_id)
    except Exception as e:
        logger.error(f"Failed to get thread {thread_id}: {e!s}")
        raise HTTPException(
            status_code=500,
            detail="Failed to load conversation history",
        )
    live_ids = await live_interaction_ids([res]) if res else set()
    return _no_store(
        JSONResponse(
            content=public_thread(res, include_settings=True, live_ids=live_ids)
        )
    )


@router.get("/project/share/{thread_id}")
async def get_shared_thread(
    request: Request,
    thread_id: str,
    current_user: OptionalUserParam,
):
    """Get a shared thread (read-only for everyone).

    This endpoint is separate from the resume endpoint and does not require the caller
    to be the author of the thread. It only returns the thread if its metadata
    contains is_shared=True. Otherwise, it returns 404 to avoid leaking existence.
    """

    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    # No auth required: allow anonymous access to shared threads
    thread = await data_layer.get_thread(thread_id)

    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")

    access = await authorize_thread_read(
        thread_id,
        current_user,
        thread=thread,
        allow_public_share=True,
        data_layer=data_layer,
    )
    if access not in (ThreadAccess.OWNER, ThreadAccess.PUBLIC, ThreadAccess.SHARED):
        raise HTTPException(status_code=404, detail="Thread not found")

    # Legacy share attribution: with thread grants off the owner identity is
    # kept (an existing contract), and stripped only when grants are active.
    live_ids = await live_interaction_ids([thread])
    return _no_store(
        JSONResponse(
            content=public_thread(
                thread,
                # Anonymous share links do not get owner attribution.
                include_owner=bool(current_user)
                and not _thread_grants_active(data_layer),
                live_ids=live_ids,
                strip_storage_keys=True,
            )
        )
    )


# ---- Prompt scheduling endpoints ----


def _scheduled_feature_enabled() -> None:
    if not config.features.prompt_scheduling:
        raise HTTPException(
            status_code=400, detail="Prompt scheduling feature is not enabled"
        )


async def _scheduled_user_id(
    current_user: GenericUser, data_layer: BaseDataLayer
) -> str:
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    persisted = await data_layer.get_user(identifier=current_user.identifier)
    if not persisted or (
        isinstance(current_user, PersistedUser) and persisted.id != current_user.id
    ):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return persisted.id


async def _scheduled_layer(current_user: GenericUser) -> tuple[BaseDataLayer, str]:
    _scheduled_feature_enabled()
    if current_user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    data_layer = get_data_layer()
    if data_layer is None:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    if not _supports_scheduled_tasks(data_layer):
        raise HTTPException(
            status_code=501,
            detail="Prompt scheduling is not supported by this data layer",
        )
    return data_layer, await _scheduled_user_id(current_user, data_layer)


async def _validate_scheduled_payload(
    payload: CreateScheduledTaskRequest | UpdateScheduledTaskRequest,
    current_user: GenericUser,
) -> None:
    if payload.prompt is not None:
        try:
            validate_prompt(payload.prompt)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    for value, validator in (
        (payload.cron, validate_cron),
        (payload.timezone, validate_timezone),
    ):
        if value is not None:
            try:
                validator(value)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
    hooks = payload.afterHooks
    if hooks is not None:
        available = config.code.scheduled_task_after_hooks
        if len(hooks) != len(set(hooks)) or any(
            name not in available for name in hooks
        ):
            raise HTTPException(
                status_code=422, detail="Unknown scheduled task after-hook"
            )
    if payload.chatProfile is not None:
        if not config.code.set_chat_profiles:
            raise HTTPException(status_code=422, detail="Unknown chat profile")
        profiles = await config.code.set_chat_profiles(current_user, None)
        if not any(profile.name == payload.chatProfile for profile in profiles):
            raise HTTPException(status_code=422, detail="Unknown chat profile")
    if payload.model is not None:
        if not config.code.set_scheduled_task_models:
            raise HTTPException(status_code=422, detail="Unknown scheduled task model")
        models = await config.code.set_scheduled_task_models(current_user, None)
        if payload.model not in models:
            raise HTTPException(status_code=422, detail="Unknown scheduled task model")


@router.get("/project/scheduled-tasks")
async def list_scheduled_tasks(current_user: UserParam):
    data_layer, user_id = await _scheduled_layer(current_user)
    try:
        tasks = await data_layer.list_scheduled_tasks(user_id)
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    return JSONResponse(content=tasks)


@router.post("/project/scheduled-tasks")
async def create_scheduled_task(
    payload: CreateScheduledTaskRequest, current_user: UserParam
):
    data_layer, user_id = await _scheduled_layer(current_user)
    await _validate_scheduled_payload(payload, current_user)
    now = utc_now()
    cron = validate_cron(payload.cron)
    timezone_name = validate_timezone(payload.timezone)
    import uuid as _uuid

    task: ScheduledTaskDict = {
        "id": str(_uuid.uuid4()),
        "userId": user_id,
        "userIdentifier": current_user.identifier,  # type: ignore[union-attr]
        "prompt": validate_prompt(payload.prompt),
        "chatProfile": payload.chatProfile,
        "model": payload.model,
        "cron": cron,
        "timezone": timezone_name,
        "enabled": payload.enabled,
        "afterHooks": payload.afterHooks,
        "nextRunAt": next_run_at(cron, timezone_name)
        .isoformat()
        .replace("+00:00", "Z"),
        "manualRunRequestedAt": None,
        "lastRunAt": None,
        "lastStatus": None,
        "lastError": None,
        "lastThreadId": None,
        "leaseId": None,
        "leaseUntil": None,
        "createdAt": now,
        "updatedAt": now,
    }
    try:
        return JSONResponse(content=await data_layer.create_scheduled_task(task))
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc


@router.put("/project/scheduled-tasks/{task_id}")
async def update_scheduled_task(
    task_id: str, payload: UpdateScheduledTaskRequest, current_user: UserParam
):
    data_layer, user_id = await _scheduled_layer(current_user)
    existing = await data_layer.get_scheduled_task(task_id)
    if not existing or existing["userId"] != user_id:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    await _validate_scheduled_payload(payload, current_user)
    updated = cast(ScheduledTaskDict, dict(existing))
    for field in (
        "prompt",
        "chatProfile",
        "model",
        "cron",
        "timezone",
        "enabled",
        "afterHooks",
    ):
        if field in payload.model_fields_set:
            updated[field] = getattr(payload, field)
    updated["prompt"] = validate_prompt(updated["prompt"])
    updated["cron"] = validate_cron(updated["cron"])
    updated["timezone"] = validate_timezone(updated["timezone"])
    if {"cron", "timezone"} & payload.model_fields_set or (
        "enabled" in payload.model_fields_set and updated["enabled"]
    ):
        updated["nextRunAt"] = (
            next_run_at(updated["cron"], updated["timezone"])
            .isoformat()
            .replace("+00:00", "Z")
        )
    updated["updatedAt"] = utc_now()
    return JSONResponse(content=await data_layer.update_scheduled_task(updated))


@router.delete("/project/scheduled-tasks/{task_id}")
async def delete_scheduled_task(task_id: str, current_user: UserParam):
    data_layer, user_id = await _scheduled_layer(current_user)
    if not await data_layer.delete_scheduled_task(task_id, user_id):
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    return JSONResponse(content={"success": True})


@router.post("/project/scheduled-tasks/{task_id}/run", status_code=202)
async def run_scheduled_task(task_id: str, current_user: UserParam):
    data_layer, user_id = await _scheduled_layer(current_user)
    existing = await data_layer.get_scheduled_task(task_id)
    if not existing or existing["userId"] != user_id:
        raise HTTPException(status_code=404, detail="Scheduled task not found")
    requested_at = utc_now()
    try:
        queued = await data_layer.request_scheduled_task_run(
            task_id, user_id, requested_at
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if not queued:
        raise HTTPException(
            status_code=400, detail="A scheduled task run is already queued"
        )
    updated = await data_layer.get_scheduled_task(task_id)
    return JSONResponse(
        status_code=202,
        content=updated or {**existing, "manualRunRequestedAt": requested_at},
    )


# ---- Prompt Gallery endpoints ----


async def _get_user_id(current_user: GenericUser, data_layer: "BaseDataLayer") -> str:
    """Return the persisted user id, looking it up when current_user is an unpersisted User."""
    if isinstance(current_user, PersistedUser):
        return current_user.id
    if current_user is None:
        if require_login():
            raise HTTPException(status_code=401, detail="Unauthorized")
        # No-auth mode: use a stable anonymous user ID so prompt gallery works.
        return "anonymous"
    persisted = await data_layer.get_user(identifier=current_user.identifier)
    if not persisted:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return persisted.id


def _require_authenticated_sharing(current_user: GenericUser) -> None:
    """Sharing needs a real identity; the no-auth anonymous user has none."""
    if current_user is None:
        raise HTTPException(
            status_code=403,
            detail="Sharing requires an authenticated user",
        )


def _prompt_gallery_enabled() -> None:
    """Raise 400 when the feature is disabled, to keep guards DRY."""
    if not config.features.prompt_gallery:
        raise HTTPException(
            status_code=400, detail="Prompt gallery feature is not enabled"
        )


async def _dl_prompt(coro: Any) -> Any:
    """Await a prompt data-layer call; map typed errors to HTTP responses."""
    try:
        return await coro
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=str(exc) or "Prompt gallery is not supported by this data layer",
        ) from exc
    except PromptValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    except PromptTitleTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "title_too_large", "message": str(exc)},
        ) from exc
    except PromptDiffTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "diff_too_large", "message": str(exc)},
        ) from exc
    except PromptContentTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "content_too_large", "message": str(exc)},
        ) from exc
    except PromptVersionLimitReached as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "version_limit_reached", "message": str(exc)},
        ) from exc
    except PromptVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except PromptVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


def _prompt_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code, detail={"code": code, "message": message}
    )


def _validate_prompt_title(title: str) -> None:
    try:
        enforce_title_limit(title)
    except PromptValidationError as exc:
        raise _prompt_error(422, "validation_error", str(exc)) from exc
    except PromptTitleTooLarge as exc:
        raise _prompt_error(413, "title_too_large", str(exc)) from exc


def _validate_prompt_content(content: str) -> None:
    """Enforce the prompt body cap before any data-layer call."""
    try:
        enforce_content_limit(content)
    except PromptValidationError as exc:
        raise _prompt_error(422, "validation_error", str(exc)) from exc
    except PromptContentTooLarge as exc:
        raise _prompt_error(413, "content_too_large", str(exc)) from exc


def _validate_prompt(title: str, content: str) -> None:
    _validate_prompt_title(title)
    _validate_prompt_content(content)


@router.get("/project/prompts")
async def list_prompts(
    request: Request,
    current_user: UserParam,
):
    """List the current user's saved prompts."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    prompts = await _dl_prompt(
        data_layer.list_prompts(await _get_user_id(current_user, data_layer))
    )
    return JSONResponse(content=prompts)


@router.post("/project/prompts")
async def create_prompt(
    request: Request,
    payload: CreatePromptRequest,
    current_user: UserParam,
):
    """Save a new prompt to the gallery."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    _validate_prompt(payload.title, payload.content)
    import uuid as _uuid

    now = utc_now()
    prompt = {
        "id": str(_uuid.uuid4()),
        "title": payload.title,
        "content": payload.content,
        "userId": await _get_user_id(current_user, data_layer),
        "isShared": False,
        "createdAt": now,
        "updatedAt": now,
    }
    created = await _dl_prompt(data_layer.create_prompt(prompt))  # type: ignore[arg-type]
    return JSONResponse(content=created)


@router.put("/project/prompts/{prompt_id}")
async def update_prompt(
    request: Request,
    prompt_id: str,
    payload: UpdatePromptRequest,
    current_user: UserParam,
):
    """Edit a prompt (author only)."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    async with _mutation_lock(f"prompt-mutation:{prompt_id}", "Prompt"):
        user_id = await _get_user_id(current_user, data_layer)
        existing = await _dl_prompt(data_layer.get_prompt(prompt_id))
        if not existing or existing["userId"] != user_id:
            raise HTTPException(status_code=404, detail="Prompt not found")
        updated = dict(existing)
        if payload.title is not None:
            updated["title"] = payload.title
        if payload.content is not None:
            updated["content"] = payload.content
        _validate_prompt(updated["title"], updated["content"])
        updated["updatedAt"] = utc_now()
        result = await _dl_prompt(data_layer.update_prompt(updated))  # type: ignore[arg-type]
        return JSONResponse(content=result)


@router.delete("/project/prompts/{prompt_id}")
async def delete_prompt(
    request: Request,
    prompt_id: str,
    current_user: UserParam,
):
    """Delete a prompt (author only)."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    async with _mutation_lock(f"prompt-mutation:{prompt_id}", "Prompt"):
        deleted = await _dl_prompt(
            data_layer.delete_prompt(
                prompt_id, await _get_user_id(current_user, data_layer)
            )
        )
        if not deleted:
            raise HTTPException(status_code=404, detail="Prompt not found")
        return JSONResponse(content={"success": True})


@router.post("/project/prompts/{prompt_id}/share")
async def share_prompt(
    request: Request,
    prompt_id: str,
    payload: SharePromptRequest,
    current_user: UserParam,
):
    """Toggle sharing of a prompt (author only). Returns share URL."""
    _prompt_gallery_enabled()
    _require_authenticated_sharing(current_user)
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    async with _mutation_lock(f"prompt-mutation:{prompt_id}", "Prompt"):
        user_id = await _get_user_id(current_user, data_layer)
        existing = await _dl_prompt(data_layer.get_prompt(prompt_id))
        if not existing or existing["userId"] != user_id:
            raise HTTPException(status_code=404, detail="Prompt not found")
        if payload.isShared:
            _validate_prompt(existing["title"], existing["content"])
        updated = dict(existing)
        updated["isShared"] = payload.isShared
        updated["authorLabel"] = (
            await resolve_shared_author_label(current_user)
            if payload.isShared
            else None
        )
        if payload.isShared and not existing.get("isShared"):
            updated["sharedAt"] = utc_now()
            updated["sharedVersion"] = int(existing.get("version") or 1)
        updated["updatedAt"] = utc_now()
        result = await _dl_prompt(data_layer.update_prompt(updated))  # type: ignore[arg-type]
        return JSONResponse(
            content={"prompt": result, "shareUrl": f"/prompt/{prompt_id}"}
        )


async def _shared_prompt_or_404(
    data_layer: BaseDataLayer, prompt_id: str
) -> PromptDict:
    """Load a prompt for an unauthenticated share endpoint, or raise 404."""
    prompt = await _dl_prompt(data_layer.get_prompt(prompt_id))
    if not prompt or not prompt.get("isShared"):
        raise HTTPException(status_code=404, detail="Prompt not found")
    return prompt


@router.get("/project/prompt/share/{prompt_id}")
async def get_shared_prompt(
    request: Request,
    prompt_id: str,
):
    """Fetch a shared prompt anonymously. Returns 404 if not shared."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    prompt = await _shared_prompt_or_404(data_layer, prompt_id)

    current_version_id = None
    if _supports_prompt_versioning(data_layer):
        versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
        head = latest_head(versions) if isinstance(versions, list) else None
        if head:
            current_version_id = head["id"]

    # Public projection only: this endpoint is unauthenticated, so owner-only
    # fields (userId, createdBy and future provenance fields) must never leak.
    # Version metadata is limited to the head number, id and content hash.
    content = {
        "id": prompt["id"],
        "title": prompt["title"],
        "content": prompt["content"],
        "isShared": prompt["isShared"],
        "createdAt": prompt["createdAt"],
        "updatedAt": prompt["updatedAt"],
        "version": int(prompt.get("version") or 1),
        "currentVersionId": current_version_id,
        "currentContentHash": prompt.get("contentHash"),
    }
    if "authorLabel" in prompt:
        content["authorLabel"] = prompt.get("authorLabel")
    return JSONResponse(content=content)


@router.get("/project/prompt/share/{prompt_id}/versions")
async def get_shared_prompt_versions(
    request: Request,
    prompt_id: str,
):
    """List public version metadata for a shared prompt, newest first.

    Anonymous callers never see the owner or version author identities.
    """
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    prompt = await _shared_prompt_or_404(data_layer, prompt_id)
    versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
    visible = public_versions(prompt, versions)
    ordered = sorted(visible, key=lambda version: int(version["version"]), reverse=True)
    return JSONResponse(
        content=[public_version(version, include_author=False) for version in ordered]
    )


@router.get("/project/prompt/share/{prompt_id}/versions/{version_id}")
async def get_shared_prompt_version(
    request: Request,
    prompt_id: str,
    version_id: str,
):
    """Return one public version snapshot of a shared prompt."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    prompt = await _shared_prompt_or_404(data_layer, prompt_id)
    versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
    if version_id not in {
        version["id"] for version in public_versions(prompt, versions)
    }:
        raise HTTPException(status_code=404, detail="Prompt version not found")
    version = await _dl_prompt(data_layer.get_prompt_version(prompt_id, version_id))
    if not version:
        raise HTTPException(status_code=404, detail="Prompt version not found")
    return JSONResponse(content=public_version(version, include_author=False))


async def _prompt_for_reader(
    data_layer: BaseDataLayer, prompt_id: str, current_user: GenericUser
) -> tuple[PromptDict, bool]:
    """Load a prompt the caller may read; return ``(prompt, is_owner)``."""
    user_id = await _get_user_id(current_user, data_layer)
    prompt = await _dl_prompt(data_layer.get_prompt(prompt_id))
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    is_owner = prompt["userId"] == user_id
    if not is_owner and not prompt.get("isShared"):
        raise HTTPException(status_code=404, detail="Prompt not found")
    return prompt, is_owner


@router.post("/project/prompt/share/{prompt_id}/add")
async def add_shared_prompt(
    request: Request,
    prompt_id: str,
    current_user: UserParam,
    payload: Optional[AddSharedPromptRequest] = None,
):
    """Copy a shared prompt into the current user's gallery, pinned to a version."""
    _prompt_gallery_enabled()
    _require_authenticated_sharing(current_user)
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    user_id = await _get_user_id(current_user, data_layer)
    async with _mutation_locks(
        [f"prompt-copy:{user_id}:{prompt_id}", f"prompt-mutation:{prompt_id}"],
        "Prompt",
    ):
        source = await _dl_prompt(data_layer.get_prompt(prompt_id))
        if not source or not source["isShared"]:
            raise HTTPException(status_code=404, detail="Prompt not found")
        _validate_prompt(source["title"], source["content"])
        existing = await _dl_prompt(data_layer.list_prompts(user_id))
        if any(item.get("sourcePromptId") == prompt_id for item in existing):
            raise _prompt_error(
                409, "already_added", "This shared prompt is already in your gallery"
            )

        title = source["title"]
        content = source["content"]
        pinned_version_id: Optional[str] = None
        pinned_content_hash: Optional[str] = source.get("contentHash") or fingerprint(
            source
        )
        requested_version = payload.versionId if payload else None
        target: Optional[Dict[str, Any]] = None

        if _supports_prompt_versioning(data_layer):
            versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
            visible = (
                public_versions(source, versions) if isinstance(versions, list) else []
            )
            if requested_version:
                if requested_version not in {version["id"] for version in visible}:
                    raise HTTPException(
                        status_code=404, detail="Prompt version not found"
                    )
                target = await _dl_prompt(
                    data_layer.get_prompt_version(prompt_id, requested_version)
                )
                if not target:
                    raise HTTPException(
                        status_code=404, detail="Prompt version not found"
                    )
            else:
                head = latest_head(visible)
                target = (
                    await _dl_prompt(
                        data_layer.get_prompt_version(prompt_id, head["id"])
                    )
                    if head
                    else None
                )
            if target:
                title = target["title"]
                content = target["content"]
                pinned_version_id = target["id"]
                pinned_content_hash = target["contentHash"]
            else:
                raise _prompt_error(
                    409,
                    "source_unavailable",
                    "The shared prompt has no public versions",
                )
        elif requested_version:
            raise HTTPException(
                status_code=501, detail="Prompt versioning is unavailable"
            )

        _validate_prompt(title, content)
        import uuid as _uuid

        now = utc_now()
        copy = {
            "id": str(_uuid.uuid4()),
            "title": title,
            "content": content,
            "userId": user_id,
            "isShared": False,
            "createdAt": now,
            "updatedAt": now,
            "sourcePromptId": prompt_id,
            "pinnedVersionId": pinned_version_id,
            "pinnedContentHash": pinned_content_hash,
            "pinnedAt": now,
        }
        if target:
            copy["pinnedVersion"] = target["version"]
        created = await _dl_prompt(data_layer.create_prompt(copy))  # type: ignore[arg-type]
        return JSONResponse(content=created)


@router.get("/project/prompts/updates")
async def list_prompt_updates(
    request: Request,
    current_user: UserParam,
):
    """List pinned copies whose shared source has a newer version."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    user_id = await _get_user_id(current_user, data_layer)
    updates = await _dl_prompt(data_layer.list_prompt_updates(user_id))
    return JSONResponse(content=updates)


@router.post("/project/prompts/updates/ack")
async def ack_prompt_update(
    request: Request,
    payload: AckPromptUpdateRequest,
    current_user: UserParam,
):
    """Acknowledge an update; the notice version only moves forward."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    user_id = await _get_user_id(current_user, data_layer)
    prompt = await _dl_prompt(data_layer.get_prompt(payload.promptId))
    if not prompt or prompt["userId"] != user_id:
        raise HTTPException(status_code=404, detail="Prompt not found")
    updated = await _dl_prompt(
        data_layer.ack_prompt_update(user_id, payload.promptId, payload.version)
    )
    return JSONResponse(
        content={"promptId": payload.promptId, "notifiedVersion": updated}
    )


@router.get("/project/prompts/{prompt_id}/versions")
async def list_prompt_versions(
    request: Request,
    prompt_id: str,
    current_user: UserParam,
):
    """List version metadata for an owned or shared prompt."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    prompt, is_owner = await _prompt_for_reader(data_layer, prompt_id, current_user)
    versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
    if not is_owner:
        versions = public_versions(prompt, versions)
    projected = [
        public_version(version, include_author=is_owner) for version in versions
    ]
    return JSONResponse(content=projected)


@router.get("/project/prompts/{prompt_id}/versions/{version_id}")
async def get_prompt_version(
    request: Request,
    prompt_id: str,
    version_id: str,
    current_user: UserParam,
):
    """Return one immutable version, with author identity only to the owner."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    prompt, is_owner = await _prompt_for_reader(data_layer, prompt_id, current_user)
    if not is_owner:
        versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
        if version_id not in {
            version["id"] for version in public_versions(prompt, versions)
        }:
            raise HTTPException(status_code=404, detail="Prompt version not found")
    version = await _dl_prompt(data_layer.get_prompt_version(prompt_id, version_id))
    if not version:
        raise HTTPException(status_code=404, detail="Prompt version not found")
    return JSONResponse(content=public_version(version, include_author=is_owner))


@router.get("/project/prompts/{prompt_id}/diff")
async def prompt_diff(
    request: Request,
    prompt_id: str,
    current_user: UserParam,
    from_: Literal["pinned", "local"] = Query("pinned", alias="from"),
    to: str = Query(..., description="Version id to compare against"),
):
    """Return line rows, never HTML, for a pinned/local to version comparison."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    prompt, is_owner = await _prompt_for_reader(data_layer, prompt_id, current_user)
    if not is_owner:
        versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
        if to not in {version["id"] for version in public_versions(prompt, versions)}:
            raise HTTPException(status_code=404, detail="Prompt version not found")

    target = await _dl_prompt(data_layer.get_prompt_version(prompt_id, to))
    if not target:
        raise HTTPException(status_code=404, detail="Prompt version not found")

    from_version_id: Optional[str] = None
    from_version: Optional[int] = None
    from_hash: Optional[str] = None
    if from_ == "pinned":
        pinned_hash = prompt.get("pinnedContentHash")
        pinned_id = prompt.get("pinnedVersionId")
        if not pinned_hash and not pinned_id:
            raise _prompt_error(
                409, "not_pinned", "This prompt is not pinned to a source version"
            )
        old_text: Optional[str] = None
        versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
        for summary in versions:
            if pinned_hash and summary.get("contentHash") == pinned_hash:
                local = await _dl_prompt(
                    data_layer.get_prompt_version(prompt_id, summary["id"])
                )
                if local:
                    old_text = local["content"]
                    from_version_id = local["id"]
                    from_version = local["version"]
                    from_hash = local["contentHash"]
                    break
        if old_text is None and pinned_id and prompt.get("sourcePromptId"):
            source_version = await _dl_prompt(
                data_layer.get_prompt_version(prompt["sourcePromptId"], pinned_id)
            )
            if source_version:
                old_text = source_version["content"]
                from_version_id = source_version["id"]
                from_version = source_version["version"]
                from_hash = source_version["contentHash"]
        if old_text is None:
            raise _prompt_error(
                409,
                "pinned_unavailable",
                "The pinned version is no longer available",
            )
    else:
        old_text = prompt["content"]
        from_hash = prompt.get("contentHash")
        from_version = int(prompt.get("version") or 1)
        versions = await _dl_prompt(data_layer.list_prompt_versions(prompt_id))
        if versions:
            from_version_id = versions[0]["id"]

    try:
        rows, truncated = build_diff(old_text, target["content"])
    except PromptDiffTooLarge as exc:
        raise _prompt_error(413, "diff_too_large", str(exc)) from exc
    return JSONResponse(
        content={
            "promptId": prompt_id,
            "from": from_,
            "to": to,
            "fromVersionId": from_version_id,
            "toVersionId": target["id"],
            "fromVersion": from_version,
            "toVersion": target["version"],
            "fromContentHash": from_hash,
            "toContentHash": target["contentHash"],
            "rows": rows,
            "truncated": truncated,
            "maxRows": MAX_DIFF_ROWS,
            "maxBytes": MAX_DIFF_BYTES,
        }
    )


@router.post("/project/prompts/{prompt_id}/sync")
async def sync_prompt(
    request: Request,
    prompt_id: str,
    payload: SyncPromptRequest,
    current_user: UserParam,
):
    """Copy a source version into a pinned prompt, appending one version."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    user_id = await _get_user_id(current_user, data_layer)
    async with _locked_pinned_source(data_layer, user_id, prompt_id, skill=False) as (
        prompt,
        source,
    ):
        source_id = source["id"]
        versions = await _dl_prompt(data_layer.list_prompt_versions(source_id))
        visible = (
            public_versions(source, versions) if isinstance(versions, list) else []
        )
        if payload.versionId:
            if payload.versionId not in {version["id"] for version in visible}:
                raise _prompt_error(
                    409,
                    "version_conflict",
                    "That version does not belong to the source",
                )
            target = await _dl_prompt(
                data_layer.get_prompt_version(source_id, payload.versionId)
            )
            if not target:
                raise _prompt_error(
                    409,
                    "version_conflict",
                    "That version does not belong to the source",
                )
        else:
            head = latest_head(visible)
            if not head:
                raise _prompt_error(
                    409, "source_unavailable", "The source prompt has no versions"
                )
            target = await _dl_prompt(
                data_layer.get_prompt_version(source_id, head["id"])
            )
            if not target:
                raise _prompt_error(
                    409, "source_unavailable", "The source prompt has no versions"
                )

        _validate_prompt(target["title"], target["content"])
        if prompt.get("contentHash") == target["contentHash"]:
            raise _prompt_error(
                409, "already_current", "This prompt is already current"
            )
        if (
            payload.expectedLocalHash is not None
            and payload.expectedLocalHash != prompt.get("contentHash")
        ):
            raise _prompt_error(
                409,
                "preview_stale",
                "The prompt changed after the preview; review the diff again",
            )
        pinned_hash = prompt.get("pinnedContentHash")
        if (
            pinned_hash
            and prompt.get("contentHash") != pinned_hash
            and not payload.confirm
        ):
            raise _prompt_error(
                409,
                "local_changes",
                "This prompt has local edits; confirm to replace them",
            )

        now = utc_now()
        updated = dict(prompt)
        updated["title"] = target["title"]
        updated["content"] = target["content"]
        if target.get("kind") == "skill":
            updated["description"] = target.get("description") or ""
        updated["pinnedVersionId"] = target["id"]
        updated["pinnedVersion"] = target["version"]
        updated["pinnedContentHash"] = target["contentHash"]
        updated["pinnedAt"] = now
        updated["updatedAt"] = now
        result = await _dl_prompt(data_layer.update_prompt(updated))  # type: ignore[arg-type]
        return JSONResponse(content=result)


@router.post("/project/prompts/{prompt_id}/detach")
async def detach_prompt(
    request: Request,
    prompt_id: str,
    current_user: UserParam,
):
    """Remove the source pin while keeping the prompt and its history."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    async with _mutation_lock(f"prompt-mutation:{prompt_id}", "Prompt"):
        user_id = await _get_user_id(current_user, data_layer)
        prompt = await _dl_prompt(data_layer.get_prompt(prompt_id))
        if not prompt or prompt["userId"] != user_id:
            raise HTTPException(status_code=404, detail="Prompt not found")
        if not prompt.get("sourcePromptId"):
            raise _prompt_error(
                409, "not_pinned", "This prompt is not pinned to a source"
            )
        updated = dict(prompt)
        updated["sourcePromptId"] = None
        updated["pinnedVersionId"] = None
        updated["pinnedContentHash"] = None
        updated["pinnedAt"] = None
        updated["updatedAt"] = utc_now()
        result = await _dl_prompt(data_layer.update_prompt(updated))  # type: ignore[arg-type]
        return JSONResponse(content=result)


# ---- Per-user skills endpoints (router registered only when the flag is on) ----

skills_router = APIRouter(prefix=config.run.root_path)


def _skills_data_layer() -> BaseDataLayer:
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    return data_layer


def _require_skill_sharing(data_layer: BaseDataLayer) -> None:
    if not _supports_skill_sharing(data_layer):
        raise HTTPException(
            status_code=501,
            detail="Skill sharing is not supported by this data layer",
        )


def _require_skill_versions(data_layer: BaseDataLayer) -> None:
    if not _supports_skill_versions(data_layer):
        raise HTTPException(
            status_code=501,
            detail="Skill versioning is not supported by this data layer",
        )


async def _dl_skill(coro: Any) -> Any:
    """Await a skills data-layer call; map typed errors to HTTP responses."""
    try:
        return await coro
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=str(exc) or "Skills are not supported by this data layer",
        ) from exc
    except PromptValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    except PromptTitleTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "title_too_large", "message": str(exc)},
        ) from exc
    except PromptDiffTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "diff_too_large", "message": str(exc)},
        ) from exc
    except PromptContentTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "content_too_large", "message": str(exc)},
        ) from exc
    except PromptVersionLimitReached as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "version_limit_reached", "message": str(exc)},
        ) from exc
    except SkillNameConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "name_taken", "message": str(exc)},
        ) from exc
    except PromptVersionError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


def _skill_markdown(name: str, description: str, content: str) -> str:
    for value, field in (
        (name, "skill name"),
        (description, "skill description"),
        (content, "content"),
    ):
        try:
            validate_unicode_scalar(value, field)
        except PromptValidationError as exc:
            raise HTTPException(
                status_code=422,
                detail={"code": "validation_error", "message": str(exc)},
            ) from exc
    if "\n" in description or "\r" in description:
        raise HTTPException(
            status_code=400, detail="Skill description must be a single line"
        )
    return f"---\nname: {name}\ndescription: {description}\n---\n{content}"


def _validate_skill(name: str, description: str, content: str) -> Skill:
    try:
        return parse_skill_markdown(
            _skill_markdown(name, description, content), origin="skill"
        )
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _check_skill_name(
    name: str,
    existing: List[Dict[str, Any]],
    skill_id: Optional[str] = None,
    code: Optional[str] = None,
) -> None:
    def conflict(message: str) -> HTTPException:
        detail: Any = {"code": code, "message": message} if code else message
        return HTTPException(status_code=409, detail=detail)

    if name in {skill.name for skill in load_skills()}:
        raise conflict(f"Skill name {name!r} is reserved by an app skill")
    if any(item["title"] == name and item["id"] != skill_id for item in existing):
        raise conflict(f"Skill name {name!r} already exists")


def _skill_row(
    skill_id: str,
    user_id: str,
    name: str,
    description: str,
    content: str,
    enabled: bool,
    imported_from: Optional[str] = None,
    imported_at: Optional[str] = None,
    content_hash: Optional[str] = None,
    is_shared: bool = False,
    version: int = 1,
    source_prompt_id: Optional[str] = None,
    pinned_version_id: Optional[str] = None,
    pinned_content_hash: Optional[str] = None,
    pinned_at: Optional[str] = None,
    update_seen_version_id: Optional[str] = None,
) -> PromptDict:
    now = utc_now()
    return {
        "id": skill_id,
        "userId": user_id,
        "title": name,
        "content": content,
        "isShared": is_shared,
        "kind": "skill",
        "description": description,
        "enabled": enabled,
        "importedFrom": imported_from,
        "importedAt": imported_at,
        "contentHash": content_hash,
        "createdAt": now,
        "updatedAt": now,
        "version": version,
        "sourcePromptId": source_prompt_id,
        "pinnedVersionId": pinned_version_id,
        "pinnedContentHash": pinned_content_hash,
        "pinnedAt": pinned_at,
        "updateSeenVersionId": update_seen_version_id,
    }


async def _skill_versions(
    data_layer: BaseDataLayer, skill_id: str
) -> List[Dict[str, Any]]:
    versions = await _dl_skill(data_layer.list_prompt_versions(skill_id))
    if not isinstance(versions, list):
        raise HTTPException(status_code=500, detail="Could not read skill versions")
    return versions


async def _skill_review_hash(
    data_layer: BaseDataLayer, skill: PromptDict
) -> Optional[str]:
    """Return the current version fingerprint used by the enable review gate."""
    if _supports_skill_versions(data_layer):
        try:
            versions = await data_layer.list_prompt_versions(skill["id"])
        except NotImplementedError:
            versions = None
        if isinstance(versions, list):
            head = latest_head(cast(List[Dict[str, Any]], versions))
            if head:
                return head["contentHash"]
    return skill.get("contentHash")


async def _shared_skill_or_404(data_layer: BaseDataLayer, skill_id: str) -> PromptDict:
    skill = await _dl_skill(data_layer.get_shared_skill(skill_id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill


@skills_router.get("/project/skills")
async def list_user_skills(
    request: Request,
    current_user: UserParam,
):
    """List the current user's skills, including disabled ones."""
    data_layer = _skills_data_layer()
    user_id = await _get_user_id(current_user, data_layer)
    skills = await _dl_skill(data_layer.list_skills(user_id))
    return JSONResponse(content=skills)


@skills_router.post("/project/skills")
async def create_user_skill(
    request: Request,
    payload: CreateSkillRequest,
    current_user: UserParam,
):
    """Create a skill owned by the current user."""
    data_layer = _skills_data_layer()
    user_id = await _get_user_id(current_user, data_layer)
    skill = _validate_skill(payload.name, payload.description, payload.content)
    async with _mutation_lock(f"skill-library:{user_id}", "Skill library"):
        existing = await _dl_skill(data_layer.list_skills(user_id))
        if len(existing) >= MAX_SKILLS:
            raise HTTPException(
                status_code=400, detail=f"At most {MAX_SKILLS} skills per user"
            )
        _check_skill_name(skill.name, existing)

        import uuid as _uuid

        created = await _dl_skill(
            data_layer.create_skill(
                _skill_row(
                    str(_uuid.uuid4()),
                    user_id,
                    skill.name,
                    skill.description,
                    skill.body,
                    payload.enabled,
                )
            )
        )
        return JSONResponse(status_code=201, content=created)


@skills_router.post("/project/skills/import")
async def import_user_skill(
    request: Request,
    payload: ImportSkillRequest,
    current_user: UserParam,
):
    """Import exported Markdown as a disabled, provenance-tagged skill."""
    data_layer = _skills_data_layer()
    user_id = await _get_user_id(current_user, data_layer)
    try:
        validate_unicode_scalar(payload.markdown, "skill Markdown")
        validate_unicode_scalar(payload.imported_from, "imported_from")
    except PromptValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "validation_error", "message": str(exc)},
        ) from exc
    if "\n" in payload.imported_from or "\r" in payload.imported_from:
        raise HTTPException(
            status_code=400, detail="imported_from must be a single line"
        )
    try:
        skill = parse_skill_markdown(payload.markdown, origin="imported skill")
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with _mutation_lock(f"skill-library:{user_id}", "Skill library"):
        existing = await _dl_skill(data_layer.list_skills(user_id))
        if len(existing) >= MAX_SKILLS:
            raise HTTPException(
                status_code=400, detail=f"At most {MAX_SKILLS} skills per user"
            )
        _check_skill_name(skill.name, existing)

        import uuid as _uuid

        created = await _dl_skill(
            data_layer.create_skill(
                _skill_row(
                    str(_uuid.uuid4()),
                    user_id,
                    skill.name,
                    skill.description,
                    skill.body,
                    False,
                    imported_from=payload.imported_from,
                    imported_at=utc_now(),
                    content_hash=skill_content_hash(skill_to_markdown(skill)),
                )
            )
        )
        return JSONResponse(status_code=201, content=created)


@skills_router.get("/project/skills/updates")
async def list_skill_updates(
    request: Request,
    current_user: UserParam,
):
    """List pinned skill copies whose shared source has a newer version."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    user_id = await _get_user_id(current_user, data_layer)
    updates = await _dl_skill(data_layer.list_skill_updates(user_id))
    return JSONResponse(content=updates)


@skills_router.get("/project/skills/{skill_id}")
async def get_user_skill(
    request: Request,
    skill_id: str,
    current_user: UserParam,
):
    """Return one of the current user's skills."""
    data_layer = _skills_data_layer()
    user_id = await _get_user_id(current_user, data_layer)
    skill = await _dl_skill(data_layer.get_skill(skill_id, user_id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return JSONResponse(content=skill)


@skills_router.get("/project/skills/{skill_id}/export")
async def export_user_skill(
    request: Request,
    skill_id: str,
    current_user: UserParam,
):
    """Export one of the current user's skills as Markdown."""
    data_layer = _skills_data_layer()
    user_id = await _get_user_id(current_user, data_layer)
    skill = await _dl_skill(data_layer.get_skill(skill_id, user_id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    markdown = _skill_markdown(
        skill["title"], skill.get("description", ""), skill["content"]
    )
    return Response(
        content=markdown,
        media_type="text/markdown",
        headers={
            "Content-Disposition": f'attachment; filename="{skill["title"]}.md"',
            "X-Skill-Content-Hash": skill_content_hash(markdown),
        },
    )


@skills_router.put("/project/skills/{skill_id}")
async def update_user_skill(
    request: Request,
    skill_id: str,
    payload: UpdateSkillRequest,
    current_user: UserParam,
):
    """Edit, share-toggle or enable one of the current user's skills.

    Imported and pinned skills must be reviewed before they are enabled. Their
    content, description or name edits disable the skill for a fresh review.
    Local, self-authored skills keep their enabled state across edits.
    """
    data_layer = _skills_data_layer()
    async with _mutation_lock(f"skill-mutation:{skill_id}", "Skill"):
        user_id = await _get_user_id(current_user, data_layer)
        existing = await _dl_skill(data_layer.get_skill(skill_id, user_id))
        if not existing:
            raise HTTPException(status_code=404, detail="Skill not found")

        updated = dict(existing)
        if payload.name is not None:
            updated["title"] = payload.name
        if payload.description is not None:
            updated["description"] = payload.description
        if payload.content is not None:
            updated["content"] = payload.content
        skill = _validate_skill(
            updated["title"], updated.get("description", ""), updated["content"]
        )
        if skill.name != existing["title"]:
            all_skills = await _dl_skill(data_layer.list_skills(user_id))
            _check_skill_name(skill.name, all_skills, skill_id)
        updated["title"] = skill.name
        updated["description"] = skill.description
        updated["content"] = skill.body

        content_changed = (
            skill.name != existing["title"]
            or skill.description != existing.get("description", "")
            or skill.body != existing["content"]
        )
        imported_or_shared = bool(
            existing.get("sourcePromptId") or existing.get("importedFrom")
        )
        if content_changed:
            # A local, self-authored skill stays enabled across edits. Imported and
            # linked skills must be reviewed again because their body is untrusted.
            updated["enabled"] = (
                False if imported_or_shared else existing.get("enabled", False)
            )
        elif payload.enabled is True:
            if imported_or_shared:
                expected_hash = await _skill_review_hash(data_layer, existing)
                if not expected_hash or payload.reviewedContentHash != expected_hash:
                    raise _prompt_error(
                        409,
                        "content_changed",
                        "Review the current skill version before enabling it",
                    )
            updated["enabled"] = True
        elif payload.enabled is False:
            updated["enabled"] = False
        updated["updatedAt"] = utc_now()

        result = await _dl_skill(data_layer.update_skill(updated))  # type: ignore[arg-type]
        return JSONResponse(content=result)


@skills_router.delete("/project/skills/{skill_id}")
async def delete_user_skill(
    request: Request,
    skill_id: str,
    current_user: UserParam,
):
    """Delete one of the current user's skills."""
    data_layer = _skills_data_layer()
    async with _mutation_lock(f"skill-mutation:{skill_id}", "Skill"):
        user_id = await _get_user_id(current_user, data_layer)
        deleted = await _dl_skill(data_layer.delete_skill(skill_id, user_id))
        if not deleted:
            raise HTTPException(status_code=404, detail="Skill not found")
        return JSONResponse(content={"success": True})


@skills_router.post("/project/skills/{skill_id}/share")
async def share_user_skill(
    request: Request,
    skill_id: str,
    payload: ShareSkillRequest,
    current_user: UserParam,
):
    """Toggle sharing of a skill (owner only). Returns the public URL."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    _require_authenticated_sharing(current_user)
    async with _mutation_lock(f"skill-mutation:{skill_id}", "Skill"):
        user_id = await _get_user_id(current_user, data_layer)
        existing = await _dl_skill(data_layer.get_skill(skill_id, user_id))
        if not existing:
            raise HTTPException(status_code=404, detail="Skill not found")
        if payload.isShared:
            _validate_skill(
                existing["title"],
                existing.get("description", ""),
                existing["content"],
            )

        updated = dict(existing)
        updated["isShared"] = payload.isShared
        updated["authorLabel"] = (
            await resolve_shared_author_label(current_user)
            if payload.isShared
            else None
        )
        if payload.isShared and not existing.get("isShared"):
            updated["sharedAt"] = utc_now()
            updated["sharedVersion"] = int(existing.get("version") or 1)
        updated["updatedAt"] = utc_now()
        result = await _dl_skill(data_layer.update_skill(updated))  # type: ignore[arg-type]
        return JSONResponse(content={"skill": result, "shareUrl": f"/skill/{skill_id}"})


@skills_router.get("/project/skill/share/{skill_id}")
async def get_shared_skill(
    request: Request,
    skill_id: str,
):
    """Return a sanitized preview of a shared skill to anonymous callers."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    skill = await _shared_skill_or_404(data_layer, skill_id)
    versions = await _skill_versions(data_layer, skill_id)
    head = latest_head(versions)

    # Public projection only: no userId, createdBy, enabled or import label.
    return JSONResponse(
        content={
            "id": skill["id"],
            "title": skill["title"],
            "description": skill.get("description", ""),
            "content": skill["content"],
            "version": int(head["version"]) if head else int(skill.get("version") or 1),
            "currentVersionId": head["id"] if head else None,
            "contentHash": head["contentHash"] if head else skill.get("contentHash"),
            "updatedAt": skill["updatedAt"],
            "sharedAt": skill.get("sharedAt"),
            "authorLabel": skill.get("authorLabel", "Chainlit NG user"),
        }
    )


@skills_router.get("/project/skill/share/{skill_id}/versions")
async def get_shared_skill_versions(
    request: Request,
    skill_id: str,
):
    """List public version metadata for a shared skill, newest first."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    skill = await _shared_skill_or_404(data_layer, skill_id)
    versions = await _skill_versions(data_layer, skill_id)
    ordered = sorted(
        public_versions(skill, versions),
        key=lambda version: int(version["version"]),
        reverse=True,
    )
    return JSONResponse(
        content=[public_version(version, include_author=False) for version in ordered]
    )


@skills_router.get("/project/skill/share/{skill_id}/versions/{version_id}")
async def get_shared_skill_version(
    request: Request,
    skill_id: str,
    version_id: str,
):
    """Return one anonymous version snapshot of a shared skill."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    skill = await _shared_skill_or_404(data_layer, skill_id)
    versions = await _skill_versions(data_layer, skill_id)
    if version_id not in {
        version["id"] for version in public_versions(skill, versions)
    }:
        raise HTTPException(status_code=404, detail="Skill version not found")
    version = await _dl_skill(data_layer.get_prompt_version(skill_id, version_id))
    if not version:
        raise HTTPException(status_code=404, detail="Skill version not found")
    return JSONResponse(content=public_version(version, include_author=False))


@skills_router.post("/project/skill/share/{skill_id}/add")
async def add_shared_skill(
    request: Request,
    skill_id: str,
    current_user: UserParam,
    payload: Optional[AddSharedSkillRequest] = None,
):
    """Copy a shared skill as a disabled, pinned copy owned by the caller."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    _require_authenticated_sharing(current_user)
    user_id = await _get_user_id(current_user, data_layer)
    async with _mutation_locks(
        [
            f"skill-library:{user_id}",
            f"skill-copy:{user_id}:{skill_id}",
            f"skill-mutation:{skill_id}",
        ],
        "Skill",
    ):
        source = await _shared_skill_or_404(data_layer, skill_id)
        existing = await _dl_skill(data_layer.list_skills(user_id))
        if any(item.get("sourcePromptId") == skill_id for item in existing):
            raise _prompt_error(
                409, "already_added", "This shared skill is already in your library"
            )
        if len(existing) >= MAX_SKILLS:
            raise HTTPException(
                status_code=400, detail=f"At most {MAX_SKILLS} skills per user"
            )

        versions = await _skill_versions(data_layer, skill_id)
        visible = public_versions(source, versions)
        target: Optional[Dict[str, Any]] = None
        requested_version = payload.versionId if payload else None
        if requested_version:
            if requested_version not in {version["id"] for version in visible}:
                raise HTTPException(status_code=404, detail="Skill version not found")
            target = await _dl_skill(
                data_layer.get_prompt_version(skill_id, requested_version)
            )
            if not target:
                raise HTTPException(status_code=404, detail="Skill version not found")
        else:
            head = latest_head(visible)
            if head:
                target = await _dl_skill(
                    data_layer.get_prompt_version(skill_id, head["id"])
                )
        if target is None:
            raise _prompt_error(
                409, "source_unavailable", "The shared skill has no versions"
            )

        _validate_skill(
            target["title"], target.get("description") or "", target["content"]
        )
        _check_skill_name(target["title"], existing, code="name_taken")

        import uuid as _uuid

        now = utc_now()
        copy = _skill_row(
            str(_uuid.uuid4()),
            user_id,
            target["title"],
            target.get("description") or "",
            target["content"],
            False,
            content_hash=target["contentHash"],
            source_prompt_id=skill_id,
            pinned_version_id=target["id"],
            pinned_content_hash=target["contentHash"],
            pinned_at=now,
            update_seen_version_id=target["id"],
        )
        copy["pinnedVersion"] = target["version"]
        copy["createdAt"] = now
        copy["updatedAt"] = now
        created = await _dl_skill(data_layer.create_skill(copy))
        return JSONResponse(content=created)


@skills_router.post("/project/skills/{skill_id}/updates/seen")
async def mark_skill_update_seen(
    request: Request,
    skill_id: str,
    payload: SkillSeenUpdateRequest,
    current_user: UserParam,
):
    """Acknowledge a source version; the stored notice never moves down."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    async with _mutation_lock(f"skill-mutation:{skill_id}", "Skill"):
        user_id = await _get_user_id(current_user, data_layer)
        skill = await _dl_skill(data_layer.get_skill(skill_id, user_id))
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        if not skill.get("sourcePromptId"):
            raise _prompt_error(
                409, "not_pinned", "This skill is not pinned to a source"
            )
        source = await _dl_skill(data_layer.get_shared_skill(skill["sourcePromptId"]))
        if not source:
            raise _prompt_error(
                409, "source_unavailable", "The source skill is no longer available"
            )

        clamped = await _dl_skill(
            data_layer.ack_skill_update(user_id, skill_id, payload.version)
        )
        updated = await _dl_skill(data_layer.get_skill(skill_id, user_id))
        return JSONResponse(
            content={
                "skillId": skill_id,
                "version": clamped,
                "updateSeenVersionId": (updated or {}).get("updateSeenVersionId"),
            }
        )


@skills_router.post("/project/skills/{skill_id}/update-from-source")
async def update_skill_from_source(
    request: Request,
    skill_id: str,
    payload: UpdateSkillFromSourceRequest,
    current_user: UserParam,
):
    """Apply a source version to a pinned skill, disabling it for review."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    user_id = await _get_user_id(current_user, data_layer)
    async with _locked_pinned_source(data_layer, user_id, skill_id, skill=True) as (
        skill,
        source,
    ):
        source_id = source["id"]
        source_versions = await _skill_versions(data_layer, source_id)
        visible = public_versions(source, source_versions)
        if not visible:
            raise _prompt_error(
                409, "source_unavailable", "The source skill has no versions"
            )

        target: Optional[Dict[str, Any]] = None
        if payload.versionId:
            if payload.versionId not in {version["id"] for version in visible}:
                raise _prompt_error(
                    409,
                    "version_conflict",
                    "That version does not belong to the source",
                )
            target = await _dl_skill(
                data_layer.get_prompt_version(source_id, payload.versionId)
            )
            if not target or target.get("kind") != "skill":
                raise _prompt_error(
                    409,
                    "version_conflict",
                    "That version does not belong to the source",
                )
        else:
            head = latest_head(visible)
            if head:
                target = await _dl_skill(
                    data_layer.get_prompt_version(source_id, head["id"])
                )
        if target is None:
            raise _prompt_error(
                409, "source_unavailable", "The source skill has no versions"
            )

        _validate_skill(
            target["title"], target.get("description") or "", target["content"]
        )
        local_versions = await _skill_versions(data_layer, skill_id)
        local_head = latest_head(local_versions)
        local_hash = local_head["contentHash"] if local_head else None

        if (
            payload.expectedLocalHash is not None
            and payload.expectedLocalHash != local_hash
        ):
            raise _prompt_error(
                409,
                "preview_stale",
                "The skill changed after the preview; review the update again",
            )
        pinned_hash = skill.get("pinnedContentHash")
        if pinned_hash and local_hash != pinned_hash and not payload.confirm:
            raise _prompt_error(
                409,
                "local_changes",
                "This skill has local edits; confirm to replace them",
            )

        now = utc_now()
        updated = dict(skill)
        updated["title"] = target["title"]
        updated["description"] = target.get("description") or ""
        updated["content"] = target["content"]
        updated["enabled"] = False
        updated["isShared"] = False
        updated["pinnedVersionId"] = target["id"]
        updated["pinnedVersion"] = target["version"]
        updated["pinnedContentHash"] = target["contentHash"]
        updated["pinnedAt"] = now
        updated["updateSeenVersionId"] = target["id"]
        updated["updatedAt"] = now
        result = await _dl_skill(data_layer.update_skill(updated))  # type: ignore[arg-type]
        return JSONResponse(content=result)


@skills_router.post("/project/skills/{skill_id}/detach")
async def detach_skill(
    request: Request,
    skill_id: str,
    current_user: UserParam,
):
    """Remove a skill's source pin while keeping the skill and its history."""
    data_layer = _skills_data_layer()
    _require_skill_sharing(data_layer)
    async with _mutation_lock(f"skill-mutation:{skill_id}", "Skill"):
        user_id = await _get_user_id(current_user, data_layer)
        skill = await _dl_skill(data_layer.get_skill(skill_id, user_id))
        if not skill:
            raise HTTPException(status_code=404, detail="Skill not found")
        if not skill.get("sourcePromptId"):
            raise _prompt_error(
                409, "not_pinned", "This skill is not pinned to a source"
            )
        if not skill.get("enabled"):
            raise _prompt_error(
                409,
                "review_required",
                "Review and enable this skill before detaching it",
            )

        updated = dict(skill)
        updated["sourcePromptId"] = None
        updated["pinnedVersionId"] = None
        updated["pinnedContentHash"] = None
        updated["pinnedAt"] = None
        updated["updateSeenVersionId"] = None
        updated["updatedAt"] = utc_now()
        result = await _dl_skill(data_layer.update_skill(updated))  # type: ignore[arg-type]
        return JSONResponse(content=result)


@skills_router.get("/project/skills/{skill_id}/versions")
async def list_skill_versions(
    request: Request,
    skill_id: str,
    current_user: UserParam,
):
    """List immutable versions for one of the current user's skills."""
    data_layer = _skills_data_layer()
    _require_skill_versions(data_layer)
    user_id = await _get_user_id(current_user, data_layer)
    skill = await _dl_skill(data_layer.get_skill(skill_id, user_id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    versions = await _skill_versions(data_layer, skill_id)
    ordered = sorted(
        versions, key=lambda version: int(version["version"]), reverse=True
    )
    return JSONResponse(
        content=[public_version(version, include_author=True) for version in ordered]
    )


@skills_router.get("/project/skills/{skill_id}/versions/{version_id}")
async def get_skill_version(
    request: Request,
    skill_id: str,
    version_id: str,
    current_user: UserParam,
):
    """Return one immutable version of the current user's skill."""
    data_layer = _skills_data_layer()
    _require_skill_versions(data_layer)
    user_id = await _get_user_id(current_user, data_layer)
    skill = await _dl_skill(data_layer.get_skill(skill_id, user_id))
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    version = await _dl_skill(data_layer.get_prompt_version(skill_id, version_id))
    if not version:
        raise HTTPException(status_code=404, detail="Skill version not found")
    return JSONResponse(content=public_version(version, include_author=True))


@router.get("/project/thread/{thread_id}/element/{element_id}")
async def get_thread_element(
    request: Request,
    thread_id: str,
    element_id: str,
    current_user: UserParam,
):
    """Get a specific thread element."""
    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    access = await authorize_thread_read(thread_id, current_user, data_layer=data_layer)
    if access in (ThreadAccess.MISSING, ThreadAccess.OWNERLESS):
        raise HTTPException(status_code=404, detail="Thread not found")
    if access not in (ThreadAccess.OWNER, ThreadAccess.SHARED):
        raise HTTPException(status_code=401, detail="Unauthorized")

    res = await data_layer.get_element(thread_id, element_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Element not found")
    # Storage ids are not needed by any renderer and must not reach named
    # share readers (or the owner's browser).
    res = {
        key: value for key, value in res.items() if key not in ("PK", "SK", "objectKey")
    }
    return _no_store(JSONResponse(content=res))


def _get_ws_session(session_id: str) -> "WebsocketSession":
    """Return the session or 404; an unknown id is caller input, not a fault."""
    from chainlit.session import WebsocketSession

    session = WebsocketSession.get_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.put("/project/element")
async def update_thread_element(
    payload: ElementRequest,
    current_user: UserParam,
):
    """Update a specific thread element."""

    from chainlit.context import init_ws_context
    from chainlit.element import ElementDict

    session = _get_ws_session(payload.sessionId)
    context = init_ws_context(session)

    element_dict = cast(ElementDict, payload.element)

    try:
        if element_dict["type"] != "custom":
            return {"success": False}

        element = _sanitize_custom_element(element_dict)
    except (KeyError, TypeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Invalid element payload") from exc

    _require_session_principal(
        session,
        current_user,
        detail="You are not authorized to update elements for this session",
    )

    # The session thread is server-side state; the body's threadId is not
    # trusted for authorization.
    element.thread_id = context.session.thread_id
    await _authorize_custom_element(session, element, current_user)

    await element.update()

    return {"success": True}


@router.delete("/project/element")
async def delete_thread_element(
    payload: ElementRequest,
    current_user: UserParam,
):
    """Delete a specific thread element."""

    from chainlit.context import init_ws_context
    from chainlit.element import ElementDict

    session = _get_ws_session(payload.sessionId)
    context = init_ws_context(session)

    element_dict = cast(ElementDict, payload.element)

    try:
        if element_dict["type"] != "custom":
            return {"success": False}

        element = _sanitize_custom_element(element_dict)
    except (KeyError, TypeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Invalid element payload") from exc

    _require_session_principal(
        session,
        current_user,
        detail="You are not authorized to remove elements for this session",
    )

    # The session thread is server-side state; the body's threadId is not
    # trusted for authorization.
    element.thread_id = context.session.thread_id
    await _authorize_custom_element(session, element, current_user)

    await element.remove()

    return {"success": True}


def _sanitize_custom_element(element_dict: "ElementDict") -> "CustomElement":
    from chainlit.element import CustomElement

    return CustomElement(
        id=element_dict["id"],
        for_id=element_dict.get("forId") or "",
        thread_id=element_dict.get("threadId") or "",
        name=element_dict["name"],
        props=element_dict.get("props") or {},
        display=element_dict["display"],
        auto_expand=element_dict.get("autoExpand", True),
    )


async def _authorize_custom_element(
    session: "WebsocketSession",
    element: "CustomElement",
    current_user: Optional[User],
) -> None:
    """Reject mutations of elements that are not in the caller's session thread."""
    data_layer = get_data_layer()
    if not data_layer or not current_user:
        return

    if not await data_layer.get_element(session.thread_id, element.id):
        # Element writes are queued until the first user message, so a new
        # element may not have a row yet. The data layer still scopes the
        # write to the session thread.
        if not session.has_first_interaction:
            return
        raise HTTPException(status_code=404, detail="Element not found")

    await is_thread_author(current_user.identifier, session.thread_id)


@router.put("/project/thread")
async def rename_thread(
    request: Request,
    payload: UpdateThreadRequest,
    current_user: UserParam,
):
    """Rename a thread."""

    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    thread_id = payload.threadId

    await is_thread_author(current_user.identifier, thread_id)

    await data_layer.update_thread(thread_id, name=payload.name)

    return JSONResponse(content={"success": True})


@router.put("/project/thread/share")
async def share_thread(
    request: Request,
    payload: ShareThreadRequest,
    current_user: UserParam,
):
    """Share or un-share a thread (author only)."""

    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    thread_id = payload.threadId

    await is_thread_author(current_user.identifier, thread_id)

    # Fetch current thread and metadata, then toggle is_shared
    thread = await data_layer.get_thread(thread_id=thread_id)
    metadata = (thread.get("metadata") if thread else {}) or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    metadata = dict(metadata)
    is_shared = bool(payload.isShared)
    metadata["is_shared"] = is_shared
    if is_shared:
        metadata["shared_at"] = utc_now()
    else:
        metadata.pop("shared_at", None)
    try:
        await data_layer.update_thread(thread_id=thread_id, metadata=metadata)
        logger.debug(
            "[share_thread] updated metadata for thread=%s to %s",
            thread_id,
            metadata,
        )
    except Exception as e:
        logger.exception("[share_thread] update_thread failed: %s", e)
        raise

    return JSONResponse(content={"success": True})


@router.delete("/project/thread")
async def delete_thread(
    request: Request,
    payload: DeleteThreadRequest,
    current_user: UserParam,
):
    """Delete a thread."""

    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    thread_id = payload.threadId

    await is_thread_author(current_user.identifier, thread_id)

    await supersede_thread_approvals(thread_id)
    mark_thread_deleted(thread_id)
    try:
        await data_layer.delete_thread(thread_id)
    except Exception:
        clear_thread_deleted(thread_id)
        raise
    return JSONResponse(content={"success": True})


@router.get("/project/thread/{thread_id}/grants")
async def list_thread_grants(
    request: Request,
    thread_id: str,
    current_user: UserParam,
):
    """List the read grants of a thread (owner only)."""
    data_layer = _thread_grants_layer()

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    await _require_owner_or_404(thread_id, current_user)

    grants = await data_layer.list_thread_grants(thread_id)
    return _no_store(JSONResponse(content=[asdict(grant) for grant in grants]))


@asynccontextmanager
async def _thread_grant_mutation_lock(thread_id: str, data_layer: BaseDataLayer):
    """Order grant changes against each other and clear-all-threads deletion."""
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            _mutation_lock(f"thread-grants:{thread_id}", "Thread grants")
        )
        if _supports_delete_all_threads(data_layer):
            await stack.enter_async_context(
                _mutation_lock("thread-grants:bulk-delete", "Thread grants")
            )
        yield


@router.post("/project/thread/{thread_id}/grants")
async def create_thread_grant(
    request: Request,
    thread_id: str,
    payload: CreateThreadGrantRequest,
    current_user: UserParam,
):
    """Grant read access to one identifier (owner only, idempotent)."""
    data_layer = _thread_grants_layer()

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _require_same_origin(request)

    async with _thread_grant_mutation_lock(thread_id, data_layer):
        await _require_owner_or_404(thread_id, current_user)

        if payload.grantee == current_user.identifier:
            raise HTTPException(
                status_code=400, detail="Cannot grant a thread to its owner"
            )

        # Quiet lookup: the grant path must not log the grantee identifier.
        grantee = await data_layer.get_user_for_acl(payload.grantee)
        if not grantee or not getattr(grantee, "createdAt", None):
            # Same body as a created grant: no one-request existence oracle.
            return _no_store(JSONResponse(content={"success": True}))

        current_user_id = getattr(current_user, "id", None)
        if current_user_id is not None and grantee.id == current_user_id:
            raise HTTPException(
                status_code=400, detail="Cannot grant a thread to its owner"
            )

        grants = await data_layer.list_thread_grants(thread_id)
        if any(grant.grantee_user_id == grantee.id for grant in grants):
            return _no_store(JSONResponse(content={"success": True}))
        if len(grants) >= MAX_THREAD_GRANTS_PER_THREAD:
            raise HTTPException(status_code=409, detail="Thread grant limit reached")

        await data_layer.create_thread_grant(
            ThreadGrant(
                thread_id=thread_id,
                grantee_user_id=grantee.id,
                grantee_user_created_at=grantee.createdAt,
                grantee_identifier=grantee.identifier,
                granted_by_user_id=current_user_id or current_user.identifier,
                granted_at=utc_now(),
            )
        )
        return _no_store(JSONResponse(content={"success": True}))


@router.delete("/project/thread/{thread_id}/grants/{grantee_user_id}")
async def delete_thread_grant(
    request: Request,
    thread_id: str,
    grantee_user_id: str,
    current_user: UserParam,
):
    """Revoke a read grant (owner only, idempotent)."""
    data_layer = _thread_grants_layer()

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    _require_same_origin(request)
    async with _thread_grant_mutation_lock(thread_id, data_layer):
        await _require_owner_or_404(thread_id, current_user)
        await data_layer.delete_thread_grant(thread_id, grantee_user_id)
        return _no_store(JSONResponse(content={"success": True}))


@router.delete("/project/threads")
async def delete_user_threads(current_user: UserParam):
    """Delete all threads owned by the current user."""

    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")

    data_layer = get_data_layer()

    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")

    identifier = current_user.identifier
    user_id = await _get_user_id(current_user, data_layer)
    start = time.perf_counter()

    async with _mutation_lock("thread-grants:bulk-delete", "Thread grants"):
        pending_thread_ids = managed_thread_ids_for(
            identifier
        ) | await shared_managed_thread_ids(identifier)
        for thread_id in pending_thread_ids:
            await supersede_thread_approvals(thread_id)
            mark_thread_deleted(thread_id)
        try:
            deleted = await data_layer.delete_all_threads(user_id)
        except NotImplementedError as exc:
            for thread_id in pending_thread_ids:
                clear_thread_deleted(thread_id)
            raise HTTPException(
                status_code=501,
                detail=str(exc)
                or "Bulk thread deletion is not supported by this data layer",
            ) from exc
        except Exception:
            for thread_id in pending_thread_ids:
                clear_thread_deleted(thread_id)
            raise

        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "Deleted %s threads for user %s in %sms", deleted, identifier, duration_ms
        )
        return JSONResponse(content={"success": True, "deleted": deleted})


def _configured_external_origin(request: Request) -> Optional[tuple[str, str]]:
    """Return the browser origin this deployment expects, or None.

    ``CHAINLIT_URL`` is authoritative when configured. A reverse proxy may
    present a different Host/scheme to the app than the browser used, so the
    configured external URL must win over the request-derived origin.
    """
    chainlit_url = os.environ.get("CHAINLIT_URL")
    if chainlit_url:
        parsed = urllib.parse.urlsplit(chainlit_url)
        if parsed.scheme.lower() in ("http", "https") and parsed.netloc:
            return parsed.scheme.lower(), parsed.netloc.lower()
        return None

    host = request.headers.get("host")
    if not host:
        return None
    return request.url.scheme.lower(), host.lower()


def _is_json_same_origin(request: Request) -> bool:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return False
    # Preserve the original downgrade check: an HTTPS request must not accept
    # an HTTP Origin solely because CHAINLIT_URL was configured as HTTP.
    if request.url.scheme == "https" and parsed.scheme.lower() != "https":
        return False
    expected = _configured_external_origin(request)
    return expected is not None and (
        parsed.scheme.lower() == expected[0] and parsed.netloc.lower() == expected[1]
    )


_APPROVAL_STATUS_BY_DECISION: Dict[str, ApprovalStatus] = {
    "approve": "approved",
    "reject": "rejected",
    "edit": "edited",
    "request_changes": "changes_requested",
}


def _approval_response(record: dict, *, error: Optional[str] = None) -> dict:
    return ApprovalDecisionResponse(
        approvalId=record["id"],
        status=record["status"],
        decision=record.get("decision"),
        decidedBy=record.get("decidedBy"),
        decidedAt=record.get("decidedAt") or record.get("terminalAt"),
        terminalReason=record.get("terminalReason"),
        reason=record.get("reason"),
        revision=int(record.get("revision", 1)),
        error=error,
    ).model_dump()


@router.post("/project/approval")
async def decide_approval(
    request: Request,
    payload: ApprovalDecisionRequest,
    current_user: UserParam,
):
    """Decide a pending approval. The server owns the arguments."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not _is_json_same_origin(request):
        raise HTTPException(status_code=403, detail="Invalid request origin")

    session = WebsocketSession.get_by_id(payload.sessionId)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_principal(
        session,
        current_user,
        status_code=403,
        detail="Approval author required",
    )
    if session.unavailable_chat_profile:
        raise HTTPException(
            status_code=409,
            detail="This conversation is read-only because its chat profile is unavailable.",
        )

    try:
        if payload.decision != "edit" and payload.editedArguments is not None:
            raise ApprovalInvalidArguments(
                "editedArguments requires decision=edit",
                field="editedArguments",
                error_type="not_allowed",
            )
        record = await transition(
            payload.approvalId,
            _APPROVAL_STATUS_BY_DECISION[payload.decision],
            actor=session.user,
            thread_id=session.thread_id,
            session_generation=session.generation,
            reason=payload.reason,
            decision_request_id=payload.decisionRequestId,
            edited_arguments=payload.editedArguments,
            expected_kind="approval",
        )
    except ApprovalNotPending as exc:
        if exc.record is None:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_approval_response(exc.record, error=exc.code),
        )
    except ApprovalInvalidArguments as exc:
        # Field path and error type only; never the offending value.
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": {
                    "error": exc.code,
                    "field": exc.field,
                    "type": exc.error_type,
                }
            },
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)

    return JSONResponse(content=_approval_response(record))


def _question_response(record: dict, *, error: Optional[str] = None) -> dict:
    status = record["status"]
    return QuestionAnswerResponse(
        questionId=record["id"],
        status=status,
        answer=record.get("answer") if status == "answered" else None,
        answeredBy=record.get("decidedBy"),
        answeredAt=record.get("decidedAt") or record.get("terminalAt"),
        revision=int(record.get("revision", 1)),
        durable=get_data_layer() is not None and status == "answered",
        error=error,
    ).model_dump()


@router.post("/project/question")
async def answer_question(
    request: Request,
    payload: QuestionAnswerRequest,
    current_user: UserParam,
):
    """Answer a pending question through the shared transition engine."""
    if not current_user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not _is_json_same_origin(request):
        raise HTTPException(status_code=403, detail="Invalid request origin")

    session = WebsocketSession.get_by_id(payload.sessionId)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_session_principal(
        session,
        current_user,
        status_code=403,
        detail="Question author required",
    )
    if session.unavailable_chat_profile:
        raise HTTPException(
            status_code=409,
            detail="This conversation is read-only because its chat profile is unavailable.",
        )

    answer: Dict[str, Any]
    if payload.answers is not None:
        answer = {
            "answers": [item.model_dump(exclude_none=True) for item in payload.answers]
        }
    elif payload.optionId is not None:
        answer = {"optionId": payload.optionId}
    else:
        answer = {"text": payload.text}
    try:
        record = await transition(
            payload.questionId,
            "answered",
            actor=session.user,
            thread_id=session.thread_id,
            session_generation=session.generation,
            decision_request_id=payload.decisionRequestId,
            expected_kind="question",
            answer=answer,
        )
    except ApprovalNotPending as exc:
        if exc.record is None:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_question_response(exc.record, error="QuestionNotPending"),
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)

    return JSONResponse(content=_question_response(record))


@router.post("/project/action")
async def call_action(
    payload: CallActionRequest,
    current_user: UserParam,
):
    """Run an action."""

    from chainlit.action import Action
    from chainlit.context import init_ws_context

    session = _get_ws_session(payload.sessionId)
    context = init_ws_context(session)
    config: ChainlitConfig = session.get_config()

    _require_session_principal(
        session,
        current_user,
        detail="You are not authorized to upload files for this session",
    )

    if session.unavailable_chat_profile:
        raise HTTPException(
            status_code=409,
            detail="This conversation is read-only because its chat profile is unavailable.",
        )

    try:
        action = Action(**payload.action)
    except (TypeError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail="Invalid action payload") from exc

    callback = config.code.action_callbacks.get(action.name)
    if callback:
        if not context.session.has_first_interaction:
            context.session.has_first_interaction = True
            asyncio.create_task(context.emitter.init_thread(action.name))

        response = await callback(action)
    else:
        raise HTTPException(
            status_code=404,
            detail=f"No callback found for action {action.name}",
        )

    return JSONResponse(content={"success": True, "response": response})


@router.get(MCP_OAUTH_CALLBACK_PATH)
async def mcp_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    current_user: UserParam = None,
):
    """Deliver an authorization code to the flow that parked it.

    Every failure answers with a fixed detail: exception text can carry a state,
    a URL or a token, and a state mismatch names both states.
    """
    from chainlit.mcp_oauth import (
        MCP_OAUTH_OTHER_USER_DETAIL,
        MCP_OAUTH_UNKNOWN_STATE_DETAIL,
        McpAuthorizationTimeout,
        get_pending_authorizations,
    )

    if current_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication token",
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=MCP_OAUTH_UNKNOWN_STATE_DETAIL,
        )

    try:
        get_pending_authorizations().resolve(state, current_user.identifier, code)
    except PermissionError:
        # The flow is left alone: its owner can still complete it.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=MCP_OAUTH_OTHER_USER_DETAIL,
        )
    except (KeyError, McpAuthorizationTimeout):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=MCP_OAUTH_UNKNOWN_STATE_DETAIL,
        )

    return HTMLResponse(
        content=MCP_OAUTH_CALLBACK_PAGE,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.post("/mcp")
async def connect_mcp(
    request: Request,
    payload: ConnectMCPRequest,
    current_user: UserParam,
):
    session = WebsocketSession.get_by_id(payload.sessionId)
    session_config = session.get_config()
    _require_session_principal(session, current_user, detail="Unauthorized")
    if not session_config.features.mcp.enabled:
        raise HTTPException(
            status_code=400,
            detail="This app does not support MCP.",
        )
    if _mcp_capacity_reached(session, payload.name):
        raise HTTPException(
            status_code=429,
            detail="Too many MCP connections for this session",
        )

    reservations = session.mcp_reservations
    reservations[payload.name] = reservations.get(payload.name, 0) + 1
    try:
        return await _connect_mcp(
            request, payload, current_user, session, session_config
        )
    finally:
        remaining = reservations.get(payload.name, 0) - 1
        if remaining > 0:
            reservations[payload.name] = remaining
        else:
            reservations.pop(payload.name, None)


async def _connect_mcp(
    request: Request,
    payload: ConnectMCPRequest,
    current_user: UserParam,
    session: WebsocketSession,
    config: ChainlitConfig,
):
    import asyncio
    import shlex

    import httpx2
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    from mcp.client.stdio import (
        StdioServerParameters,
        get_default_environment,
        stdio_client,
    )
    from mcp.client.streamable_http import streamable_http_client

    from chainlit.config import SseMcpServer, StdioMcpServer, StreamableHttpMcpServer
    from chainlit.context import init_ws_context
    from chainlit.mcp import (
        _MCP_CONNECT_TIMEOUT_HTTP,
        _MCP_CONNECT_TIMEOUT_STDIO,
        HttpMcpConnection,
        McpConnection,
        McpDestinationError,
        McpHttpClientFactory,
        SseMcpConnection,
        StdioMcpConnection,
        _destination_in_allowlist,
        _destination_on_origin,
        make_mcp_http_client_factory,
        validate_mcp_headers,
        validate_mcp_url,
    )
    from chainlit.session import McpSession, stop_mcp_task

    init_ws_context(session)
    # Reject user-provided connections that try to claim a configured server's
    # name — must run BEFORE any eviction, otherwise a rejected attempt would
    # still have killed the legitimate session. Compared case-insensitively
    # and whitespace-stripped so a near-miss name can't impersonate a
    # configured server in the UI.
    if payload.url is not None and any(
        s.name.strip().casefold() == payload.name.strip().casefold()
        for s in config.features.mcp.servers
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"MCP server name {payload.name!r} is reserved by a configured server "
                "and cannot be used for a user-provided connection."
            ),
        )

    use_oauth = getattr(payload, "useOAuth", False)
    oauth_allowed_origins = None
    oauth_pending = None
    oauth_token_store = None
    oauth_server_key = None
    oauth_storage = None
    oauth_client_info = None
    oauth_url = None
    oauth_user_identifier = None

    if use_oauth:
        from chainlit.mcp_oauth import (
            MCP_OAUTH_FAILED_DETAIL,
            MCP_OAUTH_TIMEOUT_DETAIL,
            MCP_OAUTH_TOO_MANY_DETAIL,
            MarkedOAuthAuth,
            McpAuthorizationTimeout,
            McpOAuthError,
            McpTooManyFlows,
            OriginAllowlist,
            build_oauth_provider,
            canonical_server_key,
            get_pending_authorizations,
            get_rate_limiter,
            get_token_store,
            normalize_origin,
            oauth_request_guard,
        )

        if current_user is None:
            raise HTTPException(
                status_code=401,
                detail="Invalid authentication token",
            )

        if payload.url is None:
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_URL_REQUIRED_DETAIL,
            )

        if payload.clientType == "sse":
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_SSE_UNSUPPORTED_DETAIL,
            )
        oauth_url = payload.url

        origins = config.features.mcp.oauth_authorization_origins
        if not origins:
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_NO_ORIGINS_DETAIL,
            )
        try:
            oauth_allowed_origins = OriginAllowlist(origins)
        except ValueError:
            logger.error(
                "features.mcp.oauth_authorization_origins is invalid; "
                "refusing MCP OAuth."
            )
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_NO_ORIGINS_DETAIL,
            )

        if not config.features.mcp.user_oauth.enabled:
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_DISABLED_DETAIL,
            )

        chainlit_url = os.environ.get("CHAINLIT_URL")
        if not chainlit_url:
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_CHAINLIT_URL_DETAIL,
            )
        try:
            normalize_origin(chainlit_url)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_CHAINLIT_URL_DETAIL,
            )

        if getattr(payload, "headers", None):
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_HEADERS_DETAIL,
            )

        try:
            normalize_origin(payload.url)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_URL_DETAIL,
            )
        url_path = urllib.parse.urlsplit(payload.url).path
        decoded_path = urllib.parse.unquote(url_path)
        if (
            "/.well-known/" in url_path.lower()
            or "/.well-known/" in decoded_path.lower()
        ):
            raise HTTPException(
                status_code=400,
                detail=MCP_OAUTH_WELL_KNOWN_DETAIL,
            )

        # User-provided servers, including the OAuth flow, stay behind the
        # allowlist: the OAuth branch opens a transport to a client-supplied
        # URL before any named-server lookup, so leaving it ungated would
        # bypass the allowlist with "useOAuth": true. Runs after the OAuth
        # URL gates above so their fixed details keep precedence.
        if not config.features.mcp.user_servers.enabled:
            raise HTTPException(
                status_code=400,
                detail=MCP_USER_SERVERS_DISABLED_DETAIL,
            )
        try:
            validate_mcp_url(oauth_url, config.features.mcp.user_servers.allowed_urls)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        oauth_user_identifier = current_user.identifier
        oauth_pending = get_pending_authorizations()
        previous_pending = oauth_pending.get_for_session(session.id, payload.name)
        other_flows = oauth_pending.flow_count(oauth_user_identifier) - (
            1 if previous_pending else 0
        )
        user_oauth = config.features.mcp.user_oauth

        if not get_rate_limiter().allow(
            oauth_user_identifier, user_oauth.max_starts_per_user_per_minute
        ):
            raise HTTPException(
                status_code=429,
                detail=MCP_OAUTH_RATE_DETAIL,
            )
        if other_flows >= user_oauth.max_pending_per_user:
            raise HTTPException(
                status_code=429,
                detail=MCP_OAUTH_TOO_MANY_DETAIL,
            )

        oauth_token_store = get_token_store()
        oauth_token_store.evict_for_new_flow(oauth_user_identifier)
        oauth_server_key = canonical_server_key(payload.url)
        oauth_storage = oauth_token_store.scoped(
            oauth_user_identifier, oauth_server_key
        )
        oauth_client_info = await oauth_storage.get_client_info()

    # Disconnect previous session for this name (reconnection) before a new
    # OAuth flow starts. The plain connect path evicts only after the new
    # session is known to work (swap_mcp_session below), so a failed reconnect
    # doesn't kill the session it was trying to replace.
    if use_oauth and payload.name in session.mcp_sessions:
        old_mcp = session.mcp_sessions.pop(payload.name)
        if on_mcp_disconnect := config.code.on_mcp_disconnect:
            try:
                await on_mcp_disconnect(payload.name, old_mcp.client)
            except Exception:
                logger.debug(
                    "Error in on_mcp_disconnect callback for %s",
                    payload.name,
                    exc_info=True,
                )
        try:
            await old_mcp.close()
        except Exception:
            logger.debug(
                "Error closing old MCP session %s", payload.name, exc_info=True
            )

    # A new connect for this name supersedes any OAuth attempt still signing in,
    # whatever the new transport is.
    previous_oauth_task = session.mcp_oauth_tasks.pop(payload.name, None)
    if previous_oauth_task is not None:
        previous_oauth_task.cancel()
        try:
            await previous_oauth_task
        except BaseException:
            pass

    # ── OAuth: start the connect in the background, return the sign-in URL ──
    if use_oauth:
        assert oauth_allowed_origins is not None
        assert oauth_pending is not None
        assert oauth_token_store is not None
        assert oauth_server_key is not None
        assert oauth_storage is not None
        assert oauth_url is not None
        assert oauth_user_identifier is not None

        async def _emit_oauth_event(event: str, data: dict) -> None:
            try:
                await session.emit(event, data)  # type: ignore[misc]
            except BaseException:
                logger.debug(
                    "Error emitting %s for MCP OAuth connection %r",
                    event,
                    payload.name,
                    exc_info=True,
                )

        async def _on_oauth_redirect(authorization_url: str) -> None:
            await _emit_oauth_event(
                "mcp_authorization_required",
                {"name": payload.name, "url": authorization_url},
            )

        oauth_flow_failure: dict[str, McpOAuthError] = {}

        def _record_oauth_failure(exc: McpOAuthError) -> None:
            oauth_flow_failure["error"] = exc

        oauth_flow = build_oauth_provider(
            storage=oauth_storage,
            pending=oauth_pending,
            user_identifier=oauth_user_identifier,
            server_url=oauth_url,
            session_id=session.id,
            name=payload.name,
            redirect_uri=get_mcp_oauth_callback_url(request),
            on_redirect=_on_oauth_redirect,
            on_failure=_record_oauth_failure,
            max_pending=config.features.mcp.user_oauth.max_pending_per_user,
        )
        # The SDK follows same-origin redirects itself, so the origin check
        # alone would let an allowlisted path redirect to a sibling path. The
        # guard also validates every transport request against the user-server
        # allowlist; this event is its fail-fast channel because the SDK
        # swallows request-hook exceptions.
        oauth_transport_blocked = asyncio.Event()
        oauth_transport_blocked_detail: list[str] = []
        oauth_task_holder: dict[str, asyncio.Task] = {}

        def _record_transport_blocked(detail: str) -> None:
            oauth_transport_blocked_detail.append(detail)
            oauth_transport_blocked.set()
            task = oauth_task_holder.get("task")
            if task is not None and not task.done():
                task.cancel()

        oauth_guard = oauth_request_guard(
            oauth_url,
            oauth_allowed_origins,
            config.features.mcp.user_servers.allowed_urls,
            _record_transport_blocked,
        )
        oauth_connection = HttpMcpConnection(
            url=oauth_url, name=payload.name, headers=None
        )
        oauth_stop_event = asyncio.Event()
        oauth_connected = asyncio.Event()
        oauth_publish_lock = asyncio.Lock()
        oauth_result: dict[str, object] = {}

        def _oauth_failure_detail(exc: BaseException) -> str:
            # The SDK may wrap the flow's own exception, so walk the chain.
            pending: list[BaseException] = [exc]
            seen: set[int] = set()
            while pending:
                current = pending.pop()
                if id(current) in seen:
                    continue
                seen.add(id(current))
                if isinstance(current, McpTooManyFlows):
                    return MCP_OAUTH_TOO_MANY_DETAIL
                if isinstance(current, McpAuthorizationTimeout):
                    return MCP_OAUTH_TIMEOUT_DETAIL
                if current.__cause__ is not None:
                    pending.append(current.__cause__)
                if current.__context__ is not None:
                    pending.append(current.__context__)
                nested = getattr(current, "exceptions", None)
                if isinstance(nested, tuple):
                    pending.extend(nested)
            return MCP_OAUTH_FAILED_DETAIL

        async def _discard_oauth_client_info() -> None:
            current = await oauth_storage.get_client_info()
            if current is None:
                return
            if (
                oauth_client_info is not None
                and oauth_client_info.client_id != current.client_id
            ):
                return
            oauth_token_store.discard_client_info(
                oauth_user_identifier, oauth_server_key, current.client_id
            )

        async def _run_oauth_session() -> None:
            task = asyncio.current_task()
            assert task is not None
            connected = False
            detail = MCP_OAUTH_FAILED_DETAIL
            exit_stack = AsyncExitStack()
            try:
                try:
                    http_client = await exit_stack.enter_async_context(
                        httpx2.AsyncClient(
                            auth=MarkedOAuthAuth(oauth_flow.provider),
                            event_hooks={"request": [oauth_guard]},
                            timeout=httpx2.Timeout(30, read=300),
                        )
                    )
                    transport = await exit_stack.enter_async_context(
                        streamable_http_client(url=oauth_url, http_client=http_client)
                    )

                    read, write = transport[:2]
                    mcp_client: ClientSession = await exit_stack.enter_async_context(
                        ClientSession(
                            read_stream=read,
                            write_stream=write,
                            sampling_callback=None,
                        )
                    )
                    await negotiate_protocol(mcp_client)

                    if config.code.on_mcp_connect:
                        await config.code.on_mcp_connect(oauth_connection, mcp_client)

                    mcp_data = {
                        "name": payload.name,
                        **await describe_mcp_server(mcp_client, payload.name),
                        "isUserProvided": True,
                        "clientType": payload.clientType,
                        "command": None,
                        "url": oauth_url,
                        "headers": None,
                    }
                    # Publishing the connection and cancelling the task from the
                    # endpoint must not interleave. The event is set inside the
                    # lock so a waiter that holds it never cancels a task whose
                    # session is already attached.
                    async with oauth_publish_lock:
                        mcp_session_obj = McpSession(
                            name=payload.name,
                            client=mcp_client,
                            task=task,
                            stop_event=oauth_stop_event,
                            oauth_server_key=oauth_server_key,
                        )
                        old_mcp = session.swap_mcp_session(
                            payload.name, mcp_session_obj
                        )
                        if session.mcp_oauth_tasks.get(payload.name) is task:
                            del session.mcp_oauth_tasks[payload.name]

                        connected = True
                        oauth_result["mcp"] = mcp_data
                        oauth_connected.set()

                    if old_mcp is not None:
                        # A plain connect may have published between our
                        # initialization and this swap. Close the displaced
                        # client instead of orphaning its transport/task.
                        if on_mcp_disconnect := config.code.on_mcp_disconnect:
                            try:
                                await on_mcp_disconnect(payload.name, old_mcp.client)
                            except Exception:
                                logger.debug(
                                    "Error in on_mcp_disconnect callback for %s",
                                    payload.name,
                                    exc_info=True,
                                )
                        try:
                            await old_mcp.close()
                        except Exception:
                            logger.debug(
                                "Error closing displaced MCP session %s",
                                payload.name,
                                exc_info=True,
                            )

                    await _emit_oauth_event("mcp_connected", {"mcp": mcp_data})
                    await oauth_stop_event.wait()
                finally:
                    try:
                        await exit_stack.aclose()
                    except BaseException:
                        logger.debug(
                            "Error closing OAuth MCP exit stack for %r",
                            payload.name,
                            exc_info=True,
                        )
            except asyncio.CancelledError:
                # The SDK transport can turn an auth-flow error into a task
                # cancellation, dropping the exception. on_failure kept it.
                reported = oauth_flow_failure.get("error")
                detail = (
                    _oauth_failure_detail(reported)
                    if reported is not None
                    else MCP_OAUTH_FAILED_DETAIL
                )
                raise
            except BaseException as exc:
                detail = _oauth_failure_detail(exc)
                oauth_result["error"] = exc
                logger.warning(
                    "MCP OAuth connection %r failed (%s)",
                    payload.name,
                    type(exc).__name__,
                )
            finally:
                if session.mcp_oauth_tasks.get(payload.name) is task:
                    del session.mcp_oauth_tasks[payload.name]
                if not connected:
                    authorization = getattr(oauth_flow, "_authorization", None)
                    if authorization is not None:
                        oauth_pending.cancel(
                            authorization.state,
                            "The MCP OAuth connection ended.",
                        )
                    try:
                        await _discard_oauth_client_info()
                    except BaseException:
                        logger.debug(
                            "Error discarding OAuth client info for %r",
                            payload.name,
                            exc_info=True,
                        )
                    await _emit_oauth_event(
                        "mcp_connection_failed",
                        {"name": payload.name, "detail": detail},
                    )
                    oauth_result["detail"] = detail

        oauth_task = asyncio.create_task(
            _run_oauth_session(), name=f"mcp-oauth-{payload.name}"
        )
        oauth_task_holder["task"] = oauth_task
        session.mcp_oauth_tasks[payload.name] = oauth_task

        def _oauth_success_response() -> JSONResponse:
            return JSONResponse(content={"success": True, "mcp": oauth_result["mcp"]})

        # A stored token can connect without ever calling the redirect handler,
        # so wait for the first of: the sign-in URL, the connection, or the
        # task ending with a failure.
        url_waiter = asyncio.create_task(
            oauth_flow.wait_for_authorization_url(),
            name=f"mcp-oauth-url-{payload.name}",
        )
        connected_waiter = asyncio.create_task(oauth_connected.wait())
        blocked_waiter = asyncio.create_task(oauth_transport_blocked.wait())
        try:
            await asyncio.wait(
                {url_waiter, connected_waiter, oauth_task, blocked_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not connected_waiter.done():
                connected_waiter.cancel()
            if not blocked_waiter.done():
                blocked_waiter.cancel()
            await asyncio.gather(
                connected_waiter, blocked_waiter, return_exceptions=True
            )

        # An allowlisted path that redirected to a sibling path (or any other
        # out-of-grant transport request) fails here, before the 202/connected
        # branches below can claim success.
        if oauth_transport_blocked.is_set():
            if not url_waiter.done():
                url_waiter.cancel()
            await asyncio.gather(url_waiter, return_exceptions=True)
            if not oauth_task.done():
                oauth_task.cancel()
            await asyncio.gather(oauth_task, return_exceptions=True)
            detail = (
                oauth_transport_blocked_detail[0]
                if oauth_transport_blocked_detail
                else MCP_OAUTH_FAILED_DETAIL
            )
            raise HTTPException(status_code=400, detail=detail)

        # A produced URL means the SDK is waiting for the browser callback,
        # even if a test or a pre-resolved callback already let it connect.
        if (
            url_waiter.done()
            and not url_waiter.cancelled()
            and url_waiter.exception() is None
        ):
            authorization_url = url_waiter.result()
            return JSONResponse(
                status_code=202,
                content={
                    "status": "authorization_required",
                    "url": authorization_url,
                    "mcp": {
                        "name": payload.name,
                        "tools": [],
                        "isUserProvided": True,
                        "clientType": payload.clientType,
                        "command": None,
                        "url": oauth_url,
                        "headers": None,
                    },
                },
            )

        if oauth_connected.is_set():
            if not url_waiter.done():
                url_waiter.cancel()
            await asyncio.gather(url_waiter, return_exceptions=True)
            return _oauth_success_response()

        error: Optional[BaseException] = None
        if url_waiter.done() and not url_waiter.cancelled():
            error = url_waiter.exception()
        if error is None:
            stored_error = oauth_result.get("error")
            if isinstance(stored_error, BaseException):
                error = stored_error

        # No URL and no connection: fail the connect. Cancel the task, unless
        # it published a connection while this handler was deciding.
        async with oauth_publish_lock:
            if oauth_connected.is_set():
                if not url_waiter.done():
                    url_waiter.cancel()
                await asyncio.gather(url_waiter, return_exceptions=True)
                return _oauth_success_response()
            if not oauth_task.done():
                oauth_task.cancel()
        if not url_waiter.done():
            url_waiter.cancel()
        await asyncio.gather(url_waiter, return_exceptions=True)
        try:
            await oauth_task
        except BaseException:
            pass

        detail = MCP_OAUTH_FAILED_DETAIL
        for candidate in (
            oauth_result.get("detail"),
            _oauth_failure_detail(error) if error is not None else None,
        ):
            if isinstance(candidate, str) and candidate != MCP_OAUTH_FAILED_DETAIL:
                detail = candidate
                break
        if detail == MCP_OAUTH_TOO_MANY_DETAIL:
            raise HTTPException(
                status_code=429,
                detail=detail,
            ) from None
        raise HTTPException(
            status_code=400,
            detail=detail,
        ) from None

    # ── Validate config before launching the background task ──
    mcp_connection: McpConnection
    # Computed once and reused by the destination-binding factory and the
    # connect-response / error-hygiene branches below.
    is_user_provided = payload.url is not None
    # Only set for stdio named servers; merged into the spawn environment in
    # the background runner.
    stdio_env: Optional[Dict[str, str]] = None

    try:
        if payload.url is None:
            # Named server: look it up in developer-configured servers. No
            # request field can describe a command, so the only stdio process
            # that can start is one the app operator configured.
            server_cfg = next(
                (s for s in config.features.mcp.servers if s.name == payload.name),
                None,
            )
            if server_cfg is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"MCP server {payload.name!r} is not configured. "
                        "Add it to [[features.mcp.servers]] in your .chainlit/config.toml."
                    ),
                )
            if isinstance(server_cfg, StdioMcpServer):
                parts = shlex.split(server_cfg.command, posix=True)
                if not parts:
                    raise ValueError(f"Empty command for MCP server {payload.name!r}")
                mcp_connection = StdioMcpConnection(
                    command=parts[0], args=parts[1:], name=payload.name
                )
                stdio_env = server_cfg.env
            elif isinstance(server_cfg, SseMcpServer):
                mcp_connection = SseMcpConnection(
                    url=server_cfg.url,
                    name=payload.name,
                    headers=server_cfg.headers,
                )
            elif isinstance(server_cfg, StreamableHttpMcpServer):
                mcp_connection = HttpMcpConnection(
                    url=server_cfg.url,
                    name=payload.name,
                    headers=server_cfg.headers,
                )
            else:
                raise HTTPException(
                    status_code=500, detail="Unknown server config type"
                )
        else:
            # User-provided SSE/HTTP connections are enabled only behind the
            # operator's allowlist (CVE-2026-45019); an empty list denies all.
            if not config.features.mcp.user_servers.enabled:
                raise HTTPException(
                    status_code=400,
                    detail=MCP_USER_SERVERS_DISABLED_DETAIL,
                )
            validate_mcp_url(payload.url, config.features.mcp.user_servers.allowed_urls)
            filtered_headers = validate_mcp_headers(payload.headers)
            if payload.clientType == "sse":
                mcp_connection = SseMcpConnection(
                    url=payload.url,
                    name=payload.name,
                    headers=filtered_headers,
                )
            else:  # streamable-http
                mcp_connection = HttpMcpConnection(
                    url=payload.url,
                    name=payload.name,
                    headers=filtered_headers,
                )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Launch the MCP connection in its own background task ──
    #
    # The background task owns the AsyncExitStack: it enters all context
    # managers, calls initialize(), signals ``ready_event``, and then
    # blocks on ``stop_event.wait()``.  When the stop event fires the
    # task wakes up and closes the exit stack *in the same task* that
    # opened it — avoiding the cross-task cancel-scope corruption from
    # https://github.com/Chainlit/chainlit/issues/2182.

    ready_event: asyncio.Event = asyncio.Event()
    stop_event: asyncio.Event = asyncio.Event()
    # Mutable container to pass the ClientSession back from the bg task.
    result_holder: dict[str, object] = {}

    def _record_blocked(exc: McpDestinationError) -> None:
        # Fail-fast side channel: mcp/client/sse.py and
        # mcp/client/streamable_http.py swallow exceptions raised from the
        # httpx2 request hook (bare ``except Exception: logger.exception``,
        # no re-raise), so a blocked destination would otherwise never reach
        # ``ClientSession.initialize()`` and ``ready_event`` would never be
        # set — hanging the bounded wait below until its timeout. This
        # unblocks it immediately instead.
        if "error" not in result_holder:
            result_holder["error"] = exc
        ready_event.set()

    # ── Bind the destination-checking httpx2 client factory ──
    #
    # The SSE transport takes its POST target from the server's ``endpoint``
    # event, and the SDK only validates that event's netloc/scheme — not its
    # path. Re-checking every outgoing request against the *right* grant
    # closes that gap: user-provided connections are bound to the allowlist,
    # named servers to their configured origin (sibling paths are normal for
    # SSE message endpoints).
    mcp_http_client_factory: Optional[McpHttpClientFactory] = None
    if isinstance(mcp_connection, (SseMcpConnection, HttpMcpConnection)):
        if is_user_provided:
            mcp_http_client_factory = make_mcp_http_client_factory(
                _destination_in_allowlist(
                    config.features.mcp.user_servers.allowed_urls
                ),
                on_blocked=_record_blocked,
            )
        else:
            mcp_http_client_factory = make_mcp_http_client_factory(
                _destination_on_origin(mcp_connection.url),
                on_blocked=_record_blocked,
            )

    async def _mcp_session_runner() -> None:
        exit_stack = AsyncExitStack()
        try:
            try:
                if isinstance(mcp_connection, SseMcpConnection):
                    assert mcp_http_client_factory is not None
                    transport = await exit_stack.enter_async_context(
                        sse_client(
                            url=mcp_connection.url,
                            headers=mcp_connection.headers,
                            httpx_client_factory=mcp_http_client_factory,
                        )
                    )
                elif isinstance(mcp_connection, StdioMcpConnection):
                    spawn_env = get_default_environment()
                    if stdio_env:
                        spawn_env.update(stdio_env)
                    transport = await exit_stack.enter_async_context(
                        stdio_client(
                            StdioServerParameters(
                                command=mcp_connection.command,
                                args=mcp_connection.args,
                                env=spawn_env,
                            )
                        )
                    )
                elif isinstance(mcp_connection, HttpMcpConnection):
                    assert mcp_http_client_factory is not None
                    http_client = await exit_stack.enter_async_context(
                        mcp_http_client_factory(
                            headers=mcp_connection.headers,
                            timeout=httpx2.Timeout(30, read=300),
                        )
                    )
                    transport = await exit_stack.enter_async_context(
                        streamable_http_client(
                            url=mcp_connection.url, http_client=http_client
                        )
                    )
                else:
                    raise ValueError(
                        f"Unknown client type: {mcp_connection.clientType}"
                    )

                read, write = transport[:2]

                mcp_client: ClientSession = await exit_stack.enter_async_context(
                    ClientSession(
                        read_stream=read,
                        write_stream=write,
                        sampling_callback=None,
                    )
                )

                await negotiate_protocol(mcp_client)
                result_holder["client"] = mcp_client

            except BaseException as exc:
                # First error wins. The caller may already have recorded the
                # causal failure — a blocked destination (_record_blocked) or
                # the synthesized connect timeout — and then cancelled us to
                # unstick ``initialize()``. The resulting CancelledError
                # arrives here second and carries no message, so overwriting
                # would replace the real reason with a blank one.
                if "error" not in result_holder:
                    result_holder["error"] = exc
                return  # outer finally closes exit_stack
            finally:
                # Always signal the caller so it doesn't wait forever.
                ready_event.set()

            # ── Keep the task (and the exit stack) alive ──
            try:
                await stop_event.wait()
            except asyncio.CancelledError:
                logger.debug("MCP background task for %r cancelled", payload.name)
        finally:
            # Close exit_stack in ALL paths (error, normal shutdown,
            # cancellation) — always in the same task that opened it.
            logger.debug("Closing MCP exit stack for %r (same-task)", payload.name)
            try:
                await exit_stack.aclose()
            except BaseException:
                logger.debug(
                    "Error closing MCP exit stack for %r",
                    payload.name,
                    exc_info=True,
                )

    task = asyncio.create_task(
        _mcp_session_runner(), name=f"mcp-session-{payload.name}"
    )

    # Wait for the background task to finish initialisation, bounded so a
    # blocked destination (swallowed by the SDK — see _record_blocked above)
    # or a slow/hanging transport can't hang the request (and its task)
    # forever. Stdio gets a much longer budget: `npx -y ...` can cold-download
    # a package on first run.
    connect_timeout = (
        _MCP_CONNECT_TIMEOUT_STDIO
        if isinstance(mcp_connection, StdioMcpConnection)
        else _MCP_CONNECT_TIMEOUT_HTTP
    )
    try:
        await asyncio.wait_for(ready_event.wait(), timeout=connect_timeout)
    except asyncio.TimeoutError:
        pass
    except asyncio.CancelledError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise
    if "error" not in result_holder and "client" not in result_holder:
        result_holder["error"] = asyncio.TimeoutError(
            f"Timed out after {connect_timeout:.0f}s waiting for the MCP "
            "connection to initialize."
        )

    if "error" in result_holder:
        # The task already exited and cleaned up its exit stack, or is stuck
        # in initialize(); stop_mcp_task's bounded wait-then-cancel handles
        # both.
        await stop_mcp_task(task, stop_event, payload.name)
        error = cast(BaseException, result_holder["error"])
        logger.warning(
            "MCP connection %r failed: %s",
            payload.name,
            error,
            exc_info=error,
        )
        if is_user_provided:
            # The client already supplied this URL, so echoing the failure
            # detail back is useful and leaks nothing new.
            detail = f"Could not connect to the MCP: {error!s}"
        else:
            # Named servers are developer config and may embed secrets in
            # userinfo or query params; httpx2 errors routinely include the
            # request URL in their string form, so never return it verbatim.
            detail = MCP_CONNECT_FAILED_DETAIL
        return JSONResponse(
            status_code=400,
            content={"detail": detail},
        )

    mcp_client_session = cast("ClientSession", result_holder["client"])

    # Call the user callback
    if config.code.on_mcp_connect:
        try:
            await config.code.on_mcp_connect(mcp_connection, mcp_client_session)
        except asyncio.CancelledError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        except Exception as e:
            # Callback failed — tear down the connection.
            await stop_mcp_task(task, stop_event, payload.name)
            logger.warning(
                "on_mcp_connect callback for %r failed", payload.name, exc_info=e
            )
            # A developer callback can raise anything (DB URLs, API tokens,
            # internal hostnames), so never reflect its text to the client --
            # not even for user-provided servers.
            return JSONResponse(
                status_code=400,
                content={"detail": MCP_CONNECT_FAILED_DETAIL},
            )

    # Store the session. The pop+store is a single await-free step, so a
    # concurrent reconnect for the same name can never observe a state where
    # the name is briefly absent and then have its own session overwritten.
    # Eviction happens only now that the replacement is known to work: a
    # reconnect that fails (bad config, server down, rejecting callback)
    # leaves the existing session intact instead of killing it up front.
    mcp_session_obj = McpSession(
        name=mcp_connection.name,
        client=mcp_client_session,
        task=task,
        stop_event=stop_event,
    )
    displaced_mcp = session.swap_mcp_session(mcp_connection.name, mcp_session_obj)
    if displaced_mcp is not None:
        if on_mcp_disconnect := config.code.on_mcp_disconnect:
            try:
                await on_mcp_disconnect(payload.name, displaced_mcp.client)
            except Exception:
                logger.debug(
                    "Error in on_mcp_disconnect callback for %s",
                    payload.name,
                    exc_info=True,
                )
        try:
            await displaced_mcp.close()
        except Exception:
            logger.debug(
                "Error closing old MCP session %s", payload.name, exc_info=True
            )

    # Echo url/headers only for user-provided servers — the client already
    # sent those. Named servers come from developer config and may embed
    # secrets, so they expose only their name, transport type and tools.
    mcp_payload: Dict[str, object] = {
        "name": mcp_connection.name,
        **await describe_mcp_server(mcp_client_session, mcp_connection.name),
        "isUserProvided": is_user_provided,
    }
    if is_user_provided:
        mcp_payload["clientType"] = mcp_connection.clientType
        mcp_payload["url"] = getattr(mcp_connection, "url", None)
        # Headers are deliberately not echoed: the client already holds the
        # values it submitted, and the current frontend persists returned
        # headers to localStorage, which would make a bearer token readable by
        # same-origin JS and survive logout. P3 decides how to keep them for
        # reconnects.
    else:
        mcp_payload["type"] = mcp_connection.clientType

    return JSONResponse(
        content={
            "success": True,
            "mcp": mcp_payload,
        }
    )


@router.delete("/mcp")
async def disconnect_mcp(
    payload: DisconnectMCPRequest,
    current_user: UserParam,
):
    from chainlit.context import init_ws_context
    from chainlit.mcp_oauth import get_pending_authorizations, get_token_store
    from chainlit.session import WebsocketSession

    session = WebsocketSession.get_by_id(payload.sessionId)
    init_ws_context(session)

    _require_session_principal(session, current_user, detail="Unauthorized")

    # While a flow is still signing in there is no McpSession yet; the pending
    # entry carries the key that was used for the store.
    owner = current_user.identifier if current_user else None
    if owner is None and session.user is not None:
        owner = session.user.identifier
    oauth_server_key = None

    pending_map = get_pending_authorizations()
    pending_handle = pending_map.get_for_session(session.id, payload.name)
    if pending_handle is not None:
        owner = pending_handle.user_identifier
        oauth_server_key = pending_handle.server_key
        pending_map.cancel(pending_handle.state, "The MCP connection was removed.")

    pending_task = session.mcp_oauth_tasks.pop(payload.name, None)
    if pending_task is not None:
        pending_task.cancel()
        try:
            await pending_task
        except BaseException:
            pass

    attached = session.mcp_sessions.get(payload.name)
    if attached is not None and attached.oauth_server_key is not None:
        oauth_server_key = attached.oauth_server_key

    if oauth_server_key is not None and owner is not None:
        get_token_store().forget_server(owner, oauth_server_key)

    callback = config.code.on_mcp_disconnect
    if payload.name in session.mcp_sessions:
        mcp_session_obj = session.mcp_sessions.pop(payload.name)
        try:
            if callback:
                await callback(payload.name, mcp_session_obj.client)
        except Exception:
            logger.exception("MCP disconnect callback failed")
            raise HTTPException(
                status_code=400,
                detail="Could not disconnect from the MCP server.",
            ) from None
        finally:
            await mcp_session_obj.close()

    return JSONResponse(content={"success": True})


@router.post("/mcp/prompt")
async def invoke_mcp_prompt(
    payload: McpPromptRequest,
    current_user: UserParam,
):
    """Render one MCP prompt for the composer.

    Only ever called because the user picked the prompt: Chainlit never
    injects prompt messages into a conversation on its own.
    """
    from chainlit.context import init_ws_context
    from chainlit.mcp_protocol import (
        McpContentTooLarge,
        McpPromptArgumentsError,
        McpPromptNotFound,
        McpPromptsUnsupported,
    )

    session = _get_ws_session(payload.sessionId)
    init_ws_context(session)

    _require_session_principal(session, current_user, detail="Unauthorized")

    attached = session.mcp_sessions.get(payload.name)
    if attached is None:
        raise HTTPException(status_code=404, detail=MCP_NOT_CONNECTED_DETAIL)

    try:
        text = await asyncio.wait_for(
            render_prompt(attached.client, payload.prompt, payload.arguments),
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
    except McpPromptNotFound:
        raise HTTPException(status_code=404, detail=MCP_PROMPT_NOT_FOUND_DETAIL)
    except McpPromptsUnsupported:
        raise HTTPException(status_code=400, detail=MCP_PROMPTS_UNSUPPORTED_DETAIL)
    except McpPromptArgumentsError:
        raise HTTPException(status_code=422, detail=MCP_PROMPT_ARGUMENTS_DETAIL)
    except McpContentTooLarge:
        raise HTTPException(status_code=413, detail=MCP_PROMPT_TOO_LARGE_DETAIL)
    except Exception:
        logger.warning(
            "MCP prompt %r from server %r failed",
            payload.prompt,
            payload.name,
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail=MCP_PROMPT_FAILED_DETAIL)

    return JSONResponse(content={"text": text})


@router.post("/mcp/resource")
async def read_mcp_resource(
    payload: McpResourceRequest,
    current_user: UserParam,
):
    """Read one MCP resource and show it as an element in the chat."""
    from chainlit.context import init_ws_context
    from chainlit.element import File
    from chainlit.mcp_protocol import McpContentTooLarge, McpResourceReadError
    from chainlit.message import Message

    session = _get_ws_session(payload.sessionId)
    init_ws_context(session)

    _require_session_principal(session, current_user, detail="Unauthorized")

    attached = session.mcp_sessions.get(payload.name)
    if attached is None:
        raise HTTPException(status_code=404, detail=MCP_NOT_CONNECTED_DETAIL)

    try:
        resource = await asyncio.wait_for(
            read_resource(attached.client, payload.uri, payload.resourceName),
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
    except McpContentTooLarge:
        raise HTTPException(status_code=413, detail=MCP_RESOURCE_TOO_LARGE_DETAIL)
    except McpResourceReadError:
        raise HTTPException(status_code=502, detail=MCP_RESOURCE_FAILED_DETAIL)
    except Exception:
        logger.warning(
            "MCP resource %r from server %r failed",
            payload.resourceName or "unnamed",
            payload.name,
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail=MCP_RESOURCE_FAILED_DETAIL)

    # A plain File element: remote content is downloadable, never rendered as
    # markdown or HTML in the app. props records where it came from.
    element = File(
        name=resource.name,
        content=resource.data,
        mime=resource.mime_type,
        props={
            "mcp": {
                "server": payload.name,
                "uri": payload.uri,
                "mimeType": resource.mime_type,
            }
        },
    )
    await Message(content="", elements=[element]).send()

    return JSONResponse(content={"element": element.to_dict()})


@router.post("/project/file")
async def upload_file(
    current_user: UserParam,
    session_id: str,
    file: UploadFile,
    ask_parent_id: Optional[str] = None,
):
    """Upload a file to the session files directory."""

    from chainlit.session import WebsocketSession

    session = WebsocketSession.get_by_id(session_id)

    if not session:
        raise HTTPException(
            status_code=404,
            detail="Session not found",
        )

    _require_session_principal(
        session,
        current_user,
        detail="You are not authorized to upload files for this session",
    )

    session.files_dir.mkdir(exist_ok=True)

    try:
        if not file.filename:
            raise HTTPException(
                status_code=400,
                detail="No filename for uploaded file",
            )

        if not file.content_type:
            raise HTTPException(
                status_code=400,
                detail="No content type for uploaded file",
            )

        spec: AskFileSpec = session.files_spec.get(ask_parent_id, None)
        if not spec and ask_parent_id:
            raise HTTPException(
                status_code=404,
                detail="Parent message not found",
            )

        # Validate before reading so an oversized upload is never buffered in memory.
        try:
            validate_file_upload(file, spec=spec)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        max_files = get_max_upload_files(spec)
        uploaded = session.uploaded_file_counts.get(ask_parent_id, 0)
        if max_files is not None and uploaded >= max_files:
            raise HTTPException(
                status_code=400,
                detail="File upload limit reached",
            )

        # Reserve the slot before awaiting so concurrent uploads cannot race past
        # the limit; rejected MIME/size uploads never reach this point.
        session.uploaded_file_counts[ask_parent_id] = uploaded + 1
        try:
            max_bytes = get_max_upload_size_bytes(spec)
            if file.size is None or file.size > _UPLOAD_STREAM_THRESHOLD:
                # Large or unknown-size bodies are streamed with a hard byte
                # cap instead of being materialized with read().
                file.file.seek(0)
                file_response = await session.persist_file(
                    name=file.filename,
                    mime=file.content_type,
                    fileobj=file.file,
                    max_bytes=max_bytes,
                )
            else:
                content = await file.read()
                if max_bytes is not None and len(content) > max_bytes:
                    raise ValueError("File size too large")
                file_response = await session.persist_file(
                    name=file.filename, content=content, mime=file.content_type
                )
        except ValueError as e:
            session.uploaded_file_counts[ask_parent_id] -= 1
            raise HTTPException(status_code=400, detail=str(e)) from None
        except BaseException:
            session.uploaded_file_counts[ask_parent_id] -= 1
            raise

        return JSONResponse(content=file_response)
    finally:
        await file.close()


# Bodies above this size are streamed to disk instead of being read into one
# bytes object. Small uploads keep the existing in-memory path.
_UPLOAD_STREAM_THRESHOLD = 8 * 1024 * 1024
_DEFAULT_MAX_UPLOAD_FILES = 20
_DEFAULT_MAX_UPLOAD_SIZE_MB = 500


def get_max_upload_files(spec: Optional[AskFileSpec]) -> Optional[int]:
    """Return the upload limit for an ask, or for spontaneous uploads."""
    if spec is not None:
        return spec.max_files

    spontaneous = config.features.spontaneous_file_upload
    if spontaneous is None:
        return _DEFAULT_MAX_UPLOAD_FILES
    return spontaneous.max_files


def get_max_upload_size_bytes(spec: Optional[AskFileSpec]) -> Optional[int]:
    """Return the configured byte cap, the safety default, or None."""
    if spec is not None:
        max_size_mb = spec.max_size_mb
    else:
        spontaneous = config.features.spontaneous_file_upload
        max_size_mb = (
            _DEFAULT_MAX_UPLOAD_SIZE_MB
            if spontaneous is None
            else spontaneous.max_size_mb
        )
    if max_size_mb is None:
        return None
    return int(max_size_mb * 1024 * 1024)


def validate_file_upload(file: UploadFile, spec: Optional[AskFileSpec] = None):
    """Validate the file upload as configured in config.features.spontaneous_file_upload or by AskFileSpec
    for a specific message.

    Args:
        file (UploadFile): The file to validate.
        spec (AskFileSpec): The file spec to validate against if any.
    Raises:
        ValueError: If the file is not allowed.
    """
    spontaneous = config.features.spontaneous_file_upload
    if not spec and spontaneous is not None and not spontaneous.enabled:
        raise ValueError("File upload is not enabled")

    validate_file_mime_type(file, spec)
    validate_file_size(file, spec)


def validate_file_mime_type(file: UploadFile, spec: Optional[AskFileSpec]):
    """Validate the file mime type as configured in config.features.spontaneous_file_upload.
    Args:
        file (UploadFile): The file to validate.
    Raises:
        ValueError: If the file type is not allowed.
    """

    if not spec and (
        config.features.spontaneous_file_upload is None
        or config.features.spontaneous_file_upload.accept is None
    ):
        "Accept is not configured, allowing all file types"
        return

    accept = config.features.spontaneous_file_upload.accept if not spec else spec.accept

    assert isinstance(accept, List) or isinstance(accept, dict), (
        "Invalid configuration for spontaneous_file_upload, accept must be a list or a dict"
    )

    if isinstance(accept, List):
        for pattern in accept:
            if fnmatch.fnmatch(str(file.content_type), pattern):
                return
    elif isinstance(accept, dict):
        for pattern, extensions in accept.items():
            if fnmatch.fnmatch(str(file.content_type), pattern):
                if len(extensions) == 0:
                    return
                for extension in extensions:
                    if file.filename is not None and file.filename.lower().endswith(
                        extension.lower()
                    ):
                        return
    raise ValueError("File type not allowed")


def validate_file_size(file: UploadFile, spec: Optional[AskFileSpec]):
    """Validate the file size as configured in config.features.spontaneous_file_upload.
    Args:
        file (UploadFile): The file to validate.
    Raises:
        ValueError: If the file size is too large.
    """
    if not spec:
        spontaneous = config.features.spontaneous_file_upload
        if spontaneous is not None and spontaneous.max_size_mb is None:
            return

    max_bytes = get_max_upload_size_bytes(spec)
    if max_bytes is not None and file.size is not None and file.size > max_bytes:
        raise ValueError("File size too large")


@router.get("/project/file/{file_id}")
async def get_file(
    file_id: str,
    session_id: str,
    current_user: UserParam,
):
    """Get a file from the session files directory."""
    from chainlit.session import WebsocketSession

    session = WebsocketSession.get_by_id(session_id) if session_id else None

    if not session:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
        )

    _require_session_principal(
        session,
        current_user,
        detail="You are not authorized to download files from this session",
    )

    if file_id in session.files:
        from chainlit.element import Element

        file = session.files[file_id]
        mime = Element.normalize_mime(file["type"]) or "application/octet-stream"
        return FileResponse(
            file["path"],
            media_type=mime,
            filename=file.get("name"),
            content_disposition_type=(
                "inline" if Element.is_browser_rendered_mime(mime) else "attachment"
            ),
            headers={
                "X-Content-Type-Options": "nosniff",
                # The URL carries the session id; keep it out of Referer
                # headers when the rendered document links elsewhere.
                "Referrer-Policy": "no-referrer",
            },
        )
    else:
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/favicon")
async def get_favicon():
    """Get the favicon for the UI."""
    public_root = Path(APP_ROOT) / "public"
    files = glob.glob(str(public_root / "favicon.*"))

    if files:
        favicon_path = files[0]
        favicon_root = public_root
    else:
        favicon_root = Path(build_dir)
        favicon_path = str(favicon_root / "favicon.svg")

    media_type, _ = mimetypes.guess_type(favicon_path)
    resolved_path = _resolve_selected_static_file(favicon_path, favicon_root)

    return FileResponse(resolved_path, media_type=media_type)


@router.get("/logo")
async def get_logo(theme: Optional[Theme] = Query(Theme.light)):
    """Get the default logo for the UI."""
    theme_value = theme.value if theme else Theme.light.value
    logo_path = None
    logo_root: Optional[Path] = None

    for root_path, pattern in (
        (Path(APP_ROOT) / "public", f"logo_{theme_value}.*"),
        (Path(build_dir), os.path.join("assets", f"logo_{theme_value}*.*")),
    ):
        files = glob.glob(str(root_path / pattern))
        if files:
            logo_path = files[0]
            logo_root = root_path
            break

    if not logo_path:
        logo_root = Path(__file__).parent / "frontend" / "dist"
        logo_path = str(logo_root / f"logo_{theme_value}.svg")
        logger.info("Missing custom logo. Falling back to default logo.")

    assert logo_root is not None
    media_type, _ = mimetypes.guess_type(logo_path)
    resolved_path = _resolve_selected_static_file(logo_path, logo_root)

    return FileResponse(resolved_path, media_type=media_type)


@router.get("/avatars/{avatar_id:str}")
async def get_avatar(avatar_id: str):
    """Get the avatar for the user based on the avatar_id."""
    if not re.match(r"^[a-zA-Z0-9_ .-]+$", avatar_id):
        raise HTTPException(status_code=400, detail="Invalid avatar_id")

    if avatar_id == "default":
        avatar_id = config.ui.name

    avatar_id = avatar_id.strip().lower().replace(" ", "_").replace(".", "_")

    base_path = Path(APP_ROOT) / "public" / "avatars"
    avatar_pattern = f"{avatar_id}.*"

    matching_files = base_path.glob(avatar_pattern)

    if avatar_path := next(matching_files, None):
        if not is_path_inside(avatar_path, base_path):
            raise HTTPException(status_code=400, detail="Invalid filename")
        media_type, _ = mimetypes.guess_type(str(avatar_path))

        return FileResponse(avatar_path, media_type=media_type)

    return await get_favicon()


@router.head("/")
def status_check():
    """Check if the site is operational."""
    return {"message": "Site is operational"}


@router.get("/health")
def health_check():
    """Health check endpoint for container orchestration and monitoring."""
    return {"status": "ok"}


@router.get("/{full_path:path}")
async def serve(request: Request):
    """Serve the UI files."""
    root_path = os.getenv("CHAINLIT_PARENT_ROOT_PATH", "") + os.getenv(
        "CHAINLIT_ROOT_PATH", ""
    )
    html_template = get_html_template(root_path)
    response = HTMLResponse(content=html_template, status_code=200)

    return response


# Register skills routes only when the flag is on: the flag is startup-time so a
# disabled app has no route, no data-layer call and no schema change.
def _register_skills_routes(target: FastAPI) -> bool:
    """Mount the skills router when the flag is on; return whether it mounted."""
    if not config.features.skills:
        return False
    target.include_router(skills_router)
    return True


_SKILLS_ROUTES_MOUNTED = _register_skills_routes(app)

app.include_router(router)

import chainlit.socket  # noqa
