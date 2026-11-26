import unittest
import unittest.mock

from gpt_rag_ui.clients.hosted_agent_client import HostedAgentError
from gpt_rag_ui.services.hosted_continuity import (
    ContinuityPersistenceError,
    ConversationNotFoundError,
    HostedContinuityCoordinator,
)
from gpt_rag_ui.config.hosted_continuity_config import HostedContinuitySettings
from gpt_rag_ui.clients.hosted_conversation_capability import (
    ConversationCapabilityManager,
)
from gpt_rag_ui.clients.hosted_conversation_store import (
    ConversationIdempotencyCache,
    ConversationItem,
    ConversationLockRegistry,
    ConversationStoreAccessDeniedError,
    ConversationStoreError,
)

OID_A = "11111111-1111-1111-1111-111111111111"
OID_B = "22222222-2222-2222-2222-222222222222"


def _settings(**overrides) -> HostedContinuitySettings:
    kwargs = dict(
        enabled=True,
        owner_binding="capability",
        capability_key="k" * 32,
        capability_key_id="key-1",
        capability_ttl_seconds=900,
        history_max_items=40,
        history_max_tokens=8000,
        history_truncation="drop_oldest",
        store_base_url="https://agent.example.com/openai/v1",
        store_resource_scope="api://hosted-agent/.default",
    )
    kwargs.update(overrides)
    return HostedContinuitySettings(**kwargs)


def _delegated_settings(**overrides) -> HostedContinuitySettings:
    """Settings for the preferred/default delegated owner-binding mode,
    gated on the protocol-version + validated-flag attestation (see
    Azure/GPT-RAG#591, "OQ-OWN")."""
    kwargs = dict(
        owner_binding="delegated",
        owner_binding_validated=True,
        protocol_version="2.0.0",
        capability_key="",
        capability_key_id="",
    )
    kwargs.update(overrides)
    return _settings(**kwargs)


class FakeStore:
    """In-memory stand-in for ConversationStoreClient.

    Also simulates the platform's per-oid ownership enforcement used by
    delegated-mode tests: a conversation created with a given ``oid`` denies
    ``list_items_ascending``/``append_items`` calls asserting a *different*
    ``oid`` with ``ConversationStoreAccessDeniedError`` — mirroring the
    live-evidence-confirmed 404/403 behavior (Azure/GPT-RAG#591, "OQ-OWN")
    without needing real network calls.
    """

    def __init__(self):
        self._conversations: dict[str, list[ConversationItem]] = {}
        self._owner_by_conversation: dict[str, str] = {}
        self._next_id = 0
        self.create_calls: list[str] = []
        self.list_calls: list[tuple[str, int]] = []
        self.append_calls: list[tuple[str, list[dict]]] = []
        self.fail_list_for: set[str] = set()
        self.fail_append_for: set[str] = set()

    def _deny_if_owner_mismatch(self, conversation_id: str, oid: str | None) -> None:
        owner = self._owner_by_conversation.get(conversation_id)
        if owner is not None and oid is not None and owner != oid:
            raise ConversationStoreAccessDeniedError(
                "Simulated platform denial: oid does not own this conversation."
            )

    async def create_conversation(
        self, *, user_access_token: str = "", oid: str | None = None
    ) -> str:
        self._next_id += 1
        conversation_id = f"conv-{self._next_id}"
        self._conversations[conversation_id] = []
        if oid is not None:
            self._owner_by_conversation[conversation_id] = oid
        self.create_calls.append(user_access_token)
        return conversation_id

    async def list_items_ascending(
        self,
        *,
        user_access_token: str = "",
        oid: str | None = None,
        conversation_id: str,
        limit: int,
    ) -> list[ConversationItem]:
        self.list_calls.append((conversation_id, limit))
        self._deny_if_owner_mismatch(conversation_id, oid)
        if conversation_id in self.fail_list_for:
            raise ConversationStoreError("simulated read failure")
        return list(self._conversations.get(conversation_id, []))

    async def append_items(
        self,
        *,
        user_access_token: str = "",
        oid: str | None = None,
        conversation_id: str,
        items: list[dict],
    ) -> None:
        self.append_calls.append((conversation_id, items))
        self._deny_if_owner_mismatch(conversation_id, oid)
        if conversation_id in self.fail_append_for:
            raise ConversationStoreError("simulated append failure")
        stored = self._conversations.setdefault(conversation_id, [])
        for item in items:
            stored.append(
                ConversationItem(
                    item_id=f"item-{len(stored)}",
                    role=item["role"],
                    content=item["content"],
                )
            )


async def _fake_stream_factory(chunks):
    """Builds a call_hosted_agent_stream-compatible async generator function
    that ignores its arguments and yields the given (text, meta) chunks,
    while recording the arguments it was called with."""

    calls = []

    async def fake_stream(messages, *, conversation_id="", question_id=None,
                           correlation_id=None, user_access_token=""):
        calls.append(
            {
                "messages": list(messages),
                "conversation_id": conversation_id,
                "question_id": question_id,
                "correlation_id": correlation_id,
            }
        )
        for chunk in chunks:
            yield chunk

    return fake_stream, calls


class ContinuityTestCase(unittest.IsolatedAsyncioTestCase):
    async def _make_coordinator(self, **setting_overrides):
        store = FakeStore()
        settings = _settings(**setting_overrides)
        capabilities = ConversationCapabilityManager(
            key=settings.capability_key,
            key_id=settings.capability_key_id,
            ttl_seconds=settings.capability_ttl_seconds,
        )
        coordinator = HostedContinuityCoordinator(
            settings=settings,
            store=store,
            capability_manager=capabilities,
            locks=ConversationLockRegistry(),
            idempotency=ConversationIdempotencyCache(),
        )
        return coordinator, store, capabilities

    async def _make_delegated_coordinator(self, **setting_overrides):
        """Builds a coordinator under the preferred/default delegated
        owner-binding mode; no capability_manager is required or supplied."""
        store = FakeStore()
        settings = _delegated_settings(**setting_overrides)
        coordinator = HostedContinuityCoordinator(
            settings=settings,
            store=store,
            locks=ConversationLockRegistry(),
            idempotency=ConversationIdempotencyCache(),
        )
        return coordinator, store

    async def _run(self, coordinator, stream_fn, **kwargs):
        import gpt_rag_ui.services.hosted_continuity as hc

        defaults = dict(
            capability="",
            oid=OID_A,
            user_ask="hello",
            client_turn_id="turn-1",
            question_id=None,
            correlation_id=None,
            user_access_token="user-token",
        )
        defaults.update(kwargs)
        frames = []
        with unittest.mock.patch.object(hc, "call_hosted_agent_stream", stream_fn):
            async for frame in coordinator.run_turn(**defaults):
                frames.append(frame)
        return frames


class TestCreateAndMint(ContinuityTestCase):
    async def test_no_capability_creates_conversation_and_mints_new_one(self):
        coordinator, store, capabilities = await self._make_coordinator()
        stream_fn, calls = await _fake_stream_factory([("hi there", {})])
        frames = await self._run(coordinator, stream_fn)

        self.assertEqual(len(store.create_calls), 1)
        first_meta = frames[0][1]
        self.assertIn("capability", first_meta)
        self.assertIn("conversation_id", first_meta)
        # The minted capability must validate for the same oid and resolve
        # to the same conversation id just created.
        validated = capabilities.validate(first_meta["capability"], oid=OID_A)
        self.assertEqual(validated.conversation_id, first_meta["conversation_id"])

    async def test_never_mints_around_a_caller_supplied_conversation_id(self):
        """There is no parameter on run_turn/_resolve_conversation_id that
        accepts a caller-supplied conversation id to mint around — the only
        conversation id a capability is ever minted for is one this
        coordinator itself just created via store.create_conversation()."""
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("ok", {})])
        await self._run(coordinator, stream_fn)
        # Only the coordinator-created id may have been used to mint; the
        # store never received any caller-chosen id as input to create.
        self.assertEqual(store.create_calls, ["user-token"])


class TestValidContinuation(ContinuityTestCase):
    async def test_valid_capability_continues_same_conversation_without_recreating(self):
        coordinator, store, capabilities = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("first", {})])
        first_frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        capability = first_frames[0][1]["capability"]
        conversation_id = first_frames[0][1]["conversation_id"]

        stream_fn2, _ = await _fake_stream_factory([("second", {})])
        second_frames = await self._run(
            coordinator, stream_fn2, capability=capability, client_turn_id="turn-2"
        )
        self.assertEqual(second_frames[0][1]["conversation_id"], conversation_id)
        # Still only one conversation ever created across both turns.
        self.assertEqual(len(store.create_calls), 1)

    async def test_history_from_prior_turn_is_read_and_forwarded(self):
        coordinator, store, capabilities = await self._make_coordinator()
        stream_fn, calls = await _fake_stream_factory([("first-answer", {})])
        first_frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        capability = first_frames[0][1]["capability"]

        stream_fn2, calls2 = await _fake_stream_factory([("second-answer", {})])
        await self._run(
            coordinator, stream_fn2, capability=capability, client_turn_id="turn-2",
            user_ask="follow up question",
        )
        sent_messages = calls2[0]["messages"]
        contents = [m.content for m in sent_messages]
        self.assertIn("hello", contents)
        self.assertIn("first-answer", contents)
        self.assertIn("follow up question", contents)


class TestCrossVersionStatelessRequest(ContinuityTestCase):
    async def test_hosted_stream_never_receives_conversation_reference(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, calls = await _fake_stream_factory([("answer", {})])
        await self._run(coordinator, stream_fn)
        self.assertEqual(calls[0]["conversation_id"], "")

    async def test_hosted_runtime_reported_conversation_id_is_stripped(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory(
            [("chunk", {"conversation_id": "hosted-runtime-ephemeral-id"})]
        )
        frames = await self._run(coordinator, stream_fn)
        # The first frame carries the real BFF-owned id; later frames must
        # never carry the hosted runtime's own unrelated conversation id.
        later_meta = frames[1][1]
        self.assertNotIn("conversation_id", later_meta)


class TestCapabilityRejection(ContinuityTestCase):
    """A *presented* (non-empty) capability that fails validation must be
    rejected with the single opaque ``ConversationNotFoundError`` and must
    never mint a replacement conversation, invoke the hosted agent, or
    append anything -- only a genuinely absent capability may create a new
    conversation (see ``TestCreateAndMint``)."""

    async def test_forged_capability_is_rejected_without_creating_a_conversation(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, calls = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(ConversationNotFoundError):
            await self._run(coordinator, stream_fn, capability="forged-garbage")
        self.assertEqual(store.create_calls, [])
        self.assertEqual(store.list_calls, [])
        self.assertEqual(store.append_calls, [])
        self.assertEqual(calls, [])

    async def test_cross_user_stolen_capability_is_rejected_without_creating_a_conversation(self):
        coordinator, store, capabilities = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        victim_frames = await self._run(coordinator, stream_fn, oid=OID_A)
        stolen_capability = victim_frames[0][1]["capability"]
        create_calls_before = len(store.create_calls)

        attacker_stream_fn, attacker_calls = await _fake_stream_factory([("answer2", {})])
        with self.assertRaises(ConversationNotFoundError):
            await self._run(
                coordinator,
                attacker_stream_fn,
                capability=stolen_capability,
                oid=OID_B,
                client_turn_id="turn-attacker",
            )
        # No new conversation was minted for the attacker, and the hosted
        # agent was never invoked for this turn.
        self.assertEqual(len(store.create_calls), create_calls_before)
        self.assertEqual(attacker_calls, [])

    async def test_expired_capability_is_rejected_without_creating_a_conversation(self):
        coordinator, store, capabilities = await self._make_coordinator(
            capability_ttl_seconds=60
        )
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn)
        capability = frames[0][1]["capability"]
        create_calls_before = len(store.create_calls)

        import time
        from unittest.mock import patch

        with patch("time.time", return_value=time.time() + 120):
            stream_fn2, calls2 = await _fake_stream_factory([("answer2", {})])
            with self.assertRaises(ConversationNotFoundError):
                await self._run(
                    coordinator, stream_fn2, capability=capability, client_turn_id="turn-2"
                )
        self.assertEqual(len(store.create_calls), create_calls_before)
        self.assertEqual(calls2, [])

    async def test_capability_signed_under_retired_key_is_rejected_without_creating_a_conversation(self):
        coordinator, store, _ = await self._make_coordinator()
        old_manager = ConversationCapabilityManager(
            key="o" * 32, key_id="old-retired-key", ttl_seconds=900
        )
        stale_capability = old_manager.mint(oid=OID_A, conversation_id="conv-old")
        stream_fn, calls = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(ConversationNotFoundError):
            await self._run(coordinator, stream_fn, capability=stale_capability)
        self.assertEqual(store.create_calls, [])
        self.assertEqual(calls, [])

    async def test_all_rejection_reasons_raise_the_identical_public_error(self):
        """Signature forgery, cross-user, expiry, and retired-key rejections
        must be indistinguishable to the caller: same exception type, same
        message. Varying either would leak which failure mode occurred."""
        coordinator, store, capabilities = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        victim_frames = await self._run(coordinator, stream_fn, oid=OID_A)
        stolen_capability = victim_frames[0][1]["capability"]

        old_manager = ConversationCapabilityManager(
            key="o" * 32, key_id="old-retired-key", ttl_seconds=900
        )
        stale_capability = old_manager.mint(oid=OID_A, conversation_id="conv-old")

        presented_bad_handles = [
            "forged-garbage",
            stale_capability,
        ]
        messages: set[str] = set()
        for handle in presented_bad_handles:
            fn, _ = await _fake_stream_factory([("answer", {})])
            with self.assertRaises(ConversationNotFoundError) as ctx:
                await self._run(coordinator, fn, capability=handle)
            messages.add(str(ctx.exception))

        fn, _ = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(ConversationNotFoundError) as ctx:
            await self._run(
                coordinator, fn, capability=stolen_capability, oid=OID_B,
                client_turn_id="turn-attacker",
            )
        messages.add(str(ctx.exception))

        self.assertEqual(len(messages), 1, f"messages differed: {messages}")

    async def test_only_absent_capability_creates_a_new_conversation(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn, capability="")
        self.assertEqual(len(store.create_calls), 1)
        self.assertIn("capability", frames[0][1])


class TestReadAppendErrors(ContinuityTestCase):
    async def test_read_failure_raises_persistence_error_not_success(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn)
        conversation_id = frames[0][1]["conversation_id"]
        store.fail_list_for.add(conversation_id)

        stream_fn2, _ = await _fake_stream_factory([("answer2", {})])
        capability = frames[0][1]["capability"]
        with self.assertRaises(ContinuityPersistenceError):
            await self._run(
                coordinator, stream_fn2, capability=capability, client_turn_id="turn-2"
            )

    async def test_append_failure_raises_persistence_error_after_streaming(self):
        """Proves the no-success-shaped-completion-on-append-failure
        requirement: the text still streams (it's already been generated by
        the hosted runtime) but the coordinator must raise before the caller
        can treat the turn as durably committed."""
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn)
        conversation_id = frames[0][1]["conversation_id"]
        store.fail_append_for.add(conversation_id)

        stream_fn2, _ = await _fake_stream_factory([("second-answer", {})])
        capability = frames[0][1]["capability"]
        import gpt_rag_ui.services.hosted_continuity as hc

        collected = []
        with self.assertRaises(ContinuityPersistenceError):
            with unittest.mock.patch.object(hc, "call_hosted_agent_stream", stream_fn2):
                async for chunk, meta in coordinator.run_turn(
                    capability=capability,
                    oid=OID_A,
                    user_ask="hi again",
                    client_turn_id="turn-2",
                    question_id=None,
                    correlation_id=None,
                    user_access_token="user-token",
                ):
                    collected.append((chunk, meta))
        # The text did stream before the append failure surfaced.
        self.assertTrue(any(chunk == "second-answer" for chunk, _ in collected))


class TestIdempotency(ContinuityTestCase):
    async def test_duplicate_client_turn_id_is_rejected_without_reappending(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        capability = frames[0][1]["capability"]
        append_count_before = len(store.append_calls)

        stream_fn2, _ = await _fake_stream_factory([("answer-retry", {})])
        with self.assertRaises(ContinuityPersistenceError):
            await self._run(
                coordinator, stream_fn2, capability=capability, client_turn_id="turn-1"
            )
        self.assertEqual(len(store.append_calls), append_count_before)


class TestOneInFlightConcurrency(ContinuityTestCase):
    async def test_concurrent_turns_on_same_conversation_are_serialized(self):
        import asyncio

        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        capability = frames[0][1]["capability"]

        order: list[str] = []

        async def dispatch_stream(messages, *, conversation_id="", question_id=None,
                                   correlation_id=None, user_access_token=""):
            # A single patched function dispatches on the last message's
            # content so both concurrent turns can be driven by one
            # unittest.mock.patch context -- patching the same target twice
            # concurrently (once per coroutine) would itself race, since
            # patch.object save/restore is not coroutine-aware.
            is_slow = messages[-1].content == "a"
            order.append("start")
            if is_slow:
                await asyncio.sleep(0.02)
            order.append("end")
            yield ("slow-answer" if is_slow else "fast-answer", {})

        import gpt_rag_ui.services.hosted_continuity as hc

        async def run_slow():
            async for _ in coordinator.run_turn(
                capability=capability, oid=OID_A, user_ask="a",
                client_turn_id="turn-slow", question_id=None,
                correlation_id=None, user_access_token="user-token",
            ):
                pass

        async def run_fast():
            async for _ in coordinator.run_turn(
                capability=capability, oid=OID_A, user_ask="b",
                client_turn_id="turn-fast", question_id=None,
                correlation_id=None, user_access_token="user-token",
            ):
                pass

        with unittest.mock.patch.object(hc, "call_hosted_agent_stream", dispatch_stream):
            await asyncio.gather(run_slow(), run_fast())
        self.assertEqual(order, ["start", "end", "start", "end"])


class TestInputValidation(ContinuityTestCase):
    async def test_rejects_empty_user_ask(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(HostedAgentError):
            await self._run(coordinator, stream_fn, user_ask="   ")

    async def test_rejects_missing_client_turn_id(self):
        coordinator, store, _ = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(HostedAgentError):
            await self._run(coordinator, stream_fn, client_turn_id="")

    async def test_coordinator_construction_requires_enabled_settings(self):
        store = FakeStore()
        capabilities = ConversationCapabilityManager(
            key="k" * 32, key_id="key-1", ttl_seconds=900
        )
        with self.assertRaises(ValueError):
            HostedContinuityCoordinator(
                settings=_settings(enabled=False),
                store=store,
                capability_manager=capabilities,
            )


class TestDelegatedOwnerBinding(ContinuityTestCase):
    """Coordinator-level tests for the preferred/default delegated
    owner-binding mode (Azure/GPT-RAG#591, "OQ-OWN"): the client-held handle
    is the raw managed conversation id, and per-user ownership is enforced by
    the platform itself via the ``x-ms-user-identity`` header (derived only
    from the validated Entra ``oid``), not by any BFF-issued signature."""

    async def test_coordinator_construction_does_not_require_capability_manager(self):
        """Delegated mode has no capability_manager dependency at all -- the
        opposite failure mode of capability mode's requirement."""
        store = FakeStore()
        coordinator = HostedContinuityCoordinator(
            settings=_delegated_settings(),
            store=store,
        )
        self.assertIsNotNone(coordinator)

    async def test_no_owner_ref_creates_conversation_and_returns_raw_id(self):
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("hi there", {})])
        frames = await self._run(coordinator, stream_fn, capability="")

        self.assertEqual(len(store.create_calls), 1)
        first_meta = frames[0][1]
        # In delegated mode the returned handle is simply the real
        # conversation id -- no cryptographic wrapping to validate locally.
        self.assertEqual(first_meta["capability"], first_meta["conversation_id"])

    async def test_valid_continuation_reuses_the_same_raw_conversation_id(self):
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("first", {})])
        first_frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        owner_ref = first_frames[0][1]["capability"]
        conversation_id = first_frames[0][1]["conversation_id"]

        stream_fn2, _ = await _fake_stream_factory([("second", {})])
        second_frames = await self._run(
            coordinator, stream_fn2, capability=owner_ref, client_turn_id="turn-2"
        )
        self.assertEqual(second_frames[0][1]["conversation_id"], conversation_id)
        # Still only one conversation ever created across both turns -- the
        # raw id round-trips as a valid continuation reference.
        self.assertEqual(len(store.create_calls), 1)

    async def test_history_from_prior_turn_is_forwarded_under_delegated_mode(self):
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("first-answer", {})])
        first_frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        owner_ref = first_frames[0][1]["capability"]

        stream_fn2, calls2 = await _fake_stream_factory([("second-answer", {})])
        await self._run(
            coordinator, stream_fn2, capability=owner_ref, client_turn_id="turn-2",
            user_ask="follow up question",
        )
        contents = [m.content for m in calls2[0]["messages"]]
        self.assertIn("hello", contents)
        self.assertIn("first-answer", contents)
        self.assertIn("follow up question", contents)

    async def test_cross_user_raw_id_is_rejected_without_creating_a_conversation(self):
        """A stolen/guessed raw conversation id cannot bypass platform
        enforcement: replaying it under a *different* oid must not resolve
        to the original conversation -- the platform (simulated here by
        FakeStore's owner-mismatch check) denies it, and per ADR-0003 the
        coordinator must fail closed with the opaque not-found error rather
        than ever minting a fresh conversation or invoking the hosted
        agent for the attacker's turn."""
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        victim_frames = await self._run(coordinator, stream_fn, oid=OID_A)
        stolen_id = victim_frames[0][1]["conversation_id"]
        create_calls_before = len(store.create_calls)
        append_calls_before = len(store.append_calls)

        attacker_stream_fn, attacker_calls = await _fake_stream_factory([("answer2", {})])
        with self.assertRaises(ConversationNotFoundError):
            await self._run(
                coordinator,
                attacker_stream_fn,
                capability=stolen_id,
                oid=OID_B,
                client_turn_id="turn-attacker",
            )
        self.assertEqual(len(store.create_calls), create_calls_before)
        self.assertEqual(len(store.append_calls), append_calls_before)
        self.assertEqual(attacker_calls, [])

    async def test_arbitrary_caller_supplied_id_that_was_never_created_is_rejected(self):
        """A raw id the BFF never created (pure guess, not even stolen from
        a real prior turn) must also be rejected rather than trusted -- the
        read probe against the platform is what decides validity, never the
        mere shape of the client-supplied string. It must not mint a new
        conversation or invoke the hosted agent either."""
        coordinator, store = await self._make_delegated_coordinator()
        store.fail_list_for.add("guessed-conv-id")
        # Force the probe read to look like a platform denial rather than a
        # generic transient error, matching real ConversationStoreAccessDeniedError
        # behavior on a nonexistent/foreign id.
        original = store.list_items_ascending

        async def deny_unknown(*, conversation_id, **kwargs):
            if conversation_id == "guessed-conv-id":
                raise ConversationStoreAccessDeniedError("unknown conversation")
            return await original(conversation_id=conversation_id, **kwargs)

        store.list_items_ascending = deny_unknown  # type: ignore[method-assign]

        stream_fn, calls = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(ConversationNotFoundError):
            await self._run(
                coordinator, stream_fn, capability="guessed-conv-id", oid=OID_A
            )
        self.assertEqual(store.create_calls, [])
        self.assertEqual(store.append_calls, [])
        self.assertEqual(calls, [])

    async def test_malformed_id_is_rejected_the_same_way_as_cross_user_id(self):
        """A malformed/garbage presented id (not even a plausible id shape)
        must be rejected through the exact same opaque error and code path
        as a cross-user id -- the platform read probe is the only source of
        truth, and the failure must be indistinguishable either way."""
        coordinator, store = await self._make_delegated_coordinator()
        store.fail_list_for.add("!!!not-a-real-id???")

        original = store.list_items_ascending

        async def deny_malformed(*, conversation_id, **kwargs):
            if conversation_id == "!!!not-a-real-id???":
                raise ConversationStoreAccessDeniedError("malformed id")
            return await original(conversation_id=conversation_id, **kwargs)

        store.list_items_ascending = deny_malformed  # type: ignore[method-assign]

        stream_fn, calls = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(ConversationNotFoundError):
            await self._run(
                coordinator, stream_fn, capability="!!!not-a-real-id???", oid=OID_A
            )
        self.assertEqual(store.create_calls, [])
        self.assertEqual(calls, [])

    async def test_cross_user_and_guessed_id_raise_the_identical_public_error(self):
        """Cross-user and pure-guess rejections must be indistinguishable to
        the caller -- same exception type, same message -- so a client can
        never use response differences as an existence oracle."""
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        victim_frames = await self._run(coordinator, stream_fn, oid=OID_A)
        stolen_id = victim_frames[0][1]["conversation_id"]

        store.fail_list_for.add("guessed-conv-id")
        original = store.list_items_ascending

        async def deny_unknown(*, conversation_id, **kwargs):
            if conversation_id == "guessed-conv-id":
                raise ConversationStoreAccessDeniedError("unknown conversation")
            return await original(conversation_id=conversation_id, **kwargs)

        store.list_items_ascending = deny_unknown  # type: ignore[method-assign]

        messages: set[str] = set()

        fn1, _ = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(ConversationNotFoundError) as ctx1:
            await self._run(
                coordinator, fn1, capability=stolen_id, oid=OID_B,
                client_turn_id="turn-attacker",
            )
        messages.add(str(ctx1.exception))

        fn2, _ = await _fake_stream_factory([("answer", {})])
        with self.assertRaises(ConversationNotFoundError) as ctx2:
            await self._run(
                coordinator, fn2, capability="guessed-conv-id", oid=OID_A,
                client_turn_id="turn-guess",
            )
        messages.add(str(ctx2.exception))

        self.assertEqual(len(messages), 1, f"messages differed: {messages}")

    async def test_only_absent_owner_ref_creates_a_new_conversation(self):
        """Restates the create path from the top of this class as an
        explicit contrast with the fail-closed presented-handle tests
        above: only a genuinely empty owner_ref may create."""
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn, capability="")
        self.assertEqual(len(store.create_calls), 1)
        self.assertEqual(frames[0][1]["capability"], frames[0][1]["conversation_id"])

    async def test_generic_transient_read_error_during_probe_is_not_a_404(self):
        """A generic transport/5xx failure surfaced by the probe read must
        remain an explicit dependency/persistence error -- never silently
        mapped to the opaque not-found error, and never treated as license
        to mint a fresh conversation."""
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        victim_frames = await self._run(coordinator, stream_fn, oid=OID_A)
        conversation_id = victim_frames[0][1]["conversation_id"]
        create_calls_before = len(store.create_calls)

        store.fail_list_for.add(conversation_id)
        stream_fn2, calls2 = await _fake_stream_factory([("answer2", {})])
        with self.assertRaises(ConversationStoreError) as ctx:
            await self._run(
                coordinator, stream_fn2, capability=conversation_id, oid=OID_A,
                client_turn_id="turn-2",
            )
        # A generic transient error is a plain ConversationStoreError, never
        # the opaque not-found error used for rejected identity/ownership
        # checks.
        self.assertNotIsInstance(ctx.exception, ConversationNotFoundError)
        self.assertEqual(len(store.create_calls), create_calls_before)
        self.assertEqual(calls2, [])

    async def test_header_identity_is_always_the_normalized_canonical_oid(self):
        """The identity asserted on every store call must be derived only
        from the validated token oid (normalized), never any raw/differently
        -cased value the caller happened to pass in."""
        coordinator, store = await self._make_delegated_coordinator()
        mixed_case_oid = OID_A.upper()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        await self._run(coordinator, stream_fn, oid=mixed_case_oid)

        # The conversation's recorded owner must be the canonical
        # (lowercase) form, regardless of the case the caller supplied.
        conversation_id = next(iter(store._owner_by_conversation))
        self.assertEqual(store._owner_by_conversation[conversation_id], OID_A)

    async def test_read_or_append_error_unrelated_to_ownership_still_fails_closed(self):
        """A denial-unrelated store failure during the main per-turn history
        read (after the resolution probe already succeeded) must still be a
        hard failure, never a silent new thread or success-shaped result."""
        coordinator, store = await self._make_delegated_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])
        frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        owner_ref = frames[0][1]["capability"]
        conversation_id = frames[0][1]["conversation_id"]

        original = store.list_items_ascending

        async def fail_only_main_read(*, conversation_id, limit, **kwargs):
            # limit == 1 is the resolution probe (must keep succeeding so the
            # test isolates the *main* per-turn history read failure); any
            # other limit is the main read this test targets.
            if limit != 1:
                raise ConversationStoreError("simulated main read failure")
            return await original(conversation_id=conversation_id, limit=limit, **kwargs)

        store.list_items_ascending = fail_only_main_read  # type: ignore[method-assign]

        stream_fn2, _ = await _fake_stream_factory([("answer2", {})])
        with self.assertRaises(ContinuityPersistenceError):
            await self._run(
                coordinator, stream_fn2, capability=owner_ref, client_turn_id="turn-2"
            )


class TestNoSecretOrRawIdLeakageInLogs(ContinuityTestCase):
    async def test_no_capability_token_secret_or_user_access_token_is_logged(self):
        """Drives a full successful turn plus a rejected-capability turn
        while capturing every log record emitted by hosted_continuity's and
        hosted_conversation_store's loggers, then asserts that neither the
        signing key, the minted opaque capability token, nor the delegated
        user access token value ever appears in a log message."""
        import logging

        coordinator, store, capabilities = await self._make_coordinator()
        stream_fn, _ = await _fake_stream_factory([("answer", {})])

        secret_key = "k" * 32
        user_token = "super-secret-user-access-token-value"

        records: list[str] = []

        class _CollectingHandler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        handler = _CollectingHandler()
        loggers = [
            logging.getLogger("gpt_rag_ui.hosted_continuity"),
            logging.getLogger("gpt_rag_ui.hosted_conversation_store"),
        ]
        for lg in loggers:
            lg.addHandler(handler)
            lg.setLevel(logging.DEBUG)
        try:
            frames = await self._run(
                coordinator, stream_fn, user_access_token=user_token
            )
            capability = frames[0][1]["capability"]

            # Also drive a rejected (forged) capability path, which logs an
            # informational message -- this must still never leak secrets.
            stream_fn2, _ = await _fake_stream_factory([("answer2", {})])
            with self.assertRaises(ConversationNotFoundError):
                await self._run(
                    coordinator,
                    stream_fn2,
                    capability="forged-" + capability,
                    client_turn_id="turn-2",
                    user_access_token=user_token,
                )
        finally:
            for lg in loggers:
                lg.removeHandler(handler)

        joined = "\n".join(records)
        self.assertNotIn(secret_key, joined)
        self.assertNotIn(user_token, joined)
        self.assertNotIn(capability, joined)


if __name__ == "__main__":
    unittest.main()
