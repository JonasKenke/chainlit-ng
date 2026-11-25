import asyncio
import json
import os
import uuid
from typing import Any, Dict, Literal, Optional, Tuple, TypedDict, Union
from urllib.parse import unquote

from starlette.requests import cookie_parser
from typing_extensions import TypeAlias

from chainlit import otel
from chainlit.approval import (
    ApprovalPending,
    guard_pending_approval,
    orphan_thread_approvals,
    supersede_thread_approvals,
)
from chainlit.auth import (
    get_current_user,
    get_token_from_cookies,
    require_login,
)
from chainlit.chat_context import chat_context
from chainlit.config import ChainlitConfig, config
from chainlit.context import init_ws_context
from chainlit.data import get_data_layer
from chainlit.data.acl import ThreadAccess, authorize_thread_read
from chainlit.logger import logger
from chainlit.message import ErrorMessage, Message
from chainlit.mode import MODE_PREFERENCES_METADATA_KEY
from chainlit.server import sio
from chainlit.session import (
    ClientType,
    WebsocketSession,
    is_valid_session_id,
    is_valid_thread_id,
)
from chainlit.types import (
    InputAudioChunk,
    InputAudioChunkPayload,
    MessagePayload,
)
from chainlit.user import PersistedUser, User
from chainlit.user_session import user_sessions

WSGIEnvironment: TypeAlias = dict[str, Any]


class WebSocketSessionAuth(TypedDict):
    sessionId: str
    userEnv: str | None
    clientType: ClientType
    chatProfile: str | None
    threadId: str | None


# Bound per-session fan-out. The run lock serializes execution; these caps
# bound the queue of work waiting behind it and the audio callback backlog.
_MAX_PENDING_RUNS = int(os.environ.get("CHAINLIT_MAX_PENDING_RUNS", "8") or 0)
_MAX_AUDIO_TASKS = int(os.environ.get("CHAINLIT_MAX_AUDIO_TASKS", "4") or 0)


def _session_owner_matches_user(
    session: WebsocketSession, user: User | PersistedUser | None
) -> bool:
    if session.user is None and user is None:
        return True

    if session.user is None or user is None:
        return False

    if isinstance(session.user, PersistedUser) and isinstance(user, PersistedUser):
        return (session.user.id, session.user.createdAt) == (user.id, user.createdAt)

    return session.user.identifier == user.identifier


def restore_existing_session(
    sid,
    session_id,
    emit_fn,
    emit_call_fn,
    environ,
    user: User | PersistedUser | None = None,
):
    """Restore a session from the sessionId provided by the client."""
    if session := WebsocketSession.get_by_id(session_id):
        if not _session_owner_matches_user(session, user):
            logger.error("Authorization for the session failed.")
            raise ConnectionRefusedError("authorization failed")

        session.restore(new_socket_id=sid)
        session.emit = emit_fn
        session.emit_call = emit_call_fn
        session.environ = environ
        return True
    return False


async def persist_user_session(thread_id: str, metadata: Dict):
    if data_layer := get_data_layer():
        await data_layer.update_thread(thread_id=thread_id, metadata=metadata)


class ChatProfileUnavailableError(ValueError):
    pass


async def _persist_mode_selection(
    session: WebsocketSession, mode_id: str, option_id: str
) -> None:
    user = session.user
    data_layer = get_data_layer()
    if not user or not data_layer:
        return

    try:
        latest_user = await data_layer.get_user(user.identifier)
        metadata = dict(getattr(latest_user or user, "metadata", None) or {})
        persisted_modes = metadata.get(MODE_PREFERENCES_METADATA_KEY)
        persisted_modes = (
            dict(persisted_modes) if isinstance(persisted_modes, dict) else {}
        )
        persisted_modes[mode_id] = option_id
        metadata[MODE_PREFERENCES_METADATA_KEY] = persisted_modes

        updated_user = await data_layer.create_user(
            User(
                identifier=user.identifier,
                display_name=getattr(user, "display_name", None),
                metadata=metadata,
            )
        )
        if updated_user:
            session.user = updated_user
        else:
            user.metadata = metadata
    except Exception:
        logger.exception(
            "Unable to persist mode preference for user '%s'", user.identifier
        )


async def resume_thread(session: WebsocketSession):
    data_layer = get_data_layer()
    if not data_layer or not session.user or not session.thread_id_to_resume:
        return
    thread = await data_layer.get_thread(thread_id=session.thread_id_to_resume)
    if not thread:
        return

    access = await authorize_thread_read(
        session.thread_id_to_resume, session.user, thread=thread, data_layer=data_layer
    )
    if access == ThreadAccess.OWNER:
        await orphan_thread_approvals(
            thread, keep_generation=getattr(session, "generation", None)
        )
        metadata = thread.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if chat_profile := metadata.get("chat_profile"):
            profiles = (
                await config.code.set_chat_profiles(session.user, session.language)
                if config.code.set_chat_profiles
                else []
            )
            if not any(profile.name == chat_profile for profile in profiles or []):
                raise ChatProfileUnavailableError(
                    f"The chat profile '{chat_profile}' is no longer available. "
                    "You can still view this conversation."
                )
        user_sessions[session.id] = metadata.copy()
        if chat_profile := metadata.get("chat_profile"):
            session.chat_profile = chat_profile
        if chat_settings := metadata.get("chat_settings"):
            session.chat_settings = chat_settings

        return thread


def load_user_env(user_env):
    """Parse the client-supplied environment map and enforce shape.

    The value crosses the socket handshake unauthenticated (when login is
    disabled), so anything other than a flat string-to-string object is
    refused before it reaches app code or a session.
    """
    user_env_dict = {}
    if user_env:
        try:
            parsed = json.loads(user_env)
        except (TypeError, ValueError) as exc:
            raise ConnectionRefusedError("Invalid user environment variables") from exc
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in parsed.items()
        ):
            raise ConnectionRefusedError("Invalid user environment variables")
        user_env_dict = parsed
    # Check user env
    if config.project.user_env:
        if not user_env_dict:
            raise ConnectionRefusedError("Missing user environment variables")
        # Check if requested user environment variables are provided
        for key in config.project.user_env:
            if key not in user_env_dict:
                raise ConnectionRefusedError(
                    "Missing user environment variable: " + key
                )
    return user_env_dict


def _get_token_from_cookie(environ: WSGIEnvironment) -> Optional[str]:
    if cookie_header := environ.get("HTTP_COOKIE", None):
        cookies = cookie_parser(cookie_header)
        return get_token_from_cookies(cookies)

    return None


def _get_token(environ: WSGIEnvironment) -> Optional[str]:
    """Take WSGI environ, return access token."""
    return _get_token_from_cookie(environ)


async def _authenticate_connection(
    environ: WSGIEnvironment,
) -> Union[Tuple[Union[User, PersistedUser], str], Tuple[None, None]]:
    if token := _get_token(environ):
        user = await get_current_user(token=token)
        if user:
            return user, token

    return None, None


@sio.on("connect")  # pyright: ignore [reportOptionalCall]
async def connect(sid: str, environ: WSGIEnvironment, auth: WebSocketSessionAuth):
    user: User | PersistedUser | None = None
    token: str | None = None

    if not is_valid_session_id(auth.get("sessionId")):
        logger.error("Invalid session id in websocket connect.")
        raise ConnectionRefusedError("invalid session id")

    thread_id = auth.get("threadId", None)

    if thread_id and not is_valid_thread_id(thread_id):
        logger.error("Invalid thread id in websocket connect.")
        raise ConnectionRefusedError("invalid thread id")

    if require_login():
        try:
            user, token = await _authenticate_connection(environ)
        except Exception as e:
            logger.exception("Exception authenticating connection: %s", e)

        if not user:
            logger.error("Authentication failed in websocket connect.")
            raise ConnectionRefusedError("authentication failed")

    # Thread authorization also applies without login. Otherwise a client
    # could supply a shared/known thread id and append steps to an owned
    # thread when authentication is disabled. Ownerless threads remain
    # resumable in no-auth mode because there is no principal to compare.
    if thread_id:
        if data_layer := get_data_layer():
            thread = await data_layer.get_thread(thread_id)
            if thread:
                access = await authorize_thread_read(
                    thread_id, user, thread=thread, data_layer=data_layer
                )
                allowed = {ThreadAccess.OWNER, ThreadAccess.MISSING}
                if not require_login():
                    allowed.add(ThreadAccess.OWNERLESS)
                if access not in allowed:
                    logger.error("Authorization for the thread failed.")
                    raise ConnectionRefusedError("authorization failed")

    # Session scoped function to emit to the client
    def emit_fn(event, data):
        return sio.emit(event, data, to=sid)

    # Session scoped function to emit to the client and wait for a response
    def emit_call_fn(event: Literal["ask", "call_fn"], data, timeout):
        return sio.call(event, data, timeout=timeout, to=sid)

    session_id = auth["sessionId"]
    if restore_existing_session(
        sid, session_id, emit_fn, emit_call_fn, environ, user=user
    ):
        return True

    user_env_string = auth.get("userEnv", None)
    user_env = load_user_env(user_env_string)

    client_type = auth["clientType"]
    url_encoded_chat_profile = auth.get("chatProfile", None)
    chat_profile = (
        unquote(url_encoded_chat_profile) if url_encoded_chat_profile else None
    )

    WebsocketSession(
        id=session_id,
        socket_id=sid,
        emit=emit_fn,
        emit_call=emit_call_fn,
        client_type=client_type,
        user_env=user_env,
        user=user,
        token=token,
        chat_profile=chat_profile,
        thread_id=thread_id,
        environ=environ,
    )

    return True


async def _run_in_turn(kind: str, thread_id, coro):
    """Run a callback inside one telemetry turn root."""
    turn = otel.start_turn(kind, thread_id)
    try:
        return await coro
    except asyncio.CancelledError:
        otel.mark_turn_cancelled(turn)
        raise
    except Exception as exc:
        otel.mark_turn_error(turn, type(exc))
        raise
    finally:
        otel.end_turn(turn)


def _start_chat_start(session) -> asyncio.Task:
    task = asyncio.create_task(
        _run_in_turn(
            "chat_start",
            getattr(session, "thread_id", None),
            config.code.on_chat_start(),
        )
    )
    session.current_task = task
    return task


@sio.on("connection_successful")  # pyright: ignore [reportOptionalCall]
async def connection_successful(sid):
    context = init_ws_context(sid)

    await context.emitter.task_end()
    await context.emitter.clear("clear_ask")
    await context.emitter.clear("clear_call_fn")

    async with context.session._thread_resume_lock:
        if context.session.unavailable_chat_profile:
            await context.emitter.emit(
                "resume_thread_unavailable",
                {
                    "thread_id": context.session.thread_id_to_resume,
                    "message": context.session.unavailable_chat_profile,
                },
            )
            return

        if context.session.restored and not context.session.thread_id_to_resume:
            if (
                not context.session.has_first_interaction
                and config.code.on_chat_start
                and not context.session.chat_started
            ):
                context.session.chat_started = True
                _start_chat_start(context.session)
            return

        if context.session.thread_id_to_resume and config.code.on_chat_resume:
            thread = context.session._resumed_thread
            interaction_sid = None
            if thread is None:
                try:
                    thread = await resume_thread(context.session)
                except ChatProfileUnavailableError as e:
                    context.session.unavailable_chat_profile = str(e)
                    await context.emitter.emit(
                        "resume_thread_unavailable",
                        {
                            "thread_id": context.session.thread_id_to_resume,
                            "message": str(e),
                        },
                    )
                    return
                except Exception as e:
                    logger.error(f"Failed to resume thread: {e!s}")
                    await context.emitter.send_resume_thread_error(
                        "Failed to load conversation history."
                    )
                    return
                if thread:
                    context.session.has_first_interaction = True
                    interaction_sid = context.session.socket_id
                    await context.emitter.emit(
                        "first_interaction",
                        {"interaction": "resume", "thread_id": thread.get("id")},
                    )
                    await _run_in_turn(
                        "chat_resume",
                        thread.get("id"),
                        config.code.on_chat_resume(thread),
                    )

                    for step in thread.get("steps", []):
                        try:
                            if "message" in step["type"]:
                                chat_context.add(Message.from_dict(step))
                        except Exception as e:
                            logger.warning(
                                f"Failed to restore step {step.get('id')}: {e!s}"
                            )

                    # Server initialization is complete even if delivery retries.
                    context.session._resumed_thread = thread

            if thread:
                delivery_sid = context.session.socket_id
                if not sio.manager.is_connected(delivery_sid, "/"):
                    return
                if interaction_sid != delivery_sid:
                    await context.emitter.emit(
                        "first_interaction",
                        {"interaction": "resume", "thread_id": thread.get("id")},
                    )
                try:
                    await context.emitter.resume_thread(thread)
                except Exception as e:
                    logger.error(f"Failed to emit resume_thread: {e!s}")
                    await context.emitter.send_resume_thread_error(
                        "Failed to load conversation history."
                    )
                    return
                if (
                    context.session.socket_id != delivery_sid
                    or not sio.manager.is_connected(delivery_sid, "/")
                ):
                    return
                context.session.thread_id_to_resume = None
                context.session._resumed_thread = None
                return
            else:
                await context.emitter.send_resume_thread_error("Thread not found.")
                context.session.thread_id_to_resume = None

        if config.code.on_chat_start and not context.session.chat_started:
            context.session.chat_started = True
            _start_chat_start(context.session)


@sio.on("clear_session")  # pyright: ignore [reportOptionalCall]
async def clean_session(sid):
    session = WebsocketSession.get(sid)
    if session:
        session.to_clear = True


@sio.on("disconnect")  # pyright: ignore [reportOptionalCall]
async def disconnect(sid):
    session = WebsocketSession.get(sid)

    if not session:
        return

    init_ws_context(session)

    async def clear(_sid):
        if session := WebsocketSession.get(_sid):
            # Clean up the user session
            if session.id in user_sessions:
                user_sessions.pop(session.id)
            # Clean up the chat context
            chat_context.delete_session(session.id)
            # Clean up the session
            await session.delete()

    try:
        if config.code.on_chat_end and not session.unavailable_chat_profile:
            await config.code.on_chat_end()

        if (
            session.thread_id
            and session.has_first_interaction
            and not session.unavailable_chat_profile
        ):
            await persist_user_session(session.thread_id, session.to_persistable())
    finally:
        if session.to_clear:
            await clear(sid)
        else:

            async def clear_on_timeout(_sid):
                await asyncio.sleep(config.project.session_timeout)
                await clear(_sid)

            asyncio.ensure_future(clear_on_timeout(sid))


@sio.on("stop")  # pyright: ignore [reportOptionalCall]
async def stop(sid):
    if session := WebsocketSession.get(sid):
        if session.unavailable_chat_profile:
            return
        init_ws_context(session)
        await supersede_thread_approvals(session.thread_id, reason="stopped")
        await Message(content="Task manually stopped.").send()

        # Cancel every run/audio task owned by the session, not only the
        # newest pointer. Tasks waiting on the run lock are cancelled too.
        owned = {
            *getattr(session, "run_tasks", ()),
            *getattr(session, "audio_tasks", ()),
        }
        current = getattr(session, "current_task", None)
        if current is not None:
            owned.add(current)
        for task in owned:
            task.cancel()

        if config.code.on_stop:
            await config.code.on_stop()


def _mark_message_once(session: WebsocketSession, payload: MessagePayload) -> bool:
    """Return False for a replayed v4 message id within this session."""
    raw = payload.get("message", {}).get("id")
    if not isinstance(raw, str) or len(raw) > 128:
        return True
    try:
        parsed = uuid.UUID(raw)
    except (ValueError, TypeError):
        return True
    if parsed.version != 4:
        return True
    seen = getattr(session, "processed_message_ids", None)
    if seen is None:
        seen = set()
        session.processed_message_ids = seen
    message_id = str(parsed)
    if message_id in seen:
        return False
    if len(seen) >= 1000:
        seen.clear()
    seen.add(message_id)
    return True


def _start_run(session: WebsocketSession, coro) -> Optional[asyncio.Task]:
    """Queue one run behind the session lock and track every task."""
    run_tasks = getattr(session, "run_tasks", None)
    if run_tasks is None:
        run_tasks = set()
        session.run_tasks = run_tasks
    if _MAX_PENDING_RUNS > 0 and len(run_tasks) >= _MAX_PENDING_RUNS:
        coro.close()
        return None

    async def _run():
        lock = getattr(session, "run_lock", None)
        if lock is None:
            return await coro
        async with lock:
            return await coro

    task = asyncio.create_task(_run())
    run_tasks.add(task)
    session.current_task = task

    def clear(completed: asyncio.Task) -> None:
        run_tasks.discard(completed)
        if session.current_task is completed:
            session.current_task = None

    task.add_done_callback(clear)
    return task


def _start_audio_task(session: WebsocketSession, coro) -> Optional[asyncio.Task]:
    """Track audio callbacks, serialized in arrival order and bounded."""
    audio_tasks = getattr(session, "audio_tasks", None)
    if audio_tasks is None:
        audio_tasks = set()
        session.audio_tasks = audio_tasks
    if _MAX_AUDIO_TASKS > 0 and len(audio_tasks) >= _MAX_AUDIO_TASKS:
        coro.close()
        return None

    async def _run():
        lock = getattr(session, "audio_lock", None)
        if lock is None:
            return await coro
        async with lock:
            return await coro

    task = asyncio.create_task(_run())
    audio_tasks.add(task)

    def clear(completed: asyncio.Task) -> None:
        audio_tasks.discard(completed)
        if completed.cancelled():
            return
        exc = completed.exception()
        if exc is not None:
            logger.error("Audio chunk callback failed: %s", exc)

    task.add_done_callback(clear)
    return task


async def _refuse_if_approval_pending(session: WebsocketSession, context) -> bool:
    try:
        await guard_pending_approval(session.thread_id)
    except ApprovalPending:
        # The pending card is already visible in the client. Refusing a new run
        # is a no-op for the user; do not add a toast that can repeat on every
        # queued message.
        logger.warning(
            "Refused a new run for thread %s while an approval or question is pending.",
            session.thread_id,
        )
        return True
    return False


async def process_message(session: WebsocketSession, payload: MessagePayload):
    """Process a message from the user."""
    if session.unavailable_chat_profile:
        return
    turn = otel.start_turn("message", getattr(session, "thread_id", None))
    context = None
    try:
        context = init_ws_context(session)
        await context.emitter.task_start()
        message = await context.emitter.process_message(payload)

        if config.code.on_message:
            await asyncio.sleep(0.001)
            await config.code.on_message(message)
    except asyncio.CancelledError:
        otel.mark_turn_cancelled(turn)
    except Exception as e:
        otel.mark_turn_error(turn, type(e))
        logger.exception(e)
        if context is None:
            raise
        await ErrorMessage(
            author="Error", content=str(e) or e.__class__.__name__
        ).send()
    finally:
        try:
            if context is not None:
                await context.emitter.task_end()
        finally:
            otel.end_turn(turn)


async def process_edit_message(session: WebsocketSession, payload: MessagePayload):
    turn = otel.start_turn("edit_message", getattr(session, "thread_id", None))
    try:
        context = init_ws_context(session)

        messages = chat_context.get()

        orig_message = None

        for message in messages:
            if orig_message:
                await message.remove()

            if (
                message.id == payload["message"]["id"]
                and message.type == "user_message"
            ):
                message.content = payload["message"]["output"]
                await message.update()
                await message.remove_children()
                orig_message = message

        await context.emitter.task_start()

        if config.code.on_message:
            try:
                await config.code.on_message(orig_message)
            except asyncio.CancelledError:
                otel.mark_turn_cancelled(turn)
            finally:
                await context.emitter.task_end()
    except asyncio.CancelledError:
        otel.mark_turn_cancelled(turn)
        raise
    except Exception as exc:
        otel.mark_turn_error(turn, type(exc))
        raise
    finally:
        otel.end_turn(turn)


@sio.on("edit_message")  # pyright: ignore [reportOptionalCall]
async def edit_message(sid, payload: MessagePayload):
    """Handle an edited message without blocking the socket loop."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return
    context = init_ws_context(session)
    if await _refuse_if_approval_pending(session, context):
        return
    _start_run(session, process_edit_message(session, payload))


@sio.on("message_favorite")  # pyright: ignore [reportOptionalCall]
async def message_favorite(sid, payload: MessagePayload):
    """Handle a message favorite toggle."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return
    context = init_ws_context(session)
    data_layer = get_data_layer()

    if not config.features.favorites or not session.user:
        return

    payload_message = payload["message"]
    payload_metadata = payload_message.get("metadata") or {}
    favorite = bool(payload_metadata.get("favorite", False))

    step_dict = None

    if favorite:
        for message in chat_context.get():
            if message.id == payload_message["id"]:
                message.metadata = message.metadata or {}
                message.metadata["favorite"] = favorite
                step_dict = message.to_dict()
                break
    elif data_layer:
        favorites = await data_layer.get_favorite_steps(session.user.id)
        for fav in favorites:
            if fav["id"] == payload_message["id"]:
                step_dict = fav
                break

    if step_dict is None:
        logger.error("Could not find step to update favorite status.")
        return

    created_at = step_dict.get("createdAt")
    if created_at and not created_at.endswith("Z"):
        step_dict["createdAt"] = f"{created_at}Z"

    if data_layer:
        step_dict = await data_layer.set_step_favorite(step_dict, favorite)

    await context.emitter.update_step(step_dict)
    await fetch_favorites(sid)


@sio.on("fetch_favorites")  # pyright: ignore [reportOptionalCall]
async def fetch_favorites(sid):
    session = WebsocketSession.require(sid)
    context = init_ws_context(session)
    if session.user and config.features.favorites:
        if data_layer := get_data_layer():
            favorites = await data_layer.get_favorite_steps(session.user.id)
            await context.emitter.set_favorites(favorites)


@sio.on("client_message")  # pyright: ignore [reportOptionalCall]
async def message(sid, payload: MessagePayload):
    """Handle a message sent by the User."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return
    context = init_ws_context(session)
    if await _refuse_if_approval_pending(session, context):
        return
    if not _mark_message_once(session, payload):
        logger.warning("Ignoring replayed client message id")
        return

    task = _start_run(session, process_message(session, payload))
    if task is None:
        await context.emitter.send_toast(
            "Too many messages are queued; wait for the current run.", "error"
        )


@sio.on("window_message")  # pyright: ignore [reportOptionalCall]
async def window_message(sid, data):
    """Handle a message send by the host window."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return
    context = init_ws_context(session)
    if await _refuse_if_approval_pending(session, context):
        return

    if config.code.on_window_message:
        try:
            await config.code.on_window_message(data)
        except asyncio.CancelledError:
            pass


@sio.on("audio_start")  # pyright: ignore [reportOptionalCall]
async def audio_start(sid):
    """Handle audio init."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return

    context = init_ws_context(session)
    config: ChainlitConfig = session.get_config()  # type: ignore

    if config.features.audio and config.features.audio.enabled:
        connected = bool(await config.code.on_audio_start())
        connection_state = "on" if connected else "off"
        await context.emitter.update_audio_connection(connection_state)


@sio.on("audio_chunk")
async def audio_chunk(sid, payload: InputAudioChunkPayload):
    """Handle an audio chunk sent by the user."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return

    init_ws_context(session)

    config: ChainlitConfig = session.get_config()

    if (
        config.features.audio
        and config.features.audio.enabled
        and config.code.on_audio_chunk
    ):
        task = _start_audio_task(
            session, config.code.on_audio_chunk(InputAudioChunk(**payload))
        )
        if task is None:
            logger.warning("Dropping audio chunk: session audio queue is full")


@sio.on("audio_end")
async def audio_end(sid):
    """Handle the end of the audio stream."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return

    context = init_ws_context(session)
    if await _refuse_if_approval_pending(session, context):
        return

    turn = otel.start_turn("audio_end", getattr(session, "thread_id", None))
    try:
        await context.emitter.task_start()

        audio_tasks = getattr(session, "audio_tasks", ())
        if audio_tasks:
            await asyncio.gather(*list(audio_tasks), return_exceptions=True)

        if not session.has_first_interaction:
            session.has_first_interaction = True
            await context.emitter.init_thread("audio")

        config: ChainlitConfig = session.get_config()  # type: ignore

        if config.features.audio and config.features.audio.enabled:
            await config.code.on_audio_end()

    except asyncio.CancelledError:
        otel.mark_turn_cancelled(turn)
    except Exception as e:
        otel.mark_turn_error(turn, type(e))
        logger.exception(e)
        await ErrorMessage(
            author="Error", content=str(e) or e.__class__.__name__
        ).send()
    finally:
        try:
            await context.emitter.task_end()
        finally:
            otel.end_turn(turn)


@sio.on("chat_settings_change")
async def change_settings(sid, settings: Dict[str, Any]):
    """Handle change settings submit from the UI."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return
    context = init_ws_context(session)

    for key, value in settings.items():
        context.session.chat_settings[key] = value

    if config.code.on_settings_update:
        await config.code.on_settings_update(settings)


@sio.on("chat_settings_edit")
async def edit_settings(sid, settings: Dict[str, Any]):
    """Handle change settings edit from the UI (on the fly)."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return
    init_ws_context(session)

    if config.code.on_settings_edit:
        await config.code.on_settings_edit(settings)


@sio.on("mode_change")
async def change_mode(sid, payload: Dict[str, Any]):
    """Validate and apply a mode selection made in the composer."""
    session = WebsocketSession.require(sid)
    if session.unavailable_chat_profile:
        return
    context = init_ws_context(session)

    mode_id = payload.get("modeId") if isinstance(payload, dict) else None
    option_id = payload.get("optionId") if isinstance(payload, dict) else None
    mode = next(
        (candidate for candidate in session.modes if candidate.id == mode_id),
        None,
    )
    option = mode.get_option_by_id(option_id) if mode and option_id else None

    if not mode or not option:
        await context.emitter.emit("mode_change", dict(session.mode_values))
        return

    values = dict(session.mode_values)
    values[mode.id] = option.id
    session.mode_values = values

    from chainlit.user_session import user_session

    user_session.set("modes", values)
    await context.emitter.emit("mode_change", values)

    if mode.persist_to_user:
        await _persist_mode_selection(session, mode.id, option.id)

    if config.code.on_mode_update:
        await config.code.on_mode_update(values)
