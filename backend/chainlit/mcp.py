from typing import Dict, Literal, Optional, Union

from pydantic import BaseModel


class StdioMcpConnection(BaseModel):
    name: str
    command: str
    args: list[str]
    clientType: Literal["stdio"] = "stdio"


class SseMcpConnection(BaseModel):
    name: str
    url: str
    headers: Optional[Dict[str, str]] = None
    clientType: Literal["sse"] = "sse"


class HttpMcpConnection(BaseModel):
    name: str
    url: str
    headers: Optional[Dict[str, str]] = None
    clientType: Literal["streamable-http"] = "streamable-http"


McpConnection = Union[StdioMcpConnection, SseMcpConnection, HttpMcpConnection]
