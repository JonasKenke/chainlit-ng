import asyncio
import inspect
import json
import time
import uuid
from copy import deepcopy
from functools import wraps
from typing import Callable, Dict, List, Literal, Optional, Type, TypedDict, Union

from pydantic import BaseModel

from chainlit import otel
from chainlit.config import config
from chainlit.context import CL_RUN_NAMES, context, local_steps
from chainlit.data import get_data_layer
from chainlit.element import Element
from chainlit.logger import logger
from chainlit.task_refs import create_persistence_task
from chainlit.types import FeedbackDict
from chainlit.utils import utc_now

TrueStepType = Literal[
    "run", "tool", "llm", "embedding", "retrieval", "rerank", "undefined"
]
MessageStepType = Literal["user_message", "assistant_message", "system_message"]
StepType = Union[TrueStepType, MessageStepType]


def check_add_step_in_cot(step: "Step"):
    is_message = step.type in [
        "user_message",
        "assistant_message",
    ]
    is_cl_run = step.name in CL_RUN_NAMES and step.type == "run"
    if config.ui.cot == "hidden" and not is_message and not is_cl_run:
        return False
    return True


def stub_step(step: "Step") -> "StepDict":
    return {
        "type": step.type,
        "name": step.name,
        "id": step.id,
        "parentId": step.parent_id,
        "threadId": step.thread_id,
        "input": "",
        "output": "",
    }


class StepDict(TypedDict, total=False):
    name: str
    type: StepType
    id: str
    threadId: str
    parentId: str | None
    command: str | None
    modes: Dict[str, str] | None
    streaming: bool
    waitForAnswer: bool | None
    isError: bool | None
    metadata: Dict
    tags: List[str] | None
    input: str
    output: str
    createdAt: str | None
    start: str | None
    end: str | None
    generation: Dict | None
    showInput: Union[bool, str] | None
    defaultOpen: bool | None
    autoCollapse: bool | None
    language: str | None
    icon: str | None
    feedback: FeedbackDict | None


def flatten_args_kwargs(func, args, kwargs):
    signature = inspect.signature(func)
    bound_arguments = signature.bind(*args, **kwargs)
    bound_arguments.apply_defaults()
    return {k: deepcopy(v) for k, v in bound_arguments.arguments.items()}


def _approval_arguments(func, args, kwargs) -> Dict:
    """Snapshot JSON-only arguments, excluding self/cls.

    The snapshot is normalized to plain JSON values, so later copies are
    identical. The tool executes this snapshot, not the caller's objects.
    """
    from chainlit.approval import ApprovalInvalidArguments, _canonical_json

    signature = inspect.signature(func)
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    parameters = list(signature.parameters.values())
    drop_first = bool(parameters) and parameters[0].name in ("self", "cls")
    arguments: Dict = {}
    for index, (key, value) in enumerate(bound.arguments.items()):
        if index == 0 and drop_first:
            continue
        try:
            arguments[key] = json.loads(_canonical_json(value))
        except ApprovalInvalidArguments as exc:
            raise ApprovalInvalidArguments(
                "approval arguments must be JSON-compatible",
                field=key,
                error_type="json_invalid",
            ) from exc
    return arguments


def _approval_call_arguments(func, args, kwargs, arguments: Dict, *, edited: bool):
    """Overlay the approved snapshot onto the original call."""
    from chainlit.approval import ApprovalInvalidArguments

    bound = inspect.signature(func).bind(*args, **kwargs)
    bound.apply_defaults()
    for key, value in arguments.items():
        if key not in bound.arguments:
            raise ApprovalInvalidArguments(
                "approved arguments do not match the function signature",
                field=f"editedArguments.{key}" if edited else key,
                error_type="unknown_field",
            )
        bound.arguments[key] = value
    return bound.args, bound.kwargs


def _approval_preview_input(name: str, arguments: Dict) -> Dict:
    from chainlit.approval import _arguments_preview

    return {
        "approval": {
            "tool": name,
            "argumentsPreview": _arguments_preview(arguments),
            "redacted": True,
        }
    }


def step(
    original_function: Callable | None = None,
    *,
    name: str | None = "",
    type: TrueStepType = "undefined",
    id: str | None = None,
    parent_id: str | None = None,
    thread_id: str | None = None,
    tags: List[str] | None = None,
    metadata: Dict | None = None,
    language: str | None = None,
    icon: str | None = None,
    show_input: Union[bool, str] = "json",
    default_open: bool = False,
    auto_collapse: bool = False,
    requires_approval: bool = False,
    approval_schema: Optional[Type[BaseModel]] = None,
    approval_content: Optional[str] = None,
    allow_edit: Optional[bool] = None,
    allow_request_changes: Optional[bool] = None,
    allow_json_edit: Optional[bool] = None,
):
    """Step decorator for async and sync functions."""

    def wrapper(func: Callable):
        nonlocal name
        if not name:
            name = func.__name__

        if requires_approval and not inspect.iscoroutinefunction(func):
            from chainlit.approval import ApprovalUnsupported

            raise ApprovalUnsupported(
                "requires_approval is only supported on async functions"
            )

        # Handle async decorator

        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                approval_arguments: Dict = {}
                if requires_approval:
                    # Snapshot and validate before creating a step or a card.
                    approval_arguments = _approval_arguments(func, args, kwargs)

                step_instance = Step(
                    type=type,
                    name=name,
                    id=id,
                    parent_id=parent_id,
                    thread_id=thread_id,
                    tags=tags,
                    language=language,
                    icon=icon,
                    show_input=show_input,
                    default_open=default_open,
                    auto_collapse=auto_collapse,
                    metadata=metadata,
                    requires_approval=requires_approval,
                    approval_schema=approval_schema,
                    approval_content=approval_content,
                )
                if requires_approval:
                    step_instance.input = _approval_preview_input(
                        step_instance.name, approval_arguments
                    )
                    # The decorator alone may satisfy the manual-flag guard.
                    step_instance._approval_decorated = True
                    step_instance._approval_managed = True

                async with step_instance as step:
                    if requires_approval:
                        from chainlit.approval import Approval

                        decision = await Approval(
                            step=step,
                            arguments=approval_arguments,
                            content=approval_content,
                            arguments_schema=approval_schema,
                            allow_edit=allow_edit,
                            allow_request_changes=allow_request_changes,
                            allow_json_edit=allow_json_edit,
                        ).ask()
                        if not decision.approved:
                            step.is_error = True
                            label = {
                                "reject": "rejected",
                                "request_changes": "changes_requested",
                            }.get(decision.decision, decision.decision)
                            # The comment is untrusted user input and stays
                            # owner-only in metadata.approval.reason.
                            step.output = f"Approval {label}"
                            return None
                        call_args, call_kwargs = _approval_call_arguments(
                            func,
                            args,
                            kwargs,
                            decision.arguments,
                            edited=decision.decision == "edit",
                        )
                        result = await func(*call_args, **call_kwargs)
                    else:
                        try:
                            step.input = flatten_args_kwargs(func, args, kwargs)
                        except Exception as e:
                            logger.exception(e)
                        result = await func(*args, **kwargs)
                    try:
                        if result is not None and not step.output:
                            step.output = result
                    except Exception as e:
                        step.is_error = True
                        step.output = str(e)
                    return result

            return async_wrapper
        else:
            # Handle sync decorator
            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                with Step(
                    type=type,
                    name=name,
                    id=id,
                    parent_id=parent_id,
                    thread_id=thread_id,
                    tags=tags,
                    language=language,
                    icon=icon,
                    show_input=show_input,
                    default_open=default_open,
                    auto_collapse=auto_collapse,
                    metadata=metadata,
                ) as step:
                    try:
                        step.input = flatten_args_kwargs(func, args, kwargs)
                    except Exception as e:
                        logger.exception(e)
                    result = func(*args, **kwargs)
                    try:
                        if result is not None and not step.output:
                            step.output = result
                    except Exception as e:
                        step.is_error = True
                        step.output = str(e)
                    return result

            return sync_wrapper

    func = original_function
    if not func:
        return wrapper
    else:
        return wrapper(func)


class Step:
    # Constructor
    name: str
    type: TrueStepType
    id: str
    parent_id: str | None

    streaming: bool
    persisted: bool

    show_input: Union[bool, str]

    is_error: bool | None
    metadata: Dict
    tags: List[str] | None
    thread_id: str
    created_at: Union[str, None]
    start: Union[str, None]
    end: Union[str, None]
    generation: Dict | None
    language: str | None
    icon: str | None
    default_open: bool | None
    auto_collapse: bool | None
    elements: List[Element] | None
    fail_on_persist_error: bool

    def __init__(
        self,
        name: str | None = config.ui.name,
        type: TrueStepType = "undefined",
        id: str | None = None,
        parent_id: str | None = None,
        elements: List[Element] | None = None,
        metadata: Dict | None = None,
        tags: List[str] | None = None,
        language: str | None = None,
        icon: str | None = None,
        default_open: bool | None = False,
        auto_collapse: bool | None = False,
        show_input: Union[bool, str] = "json",
        thread_id: str | None = None,
        requires_approval: bool = False,
        approval_schema: Optional[Type[BaseModel]] = None,
        approval_content: Optional[str] = None,
    ):
        if metadata and "approval" in metadata:
            raise ValueError("metadata['approval'] is reserved")

        time.sleep(0.001)
        self._input = ""
        self._output = ""
        self._persistence_tasks: set[asyncio.Task] = set()
        self._last_persistence_task: Optional[asyncio.Task] = None
        self._approval_managed = False
        self._approval_decorated = False
        self.requires_approval = requires_approval
        self.approval_schema = approval_schema
        self.approval_content = approval_content
        self.thread_id = thread_id or context.session.thread_id
        self.name = name or ""
        self.type = type
        self.id = id or str(uuid.uuid4())
        self.metadata = metadata or {}
        self.tags = tags
        self.is_error = False
        self.show_input = show_input
        self.parent_id = parent_id

        self.language = language
        self.icon = icon
        self.default_open = default_open
        self.auto_collapse = auto_collapse
        self.generation = None
        self.elements = elements or []

        self.created_at = utc_now()
        self.start = None
        self.end = None

        self.streaming = False
        self.persisted = False
        self.fail_on_persist_error = False

    def _clean_content(self, content):
        """
        Recursively checks and converts bytes objects in content.
        """

        def handle_bytes(item):
            if isinstance(item, bytes):
                return "STRIPPED_BINARY_DATA"
            elif isinstance(item, dict):
                return {k: handle_bytes(v) for k, v in item.items()}
            elif isinstance(item, list):
                return [handle_bytes(i) for i in item]
            elif isinstance(item, tuple):
                return tuple(handle_bytes(i) for i in item)
            return item

        return handle_bytes(content)

    def _process_content(self, content, set_language=False):
        if content is None:
            return ""
        content = self._clean_content(content)

        if (
            isinstance(content, dict)
            or isinstance(content, list)
            or isinstance(content, tuple)
        ):
            try:
                processed_content = json.dumps(content, indent=4, ensure_ascii=False)
                if set_language:
                    self.language = "json"
            except TypeError:
                processed_content = str(content).replace("\\n", "\n")
                if set_language:
                    self.language = "text"
        elif isinstance(content, str):
            processed_content = content
        else:
            processed_content = str(content).replace("\\n", "\n")
            if set_language:
                self.language = "text"
        return processed_content

    def _track_persistence(self, coro) -> asyncio.Task:
        previous = self._last_persistence_task

        async def chained():
            if previous is not None:
                await asyncio.gather(previous, return_exceptions=True)
            return await coro

        task = create_persistence_task(chained())
        self._last_persistence_task = task
        self._persistence_tasks.add(task)
        task.add_done_callback(self._persistence_tasks.discard)
        return task

    def _approval_persistence_suppressed(self) -> bool:
        from chainlit.approval import is_thread_deleted

        return is_thread_deleted(self.thread_id)

    async def flush_persistence(self) -> None:
        """Await every write scheduled for this step so far."""
        while self._persistence_tasks:
            await asyncio.gather(*list(self._persistence_tasks), return_exceptions=True)
            await asyncio.sleep(0)

    @property
    def input(self):
        return self._input

    @input.setter
    def input(self, content: Union[Dict, str]):
        self._input = self._process_content(content, set_language=False)

    @property
    def output(self):
        return self._output

    @output.setter
    def output(self, content: Union[Dict, str]):
        self._output = self._process_content(content, set_language=True)

    def to_dict(self) -> StepDict:
        # Move icon into metadata for storage
        metadata = dict(self.metadata)
        if self.icon:
            metadata["icon"] = self.icon

        generation = self.generation
        to_dict = getattr(generation, "to_dict", None)
        if callable(to_dict):
            generation = to_dict()

        _dict: StepDict = {
            "name": self.name,
            "type": self.type,
            "id": self.id,
            "threadId": self.thread_id,
            "parentId": self.parent_id,
            "streaming": self.streaming,
            "metadata": metadata,
            "tags": self.tags,
            "input": self.input,
            "isError": self.is_error,
            "output": self.output,
            "createdAt": self.created_at,
            "start": self.start,
            "end": self.end,
            "language": self.language,
            "defaultOpen": self.default_open,
            "autoCollapse": self.auto_collapse,
            "showInput": self.show_input,
            "generation": generation,
        }
        return _dict

    async def update(self):
        """
        Update a step already sent to the UI.
        """
        if self.streaming:
            self.streaming = False

        step_dict = self.to_dict()
        data_layer = get_data_layer()

        if data_layer and not self._approval_persistence_suppressed():
            try:
                if self._approval_managed:
                    from chainlit.approval import is_thread_deleted, step_lock

                    async with step_lock(self.thread_id, self.id):
                        if not is_thread_deleted(self.thread_id):
                            step_dict = self.to_dict()
                            await data_layer.update_step(step_dict.copy())
                else:
                    self._track_persistence(data_layer.update_step(step_dict.copy()))
            except Exception as e:
                if self.fail_on_persist_error:
                    raise e
                logger.error(f"Failed to persist step update: {e!s}")

        tasks = [el.send(for_id=self.id) for el in self.elements]
        await asyncio.gather(*tasks)

        if not check_add_step_in_cot(self):
            await context.emitter.update_step(stub_step(self))
        else:
            await context.emitter.update_step(step_dict)

        return True

    async def remove(self):
        """
        Remove a step already sent to the UI.
        """
        step_dict = self.to_dict()
        data_layer = get_data_layer()

        if data_layer:
            try:
                if self._approval_managed:
                    from chainlit.approval import step_lock

                    async with step_lock(self.thread_id, self.id):
                        await data_layer.delete_step(self.id)
                else:
                    self._track_persistence(data_layer.delete_step(self.id))
            except Exception as e:
                if self.fail_on_persist_error:
                    raise e
                logger.error(f"Failed to persist step deletion: {e!s}")

        await context.emitter.delete_step(step_dict)

        return True

    async def send(self):
        if self.persisted:
            return self

        if config.code.author_rename:
            self.name = await config.code.author_rename(self.name)

        if self.streaming:
            self.streaming = False

        step_dict = self.to_dict()

        data_layer = get_data_layer()

        if data_layer and not self._approval_persistence_suppressed():
            try:
                if self._approval_managed:
                    from chainlit.approval import is_thread_deleted, step_lock

                    async with step_lock(self.thread_id, self.id):
                        if not is_thread_deleted(self.thread_id):
                            await data_layer.create_step(step_dict.copy())
                            self.persisted = True
                else:
                    task = self._track_persistence(
                        data_layer.create_step(step_dict.copy())
                    )

                    def mark_persisted(completed: asyncio.Task) -> None:
                        if not completed.cancelled() and completed.exception() is None:
                            self.persisted = True

                    task.add_done_callback(mark_persisted)
            except Exception as e:
                if self.fail_on_persist_error:
                    raise e
                logger.error(f"Failed to persist step creation: {e!s}")

        tasks = [el.send(for_id=self.id) for el in self.elements]
        await asyncio.gather(*tasks)

        if not check_add_step_in_cot(self):
            await context.emitter.send_step(stub_step(self))
        else:
            await context.emitter.send_step(step_dict)

        return self

    async def stream_token(self, token: str, is_sequence=False, is_input=False):
        """
        Sends a token to the UI.
        Once all tokens have been streamed, call .send() to end the stream and persist the step if persistence is enabled.
        """
        if not token:
            return

        if is_sequence:
            if is_input:
                self.input = token
            else:
                self.output = token
        else:
            if is_input:
                self.input += token
            else:
                self.output += token

        assert self.id

        if not check_add_step_in_cot(self):
            await context.emitter.send_step(stub_step(self))
            return

        if not self.streaming:
            self.streaming = True
            step_dict = self.to_dict()
            await context.emitter.stream_start(step_dict)
        else:
            await context.emitter.send_token(
                id=self.id, token=token, is_sequence=is_sequence, is_input=is_input
            )

    # Handle parameter less decorator
    def __call__(self, func):
        return step(
            original_function=func,
            type=self.type,
            name=self.name,
            id=self.id,
            parent_id=self.parent_id,
            thread_id=self.thread_id,
            requires_approval=self.requires_approval,
            approval_schema=self.approval_schema,
            approval_content=self.approval_content,
        )

    def _check_manual_approval(self) -> None:
        if self.requires_approval and not self._approval_decorated:
            from chainlit.approval import ApprovalUnsupported

            raise ApprovalUnsupported(
                "Step(requires_approval=True) is only supported through @step"
            )

    # Handle Context Manager Protocol
    async def __aenter__(self):
        self._check_manual_approval()
        self.start = utc_now()
        previous_steps = local_steps.get() or []
        parent_step = previous_steps[-1] if previous_steps else None

        if not self.parent_id:
            if parent_step:
                self.parent_id = parent_step.id
        local_steps.set(previous_steps + [self])
        otel.start_step(self)
        await self.send()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.end = utc_now()

        if exc_type:
            self.output = str(exc_val)
            self.is_error = True

        current_steps = local_steps.get()
        if current_steps and self in current_steps:
            current_steps.remove(self)
            local_steps.set(current_steps)

        otel.end_step(self, exc_type)
        await self.update()

    def __enter__(self):
        self._check_manual_approval()
        self.start = utc_now()

        previous_steps = local_steps.get() or []
        parent_step = previous_steps[-1] if previous_steps else None

        if not self.parent_id:
            if parent_step:
                self.parent_id = parent_step.id
        local_steps.set(previous_steps + [self])

        otel.start_step(self)
        create_persistence_task(self.send())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end = utc_now()

        if exc_type:
            self.output = str(exc_val)
            self.is_error = True

        current_steps = local_steps.get()
        if current_steps and self in current_steps:
            current_steps.remove(self)
            local_steps.set(current_steps)

        otel.end_step(self, exc_type)
        create_persistence_task(self.update())
