from __future__ import annotations

import asyncio
import re
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Literal, Mapping, Optional, Sequence, Union, cast
from urllib.parse import urlparse

from chainlit.approval import (
    APPROVAL_METADATA_KEY,
    ApprovalPersistenceError,
    ApprovalUnsupported,
    QuestionAlreadyAsked,
    QuestionInvalid,
    QuestionStatus,
    _ask,
    _commit_reservation,
    _contains_forbidden_text_char,
    _emit,
    _Entry,
    _has_data_layer,
    _interaction_session,
    _iso,
    _pending_by_approval,
    _pending_by_step,
    _release_reservation,
    _release_shared_capacity,
    _remember_managed_thread,
    _reserve_capacity,
    _start_shared_pending,
    _stop_shared_pending,
    _store_managed_thread,
    _transition_locked,
    _write,
    step_lock,
)
from chainlit.config import config
from chainlit.context import context
from chainlit.step import Step
from chainlit.user import User

_MAX_QUESTION_PROMPT = 2000
_MAX_QUESTION_ANSWER = 2000
_MAX_QUESTION_OPTIONS = 8
_MAX_QUESTIONS = 10
_MAX_OPTION_LABEL = 200
_MAX_OPTION_DESCRIPTION = 500
_MAX_QUESTION_PLACEHOLDER = 200
_OPTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TEL_PATTERN = re.compile(r"^[0-9+().\-\s]{3,32}$")
_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_QUESTION_TYPES = frozenset({"text", "email", "date", "number", "url", "tel", "choice"})
_QUESTION_SPEC_KEYS = frozenset(
    {
        "id",
        "prompt",
        "type",
        "multiple",
        "options",
        "allowFreeText",
        "allow_free_text",
        "default",
        "required",
        "placeholder",
        "multiline",
    }
)

QuestionType = Literal["text", "email", "date", "number", "url", "tel", "choice"]


@dataclass(frozen=True)
class QuestionOption:
    id: str
    label: str
    description: Optional[str] = None


@dataclass(frozen=True)
class Question:
    """One field in a (possibly multi-question) ask.

    ``type`` selects the input control and the server-side validation that
    runs in addition to the client-side one. ``options`` turns the field into
    a choice list; ``allow_free_text`` keeps the "Other" textarea for a choice.
    ``default`` is an option id for a choice and a typed value otherwise.
    """

    prompt: str
    id: Optional[str] = None
    type: QuestionType = "text"
    multiple: bool = False
    options: Sequence[QuestionOption] = ()
    allow_free_text: bool = True
    default: Optional[Union[str, Sequence[str]]] = None
    required: bool = True
    placeholder: Optional[str] = None
    multiline: bool = True


@dataclass(frozen=True)
class QuestionFieldAnswer:
    question_id: str
    option_id: Optional[str] = None
    label: Optional[str] = None
    text: Optional[str] = None
    option_ids: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class QuestionAnswer:
    question_id: str
    thread_id: str
    step_id: str
    status: QuestionStatus
    option_id: Optional[str]
    label: Optional[str]
    text: Optional[str]
    user: Optional[User]
    answered_at: str
    durable: bool
    answers: Mapping[str, QuestionFieldAnswer] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.status == "answered"

    def as_tool_result(self) -> Dict[str, Any]:
        """Provenance-marked payload; the answer is untrusted user input."""
        result: Dict[str, Any] = {
            "provenance": "chainlit.question_answer",
            "trust": "untrusted_user_input",
            "questionId": self.question_id,
            "status": self.status,
            "optionId": self.option_id,
            "text": self.text,
            "durable": self.durable,
        }
        if self.answers:
            result["answers"] = []
            for answer in self.answers.values():
                item: Dict[str, Any] = {
                    "questionId": answer.question_id,
                    "optionId": answer.option_id,
                    "text": answer.text,
                }
                if answer.option_ids:
                    item["optionIds"] = list(answer.option_ids)
                    item["labels"] = list(answer.labels)
                result["answers"].append(item)
        return result


def _question_payload(record: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "kind": "question",
        "id": record["id"],
        "stepId": record["stepId"],
        "threadId": record["threadId"],
        "status": record["status"],
        "prompt": record.get("prompt"),
        "options": deepcopy(record.get("options") or []),
        "allowFreeText": bool(record.get("allowFreeText")),
        "questions": deepcopy(record.get("questions") or []),
        "structured": bool(record.get("structured")),
        "multi": bool(record.get("multi")),
        "requestedAt": record.get("requestedAt"),
        "expiresAt": record.get("expiresAt"),
        "revision": record.get("revision"),
        "decidedBy": record.get("decidedBy"),
        "decidedAt": record.get("decidedAt"),
        "answer": deepcopy(record.get("answer")),
        "terminalReason": record.get("terminalReason"),
        "terminalAt": record.get("terminalAt"),
    }


def _normalize_question_text(value: Any) -> str:
    if not isinstance(value, str):
        raise QuestionInvalid("answer text must be a string")
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text or len(text) > _MAX_QUESTION_ANSWER:
        raise QuestionInvalid("answer text must be 1..2000 characters")
    if _contains_forbidden_text_char(text):
        raise QuestionInvalid("answer text contains control characters")
    return text


def _normalize_field_answer(
    spec: Mapping[str, Any], answer: Mapping[str, Any]
) -> Dict[str, Any]:
    unknown = set(answer) - {"questionId", "optionId", "optionIds", "text"}
    if unknown:
        raise QuestionInvalid("unknown answer field")
    option_id = answer.get("optionId")
    option_ids = answer.get("optionIds")
    text = answer.get("text")
    question_id = spec["id"]

    if spec["type"] == "choice" and spec.get("multiple"):
        if option_id is not None:
            raise QuestionInvalid("this question accepts optionIds, not optionId")
        if option_ids is None and text is None:
            raise QuestionInvalid("select at least one option or type an answer")
        selected_ids: list[str] = []
        labels: list[str] = []
        if option_ids is not None:
            if isinstance(option_ids, (str, bytes)) or not isinstance(
                option_ids, Sequence
            ):
                raise QuestionInvalid("optionIds must be a sequence")
            for candidate in option_ids:
                if not isinstance(candidate, str):
                    raise QuestionInvalid("invalid option id")
                match = next(
                    (option for option in spec["options"] if option["id"] == candidate),
                    None,
                )
                if match is None:
                    raise QuestionInvalid("unknown option")
                if match["id"] in selected_ids:
                    raise QuestionInvalid("duplicate option id")
                selected_ids.append(match["id"])
                labels.append(match["label"])
        result: Dict[str, Any] = {"questionId": question_id}
        if selected_ids:
            result["optionIds"] = selected_ids
            result["labels"] = labels
        if text is not None:
            if not spec["allowFreeText"]:
                raise QuestionInvalid("free text is not allowed for this question")
            result["text"] = _normalize_question_text(text)
        if not selected_ids and "text" not in result:
            raise QuestionInvalid("select at least one option or type an answer")
        return result

    if option_ids is not None:
        raise QuestionInvalid("this question accepts a single option")
    if (option_id is None) == (text is None):
        raise QuestionInvalid("exactly one of optionId or text is required")
    if spec["type"] == "choice":
        if option_id is not None:
            match = next(
                (option for option in spec["options"] if option["id"] == option_id),
                None,
            )
            if match is None:
                raise QuestionInvalid("unknown option")
            return {
                "questionId": question_id,
                "optionId": match["id"],
                "label": match["label"],
            }
        if not spec["allowFreeText"]:
            raise QuestionInvalid("free text is not allowed for this question")
        return {
            "questionId": question_id,
            "text": _normalize_question_text(text),
        }
    if option_id is not None:
        raise QuestionInvalid("this question does not accept options")
    normalized = _normalize_question_text(text)
    _validate_typed_text(spec["type"], normalized)
    return {"questionId": question_id, "text": normalized}


def _normalize_answers(
    questions: Sequence[Mapping[str, Any]], answers: Any
) -> Dict[str, Any]:
    if isinstance(answers, (str, bytes)) or not isinstance(answers, Sequence):
        raise QuestionInvalid("answers must be a sequence")
    if not answers:
        if any(spec.get("required", True) for spec in questions):
            raise QuestionInvalid("an answer is required")
        return {"answers": []}
    if len(answers) > len(questions):
        raise QuestionInvalid("too many answers")
    specs = {spec["id"]: spec for spec in questions}
    seen: set[str] = set()
    normalized: Dict[str, Dict[str, Any]] = {}
    for item in answers:
        if not isinstance(item, Mapping):
            raise QuestionInvalid("answers must be objects")
        question_id = item.get("questionId")
        if not isinstance(question_id, str) or question_id not in specs:
            raise QuestionInvalid("unknown question id")
        if question_id in seen:
            raise QuestionInvalid("duplicate answer")
        seen.add(question_id)
        normalized[question_id] = _normalize_field_answer(specs[question_id], item)
    for spec in questions:
        if spec.get("required", True) and spec["id"] not in seen:
            raise QuestionInvalid("required answers are missing")
    return {
        "answers": [
            normalized[spec["id"]] for spec in questions if spec["id"] in normalized
        ]
    }


def _normalize_prompt(value: Any) -> str:
    if not isinstance(value, str):
        raise QuestionInvalid("question must be a string")
    prompt = value.strip()
    if not prompt or len(prompt) > _MAX_QUESTION_PROMPT:
        raise QuestionInvalid("question must be 1..2000 characters")
    return prompt


def _normalize_options(options: Any) -> list[Dict[str, Any]]:
    if options is None:
        options = ()
    if isinstance(options, (str, bytes)) or not isinstance(options, Sequence):
        raise QuestionInvalid("options must be a sequence")
    if len(options) > _MAX_QUESTION_OPTIONS:
        raise QuestionInvalid("at most 8 options are allowed")

    normalized: list[Dict[str, Any]] = []
    seen: set[str] = set()
    option_id: Any
    label: Any
    description: Any
    for option in options:
        if isinstance(option, QuestionOption):
            option_id, label, description = (
                option.id,
                option.label,
                option.description,
            )
        elif isinstance(option, Mapping):
            option_id = option.get("id")
            label = option.get("label")
            description = option.get("description")
        else:
            raise QuestionInvalid("options must be QuestionOption values")
        if not isinstance(option_id, str) or not _OPTION_ID_PATTERN.match(option_id):
            raise QuestionInvalid("invalid option id")
        if option_id in seen:
            raise QuestionInvalid("duplicate option id")
        seen.add(option_id)
        if not isinstance(label, str) or not label.strip():
            raise QuestionInvalid("option label is required")
        label = label.strip()
        if len(label) > _MAX_OPTION_LABEL:
            raise QuestionInvalid("option label is too long")
        if description is not None:
            if not isinstance(description, str):
                raise QuestionInvalid("option description must be a string")
            description = description.strip()
            if len(description) > _MAX_OPTION_DESCRIPTION:
                raise QuestionInvalid("option description is too long")
        normalized.append({"id": option_id, "label": label, "description": description})
    return normalized


def _validate_typed_text(question_type: str, text: str) -> None:
    """Validate a user/default value against the field type.

    The value is never echoed in the error: the client receives the fixed
    ``QuestionInvalid`` code only.
    """
    if question_type == "text":
        return
    if question_type == "email":
        if len(text) > 254 or not _EMAIL_PATTERN.match(text):
            raise QuestionInvalid("invalid email address")
        return
    if question_type == "date":
        if not _DATE_PATTERN.match(text):
            raise QuestionInvalid("date must be YYYY-MM-DD")
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise QuestionInvalid("invalid date") from exc
        return
    if question_type == "number":
        if len(text) > 100 or not _NUMBER_PATTERN.match(text):
            raise QuestionInvalid("invalid number")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise QuestionInvalid("invalid number") from exc
        if not value.is_finite():
            raise QuestionInvalid("invalid number")
        return
    if question_type == "url":
        if len(text) > 2000 or any(char.isspace() for char in text):
            raise QuestionInvalid("invalid URL")
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise QuestionInvalid("URL must be http or https")
        return
    if question_type == "tel":
        if not _TEL_PATTERN.match(text):
            raise QuestionInvalid("invalid phone number")
        return
    raise QuestionInvalid("unsupported question type")


def _normalize_default(
    spec: Mapping[str, Any], value: Any
) -> Optional[Union[str, list[str]]]:
    if value is None:
        return None
    if spec["type"] == "choice" and spec.get("multiple"):
        if isinstance(value, (str, bytes)):
            values: Sequence[Any] = [value]
        elif isinstance(value, Sequence):
            values = value
        else:
            raise QuestionInvalid("default must be a string or a list of strings")
        if not values:
            return []
        selected: list[str] = []
        for candidate in values:
            if not isinstance(candidate, str):
                raise QuestionInvalid("default must be a string or a list of strings")
            match = next(
                (option for option in spec["options"] if option["id"] == candidate),
                None,
            )
            if match is None:
                raise QuestionInvalid("default must be a known option id")
            if match["id"] in selected:
                raise QuestionInvalid("duplicate default option")
            selected.append(match["id"])
        return selected
    if not isinstance(value, str):
        raise QuestionInvalid("default must be a string")
    if not value.strip():
        return None
    if spec["type"] == "choice":
        match = next(
            (option for option in spec["options"] if option["id"] == value),
            None,
        )
        if match is None:
            raise QuestionInvalid("default must be a known option id")
        return match["id"]
    text = _normalize_question_text(value)
    _validate_typed_text(spec["type"], text)
    return text


def _normalized_question_spec(item: Any, index: int) -> Dict[str, Any]:
    prompt: Any
    question_id: Any
    question_type: Any
    multiple: Any
    options: Any
    allow_free_text: Any
    default: Any
    required: Any
    placeholder: Any
    multiline: Any
    if isinstance(item, Question):
        prompt = item.prompt
        question_id = item.id
        question_type = item.type
        multiple = item.multiple
        options = item.options
        allow_free_text = item.allow_free_text
        default = item.default
        required = item.required
        placeholder = item.placeholder
        multiline = item.multiline
    elif isinstance(item, Mapping):
        unknown = set(item) - _QUESTION_SPEC_KEYS
        if unknown:
            raise QuestionInvalid("unknown question field")
        prompt = item.get("prompt")
        question_id = item.get("id")
        question_type = item.get("type")
        multiple = item.get("multiple", False)
        options = item.get("options") or ()
        allow_free_text = item.get("allowFreeText", item.get("allow_free_text", True))
        default = item.get("default")
        required = item.get("required", True)
        placeholder = item.get("placeholder")
        multiline = item.get("multiline", True)
    else:
        raise QuestionInvalid("questions must be Question values")

    prompt = _normalize_prompt(prompt)
    if question_id is None:
        question_id = f"q{index + 1}"
    if not isinstance(question_id, str) or not _OPTION_ID_PATTERN.match(question_id):
        raise QuestionInvalid("invalid question id")
    if not isinstance(multiple, bool):
        raise QuestionInvalid("multiple must be a boolean")
    if not isinstance(allow_free_text, bool):
        raise QuestionInvalid("allow_free_text must be a boolean")
    if not isinstance(required, bool):
        raise QuestionInvalid("required must be a boolean")
    if not isinstance(multiline, bool):
        raise QuestionInvalid("multiline must be a boolean")

    normalized_options = _normalize_options(options)
    if question_type is None:
        question_type = "choice" if normalized_options else "text"
    if not isinstance(question_type, str) or question_type not in _QUESTION_TYPES:
        raise QuestionInvalid("unsupported question type")
    if question_type == "choice":
        if not normalized_options:
            raise QuestionInvalid("a choice question needs at least one option")
    elif multiple:
        raise QuestionInvalid("multiple is only supported for choice questions")
    elif normalized_options:
        raise QuestionInvalid("options require type='choice'")
    else:
        # A typed field is free text by definition; the flag only controls the
        # "Other" textarea of a choice.
        allow_free_text = True

    if placeholder is not None:
        if not isinstance(placeholder, str):
            raise QuestionInvalid("placeholder must be a string")
        placeholder = placeholder.strip() or None
        if placeholder is not None and len(placeholder) > _MAX_QUESTION_PLACEHOLDER:
            raise QuestionInvalid("placeholder is too long")

    spec: Dict[str, Any] = {
        "id": question_id,
        "prompt": prompt,
        "type": question_type,
        "multiple": multiple,
        "options": normalized_options,
        "allowFreeText": allow_free_text,
        "default": None,
        "required": required,
        "placeholder": placeholder,
        "multiline": multiline,
    }
    spec["default"] = _normalize_default(spec, default)
    return spec


def _validated_question_payload(
    question: str,
    options: Sequence[QuestionOption],
    allow_free_text: bool,
) -> Dict[str, Any]:
    """Legacy single-question form: one prompt, optional options, free text."""
    prompt = _normalize_prompt(question)
    if not isinstance(allow_free_text, bool):
        raise QuestionInvalid("allow_free_text must be a boolean")
    normalized = _normalize_options(options)
    if not allow_free_text and not normalized:
        raise QuestionInvalid("a closed question needs at least one option")
    spec = {
        "id": "q1",
        "prompt": prompt,
        "type": "choice" if normalized else "text",
        "multiple": False,
        "options": normalized,
        "allowFreeText": allow_free_text,
        "default": None,
        "required": True,
        "placeholder": None,
        "multiline": True,
    }
    return {
        "prompt": prompt,
        "options": normalized,
        "allowFreeText": allow_free_text,
        "questions": [spec],
        "structured": False,
        "multi": False,
    }


def _validated_questions_payload(questions: Any) -> Dict[str, Any]:
    if isinstance(questions, (str, bytes)) or not isinstance(questions, Sequence):
        raise QuestionInvalid("questions must be a sequence")
    if not 1 <= len(questions) <= _MAX_QUESTIONS:
        raise QuestionInvalid("questions must contain 1..10 entries")
    specs = [
        _normalized_question_spec(item, index) for index, item in enumerate(questions)
    ]
    ids = [spec["id"] for spec in specs]
    if len(set(ids)) != len(ids):
        raise QuestionInvalid("duplicate question id")
    return {
        "prompt": specs[0]["prompt"],
        "options": [],
        "allowFreeText": False,
        "questions": specs,
        "structured": True,
        "multi": len(specs) > 1,
    }


def _question_answer_from_record(
    record: Mapping[str, Any], user: Optional[User], durable: bool
) -> QuestionAnswer:
    answer = record.get("answer") or {}
    status = cast(QuestionStatus, record["status"])
    field_answers: Dict[str, QuestionFieldAnswer] = {}
    raw_answers = answer.get("answers")
    if isinstance(raw_answers, list):
        for item in raw_answers:
            if not isinstance(item, Mapping):
                continue
            question_id = item.get("questionId")
            if not isinstance(question_id, str):
                continue
            field_answers[question_id] = QuestionFieldAnswer(
                question_id=question_id,
                option_id=item.get("optionId"),
                label=item.get("label"),
                text=item.get("text"),
                option_ids=tuple(
                    value
                    for value in (item.get("optionIds") or ())
                    if isinstance(value, str)
                ),
                labels=tuple(
                    value
                    for value in (item.get("labels") or ())
                    if isinstance(value, str)
                ),
            )
    option_id = answer.get("optionId")
    label = answer.get("label")
    text = answer.get("text")
    if len(field_answers) == 1 and option_id is None and text is None:
        only = next(iter(field_answers.values()))
        if not only.option_ids:
            option_id, label, text = only.option_id, only.label, only.text
    return QuestionAnswer(
        question_id=record["id"],
        thread_id=record["threadId"],
        step_id=record["stepId"],
        status=status,
        option_id=option_id,
        label=label,
        text=text,
        user=user if status == "answered" else None,
        answered_at=record.get("decidedAt") or record.get("terminalAt") or "",
        durable=durable and status == "answered",
        answers=field_answers,
    )


async def ask(
    question: Optional[str] = None,
    *,
    questions: Sequence[Union[Question, Mapping[str, Any]]] = (),
    options: Sequence[QuestionOption] = (),
    allow_free_text: bool = True,
    timeout: Optional[float] = None,
    step: Optional[Step] = None,
) -> QuestionAnswer:
    """Ask the user one or more structured questions and wait for the answers.

    The single ``question`` form keeps its original contract. Pass
    ``questions=[...]`` for typed fields (text, email, date, number, URL,
    phone, choice); the wizard shows one field at a time with a review step.
    """
    from chainlit.question import (
        _question_answer_from_record,
        _validated_question_payload,
        _validated_questions_payload,
    )

    if question is not None:
        if questions:
            raise QuestionInvalid("pass either question or questions, not both")
        payload = _validated_question_payload(question, options, allow_free_text)
    else:
        if options or allow_free_text is not True:
            raise QuestionInvalid(
                "options and allow_free_text apply to the question form"
            )
        if not questions:
            raise QuestionInvalid("at least one question is required")
        payload = _validated_questions_payload(questions)
    target = step or context.current_step
    if target is None:
        raise ApprovalUnsupported("No current step to bind the question to")

    user, generation = _interaction_session(target)
    if timeout is None:
        timeout = float(config.project.session_timeout)
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout must be positive")

    now = datetime.now(timezone.utc)
    record: Dict[str, Any] = {
        "version": 1,
        "revision": 1,
        "kind": "question",
        "id": str(uuid.uuid4()),
        "stepId": target.id,
        "threadId": target.thread_id,
        "status": "pending",
        "prompt": payload["prompt"],
        # Separate copy: step.metadata must not alias the ask-time snapshot.
        "options": deepcopy(payload["options"]),
        "allowFreeText": payload["allowFreeText"],
        "questions": deepcopy(payload["questions"]),
        "structured": payload["structured"],
        "multi": payload["multi"],
        "requestedBy": user.identifier,
        "requestedAt": _iso(now),
        "expiresAt": _iso(now + timedelta(seconds=timeout)),
        "answer": None,
        "decidedBy": None,
        "decidedAt": None,
        "decision": None,
        "decisionRequestId": None,
        "terminalReason": None,
        "terminalAt": None,
    }
    expires_at_ts = (now + timedelta(seconds=timeout)).timestamp()

    # Synchronous, before the first await: later Step writes take the lock.
    target._approval_managed = True
    _remember_managed_thread(target.thread_id, user.identifier)
    await _store_managed_thread(target.thread_id, user.identifier)
    emitter = context.emitter
    key = (target.thread_id, target.id)
    entry = _Entry(
        approval_id=record["id"],
        thread_id=target.thread_id,
        step_id=target.id,
        step=target,
        future=asyncio.get_running_loop().create_future(),
        emitter=emitter,
        requested_by=user.identifier,
        session_generation=generation,
        revision=1,
        expires_at_ts=expires_at_ts,
        kind="question",
        payload=deepcopy(payload),
    )

    await _reserve_capacity(generation, record["id"], max(60.0, timeout + 60.0))
    reserved = True
    shared_tasks: tuple[Optional[asyncio.Task], Optional[asyncio.Task]] = (None, None)
    try:
        async with step_lock(*key):
            # L4: a terminal record in the one slot must not be overwritten.
            # A pending record is superseded below.
            previous_record = target.metadata.get(APPROVAL_METADATA_KEY)
            if isinstance(previous_record, dict) and (
                previous_record.get("status") != "pending"
            ):
                raise QuestionAlreadyAsked(
                    "This step already asked a terminal question"
                )

            previous_id = _pending_by_step.get(key)
            if previous_id:
                previous = _pending_by_approval.get(previous_id)
                if previous:
                    await _transition_locked(previous, "superseded", reason="replaced")

            # Drain a create/update scheduled before interaction management began.
            await target.flush_persistence()

            target.metadata[APPROVAL_METADATA_KEY] = record
            if not await _write(entry, record, expected_revision=None):
                if previous_record is None:
                    target.metadata.pop(APPROVAL_METADATA_KEY, None)
                else:
                    target.metadata[APPROVAL_METADATA_KEY] = previous_record
                raise ApprovalPersistenceError("Question persistence failed")

            _pending_by_approval[record["id"]] = entry
            _pending_by_step[key] = record["id"]
            _commit_reservation(generation)
            reserved = False

        shared_tasks = await _start_shared_pending(entry, record, timeout)
        await _emit(entry, record, {})
        stored = await _ask(entry, timeout)
        return _question_answer_from_record(stored, user, _has_data_layer())
    finally:
        if reserved:
            _release_reservation(generation)
            await _release_shared_capacity(generation, record["id"])
        await _stop_shared_pending(shared_tasks)
