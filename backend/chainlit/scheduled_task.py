"""Validation and recurrence helpers for prompt scheduling."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadDateError, croniter  # type: ignore[import-untyped]

MAX_CRON_LENGTH = 256
MAX_PROMPT_LENGTH = 10_000


def validate_timezone(name: str) -> str:
    if not isinstance(name, str) or not name or len(name) > 128:
        raise ValueError("timezone must be an IANA timezone")
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be an IANA timezone") from exc
    return name


def validate_cron(expression: str) -> str:
    if not isinstance(expression, str) or len(expression) > MAX_CRON_LENGTH:
        raise ValueError("cron must be exactly five fields")
    fields = expression.split()
    if len(fields) != 5 or any(
        not field or any(c.isalpha() for c in field) for field in fields
    ):
        raise ValueError("cron must be exactly five numeric fields")
    if not all(c in "0123456789*/,-?" for field in fields for c in field):
        raise ValueError("cron contains unsupported characters")
    canonical = " ".join(fields)
    if not croniter.is_valid(canonical):
        raise ValueError("invalid cron expression")
    try:
        croniter(canonical, datetime.now(timezone.utc)).get_next(datetime)
    except (CroniterBadDateError, ValueError) as exc:
        raise ValueError("cron must have a future occurrence") from exc
    return canonical


def next_run_at(
    expression: str, timezone_name: str, after: datetime | None = None
) -> datetime:
    expression = validate_cron(expression)
    timezone_name = validate_timezone(timezone_name)
    base = after or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    base_utc = base.astimezone(timezone.utc)
    zone = ZoneInfo(timezone_name)
    iterator = croniter(expression, base_utc.astimezone(zone))
    while True:
        candidate = iterator.get_next(datetime)
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=zone)
        if candidate.fold:
            first = candidate.replace(fold=0).astimezone(timezone.utc)
            if first > base_utc:
                return first
            continue
        return candidate.astimezone(timezone.utc)


def validate_prompt(prompt: str) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must not be empty")
    if len(prompt) > MAX_PROMPT_LENGTH:
        raise ValueError(f"prompt must be at most {MAX_PROMPT_LENGTH} characters")
    return prompt
