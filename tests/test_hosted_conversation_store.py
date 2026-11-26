import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from gpt_rag_ui.clients.hosted_conversation_store import (
    ConversationIdempotencyCache,
    ConversationItem,
    ConversationLockRegistry,
    ConversationStoreAccessDeniedError,
    ConversationStoreAuthenticationError,
    ConversationStoreClient,
    ConversationStoreHTTPError,
    ConversationStoreSettings,
    build_bounded_history,
)


def _settings() -> ConversationStoreSettings:
    return ConversationStoreSettings(
        base_url="https://agent.example.com/openai/v1",
        resource_scope="api://hosted-agent/.default",
    )


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _client_with(handler, *, owner_binding: str = "capability") -> tuple[ConversationStoreClient, FakeTransport]:
    transport = FakeTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = ConversationStoreClient(
        _settings(), owner_binding=owner_binding, http_client=http_client
    )
    return client, transport


class TestConversationStoreClient(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = patch(
            "gpt_rag_ui.clients.hosted_conversation_store.acquire_obo_token",
            new=AsyncMock(return_value="delegated-token"),
        )
        self.mock_obo = patcher.start()
        self.addCleanup(patcher.stop)

    async def test_create_conversation_returns_id_and_uses_bearer_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://agent.example.com/openai/v1/conversations")
            self.assertEqual(request.headers["Authorization"], "Bearer delegated-token")
            return httpx.Response(200, json={"id": "conv-abc"})

        client, transport = _client_with(handler)
        conversation_id = await client.create_conversation(user_access_token="user-token")
        self.assertEqual(conversation_id, "conv-abc")
        self.mock_obo.assert_awaited_once_with("user-token", "api://hosted-agent/.default")
        await client.aclose()

    async def test_create_conversation_requires_user_access_token(self):
        client, _ = _client_with(lambda request: httpx.Response(200, json={"id": "x"}))
        with self.assertRaises(ConversationStoreAuthenticationError):
            await client.create_conversation(user_access_token="")
        await client.aclose()

    async def test_create_conversation_raises_on_http_error(self):
        client, _ = _client_with(lambda request: httpx.Response(500))
        with self.assertRaises(ConversationStoreHTTPError):
            await client.create_conversation(user_access_token="user-token")
        await client.aclose()

    async def test_create_conversation_raises_when_id_missing(self):
        client, _ = _client_with(lambda request: httpx.Response(200, json={}))
        with self.assertRaises(ConversationStoreHTTPError):
            await client.create_conversation(user_access_token="user-token")
        await client.aclose()

    async def test_list_items_ascending_returns_ordered_items(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.params["order"], "asc")
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "i1", "role": "user", "content": "hi"},
                        {"id": "i2", "role": "assistant", "content": "hello"},
                    ]
                },
            )

        client, _ = _client_with(handler)
        items = await client.list_items_ascending(
            user_access_token="user-token", conversation_id="conv-1", limit=10
        )
        self.assertEqual(
            items,
            [
                ConversationItem(item_id="i1", role="user", content="hi"),
                ConversationItem(item_id="i2", role="assistant", content="hello"),
            ],
        )
        await client.aclose()

    async def test_list_items_skips_unsupported_roles(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "i0", "role": "system", "content": "ignored"},
                        {"id": "i1", "role": "user", "content": "hi"},
                    ]
                },
            )

        client, _ = _client_with(handler)
        items = await client.list_items_ascending(
            user_access_token="user-token", conversation_id="conv-1", limit=10
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].role, "user")
        await client.aclose()

    async def test_list_items_extracts_content_list_text_parts(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "i1",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "part-a "},
                                {"type": "output_text", "text": "part-b"},
                            ],
                        },
                    ]
                },
            )

        client, _ = _client_with(handler)
        items = await client.list_items_ascending(
            user_access_token="user-token", conversation_id="conv-1", limit=10
        )
        self.assertEqual(items[0].content, "part-a part-b")
        await client.aclose()

    async def test_list_items_raises_on_404(self):
        client, _ = _client_with(lambda request: httpx.Response(404))
        with self.assertRaises(ConversationStoreAccessDeniedError):
            await client.list_items_ascending(
                user_access_token="user-token", conversation_id="missing", limit=10
            )
        await client.aclose()

    async def test_list_items_raises_on_malformed_payload(self):
        client, _ = _client_with(lambda request: httpx.Response(200, json={"data": "not-a-list"}))
        with self.assertRaises(ConversationStoreHTTPError):
            await client.list_items_ascending(
                user_access_token="user-token", conversation_id="conv-1", limit=10
            )
        await client.aclose()

    async def test_append_items_posts_items_payload(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": []})

        client, transport = _client_with(handler)
        await client.append_items(
            user_access_token="user-token",
            conversation_id="conv-1",
            items=[{"type": "message", "role": "user", "content": "hi"}],
        )
        self.assertEqual(len(transport.requests), 1)
        request = transport.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://agent.example.com/openai/v1/conversations/conv-1/items")
        self.assertEqual(json.loads(request.content), {
            "items": [{"type": "message", "role": "user", "content": "hi"}],
        })
        await client.aclose()

    async def test_append_items_noop_for_empty_items(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={})

        client, _ = _client_with(handler)
        await client.append_items(
            user_access_token="user-token", conversation_id="conv-1", items=[]
        )
        self.assertEqual(len(calls), 0)
        await client.aclose()

    async def test_append_items_raises_on_http_error_fail_closed(self):
        client, _ = _client_with(lambda request: httpx.Response(503))
        with self.assertRaises(ConversationStoreHTTPError):
            await client.append_items(
                user_access_token="user-token",
                conversation_id="conv-1",
                items=[{"type": "message", "role": "user", "content": "hi"}],
            )
        await client.aclose()

    async def test_delete_conversation_treats_404_as_success(self):
        client, _ = _client_with(lambda request: httpx.Response(404))
        await client.delete_conversation(
            user_access_token="user-token", conversation_id="conv-1"
        )
        await client.aclose()

    async def test_delete_conversation_raises_on_other_errors(self):
        client, _ = _client_with(lambda request: httpx.Response(500))
        with self.assertRaises(ConversationStoreHTTPError):
            await client.delete_conversation(
                user_access_token="user-token", conversation_id="conv-1"
            )
        await client.aclose()


class TestConversationStoreDelegatedOwnerBinding(unittest.IsolatedAsyncioTestCase):
    """Covers HOSTED_CONVERSATION_OWNER_BINDING=delegated: a trusted
    middle-tier service-identity bearer plus an x-ms-user-identity header
    derived only from the caller's validated oid (Azure/GPT-RAG#591,
    "OQ-OWN")."""

    _OID = "11111111-1111-1111-1111-111111111111"

    async def asyncSetUp(self):
        patcher = patch(
            "gpt_rag_ui.clients.hosted_conversation_store.acquire_service_identity_token",
            new=AsyncMock(return_value="service-token"),
        )
        self.mock_service_token = patcher.start()
        self.addCleanup(patcher.stop)
        obo_patcher = patch(
            "gpt_rag_ui.clients.hosted_conversation_store.acquire_obo_token",
            new=AsyncMock(side_effect=AssertionError("OBO must not be used in delegated mode")),
        )
        obo_patcher.start()
        self.addCleanup(obo_patcher.stop)

    async def test_create_conversation_uses_service_bearer_and_identity_header(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["Authorization"], "Bearer service-token")
            self.assertEqual(request.headers["x-ms-user-identity"], self._OID)
            return httpx.Response(200, json={"id": "conv-abc"})

        client, _ = _client_with(handler, owner_binding="delegated")
        conversation_id = await client.create_conversation(oid=self._OID)
        self.assertEqual(conversation_id, "conv-abc")
        self.mock_service_token.assert_awaited_once_with("api://hosted-agent/.default")
        await client.aclose()

    async def test_header_is_derived_only_from_oid_never_from_other_input(self):
        # Passing an unrelated (and intentionally wrong-shaped) user_access_token
        # must have zero effect on the header value derived from oid: the
        # header must never be sourced from any client/browser-controlled
        # field other than the server-validated oid.
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["identity"] = request.headers["x-ms-user-identity"]
            return httpx.Response(200, json={"id": "conv-abc"})

        client, _ = _client_with(handler, owner_binding="delegated")
        await client.create_conversation(
            user_access_token="attacker-controlled-value-should-be-ignored",
            oid=self._OID,
        )
        self.assertEqual(captured["identity"], self._OID)
        await client.aclose()

    async def test_header_normalizes_oid_casing(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["identity"] = request.headers["x-ms-user-identity"]
            return httpx.Response(200, json={"id": "conv-abc"})

        client, _ = _client_with(handler, owner_binding="delegated")
        await client.create_conversation(oid=self._OID.upper())
        self.assertEqual(captured["identity"], self._OID)
        await client.aclose()

    async def test_missing_oid_is_rejected_before_any_request(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"id": "conv-abc"})

        client, _ = _client_with(handler, owner_binding="delegated")
        with self.assertRaises(ConversationStoreAuthenticationError):
            await client.create_conversation(oid="")
        self.assertEqual(len(calls), 0, "must fail closed before any network call")
        await client.aclose()

    async def test_malformed_oid_is_rejected(self):
        client, _ = _client_with(
            lambda request: httpx.Response(200, json={"id": "conv-abc"}),
            owner_binding="delegated",
        )
        with self.assertRaises(ConversationStoreAuthenticationError):
            await client.create_conversation(oid="not-a-guid")
        await client.aclose()

    async def test_cross_user_read_returns_access_denied_not_a_generic_error(self):
        # Simulates the platform's own per-user ownership enforcement: a
        # conversation id that does not belong to the currently asserted oid
        # is denied (404), distinctly from a generic infrastructure failure.
        client, _ = _client_with(lambda request: httpx.Response(404), owner_binding="delegated")
        with self.assertRaises(ConversationStoreAccessDeniedError):
            await client.list_items_ascending(
                oid=self._OID, conversation_id="someone-elses-conversation", limit=10
            )
        await client.aclose()

    async def test_raw_conversation_id_cannot_bypass_platform_enforcement(self):
        # Even a plausible-looking, guessed/stolen raw conversation id must
        # not be trusted by this client: the platform itself (simulated here
        # via a 403) is what enforces ownership, not any local heuristic. No
        # special-case in this client accepts a caller-asserted "trust me,
        # this id is mine" claim.
        client, _ = _client_with(lambda request: httpx.Response(403), owner_binding="delegated")
        with self.assertRaises(ConversationStoreAccessDeniedError):
            await client.list_items_ascending(
                oid=self._OID, conversation_id="raw-guessed-id", limit=10
            )
        await client.aclose()

    async def test_non_delegated_missing_header_scenario_is_still_403_surfaced(self):
        # Defensive: if a deployment's platform ever returns 401 for a
        # missing/invalid header scenario, it must still surface as
        # access-denied, not a silent success or an ambiguous generic error.
        client, _ = _client_with(lambda request: httpx.Response(401), owner_binding="delegated")
        with self.assertRaises(ConversationStoreAccessDeniedError):
            await client.list_items_ascending(
                oid=self._OID, conversation_id="conv-1", limit=10
            )
        await client.aclose()

    async def test_append_and_delete_also_use_identity_header(self):
        seen_headers = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_headers.append(request.headers.get("x-ms-user-identity"))
            return httpx.Response(200, json={})

        client, _ = _client_with(handler, owner_binding="delegated")
        await client.append_items(
            oid=self._OID,
            conversation_id="conv-1",
            items=[{"type": "message", "role": "user", "content": "hi"}],
        )
        await client.delete_conversation(oid=self._OID, conversation_id="conv-1")
        self.assertEqual(seen_headers, [self._OID, self._OID])
        await client.aclose()


class TestConversationLockRegistry(unittest.IsolatedAsyncioTestCase):
    async def test_same_conversation_returns_same_lock(self):
        registry = ConversationLockRegistry()
        lock_a = await registry.acquire("conv-1")
        lock_b = await registry.acquire("conv-1")
        self.assertIs(lock_a, lock_b)

    async def test_different_conversations_return_different_locks(self):
        registry = ConversationLockRegistry()
        lock_a = await registry.acquire("conv-1")
        lock_b = await registry.acquire("conv-2")
        self.assertIsNot(lock_a, lock_b)

    async def test_lock_serializes_concurrent_turns_for_same_conversation(self):
        import asyncio

        registry = ConversationLockRegistry()
        order: list[str] = []

        async def turn(name: str, delay: float):
            lock = await registry.acquire("conv-1")
            async with lock:
                order.append(f"{name}-start")
                await asyncio.sleep(delay)
                order.append(f"{name}-end")

        await asyncio.gather(turn("a", 0.02), turn("b", 0.0))
        # One-in-flight: the second turn must not start until the first ends.
        self.assertEqual(order, ["a-start", "a-end", "b-start", "b-end"])


class TestConversationIdempotencyCache(unittest.IsolatedAsyncioTestCase):
    async def test_marks_and_detects_applied_turn(self):
        cache = ConversationIdempotencyCache()
        self.assertFalse(await cache.already_applied("conv-1", "turn-1"))
        await cache.mark_applied("conv-1", "turn-1")
        self.assertTrue(await cache.already_applied("conv-1", "turn-1"))

    async def test_distinct_conversations_do_not_collide(self):
        cache = ConversationIdempotencyCache()
        await cache.mark_applied("conv-1", "turn-1")
        self.assertFalse(await cache.already_applied("conv-2", "turn-1"))

    async def test_bounded_cache_evicts_oldest_entries(self):
        cache = ConversationIdempotencyCache(max_entries=2)
        await cache.mark_applied("conv-1", "turn-1")
        await cache.mark_applied("conv-1", "turn-2")
        await cache.mark_applied("conv-1", "turn-3")
        self.assertFalse(await cache.already_applied("conv-1", "turn-1"))
        self.assertTrue(await cache.already_applied("conv-1", "turn-3"))


class TestBuildBoundedHistory(unittest.TestCase):
    def _items(self, count: int) -> list[ConversationItem]:
        return [
            ConversationItem(item_id=f"i{i}", role="user", content=f"message-{i}")
            for i in range(count)
        ]

    def test_keeps_ordering_within_max_items(self):
        items = self._items(5)
        bounded = build_bounded_history(items, max_items=3, max_tokens=100_000)
        self.assertEqual([item.item_id for item in bounded], ["i2", "i3", "i4"])

    def test_drops_oldest_when_over_token_budget(self):
        items = [
            ConversationItem(item_id="i0", role="user", content="x" * 400),
            ConversationItem(item_id="i1", role="user", content="x" * 4),
            ConversationItem(item_id="i2", role="user", content="x" * 4),
        ]
        bounded = build_bounded_history(items, max_items=10, max_tokens=5)
        # i0 (~100 tokens) must be dropped first; i1/i2 (~1 token each) fit.
        self.assertEqual([item.item_id for item in bounded], ["i1", "i2"])

    def test_never_drops_the_single_most_recent_item(self):
        items = [ConversationItem(item_id="i0", role="user", content="x" * 100_000)]
        bounded = build_bounded_history(items, max_items=10, max_tokens=1)
        self.assertEqual([item.item_id for item in bounded], ["i0"])

    def test_zero_max_items_returns_empty(self):
        items = self._items(3)
        bounded = build_bounded_history(items, max_items=0, max_tokens=100_000)
        self.assertEqual(bounded, [])


if __name__ == "__main__":
    unittest.main()
