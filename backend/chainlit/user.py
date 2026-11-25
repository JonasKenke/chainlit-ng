from typing import Dict, Literal, Optional, TypedDict

from dataclasses_json import DataClassJsonMixin
from pydantic import Field
from pydantic.dataclasses import dataclass

Provider = Literal[
    "credentials",
    "header",
    "github",
    "google",
    "azure-ad",
    "azure-ad-hybrid",
    "okta",
    "auth0",
    "descope",
]


class UserDict(TypedDict):
    id: str
    identifier: str
    display_name: Optional[str]
    metadata: Dict


# Used when logging-in a user
@dataclass
class User(DataClassJsonMixin):
    identifier: str
    display_name: Optional[str] = None
    metadata: Dict = Field(default_factory=dict)


@dataclass
class PersistedUserFields:
    id: str
    createdAt: str


@dataclass
class PersistedUser(User, PersistedUserFields):
    pass


# Provider credentials must never reach the browser through User.metadata
# (which is serialized into the JWT and GET /user).
SENSITIVE_METADATA_KEYS = frozenset(
    {"access_token", "refresh_token", "id_token", "client_secret"}
)


def redact_sensitive_metadata(user: Optional[User]) -> Optional[User]:
    if user is None:
        return None
    metadata = getattr(user, "metadata", None)
    if not isinstance(metadata, dict):
        return user
    if not any(key in metadata for key in SENSITIVE_METADATA_KEYS):
        return user
    safe_metadata = {
        key: value
        for key, value in metadata.items()
        if key not in SENSITIVE_METADATA_KEYS
    }
    if isinstance(user, PersistedUser):
        return PersistedUser(
            id=user.id,
            createdAt=user.createdAt,
            identifier=user.identifier,
            display_name=user.display_name,
            metadata=safe_metadata,
        )
    return User(
        identifier=user.identifier,
        display_name=user.display_name,
        metadata=safe_metadata,
    )
