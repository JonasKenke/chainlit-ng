from enum import Enum
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Annotated,
    Any,
    Dict,
    Generic,
    List,
    Literal,
    Optional,
    Protocol,
    TypedDict,
    TypeVar,
    Union,
)

if TYPE_CHECKING:
    from chainlit.element import ElementDict
    from chainlit.step import StepDict

from dataclasses import field

from dataclasses_json import DataClassJsonMixin
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.dataclasses import dataclass
from typing_extensions import NotRequired

InputWidgetType = Literal[
    "switch",
    "slider",
    "select",
    "textinput",
    "tags",
    "numberinput",
    "multiselect",
    "checkbox",
    "radio",
    "datepicker",
]
ToastType = Literal["info", "success", "warning", "error"]


class ThreadDict(TypedDict):
    id: str
    createdAt: str
    name: Optional[str]
    userId: Optional[str]
    userIdentifier: Optional[str]
    tags: Optional[List[str]]
    metadata: Optional[Dict]
    steps: List["StepDict"]
    elements: Optional[List["ElementDict"]]


@dataclass
class ThreadOwner:
    """Typed thread owner. A layer that knows the persisted user id returns it."""

    user_id: Optional[str] = None
    identifier: Optional[str] = None


@dataclass
class ThreadGrant:
    """Read grant from a thread owner to one persisted user."""

    thread_id: str
    grantee_user_id: str
    grantee_user_created_at: str
    grantee_identifier: str
    granted_by_user_id: str
    granted_at: str
    role: str = "read"


@dataclass
class FeedbackThreadLookup:
    """Typed answer to "which thread owns this feedback id".

    `found` with no `thread_id` is a legacy row and is a denial, not a new row.
    """

    status: Literal["found", "not_found", "error"]
    thread_id: Optional[str] = None


class Pagination(BaseModel):
    first: int = Field(ge=1)
    cursor: Optional[str] = None


class ThreadFilter(BaseModel):
    feedback: Literal[0, 1] | None = None
    userId: str | None = None
    sharedWithUserId: str | None = None
    sharedWithUserCreatedAt: str | None = None
    search: str | None = None


@dataclass
class PageInfo:
    hasNextPage: bool
    startCursor: Optional[str]
    endCursor: Optional[str]

    def to_dict(self):
        return {
            "hasNextPage": self.hasNextPage,
            "startCursor": self.startCursor,
            "endCursor": self.endCursor,
        }

    @classmethod
    def from_dict(cls, page_info_dict: Dict) -> "PageInfo":
        hasNextPage = page_info_dict.get("hasNextPage", False)
        startCursor = page_info_dict.get("startCursor", None)
        endCursor = page_info_dict.get("endCursor", None)
        return cls(
            hasNextPage=hasNextPage, startCursor=startCursor, endCursor=endCursor
        )


T = TypeVar("T", covariant=True)


class HasFromDict(Protocol[T]):
    @classmethod
    def from_dict(cls, obj_dict: Any) -> T:
        raise NotImplementedError


@dataclass
class PaginatedResponse(Generic[T]):
    pageInfo: PageInfo
    data: List[T]

    def to_dict(self):
        return {
            "pageInfo": self.pageInfo.to_dict(),
            "data": [
                (d.to_dict() if hasattr(d, "to_dict") and callable(d.to_dict) else d)
                for d in self.data
            ],
        }

    @classmethod
    def from_dict(
        cls, paginated_response_dict: Dict, the_class: HasFromDict[T]
    ) -> "PaginatedResponse[T]":
        pageInfo = PageInfo.from_dict(paginated_response_dict.get("pageInfo", {}))

        data = [the_class.from_dict(d) for d in paginated_response_dict.get("data", [])]

        return cls(pageInfo=pageInfo, data=data)


@dataclass
class FileSpec(DataClassJsonMixin):
    accept: Union[List[str], Dict[str, List[str]]]
    max_files: int
    max_size_mb: int


@dataclass
class ActionSpec(DataClassJsonMixin):
    keys: List[str]


@dataclass
class AskSpec(DataClassJsonMixin):
    """Specification for asking the user."""

    timeout: int
    type: Literal["text", "file", "action", "element"]
    step_id: str


@dataclass
class AskFileSpec(FileSpec, AskSpec, DataClassJsonMixin):
    """Specification for asking the user a file."""


@dataclass
class AskActionSpec(ActionSpec, AskSpec, DataClassJsonMixin):
    """Specification for asking the user an action"""


@dataclass
class AskElementSpec(AskSpec, DataClassJsonMixin):
    """Specification for asking the user a custom element"""

    element_id: str


class FileReference(TypedDict):
    id: str


class FileDict(TypedDict):
    id: str
    name: str
    path: Path
    size: int
    type: str


class MessagePayload(TypedDict):
    message: "StepDict"
    fileReferences: Optional[List[FileReference]]


class InputAudioChunkPayload(TypedDict):
    isStart: bool
    mimeType: str
    elapsedTime: float
    data: bytes


@dataclass
class InputAudioChunk:
    isStart: bool
    mimeType: str
    elapsedTime: float
    data: bytes


class OutputAudioChunk(TypedDict):
    track: str
    mimeType: str
    data: bytes


@dataclass
class AskFileResponse:
    id: str
    name: str
    path: str
    size: int
    type: str


class AskActionResponse(TypedDict):
    name: str
    payload: Dict
    label: str
    tooltip: str
    forId: str
    id: str


class AskElementResponse(TypedDict, total=False):
    submitted: bool


def _reject_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


# Validates without rewriting, so valid values are stored byte-for-byte.
NonBlankStr = Annotated[str, AfterValidator(_reject_blank)]


class UpdateThreadRequest(BaseModel):
    threadId: str
    name: NonBlankStr


class ShareThreadRequest(BaseModel):
    threadId: str
    isShared: bool


class DeleteThreadRequest(BaseModel):
    threadId: str


class DeleteFeedbackRequest(BaseModel):
    feedbackId: str


class CreateThreadGrantRequest(BaseModel):
    grantee: NonBlankStr


class GetThreadsRequest(BaseModel):
    pagination: Pagination
    filter: ThreadFilter


class CallActionRequest(BaseModel):
    action: Dict
    sessionId: str


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessionId: str = Field(min_length=1, max_length=128)
    approvalId: str = Field(min_length=1, max_length=64)
    decision: Literal["approve", "reject", "edit", "request_changes"]
    # Validated against the server-held schema, never typed here: a FastAPI
    # validation error would echo the client value in the response body.
    editedArguments: Optional[Any] = None
    reason: Optional[str] = Field(default=None, max_length=500)
    decisionRequestId: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )


class ApprovalDecisionResponse(BaseModel):
    approvalId: str
    status: Literal[
        "pending",
        "approved",
        "rejected",
        "edited",
        "changes_requested",
        "expired",
        "superseded",
        "orphaned",
    ]
    decision: Optional[Literal["approve", "reject", "edit", "request_changes"]] = None
    decidedBy: Optional[str] = None
    decidedAt: Optional[str] = None
    terminalReason: Optional[str] = None
    reason: Optional[str] = None
    revision: int
    error: Optional[str] = None


class QuestionAnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sessionId: str = Field(min_length=1, max_length=128)
    questionId: str = Field(min_length=1, max_length=64)
    optionId: Optional[str] = Field(default=None, min_length=1, max_length=64)
    text: Optional[str] = Field(default=None, max_length=2000)
    decisionRequestId: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )

    @model_validator(mode="after")
    def validate_single_answer(self) -> "QuestionAnswerRequest":
        if (self.optionId is None) == (self.text is None):
            raise ValueError("exactly one of optionId or text is required")
        if self.text is not None and not self.text.strip():
            raise ValueError("text must not be blank")
        return self


class QuestionAnswerResponse(BaseModel):
    questionId: str
    status: Literal["pending", "answered", "expired", "superseded", "orphaned"]
    answer: Optional[Dict[str, Any]] = None
    answeredBy: Optional[str] = None
    answeredAt: Optional[str] = None
    revision: int
    durable: bool
    error: Optional[str] = None


class ConnectMCPRequest(BaseModel):
    """Request to connect an MCP server.

    Configured server: provide sessionId + name only. The server's transport,
    command and headers come from `[[features.mcp.servers]]` and are never
    client-supplied.

    User-provided server (SSE/HTTP only, when enabled): also provide
    clientType + url.
    """

    # Reject unknown fields so a legacy client sending `fullCommand` (or any
    # other attempt to describe a command) fails validation instead of having
    # the field silently dropped on the way to a named-server lookup.
    model_config = ConfigDict(extra="forbid")

    sessionId: str
    name: str
    # Present only for user-provided connections:
    clientType: Optional[Literal["sse", "streamable-http"]] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    # Opt in to the per-user OAuth flow for a user-provided connection.
    useOAuth: bool = False

    @model_validator(mode="after")
    def validate_user_provided_consistency(self) -> "ConnectMCPRequest":
        has_url = self.url is not None
        has_type = self.clientType is not None
        if has_url != has_type:
            raise ValueError(
                "Both 'url' and 'clientType' must be provided together for user-provided connections"
            )
        return self


class DisconnectMCPRequest(BaseModel):
    sessionId: str
    name: str


class McpPromptRequest(BaseModel):
    """Invoke one prompt published by a connected MCP server.

    ``name`` is the connected server, ``prompt`` the prompt to fetch. The
    argument map is validated against the server's own schema before the
    call; nothing is injected automatically.
    """

    model_config = ConfigDict(extra="forbid")

    sessionId: str
    name: str
    prompt: str
    arguments: Optional[Dict[str, str]] = None


class McpResourceRequest(BaseModel):
    """Read one resource published by a connected MCP server."""

    model_config = ConfigDict(extra="forbid")

    sessionId: str
    name: str
    uri: str
    # Display label from the picker; the element falls back to the URI tail.
    resourceName: Optional[str] = None


class ElementRequest(BaseModel):
    element: Dict
    sessionId: str


class Theme(str, Enum):
    light = "light"
    dark = "dark"


@dataclass
class Starter(DataClassJsonMixin):
    """Specification for a starter that can be chosen by the user at the thread start."""

    label: str
    message: str
    command: Optional[str] = None
    icon: Optional[str] = None


@dataclass
class StarterCategory(DataClassJsonMixin):
    """A category/group of starters with an optional icon."""

    label: str
    icon: Optional[str] = None
    starters: List[Starter] = field(default_factory=list)


@dataclass
class ChatProfile(DataClassJsonMixin):
    """Specification for a chat profile that can be chosen by the user at the thread start."""

    name: str
    markdown_description: str
    icon: Optional[str] = None
    display_name: Optional[str] = None
    default: bool = False
    starters: Optional[List[Starter]] = None
    config_overrides: Any = None


FeedbackStrategy = Literal["BINARY"]


class CommandDict(TypedDict):
    # The identifier of the command, will be displayed in the UI
    id: str
    # The description of the command, will be displayed in the UI
    description: str
    # The lucide icon name
    icon: str
    # Display the command as a button in the composer
    button: Optional[bool]
    # Whether the command will be persistent unless the user toggles it
    persistent: Optional[bool]
    # Whether the command should be pre-selected when loaded
    selected: Optional[bool]


class FeedbackDict(TypedDict):
    forId: str
    id: Optional[str]
    value: Literal[0, 1]
    comment: Optional[str]


@dataclass
class Feedback:
    forId: str
    value: Literal[0, 1]
    threadId: Optional[str] = None
    id: Optional[str] = None
    comment: Optional[str] = None


class UpdateFeedbackRequest(BaseModel):
    feedback: Feedback
    sessionId: str


class PromptDict(TypedDict):
    id: str
    title: str
    content: str
    userId: str
    isShared: bool
    createdAt: str
    updatedAt: str
    # Skills share the prompts table; these fields stay optional so existing
    # data layers and prompt rows remain valid without changes.
    kind: NotRequired[str]
    description: NotRequired[str]
    enabled: NotRequired[bool]
    importedFrom: NotRequired[Optional[str]]
    importedAt: NotRequired[Optional[str]]
    contentHash: NotRequired[Optional[str]]


class CreatePromptRequest(BaseModel):
    title: NonBlankStr
    content: NonBlankStr


class UpdatePromptRequest(BaseModel):
    title: Optional[NonBlankStr] = None
    content: Optional[NonBlankStr] = None


class SharePromptRequest(BaseModel):
    isShared: bool


class CreateSkillRequest(BaseModel):
    name: str
    description: str
    content: str
    enabled: bool = False


class UpdateSkillRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    enabled: Optional[bool] = None


class ImportSkillRequest(BaseModel):
    markdown: str
    imported_from: str = Field(min_length=1, max_length=2048)


class ScheduledTaskDict(TypedDict):
    id: str
    userId: str
    userIdentifier: str
    prompt: str
    chatProfile: Optional[str]
    model: NotRequired[Optional[str]]
    cron: str
    timezone: str
    enabled: bool
    afterHooks: List[str]
    nextRunAt: str
    manualRunRequestedAt: Optional[str]
    lastRunAt: Optional[str]
    lastStatus: Optional[str]
    lastError: Optional[str]
    lastThreadId: Optional[str]
    leaseId: Optional[str]
    leaseUntil: Optional[str]
    createdAt: str
    updatedAt: str


class CreateScheduledTaskRequest(BaseModel):
    prompt: str
    chatProfile: Optional[str] = None
    model: Optional[str] = Field(default=None, max_length=256)
    cron: str
    timezone: str
    enabled: bool = True
    afterHooks: List[str] = Field(default_factory=list)


class UpdateScheduledTaskRequest(BaseModel):
    prompt: Optional[str] = None
    chatProfile: Optional[str] = None
    model: Optional[str] = Field(default=None, max_length=256)
    cron: Optional[str] = None
    timezone: Optional[str] = None
    enabled: Optional[bool] = None
    afterHooks: Optional[List[str]] = None


class ScheduledTaskCompletion(BaseModel):
    status: str
    completedAt: str
    lastError: Optional[str] = None
    lastThreadId: Optional[str] = None
    nextRunAt: Optional[str] = None
    enabled: Optional[bool] = None


class ScheduledTaskResult(BaseModel):
    taskId: str
    runId: str
    threadId: Optional[str] = None
    owner: str
    prompt: str
    chatProfile: Optional[str] = None
    model: Optional[str] = None
    scheduledFor: str
    startedAt: str
    completedAt: str
    status: str
    outputMessages: List[str] = Field(default_factory=list)
    error: Optional[str] = None
