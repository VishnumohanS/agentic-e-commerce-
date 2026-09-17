"""A2A (Agent-to-Agent) and MCP protocol envelopes.

A2A is carried as JSON-RPC 2.0 over HTTP POST. Each message contains typed
parts; this implementation uses `data` parts that name a merchant *skill* and
its input, which maps cleanly onto the agent card advertised at
`/.well-known/agent.json`.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

JSONRPC_VERSION = "2.0"

# JSON-RPC error codes (standard range + A2A application range)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
APPLICATION_ERROR = -32000


class DataPart(BaseModel):
    kind: Literal["data"] = "data"
    data: dict[str, Any] = Field(default_factory=dict)


class TextPart(BaseModel):
    kind: Literal["text"] = "text"
    text: str = ""


Part = DataPart | TextPart


class A2AMessage(BaseModel):
    message_id: str = Field(alias="messageId")
    role: Literal["user", "agent"] = "user"
    context_id: str = Field(default="", alias="contextId")
    parts: list[dict[str, Any]] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("parts")
    @classmethod
    def _require_parts(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not value:
            raise ValueError("A2A message must contain at least one part")
        return value

    def data_payload(self) -> dict[str, Any]:
        """Return the first `data` part's contents."""
        for part in self.parts:
            if part.get("kind") == "data" and isinstance(part.get("data"), dict):
                return part["data"]
        raise ValueError("A2A message contains no data part")


class A2AMessageParams(BaseModel):
    message: A2AMessage


class A2ARequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


class A2AError(BaseModel):
    code: int
    message: str
    data: dict[str, Any] | None = None


class A2AResponse(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    id: str | int | None = None
    result: dict[str, Any] | None = None
    error: A2AError | None = None


def build_data_message(
    message_id: str, context_id: str, skill: str, payload: dict[str, Any], role: str = "user"
) -> dict[str, Any]:
    return {
        "messageId": message_id,
        "role": role,
        "contextId": context_id,
        "parts": [{"kind": "data", "data": {"skill": skill, "input": payload}}],
    }


class MCPToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
