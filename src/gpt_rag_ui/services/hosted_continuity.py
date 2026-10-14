"""Orchestration for the opt-in hosted-agent cross-version continuity path
(``HOSTED_CONTINUITY_ENABLED=true``).

While the feature flag is false (the default), none of this module's logic
runs and the existing per-turn hosted-agent behavior in ``app.py`` /
``hosted_agent_client.py`` is completely unchanged.

Per turn, in order:

1. Resolve the managed conversation id for this turn according to the active
   owner-binding mode (``hosted_continuity_config.HostedContinuitySettings``).
   In both modes, only a genuinely *absent* client-presented handle (a
   legitimate new chat) may create a brand-new managed conversation. Any
   *presented* handle is re-validated for the current caller's own identity,
   and *any* validation failure — a cross-user id, a malformed id, a
   missing/deleted resource, an arbitrary guessed id, a forged/expired
   capability, a retired signing key, or an oid mismatch — raises the single
   opaque ``ConversationNotFoundError``. Callers MUST fail closed on this: no
   new conversation is created, the hosted agent is never invoked, and
   nothing is appended for that turn. This is deliberate: silently minting a
   fresh conversation on a rejected supplied handle would produce a
   success-shaped response that hides an attempted IDOR/BOLA probe and would
   let "did this look like a fresh conversation?" become an existence
   oracle.
   * ``delegated`` (preferred/default) — a presented conversation id is
     re-validated with a lightweight read probe against the Conversations
     store under the *current* caller's own validated Entra ``oid``, asserted
     only via the platform-trusted ``x-ms-user-identity`` header (never a raw
     client-supplied claim of ownership). The platform's own access-denied
     response (``ConversationStoreAccessDeniedError`` — HTTP 401/403/404)
     is mapped to ``ConversationNotFoundError``, never trusted or silently
     replaced with a fresh conversation.
   * ``capability`` (disabled fallback) — a presented caller-held opaque
     signed capability is validated against the caller's validated Entra oid
     (``hosted_conversation_capability.py``). Any failure — missing, forged,
     expired, retired key, or wrong oid — is mapped to the same
     ``ConversationNotFoundError``, never silently replaced with a fresh
     conversation.
   Neither mode ever mints/returns a handle around a caller-supplied
   conversation id that this BFF did not itself just create or re-validate.
2. Acquire a one-in-flight lock for the resolved managed conversation id.
3. Read ordered history from the Conversations system-of-record
   (``hosted_conversation_store.py``) and apply the explicit bounded-history
   policy (max items, then max tokens, dropping the oldest first).
4. Build a complete, ordered, canonical stateless Responses input and invoke
   the hosted runtime *without* a top-level conversation/previous_response_id
   (``hosted_agent_client.py``); the hosted runtime never receives a
   conversation reference and has zero Conversations RBAC.
5. Append the user turn and the completed assistant turn back to the
   Conversations system-of-record. This append is fail-closed: if it does not
   succeed, ``ContinuityPersistenceError`` is raised and the caller must
   render an explicit failure rather than treat the already-streamed
   assistant text as a successfully committed turn. An idempotent client
   turn id prevents a duplicate append on retry.

An optional ``on_conversation_created`` hook (issue #611, ADR-0004) lets the
panel's user-facing surfaces keep a Cosmos-backed owner index in step with
newly created managed conversations, without this module or the hosted
container ever touching panel Cosmos itself; see ``HostedContinuityCoordinator``
below.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from gpt_rag_ui.auth.auth_common import normalize_guid
from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure
from gpt_rag_ui.clients.hosted_agent_client import HostedAgentError, InvocationMessage, call_hosted_agent_stream
from gpt_rag_ui.config.hosted_continuity_config import HostedContinuitySettings
from gpt_rag_ui.clients.hosted_conversation_capability import (
    ConversationCapabilityError,
    ConversationCapabilityManager,
)
from gpt_rag_ui.clients.hosted_conversation_store import (
    ConversationIdempotencyCache,
    ConversationLockRegistry,
    ConversationStoreAccessDeniedError,
    ConversationStoreClient,
    ConversationStoreError,
    build_bounded_history,
)

logger = logging.getLogger("gpt_rag_ui.hosted_continuity")

# Single, invariant message used for every reason a client-presented
# conversation handle can be rejected (cross-user id, malformed id, missing
# resource, arbitrary guessed id, forged/expired/oid-mismatched capability,
# retired signing key, ...). Never vary this text, an HTTP-equivalent status,
# or response timing by rejection reason -- doing so re-creates an existence
# oracle that lets an attacker distinguish "wrong owner" from "doesn't exist"
# from "malformed", which is exactly what ADR-0003 requires this module to
# not do.
_NOT_FOUND_MESSAGE = "The referenced conversation could not be found."


class ContinuityPersistenceError(RuntimeError):
    """Raised when a turn could not be durably committed to the Conversations
    system-of-record (a history read failure, an append failure, or a
    detected duplicate client turn id).

    Callers MUST treat this as a hard failure: render an explicit error and
    never present the assistant text that already streamed as a successfully
    completed, durably saved turn.
    """


class ConversationNotFoundError(RuntimeError):
    """Raised when a client-presented conversation handle (a delegated-mode
    conversation id or a capability-mode token) fails validation for the
    current caller's identity.

    This is intentionally the *only* public error raised for every one of: a
    cross-user conversation id, a malformed/garbage conversation id, a
    reference to a resource that does not exist, an arbitrary/guessed
    conversation id, and a missing/expired/forged/oid-mismatched capability.
    Callers (``app.py``) MUST map this to a single opaque HTTP/UI
    404-equivalent response and MUST NOT create a new conversation, invoke
    the hosted agent, or append anything to the Conversations
    system-of-record for the turn that raised it. A genuinely *absent*
    handle on a legitimate new chat is not this error — only a *presented*
    handle that fails validation is.
    """


class HostedContinuityCoordinator:
    """Wires owner-binding resolution, the Conversations system-of-record,
    and the stateless hosted-agent invocation together for one turn."""

    def __init__(
        self,
        *,
        settings: HostedContinuitySettings,
        store: ConversationStoreClient,
        capability_manager: ConversationCapabilityManager | None = None,
        locks: ConversationLockRegistry | None = None,
        idempotency: ConversationIdempotencyCache | None = None,
        on_conversation_created: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        if not settings.enabled:
            raise ValueError(
                "HostedContinuityCoordinator requires HOSTED_CONTINUITY_ENABLED=true."
            )
        if settings.uses_capability_binding and capability_manager is None:
            raise ValueError(
                "capability_manager is required when "
                "HOSTED_CONVERSATION_OWNER_BINDING=capability."
            )
        self.settings = settings
        self._store = store
        self._capabilities = capability_manager
        self._locks = locks or ConversationLockRegistry()
        self._idempotency = idempotency or ConversationIdempotencyCache()
        # Optional, panel-only hook (issue #611, ADR-0004): invoked with
        # ``(oid, conversation_id)`` exactly once, immediately after this
        # coordinator creates a brand-new managed conversation for the
        # current authenticated user, so the panel's owner index can be
        # kept in step with the system of record. Never invoked for a
        # *presented* (existing) conversation reference, and never given a
        # caller-supplied conversation id. Best-effort: any exception is
        # logged and swallowed here so a panel metadata hiccup never fails
        # the user's turn. A missing row hides the conversation from the
        # panel and denies its read/feedback/delete routes, never disclosing
        # content (see ADR-0004 consequences).
        self._on_conversation_created = on_conversation_created

    @property
    def store(self) -> ConversationStoreClient:
        """The managed-Conversations system-of-record client this
        coordinator owns. Exposed so other authenticated-BFF-only surfaces
        (the panel's user-facing routes, ``panel_routes.py``) can reuse the
        exact same client/credentials instead of constructing a duplicate."""
        return self._store

    async def _resolve_conversation_id(
        self,
        *,
        owner_ref: str,
        oid: str,
        user_access_token: str,
    ) -> tuple[str, str]:
        """Return ``(conversation_id, owner_ref)`` for this turn, dispatching
        to the active owner-binding mode's resolution strategy."""
        if self.settings.uses_delegated_binding:
            return await self._resolve_conversation_id_delegated(
                owner_ref=owner_ref, oid=oid, user_access_token=user_access_token
            )
        return await self._resolve_conversation_id_capability(
            capability=owner_ref, oid=oid, user_access_token=user_access_token
        )

    async def _resolve_conversation_id_capability(
        self,
        *,
        capability: str,
        oid: str,
        user_access_token: str,
    ) -> tuple[str, str]:
        """Capability-mode resolution (disabled fallback).

        Only a genuinely *absent* capability (a legitimate new chat) mints a
        fresh managed conversation and a fresh capability around it. Any
        *presented* capability that fails validation for *any* reason —
        missing signature, forged, expired, a retired signing key, or an
        oid mismatch — raises the single opaque ``ConversationNotFoundError``
        instead of silently minting a replacement conversation: the caller
        must fail closed (no new conversation, no hosted-agent invocation)
        rather than turn an attempted IDOR/BOLA probe into a success-shaped
        response. Never mints a capability around a caller-supplied
        conversation id either way — the only conversation id ever bound
        here is one this method itself just created.
        """
        assert self._capabilities is not None  # enforced in __init__
        stripped = capability.strip()
        if not stripped:
            conversation_id = await self._store.create_conversation(
                user_access_token=user_access_token,
            )
            new_capability = self._capabilities.mint(
                oid=oid, conversation_id=conversation_id
            )
            return conversation_id, new_capability
        try:
            validated = self._capabilities.validate(stripped, oid=oid)
        except ConversationCapabilityError as exc:
            logger.info(
                "Hosted-continuity capability rejected; failing closed with "
                "an opaque not-found error rather than creating a new "
                "conversation."
            )
            raise ConversationNotFoundError(_NOT_FOUND_MESSAGE) from exc
        return validated.conversation_id, stripped

    async def _resolve_conversation_id_delegated(
        self,
        *,
        owner_ref: str,
        oid: str,
        user_access_token: str,
    ) -> tuple[str, str]:
        """Delegated-mode resolution (preferred/default).

        The client-held ``owner_ref`` is simply the real managed conversation
        id — there is nothing to cryptographically validate locally, because
        ownership is enforced by the platform itself via the
        ``x-ms-user-identity`` header (derived only from ``oid``, never from
        the caller-supplied ``owner_ref``) on every Conversations call.

        Only a genuinely *absent* ``owner_ref`` (a legitimate new chat) may
        create a brand-new managed conversation. Any *presented* ``owner_ref``
        is re-validated with a lightweight read probe under the current
        caller's own asserted oid first. A platform denial
        (``ConversationStoreAccessDeniedError`` — HTTP 401/403/404, covering a
        cross-user id, a stale/deleted id, a malformed id, and an arbitrary
        guessed id alike) is never treated as "start a fresh conversation
        instead": doing so would create a success-shaped response for what
        may be an attempted IDOR/BOLA probe and would let an attacker use
        "did I get a fresh conversation?" as an existence oracle. It instead
        raises the single opaque ``ConversationNotFoundError``; the caller
        must fail closed (surface an explicit not-found failure, never
        create a new conversation, and never invoke the hosted agent for
        this turn). Generic transport/5xx failures from the probe are not
        caught here and propagate as the plain ``ConversationStoreError``
        base class — those remain explicit dependency/persistence failures,
        never a 404-equivalent and never a fresh conversation.
        """
        canonical_oid = normalize_guid(oid, claim_name="oid")
        candidate = owner_ref.strip()
        if not candidate:
            conversation_id = await self._store.create_conversation(
                user_access_token=user_access_token, oid=canonical_oid
            )
            return conversation_id, conversation_id
        try:
            await self._store.list_items_ascending(
                oid=canonical_oid,
                conversation_id=candidate,
                limit=1,
            )
        except ConversationStoreAccessDeniedError as exc:
            logger.info(
                "Hosted-continuity delegated conversation reference was "
                "rejected by the platform for the current identity; "
                "failing closed with an opaque not-found error rather than "
                "creating a new conversation."
            )
            raise ConversationNotFoundError(_NOT_FOUND_MESSAGE) from exc
        return candidate, candidate

    async def run_turn(
        self,
        *,
        capability: str,
        oid: str,
        user_ask: str,
        client_turn_id: str,
        question_id: str | None,
        correlation_id: str | None,
        user_access_token: str,
    ) -> AsyncGenerator[tuple[str, dict[str, Any]], None]:
        """Run one bounded, stateless hosted-agent turn and durably persist it.

        Yields the same ``(text_chunk, meta)`` protocol as
        ``call_hosted_agent_stream``. The first yielded frame always carries
        ``meta["capability"]`` (the continuity handle the caller must persist
        for the next turn) and ``meta["conversation_id"]`` (the real,
        BFF-owned managed conversation id, safe to use only for the remainder
        of *this* request, e.g. for citation/download scoping — it must not
        be persisted across turns under its own name). Under
        ``HOSTED_CONVERSATION_OWNER_BINDING=capability`` the persisted handle
        is an opaque signed token; under the preferred ``delegated`` mode it
        is simply the real conversation id, which is safe to hold client-side
        because the platform (not a BFF-issued signature) enforces per-user
        ownership of it. Either way callers must treat the field as an opaque
        handle and never attempt to parse or trust its shape.

        Any ``conversation_id`` reported by the hosted runtime itself in
        later frames is intentionally stripped, since the hosted runtime's
        own ephemeral identifier is unrelated to the Conversations
        system-of-record this module owns.

        Raises ``ConversationNotFoundError`` before any hosted-agent
        invocation or persistence write if a *presented* ``capability``
        (a delegated-mode conversation id or a capability-mode token) fails
        validation for the current caller's identity; callers must map this
        to a single opaque 404-equivalent and must not create a new
        conversation or invoke the hosted agent for this turn.

        Raises ``ContinuityPersistenceError`` if the completed turn could not
        be durably appended (or a duplicate client turn id is detected);
        callers must treat this as a hard failure.
        """
        ask = user_ask.strip()
        if not ask:
            raise HostedAgentError("The current user message must not be empty.")
        if not client_turn_id:
            raise HostedAgentError("A client turn id is required for idempotent append.")

        canonical_oid = normalize_guid(oid, claim_name="oid")
        is_new_conversation = not capability.strip()
        conversation_id, resolved_owner_ref = await self._resolve_conversation_id(
            owner_ref=capability,
            oid=canonical_oid,
            user_access_token=user_access_token,
        )
        yield "", {
            "capability": resolved_owner_ref,
            "conversation_id": conversation_id,
        }

        if is_new_conversation and self._on_conversation_created is not None:
            try:
                await self._on_conversation_created(canonical_oid, conversation_id)
            except Exception:
                log_boundary_failure(logger,
                    "Panel owner-index write failed for a newly created "
                    "hosted conversation; continuing the turn (a missed "
                    "write hides the panel list row and denies panel "
                    "read/feedback/delete access, "
                    "never content disclosure). Owner-index repair is required; "
                    "the index write was not confirmed and no automatic repair was attempted."
                )

        lock = await self._locks.acquire(conversation_id)
        async with lock:
            if await self._idempotency.already_applied(conversation_id, client_turn_id):
                raise ContinuityPersistenceError(
                    "This turn was already applied to the managed conversation; "
                    "refusing to process a duplicate client turn id."
                )

            try:
                history_items = await self._store.list_items_ascending(
                    user_access_token=user_access_token,
                    oid=canonical_oid,
                    conversation_id=conversation_id,
                    limit=self.settings.history_max_items,
                )
            except ConversationStoreError as exc:
                raise ContinuityPersistenceError(
                    "Failed to read the managed conversation history."
                ) from exc

            bounded_history = build_bounded_history(
                history_items,
                max_items=self.settings.history_max_items,
                max_tokens=self.settings.history_max_tokens,
            )
            messages = [
                InvocationMessage(role=item.role, content=item.content)
                for item in bounded_history
            ] + [InvocationMessage(role="user", content=ask)]

            assistant_text = ""
            generator = call_hosted_agent_stream(
                messages,
                conversation_id="",
                question_id=question_id,
                correlation_id=correlation_id,
                user_access_token=user_access_token,
            )
            try:
                async for text_chunk, meta in generator:
                    if text_chunk:
                        assistant_text += text_chunk
                    filtered_meta = dict(meta)
                    filtered_meta.pop("conversation_id", None)
                    yield text_chunk, filtered_meta
            finally:
                try:
                    await generator.aclose()
                except RuntimeError as exc:
                    if "async generator ignored GeneratorExit" not in str(exc):
                        raise

            try:
                await self._store.append_items(
                    user_access_token=user_access_token,
                    oid=canonical_oid,
                    conversation_id=conversation_id,
                    items=[
                        {"type": "message", "role": "user", "content": ask},
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": assistant_text,
                        },
                    ],
                )
            except ConversationStoreError as exc:
                raise ContinuityPersistenceError(
                    "The completed response could not be durably saved to the "
                    "managed conversation."
                ) from exc

            await self._idempotency.mark_applied(conversation_id, client_turn_id)
