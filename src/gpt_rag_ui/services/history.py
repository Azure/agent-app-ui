"""
History and user operations backed by the orchestrator API.

No direct database access — all conversation data flows through the orchestrator service.
The API adapter supplies operation context; this module never resolves a
Chainlit session or registers callbacks. Existing Chainlit value types remain
the history contract rather than introducing a parallel DTO hierarchy.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from chainlit.types import (
    PaginatedResponse,
    Pagination,
    ThreadDict,
    ThreadFilter,
    PageInfo,
)
from chainlit.user import PersistedUser, User

from gpt_rag_ui.clients.orchestrator_client import (
    call_orchestrator_list_conversations,
    call_orchestrator_update_conversation,
    call_orchestrator_delete_conversation,
)
from gpt_rag_ui.services.conversation_security import (
    get_owned_conversation,
    principal_id_from_metadata,
    thread_owner_id_from_metadata,
)
from gpt_rag_ui.auth.embed_auth import (
    CopilotSession,
    is_copilot_session_active,
    resolve_access_token,
)

logger = logging.getLogger("gpt_rag_ui.datalayer")

# In-memory user store: identifier -> PersistedUser
# Populated on login, lost on restart (acceptable: users re-auth via OAuth each session).
_users: dict[str, PersistedUser] = {}


@dataclass(frozen=True)
class HistoryOperationContext:
    metadata: Optional[dict] = None
    request_session: CopilotSession | None = None


def _get_current_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")


class HistoryService:
    """History behavior and the single in-memory user cache."""

    # ── User management (in-memory) ──────────────────────────────────────

    async def get_user(
        self, context: HistoryOperationContext, identifier: str
    ) -> Optional[PersistedUser]:
        request_session = context.request_session
        if request_session:
            if not await is_copilot_session_active(
                request_session.user_metadata()
            ):
                logger.warning(
                    "Rejected persisted-user lookup for an expired Copilot session"
                )
                return None
            if request_session.principal_id != identifier:
                logger.warning(
                    "Rejected persisted-user lookup across Copilot principals"
                )
                return None
            user = PersistedUser(
                id=identifier,
                identifier=identifier,
                display_name=request_session.display_name,
                createdAt=_get_current_timestamp(),
                metadata=request_session.user_metadata(),
            )
            return user

        return _users.get(identifier)

    async def create_user(self, user: User) -> Optional[PersistedUser]:
        if not await is_copilot_session_active(user.metadata):
            logger.warning("Rejected user creation for an expired Copilot session")
            return None
        principal_id = principal_id_from_metadata(user.metadata)
        if not principal_id:
            logger.warning("No principal_id in user metadata for %s", user.identifier)
            return None
        if user.identifier.lower() != principal_id:
            logger.warning(
                "Refusing user with missing or inconsistent canonical identity"
            )
            return None

        persisted = PersistedUser(
            id=user.identifier,
            identifier=user.identifier,
            display_name=user.display_name,
            createdAt=_get_current_timestamp(),
            metadata=user.metadata or {},
        )
        if persisted.metadata.get("auth_source") != "copilot_session":
            _users[user.identifier] = persisted
        return persisted

    # ── Thread / conversation operations (via orchestrator API) ──────────

    async def list_threads(
        self,
        context: HistoryOperationContext,
        pagination: Pagination,
        filters: ThreadFilter,
    ) -> PaginatedResponse[ThreadDict]:
        empty = PaginatedResponse(
            data=[],
            pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
        )

        logger.info("list_threads called: pagination=%s filters=%s", pagination, filters)

        metadata = context.metadata
        if not metadata or not await is_copilot_session_active(metadata):
            logger.warning(
                "list_threads: no active session metadata; returning empty"
            )
            return empty

        principal_id = principal_id_from_metadata(metadata)
        user_identifier = thread_owner_id_from_metadata(metadata)
        if not principal_id:
            logger.warning(
                "list_threads: principal identity is missing; returning empty"
            )
            return empty

        access_token = await resolve_access_token(metadata)
        logger.info(
            "list_threads: metadata found (has_access_token=%s, user_name=%s, principal_id=%s)",
            bool(access_token),
            metadata.get("user_name"),
            metadata.get("principal_id") or metadata.get("client_principal_id"),
        )
        if not access_token:
            logger.warning("list_threads: no access_token in session metadata; returning empty (user may not be authenticated)")
            return empty

        skip = 0
        limit = 10
        if hasattr(pagination, "first") and pagination.first:
            limit = int(pagination.first)
        if hasattr(pagination, "cursor") and pagination.cursor:
            try:
                skip = int(pagination.cursor)
            except (ValueError, TypeError):
                logger.warning("list_threads: invalid pagination cursor; starting at zero")

        result = await call_orchestrator_list_conversations(
            access_token=access_token,
            skip=skip,
            limit=limit,
        )

        conversations = result.get("conversations", [])
        has_more = result.get("has_more", False)
        logger.info("list_threads: orchestrator returned %d conversations (skip=%d, limit=%d)", len(conversations), skip, limit)

        threads = []
        for conv in conversations:
            conversation_id = str(conv.get("id") or "").strip()
            if not conversation_id:
                logger.warning("list_threads: omitted conversation without an id")
                continue
            # The orchestrator list endpoint validates the same bearer token and
            # queries only that oid partition. Its compact response intentionally
            # omits principal_id, so the UI binds each returned thread to the
            # canonical tid:oid identity rather than inventing an owner fallback.
            threads.append(
                ThreadDict(
                    id=conversation_id,
                    name=conv.get("name", ""),
                    createdAt=conv.get("lastUpdated"),
                    userId=principal_id,
                    userIdentifier=user_identifier,
                    tags=[],
                    metadata={},
                    steps=[],
                )
            )

        return PaginatedResponse(
            data=threads,
            pageInfo=PageInfo(
                hasNextPage=has_more,
                startCursor=str(skip),
                endCursor=str(skip + len(threads)) if has_more else None,
            ),
        )

    async def get_thread(
        self, context: HistoryOperationContext, thread_id: str
    ) -> Optional[ThreadDict]:
        metadata = context.metadata
        if not metadata or not await is_copilot_session_active(metadata):
            logger.warning(
                "get_thread: no active session metadata; returning None for thread=%s",
                thread_id,
            )
            return None

        conv = await get_owned_conversation(thread_id, metadata)
        if not conv:
            logger.warning(
                "get_thread: conversation missing or ownership denied for thread=%s",
                thread_id,
            )
            return None

        messages = conv.get("messages", [])
        principal_id = principal_id_from_metadata(metadata)
        steps = self._messages_to_steps(
            messages,
            thread_id,
            principal_id,
            str(metadata.get("copilot_session_id") or ""),
        )
        thread_user_id = principal_id
        user_identifier = principal_id

        ts_value = conv.get("_ts")
        created_at = None
        if ts_value:
            try:
                if isinstance(ts_value, str):
                    created_at = ts_value if ts_value.endswith("Z") else ts_value + "Z"
                else:
                    created_at = datetime.fromtimestamp(ts_value).isoformat() + "Z"
            except (ValueError, TypeError):
                logger.warning("get_thread: invalid creation timestamp; leaving it unset")

        return ThreadDict(
            id=conv["id"],
            name=conv.get("name", ""),
            createdAt=created_at,
            userId=thread_user_id,
            userIdentifier=user_identifier,
            tags=[],
            metadata={},
            steps=steps,
        )

    async def get_thread_author(
        self, context: HistoryOperationContext, thread_id: str
    ) -> Optional[str]:
        thread = await self.get_thread(context, thread_id)
        if thread:
            return thread.get("userIdentifier")
        return None

    async def update_thread(
        self,
        context: HistoryOperationContext,
        thread_id: str,
        *,
        on_authorized: Callable[[str], None],
        **kwargs: Any,
    ) -> None:
        """Keep authorization and rename together; the API owns selection."""
        metadata = context.metadata
        if not metadata or not await is_copilot_session_active(metadata):
            logger.warning(
                "update_thread: no active session metadata; cannot rename thread=%s",
                thread_id,
            )
            return

        if not await get_owned_conversation(thread_id, metadata):
            logger.warning("update_thread: ownership denied for thread=%s", thread_id)
            return
        on_authorized(thread_id)
        name_value = kwargs.get("name") or kwargs.get("title") or ""
        name = str(name_value).strip()
        if not name:
            return
        access_token = await resolve_access_token(metadata)
        if not access_token:
            logger.warning("update_thread: auth session unavailable for thread=%s", thread_id)
            return

        updated = await call_orchestrator_update_conversation(
            access_token=access_token,
            conversation_id=thread_id,
            name=name,
        )
        if not updated:
            logger.warning("update_thread: orchestrator rename failed for thread=%s", thread_id)

    async def delete_thread(self, context: HistoryOperationContext, thread_id: str) -> bool:
        metadata = context.metadata
        if not metadata or not await is_copilot_session_active(metadata):
            logger.warning(
                "delete_thread: no active session metadata; cannot delete thread=%s",
                thread_id,
            )
            return False

        if not await get_owned_conversation(thread_id, metadata):
            logger.warning("delete_thread: ownership denied for thread=%s", thread_id)
            return False
        access_token = await resolve_access_token(metadata)
        if not access_token:
            logger.warning("delete_thread: no access_token; cannot delete thread=%s", thread_id)
            return False

        return await call_orchestrator_delete_conversation(
            access_token=access_token,
            conversation_id=thread_id,
        )

    # ── Helpers ──────────────────────────────────────────────────────────

    def _messages_to_steps(
        self,
        messages: list,
        thread_id: str,
        principal_id: str,
        copilot_session_id: str = "",
    ) -> list:
        """Convert orchestrator conversation messages to Chainlit StepDict format."""
        from gpt_rag_ui.services.citations import replace_source_reference_links

        steps = []
        for msg in messages:
            role = msg.get("role", "")
            text = msg.get("text", "")
            step_type = "user_message" if role == "user" else "assistant_message"

            # Resolve source reference links so markdown renders on resume.
            if step_type == "assistant_message" and text:
                text = replace_source_reference_links(
                    text,
                    conversation_id=thread_id,
                    principal_id=principal_id,
                    copilot_session_id=copilot_session_id,
                )

            steps.append({
                "id": str(uuid.uuid4()),
                "threadId": thread_id,
                "type": step_type,
                "output": text,
                "createdAt": _get_current_timestamp(),
                "isError": False,
                "metadata": {},
            })
        return steps
