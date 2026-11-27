"""Chat backend selection shared by runtime wiring and focused tests."""

import os
from collections.abc import Mapping
from typing import Literal, Protocol

ChatBackend = Literal["orchestrator", "hosted_agent"]
DEFAULT_CHAT_BACKEND: ChatBackend = "hosted_agent"


class ChatBackendConfig(Protocol):
    def get(self, key: str, default: str, type: type) -> str:
        """Return the configured chat backend setting or its default."""


def resolve_chat_backend(value: str | None) -> ChatBackend:
    backend = (value or "").strip().lower() or DEFAULT_CHAT_BACKEND
    if backend == "orchestrator":
        return "orchestrator"
    if backend == "hosted_agent":
        return "hosted_agent"
    raise RuntimeError(
        f"Unknown CHAT_BACKEND value: {backend!r}. "
        "Valid values are 'hosted_agent' (default) and 'orchestrator'."
    )


def load_chat_backend(
    config: ChatBackendConfig,
    *,
    environment: Mapping[str, str] | None = None,
) -> ChatBackend:
    """Resolve the env-over-App-Configuration backend selection."""
    source = os.environ if environment is None else environment
    value = source.get("CHAT_BACKEND")
    if value is None:
        value = config.get("CHAT_BACKEND", "", str)
    return resolve_chat_backend(value)


def select_upload_conversation_id(
    backend: ChatBackend,
    *,
    classic_conversation_id: str,
    hosted_conversation_id: str,
) -> str:
    """Select only the conversation namespace owned by the active backend."""
    if backend == "hosted_agent":
        return hosted_conversation_id.strip()
    return classic_conversation_id.strip()
