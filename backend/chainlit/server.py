import asyncio
import fnmatch
import glob
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
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union, cast

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
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import ValidationError
from starlette.datastructures import URL
from starlette.middleware.cors import CORSMiddleware
from starlette.types import Receive, Scope, Send
from typing_extensions import Annotated
from watchfiles import awatch

from chainlit.approval import ApprovalError, ApprovalNotPending, transition
from chainlit.auth import (
    create_jwt,
    decode_jwt,
    get_configuration,
    get_current_user,
    require_login,
    resolve_current_user_optional,
)
from chainlit.auth.cookie import (
    clear_auth_cookie,
    clear_oauth_cookies,
    get_oauth_nonce_cookie,
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
from chainlit.data.acl import is_thread_author
from chainlit.data.base import BaseDataLayer
from chainlit.logger import logger
from chainlit.markdown import get_markdown_str
from chainlit.oauth_providers import get_oauth_provider
from chainlit.scheduled_task import (
    next_run_at,
    validate_cron,
    validate_prompt,
    validate_timezone,
)
from chainlit.scheduler import ScheduledTaskWorker
from chainlit.secret import random_secret
from chainlit.session import WebsocketSession
from chainlit.skill import (
    MAX_SKILLS,
    Skill,
    SkillError,
    load_skills,
    parse_skill_markdown,
    skill_content_hash,
    skill_to_markdown,
)
from chainlit.thread_metadata import public_thread
from chainlit.types import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    AskFileSpec,
    CallActionRequest,
    ConnectMCPRequest,
    CreatePromptRequest,
    CreateScheduledTaskRequest,
    CreateSkillRequest,
    DeleteFeedbackRequest,
    DeleteThreadRequest,
    DisconnectMCPRequest,
    ElementRequest,
    GetThreadsRequest,
    ImportSkillRequest,
    PromptDict,
    ScheduledTaskDict,
    SharePromptRequest,
    ShareThreadRequest,
    Theme,
    UpdateFeedbackRequest,
    UpdatePromptRequest,
    UpdateScheduledTaskRequest,
    UpdateSkillRequest,
    UpdateThreadRequest,
)
from chainlit.user import PersistedUser, User
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
        except asyncio.exceptions.CancelledError:
            pass

        _cleanup_files_directory()

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

sio = socketio.AsyncServer(cors_allowed_origins=[], async_mode="asgi")

asgi_app = socketio.ASGIApp(socketio_server=sio, socketio_path="")

# config.run.root_path is only set when started with --root-path. Not on submounts.
SOCKET_IO_PATH = f"{config.run.root_path}/ws/socket.io"
app.mount(SOCKET_IO_PATH, asgi_app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.project.allow_origins,
    allow_credentials=True,
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


@router.get("/public/{filename:path}")
async def serve_public_file(
    filename: str,
):
    """Serve a file from public dir."""

    base_path = Path(public_dir)
    file_path = (base_path / filename).resolve()

    if not is_path_inside(file_path, base_path):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if file_path.is_file():
        return FileResponse(file_path)
    else:
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/assets/{filename:path}")
async def serve_asset_file(
    filename: str,
):
    """Serve a file from assets dir."""

    base_path = Path(os.path.join(build_dir, "assets"))
    file_path = (base_path / filename).resolve()

    if not is_path_inside(file_path, base_path):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if file_path.is_file():
        return FileResponse(file_path)
    else:
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/copilot/{filename:path}")
async def serve_copilot_file(
    filename: str,
):
    """Serve a file from assets dir."""

    base_path = Path(copilot_build_dir)
    file_path = (base_path / filename).resolve()

    if not is_path_inside(file_path, base_path):
        raise HTTPException(status_code=400, detail="Invalid filename")

    if file_path.is_file():
        return FileResponse(file_path)
    else:
        raise HTTPException(status_code=404, detail="File not found")


# -------------------------------------------------------------------------------
#                               SLACK HTTP HANDLER
# -------------------------------------------------------------------------------

if (
    os.environ.get("SLACK_BOT_TOKEN")
    and os.environ.get("SLACK_SIGNING_SECRET")
    and not os.environ.get("SLACK_WEBSOCKET_TOKEN")
):
    from chainlit.slack.app import slack_app_handler

    @router.post("/slack/events")
    async def slack_endpoint(req: Request):
        return await slack_app_handler.handle(req)


# -------------------------------------------------------------------------------
#                               TEAMS HANDLER
# -------------------------------------------------------------------------------

if os.environ.get("TEAMS_APP_ID") and os.environ.get("TEAMS_APP_PASSWORD"):
    from botbuilder.schema import Activity

    from chainlit.teams.app import adapter, bot

    @router.post("/teams/events")
    async def teams_endpoint(req: Request):
        body = await req.json()
        activity = Activity().deserialize(body)
        auth_header = req.headers.get("Authorization", "")
        response = await adapter.process_activity(activity, auth_header, bot.on_turn)
        return response


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

    default_url = config.ui.custom_meta_url or "https://github.com/Chainlit/chainlit"
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
    response = RedirectResponse(
        url=str(request.url_for("login")) + "?" + params,
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


@router.post("/login")
async def login(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """
    Login a user using the password auth callback.
    """
    if not config.code.password_auth_callback:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="No auth_callback defined"
        )

    user = await config.code.password_auth_callback(
        form_data.username, form_data.password
    )

    return await _authenticate_user(request, user)


@router.post("/logout")
async def logout(request: Request, response: Response):
    """Logout the user by calling the on_logout callback.

    Identity is resolved best effort: an absent, expired or invalid token must
    not turn logout into a 401 before the cookie is cleared. When it resolves,
    the user's MCP credentials and in-flight authorizations are dropped before
    ``on_logout`` runs, so a failing callback cannot leave them behind.
    """
    from chainlit.mcp_oauth import get_pending_authorizations, get_token_store

    user = await resolve_current_user_optional(request)
    if user is not None:
        get_token_store().forget_user(user.identifier)
        get_pending_authorizations().cancel_user(user.identifier)

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

    set_oauth_state_cookie(response, random)
    if nonce is not None:
        set_oauth_nonce_cookie(response, nonce)

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


def _supports_prompt_gallery(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a data layer implements all prompt gallery operations."""
    if data_layer is None:
        return False

    return all(
        callable(getattr(data_layer, method, None))
        and getattr(type(data_layer), method, None)
        is not getattr(BaseDataLayer, method)
        for method in _PROMPT_GALLERY_METHODS
    )


def _supports_delete_all_threads(data_layer: Optional[BaseDataLayer]) -> bool:
    """Return whether a data layer overrides bulk thread deletion."""
    if data_layer is None:
        return False

    method = getattr(type(data_layer), "delete_all_threads", None)
    return callable(method) and method is not BaseDataLayer.delete_all_threads


@router.get("/user")
async def get_user(current_user: UserParam) -> GenericUser:
    return current_user


_language_pattern = (
    "^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,4})?(-[a-zA-Z0-9]{2,8})?(-x-[a-zA-Z0-9]{1,8})?$"
)


@router.post("/set-session-cookie")
async def set_session_cookie(request: Request, response: Response):
    body = await request.json()
    session_id = body.get("session_id")

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
        content={
            "ui": cfg.ui.model_dump(),
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
            "promptGallery": bool(
                getattr(cfg.features, "prompt_gallery", False)
                and _supports_prompt_gallery(data_layer)
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
        }
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
    except Exception as e:
        raise HTTPException(detail=str(e), status_code=500) from e

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
        payload.filter.userId = persisted_user.id
    else:
        payload.filter.userId = current_user.id

    res = await data_layer.list_threads(payload.pagination, payload.filter)
    response = res.to_dict()
    response["data"] = [public_thread(thread) for thread in response["data"]]
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

    await is_thread_author(current_user.identifier, thread_id)

    try:
        res = await data_layer.get_thread(thread_id)
    except Exception as e:
        logger.error(f"Failed to get thread {thread_id}: {e!s}")
        raise HTTPException(
            status_code=500,
            detail="Failed to load conversation history",
        )
    return JSONResponse(content=public_thread(res, include_settings=True))


@router.get("/project/share/{thread_id}")
async def get_shared_thread(
    request: Request,
    thread_id: str,
    current_user: UserParam,
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
    # Extract and normalize metadata (may be dict, strified JSON, or None)
    metadata = (thread.get("metadata") if isinstance(thread, dict) else {}) or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}

    user_can_view = False
    if getattr(config.code, "on_shared_thread_view", None):
        try:
            user_can_view = await config.code.on_shared_thread_view(
                thread, current_user
            )
        except Exception:
            user_can_view = False

    is_shared = bool(metadata.get("is_shared"))

    # Proceed only raise an error if both conditions are False.
    if (not user_can_view) and (not is_shared):
        raise HTTPException(status_code=404, detail="Thread not found")

    return JSONResponse(content=public_thread(thread))


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


def _prompt_gallery_enabled() -> None:
    """Raise 400 when the feature is disabled, to keep guards DRY."""
    if not config.features.prompt_gallery:
        raise HTTPException(
            status_code=400, detail="Prompt gallery feature is not enabled"
        )


async def _dl_prompt(coro: Any) -> Any:
    """Await a prompt data-layer call; map NotImplementedError to HTTP 501."""
    try:
        return await coro
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=str(exc) or "Prompt gallery is not supported by this data layer",
        ) from exc


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
    user_id = await _get_user_id(current_user, data_layer)
    existing = await _dl_prompt(data_layer.get_prompt(prompt_id))
    if not existing or existing["userId"] != user_id:
        raise HTTPException(status_code=404, detail="Prompt not found")
    updated = dict(existing)
    if payload.title is not None:
        updated["title"] = payload.title
    if payload.content is not None:
        updated["content"] = payload.content
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
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    user_id = await _get_user_id(current_user, data_layer)
    existing = await _dl_prompt(data_layer.get_prompt(prompt_id))
    if not existing or existing["userId"] != user_id:
        raise HTTPException(status_code=404, detail="Prompt not found")
    updated = dict(existing)
    updated["isShared"] = payload.isShared
    updated["updatedAt"] = utc_now()
    result = await _dl_prompt(data_layer.update_prompt(updated))  # type: ignore[arg-type]
    return JSONResponse(content={"prompt": result, "shareUrl": f"/prompt/{prompt_id}"})


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
    prompt = await _dl_prompt(data_layer.get_prompt(prompt_id))
    if not prompt or not prompt["isShared"]:
        raise HTTPException(status_code=404, detail="Prompt not found")
    return JSONResponse(content=prompt)


@router.post("/project/prompt/share/{prompt_id}/add")
async def add_shared_prompt(
    request: Request,
    prompt_id: str,
    current_user: UserParam,
):
    """Copy a shared prompt into the current user's gallery."""
    _prompt_gallery_enabled()
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    source = await _dl_prompt(data_layer.get_prompt(prompt_id))
    if not source or not source["isShared"]:
        raise HTTPException(status_code=404, detail="Prompt not found")
    import uuid as _uuid

    now = utc_now()
    copy = {
        "id": str(_uuid.uuid4()),
        "title": source["title"],
        "content": source["content"],
        "userId": await _get_user_id(current_user, data_layer),
        "isShared": False,
        "createdAt": now,
        "updatedAt": now,
    }
    created = await _dl_prompt(data_layer.create_prompt(copy))  # type: ignore[arg-type]
    return JSONResponse(content=created)


# ---- Per-user skills endpoints (router registered only when the flag is on) ----

skills_router = APIRouter(prefix=config.run.root_path)


def _skills_data_layer() -> BaseDataLayer:
    data_layer = get_data_layer()
    if not data_layer:
        raise HTTPException(status_code=400, detail="Data persistence is not enabled")
    return data_layer


async def _dl_skill(coro: Any) -> Any:
    """Await a skills data-layer call; map NotImplementedError to HTTP 501."""
    try:
        return await coro
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=str(exc) or "Skills are not supported by this data layer",
        ) from exc


def _skill_markdown(name: str, description: str, content: str) -> str:
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
) -> None:
    if name in {skill.name for skill in load_skills()}:
        raise HTTPException(
            status_code=409,
            detail=f"Skill name {name!r} is reserved by an app skill",
        )
    if any(item["title"] == name and item["id"] != skill_id for item in existing):
        raise HTTPException(
            status_code=409, detail=f"Skill name {name!r} already exists"
        )


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
) -> PromptDict:
    now = utc_now()
    return {
        "id": skill_id,
        "userId": user_id,
        "title": name,
        "content": content,
        "isShared": False,
        "kind": "skill",
        "description": description,
        "enabled": enabled,
        "importedFrom": imported_from,
        "importedAt": imported_at,
        "contentHash": content_hash,
        "createdAt": now,
        "updatedAt": now,
    }


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
    if "\n" in payload.imported_from or "\r" in payload.imported_from:
        raise HTTPException(
            status_code=400, detail="imported_from must be a single line"
        )
    try:
        skill = parse_skill_markdown(payload.markdown, origin="imported skill")
    except SkillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
    """Edit or enable one of the current user's skills."""
    data_layer = _skills_data_layer()
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
    if payload.enabled is not None:
        updated["enabled"] = payload.enabled
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
    user_id = await _get_user_id(current_user, data_layer)
    deleted = await _dl_skill(data_layer.delete_skill(skill_id, user_id))
    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found")
    return JSONResponse(content={"success": True})


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

    await is_thread_author(current_user.identifier, thread_id)

    res = await data_layer.get_element(thread_id, element_id)
    if res is None:
        raise HTTPException(status_code=404, detail="Element not found")
    return JSONResponse(content=res)


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

    if current_user:
        if (
            not context.session.user
            or context.session.user.identifier != current_user.identifier
        ):
            raise HTTPException(
                status_code=401,
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

    if current_user:
        if (
            not context.session.user
            or context.session.user.identifier != current_user.identifier
        ):
            raise HTTPException(
                status_code=401,
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

    await data_layer.delete_thread(thread_id)
    return JSONResponse(content={"success": True})


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

    try:
        deleted = await data_layer.delete_all_threads(user_id)
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=501,
            detail=str(exc)
            or "Bulk thread deletion is not supported by this data layer",
        ) from exc

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    logger.info(
        "Deleted %s threads for user %s in %sms", deleted, identifier, duration_ms
    )
    return JSONResponse(content={"success": True, "deleted": deleted})


def _is_json_same_origin(request: Request) -> bool:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/json":
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host")
    if not host:
        return False
    return urllib.parse.urlsplit(origin).netloc == host


def _approval_response(record: dict, *, error: Optional[str] = None) -> dict:
    return ApprovalDecisionResponse(
        approvalId=record["id"],
        status=record["status"],
        decision=record.get("decision"),
        decidedBy=record.get("decidedBy"),
        decidedAt=record.get("decidedAt") or record.get("terminalAt"),
        terminalReason=record.get("terminalReason"),
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
    if not session.user or session.user.identifier != current_user.identifier:
        raise HTTPException(status_code=403, detail="Approval author required")
    if session.unavailable_chat_profile:
        raise HTTPException(
            status_code=409,
            detail="This conversation is read-only because its chat profile is unavailable.",
        )

    try:
        record = await transition(
            payload.approvalId,
            "approved" if payload.decision == "approve" else "rejected",
            actor=session.user,
            thread_id=session.thread_id,
            session_generation=session.generation,
            reason=payload.reason,
            decision_request_id=payload.decisionRequestId,
        )
    except ApprovalNotPending as exc:
        if exc.record is None:
            raise HTTPException(status_code=exc.status_code, detail=exc.code)
        return JSONResponse(
            status_code=exc.status_code,
            content=_approval_response(exc.record, error=exc.code),
        )
    except ApprovalError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code)

    return JSONResponse(content=_approval_response(record))


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

    if current_user:
        if (
            not context.session.user
            or context.session.user.identifier != current_user.identifier
        ):
            raise HTTPException(
                status_code=401,
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
    from chainlit.session import McpSession, WebsocketSession, stop_mcp_task

    session = WebsocketSession.get_by_id(payload.sessionId)
    context = init_ws_context(session)
    config: ChainlitConfig = session.get_config()

    if current_user:
        if (
            not context.session.user
            or context.session.user.identifier != current_user.identifier
        ):
            raise HTTPException(
                status_code=401,
            )

    mcp_enabled = config.features.mcp.enabled
    if not mcp_enabled:
        raise HTTPException(
            status_code=400,
            detail="This app does not support MCP.",
        )

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
                await session.emit(event, data)
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
                    await mcp_client.initialize()

                    if config.code.on_mcp_connect:
                        await config.code.on_mcp_connect(oauth_connection, mcp_client)

                    tool_list = await mcp_client.list_tools()
                    mcp_data = {
                        "name": payload.name,
                        "tools": [{"name": tool.name} for tool in tool_list.tools],
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
                        session.mcp_sessions[payload.name] = mcp_session_obj
                        if session.mcp_oauth_tasks.get(payload.name) is task:
                            del session.mcp_oauth_tasks[payload.name]

                        connected = True
                        oauth_result["mcp"] = mcp_data
                        oauth_connected.set()

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

                await mcp_client.initialize()
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
    old_mcp = session.swap_mcp_session(mcp_connection.name, mcp_session_obj)
    if old_mcp is not None:
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

    tool_list = await mcp_client_session.list_tools()

    # Echo url/headers only for user-provided servers — the client already
    # sent those. Named servers come from developer config and may embed
    # secrets, so they expose only their name, transport type and tools.
    mcp_payload: Dict[str, object] = {
        "name": mcp_connection.name,
        "tools": [{"name": t.name} for t in tool_list.tools],
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
    context = init_ws_context(session)

    if current_user:
        if (
            not context.session.user
            or context.session.user.identifier != current_user.identifier
        ):
            raise HTTPException(
                status_code=401,
            )

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
        except Exception as e:
            raise HTTPException(
                status_code=400,
                detail=f"Could not disconnect from the MCP: {e!s}",
            )
        finally:
            await mcp_session_obj.close()

    return JSONResponse(content={"success": True})


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

    if current_user:
        if not session.user or session.user.identifier != current_user.identifier:
            raise HTTPException(
                status_code=401,
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
            content = await file.read()

            file_response = await session.persist_file(
                name=file.filename, content=content, mime=file.content_type
            )
        except BaseException:
            session.uploaded_file_counts[ask_parent_id] -= 1
            raise

        return JSONResponse(content=file_response)
    finally:
        await file.close()


def get_max_upload_files(spec: Optional[AskFileSpec]) -> Optional[int]:
    """Return the upload limit for an ask, or for spontaneous uploads."""
    if spec is not None:
        return spec.max_files

    spontaneous = config.features.spontaneous_file_upload
    return spontaneous.max_files if spontaneous else None


def validate_file_upload(file: UploadFile, spec: Optional[AskFileSpec] = None):
    """Validate the file upload as configured in config.features.spontaneous_file_upload or by AskFileSpec
    for a specific message.

    Args:
        file (UploadFile): The file to validate.
        spec (AskFileSpec): The file spec to validate against if any.
    Raises:
        ValueError: If the file is not allowed.
    """
    if not spec and config.features.spontaneous_file_upload is None:
        """Default for a missing config is to allow the fileupload without any restrictions"""
        return

    if not spec and not config.features.spontaneous_file_upload.enabled:
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
    if not spec and (
        config.features.spontaneous_file_upload is None
        or config.features.spontaneous_file_upload.max_size_mb is None
    ):
        return

    max_size_mb = (
        config.features.spontaneous_file_upload.max_size_mb
        if not spec
        else spec.max_size_mb
    )
    if file.size is not None and file.size > max_size_mb * 1024 * 1024:
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

    if current_user:
        if not session.user or session.user.identifier != current_user.identifier:
            raise HTTPException(
                status_code=401,
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
            headers={"X-Content-Type-Options": "nosniff"},
        )
    else:
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/favicon")
async def get_favicon():
    """Get the favicon for the UI."""
    custom_favicon_path = os.path.join(APP_ROOT, "public", "favicon.*")
    files = glob.glob(custom_favicon_path)

    if files:
        favicon_path = files[0]
    else:
        favicon_path = os.path.join(build_dir, "favicon.svg")

    media_type, _ = mimetypes.guess_type(favicon_path)

    return FileResponse(favicon_path, media_type=media_type)


@router.get("/logo")
async def get_logo(theme: Optional[Theme] = Query(Theme.light)):
    """Get the default logo for the UI."""
    theme_value = theme.value if theme else Theme.light.value
    logo_path = None

    for path in [
        os.path.join(APP_ROOT, "public", f"logo_{theme_value}.*"),
        os.path.join(build_dir, "assets", f"logo_{theme_value}*.*"),
    ]:
        files = glob.glob(path)

        if files:
            logo_path = files[0]
            break

    if not logo_path:
        logo_path = os.path.join(
            os.path.dirname(__file__),
            "frontend",
            "dist",
            f"logo_{theme_value}.svg",
        )
        logger.info("Missing custom logo. Falling back to default logo.")

    media_type, _ = mimetypes.guess_type(logo_path)

    return FileResponse(logo_path, media_type=media_type)


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
