"""Tests for the optional panel owner-index creation hook on
``HostedContinuityCoordinator`` (issue #611, ADR-0004): invoked exactly
once for a brand-new conversation, never for a continued one, and
best-effort (a failure never fails the user's turn)."""

import unittest
import unittest.mock

from gpt_rag_ui.services import hosted_continuity as hc
from gpt_rag_ui.clients.hosted_conversation_capability import ConversationCapabilityManager
from gpt_rag_ui.clients.hosted_conversation_store import ConversationIdempotencyCache, ConversationLockRegistry

from test_hosted_continuity import FakeStore, _fake_stream_factory, _settings

OID_A = "11111111-1111-1111-1111-111111111111"


class OwnerIndexHookTests(unittest.IsolatedAsyncioTestCase):
    async def _make_coordinator(self, on_conversation_created=None):
        store = FakeStore()
        settings = _settings()
        capabilities = ConversationCapabilityManager(
            key=settings.capability_key,
            key_id=settings.capability_key_id,
            ttl_seconds=settings.capability_ttl_seconds,
        )
        coordinator = hc.HostedContinuityCoordinator(
            settings=settings,
            store=store,
            capability_manager=capabilities,
            locks=ConversationLockRegistry(),
            idempotency=ConversationIdempotencyCache(),
            on_conversation_created=on_conversation_created,
        )
        return coordinator, store

    async def _run(self, coordinator, stream_fn, **kwargs):
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

    async def test_hook_invoked_once_for_new_conversation(self):
        calls = []

        async def hook(oid, conversation_id):
            calls.append((oid, conversation_id))

        coordinator, store = await self._make_coordinator(on_conversation_created=hook)
        stream_fn, _ = await _fake_stream_factory([("hi", {})])
        frames = await self._run(coordinator, stream_fn)

        conversation_id = frames[0][1]["conversation_id"]
        self.assertEqual(calls, [(OID_A, conversation_id)])

    async def test_hook_not_invoked_when_continuing_existing_conversation(self):
        calls = []

        async def hook(oid, conversation_id):
            calls.append((oid, conversation_id))

        coordinator, store = await self._make_coordinator(on_conversation_created=hook)
        stream_fn, _ = await _fake_stream_factory([("first", {})])
        first_frames = await self._run(coordinator, stream_fn, client_turn_id="turn-1")
        capability = first_frames[0][1]["capability"]
        self.assertEqual(len(calls), 1)

        stream_fn2, _ = await _fake_stream_factory([("second", {})])
        await self._run(
            coordinator, stream_fn2, capability=capability, client_turn_id="turn-2"
        )
        # Still only the single call from conversation creation -- never
        # re-invoked for a continued turn.
        self.assertEqual(len(calls), 1)

    async def test_hook_failure_does_not_fail_the_turn(self):
        async def failing_hook(oid, conversation_id):
            raise RuntimeError("simulated panel Cosmos outage")

        coordinator, store = await self._make_coordinator(
            on_conversation_created=failing_hook
        )
        stream_fn, _ = await _fake_stream_factory([("hi there", {})])
        frames = await self._run(coordinator, stream_fn)

        # The turn must still complete normally despite the hook failing.
        self.assertTrue(any(text == "hi there" for text, _ in frames))

    async def test_none_hook_is_a_safe_no_op(self):
        coordinator, store = await self._make_coordinator(on_conversation_created=None)
        stream_fn, _ = await _fake_stream_factory([("hi", {})])
        frames = await self._run(coordinator, stream_fn)
        self.assertTrue(frames)

    async def test_failed_index_write_hides_and_denies_panel_operations(self):
        from gpt_rag_ui.clients.panel_cosmos import PanelStoreError
        from gpt_rag_ui.services.panel_store import upsert_owner_index_row
        from test_panel_routes import _Harness

        harness = _Harness()
        self.addCleanup(harness.close)

        async def hook(oid, conversation_id):
            await upsert_owner_index_row(
                client=harness.cosmos,
                principal_id=oid,
                conversation_id=conversation_id,
            )

        coordinator, _ = await self._make_coordinator(on_conversation_created=hook)
        stream_fn, _ = await _fake_stream_factory([("hi there", {})])
        with unittest.mock.patch.object(
            harness.cosmos,
            "upsert_item",
            side_effect=PanelStoreError("owner index unavailable"),
        ) as write, self.assertLogs(level="ERROR") as logs:
            frames = await self._run(coordinator, stream_fn)

        write.assert_awaited_once()
        self.assertTrue(any(text == "hi there" for text, _ in frames))
        self.assertIn("denies panel read/feedback/delete access", " ".join(logs.output))
        self.assertIn("Owner-index repair is required", " ".join(logs.output))
        self.assertIn("index write was not confirmed", " ".join(logs.output))
        self.assertIn("no automatic repair was attempted", " ".join(logs.output))
        conversation_id = frames[0][1]["conversation_id"]
        harness.store.seed(conversation_id, OID_A, [("assistant", "hi there")])
        headers = harness.auth(OID_A)
        response = harness.client.get("/panel/conversations", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])

        prefix = f"/panel/conversations/{conversation_id}"
        responses = [
            harness.client.get(f"{prefix}/messages", headers=headers),
            harness.client.get(f"{prefix}/feedback", headers=headers),
            harness.client.post(
                f"{prefix}/feedback",
                headers=headers,
                json={"feedback_id": "fb-1", "message_ref": "item-0"},
            ),
            harness.client.delete(prefix, headers=headers),
        ]
        self.assertEqual([response.status_code for response in responses], [404] * 4)
        self.assertEqual(harness.store.list_calls, [])
        self.assertEqual(harness.store.delete_calls, [])
        self.assertEqual(harness.cosmos.all_documents(), [])


if __name__ == "__main__":
    unittest.main()
