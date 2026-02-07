"""Shared Pydantic models used across projects."""

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Standard health check response."""

    status: str = "healthy"
    service: str
    version: str


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: str | None = None
    status_code: int = 500


class ChatMessage(BaseModel):
    """Standard chat message format."""

    role: str  # "user", "assistant", "system"
    content: str


class ChatRequest(BaseModel):
    """Standard chat request format."""

    messages: list[ChatMessage]
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = 2048
    stream: bool = False


class ChatResponse(BaseModel):
    """Standard chat response format."""

    message: ChatMessage
    model: str
    usage: dict[str, int] | None = None
