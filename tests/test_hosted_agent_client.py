import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from gpt_rag_ui.config.chat_backend import (
    load_chat_backend,
    resolve_chat_backend,
    select_upload_conversation_id,
)
from gpt_rag_ui.clients.hosted_agent_client import (
    HostedAgentAuthenticationError,
    HostedAgentCancelledError,
    HostedAgentClient,
    HostedAgentConfigError,
    HostedAgentHTTPError,
    HostedAgentProtocolError,
    HostedAgentResponseError,
    HostedAgentSettings,
    InvocationMessage,
    _parse_sse_block,
    _validate_auth_mode,
    build_invocation_messages,
    load_hosted_agent_settings,
    validate_hosted_agent_config,
)


def _frame(event_type: str, data: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _created(conversation_id: str) -> str:
    return _frame(
        "response.created",
        {
            "type": "response.created",
            "response": {
                "id": "resp_123",
                "object": "response",
                "status": "in_progress",
                "conversation_id": conversation_id,
                "output": [],
            },
        },
    )


def _completed() -> str:
    return _frame(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": "resp_123",
                "object": "response",
                "status": "completed",
            },
        },
    )


class StubCredential:
    def __init__(self, token: str = "server-data-plane-token", error=None):
        self.token = token
        self.error = error
        self.scopes: list[tuple[str, ...]] = []
        self.closed = False

    async def get_token(self, *scopes: str, **_kwargs) -> AccessToken:
        self.scopes.append(scopes)
        if self.error:
            raise self.error
        return AccessToken(self.token, 4_102_444_800)

    async def close(self) -> None:
        self.closed = True


class TestConfiguration(unittest.TestCase):
    def _load(self, values: dict[str, str]):
        def get_value(key, default=None, _type=str):
            return values.get(key, default)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gpt_rag_ui.clients.hosted_agent_client.config.get", side_effect=get_value),
        ):
            return load_hosted_agent_settings()

    def test_requires_endpoint(self):
        with self.assertRaisesRegex(HostedAgentConfigError, "HOSTED_AGENT_BASE_URL"):
            self._load(
                {"HOSTED_AGENT_RESOURCE_SCOPE": "https://ai.azure.com/.default"}
            )

    def test_requires_explicit_data_plane_scope(self):
        with self.assertRaisesRegex(
            HostedAgentConfigError, "HOSTED_AGENT_RESOURCE_SCOPE"
        ):
            self._load({"HOSTED_AGENT_BASE_URL": "https://agent.example.com"})

    def test_rejects_arm_scope(self):
        with self.assertRaisesRegex(HostedAgentConfigError, "not Azure ARM"):
            self._load(
                {
                    "HOSTED_AGENT_BASE_URL": "https://agent.example.com",
                    "HOSTED_AGENT_RESOURCE_SCOPE": "https://management.azure.com/.default",
                }
            )

    def test_rejects_unbounded_idle_timeout(self):
        with self.assertRaisesRegex(HostedAgentConfigError, "finite positive"):
            self._load(
                {
                    "HOSTED_AGENT_BASE_URL": "https://agent.example.com",
                    "HOSTED_AGENT_RESOURCE_SCOPE": "https://ai.azure.com/.default",
                    "HOSTED_AGENT_SSE_IDLE_TIMEOUT_SECONDS": "inf",
                }
            )

    def test_loads_valid_settings_and_invocations_url(self):
        settings = self._load(
            {
                "HOSTED_AGENT_BASE_URL": "https://agent.example.com/protocol",
                "HOSTED_AGENT_RESOURCE_SCOPE": "api://hosted-agent/.default",
                "HOSTED_AGENT_SSE_IDLE_TIMEOUT_SECONDS": "45",
            }
        )
        self.assertEqual(
            settings.invocations_url,
            "https://agent.example.com/protocol/invocations",
        )
        self.assertEqual(settings.resource_scope, "api://hosted-agent/.default")
        self.assertEqual(settings.idle_timeout_seconds, 45.0)


class TestStartupValidation(unittest.TestCase):
    """Proves validate_hosted_agent_config() fails fast at startup.

    Gap fixed: for the default ``user_delegated`` auth mode, startup
    validation previously only checked HOSTED_AGENT_BASE_URL /
    HOSTED_AGENT_RESOURCE_SCOPE / HOSTED_AGENT_AUTH_MODE and did not verify
    the OAuth confidential-client configuration (OAUTH_AZURE_AD_CLIENT_ID /
    _CLIENT_SECRET / _TENANT_ID) required for the on-behalf-of exchange. A
    deployment missing those values would pass startup and only fail on the
    very first user request. validate_hosted_agent_config() must now also
    call _resolve_confidential_client_config() whenever auth_mode resolves
    to "user_delegated" (the default), and must NOT require it when the
    explicit "service_identity" opt-out is configured.
    """

    _BASE_VALUES = {
        "HOSTED_AGENT_BASE_URL": "https://agent.example.com/protocol",
        "HOSTED_AGENT_RESOURCE_SCOPE": "api://hosted-agent/.default",
    }

    _FULL_OAUTH_VALUES = {
        "OAUTH_AZURE_AD_CLIENT_ID": "client-id",
        "OAUTH_AZURE_AD_CLIENT_SECRET": "client-secret",
        "OAUTH_AZURE_AD_TENANT_ID": "tenant-id",
    }

    def _validate(self, values: dict[str, str]):
        def get_value(key, default=None, _type=str):
            return values.get(key, default)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("gpt_rag_ui.clients.hosted_agent_client.config.get", side_effect=get_value),
        ):
            validate_hosted_agent_config()

    def test_user_delegated_default_fails_startup_when_client_id_missing(self):
        values = {
            **self._BASE_VALUES,
            "OAUTH_AZURE_AD_CLIENT_SECRET": "client-secret",
            "OAUTH_AZURE_AD_TENANT_ID": "tenant-id",
        }
        with self.assertRaisesRegex(
            HostedAgentConfigError, "OAUTH_AZURE_AD_CLIENT_ID"
        ):
            self._validate(values)

    def test_user_delegated_default_fails_startup_when_client_secret_missing(self):
        values = {
            **self._BASE_VALUES,
            "OAUTH_AZURE_AD_CLIENT_ID": "client-id",
            "OAUTH_AZURE_AD_TENANT_ID": "tenant-id",
        }
        with self.assertRaisesRegex(
            HostedAgentConfigError, "OAUTH_AZURE_AD_CLIENT_SECRET"
        ):
            self._validate(values)

    def test_user_delegated_default_fails_startup_when_tenant_id_missing(self):
        values = {
            **self._BASE_VALUES,
            "OAUTH_AZURE_AD_CLIENT_ID": "client-id",
            "OAUTH_AZURE_AD_CLIENT_SECRET": "client-secret",
        }
        with self.assertRaisesRegex(
            HostedAgentConfigError, "OAUTH_AZURE_AD_TENANT_ID"
        ):
            self._validate(values)

    def test_user_delegated_default_fails_startup_when_all_oauth_config_missing(self):
        # HOSTED_AGENT_AUTH_MODE intentionally omitted -- must default to
        # "user_delegated" and still fail fast, not silently defer the
        # failure to the first user request.
        with self.assertRaisesRegex(
            HostedAgentConfigError, "OAUTH_AZURE_AD_CLIENT_ID"
        ):
            self._validate(dict(self._BASE_VALUES))

    def test_missing_backend_defaults_hosted_and_fails_before_client_creation(self):
        with (
            patch("gpt_rag_ui.clients.hosted_agent_client.httpx.AsyncClient") as http_client,
            patch("gpt_rag_ui.clients.hosted_agent_client._default_credential") as default_credential,
        ):
            self.assertEqual(resolve_chat_backend(None), "hosted_agent")
            with self.assertRaisesRegex(
                HostedAgentConfigError, "HOSTED_AGENT_BASE_URL"
            ):
                self._validate({})

        http_client.assert_not_called()
        default_credential.assert_not_called()

    def test_user_delegated_default_passes_startup_when_oauth_config_present(self):
        values = {**self._BASE_VALUES, **self._FULL_OAUTH_VALUES}
        # Must not raise.
        self._validate(values)

    def test_explicit_user_delegated_also_requires_oauth_config_at_startup(self):
        values = {
            **self._BASE_VALUES,
            "HOSTED_AGENT_AUTH_MODE": "user_delegated",
        }
        with self.assertRaisesRegex(
            HostedAgentConfigError, "OAUTH_AZURE_AD_CLIENT_ID"
        ):
            self._validate(values)

    def test_explicit_service_identity_does_not_require_oauth_config_at_startup(self):
        values = {
            **self._BASE_VALUES,
            "HOSTED_AGENT_AUTH_MODE": "service_identity",
        }
        # The gated legacy opt-out never performs an OBO exchange, so it
        # must not require the OAuth confidential-client configuration.
        # Must not raise.
        self._validate(values)


class TestBackendSelection(unittest.TestCase):
    def test_hosted_agent_is_the_default_when_backend_is_missing(self):
        self.assertEqual(resolve_chat_backend(None), "hosted_agent")
        self.assertEqual(resolve_chat_backend(""), "hosted_agent")
        self.assertEqual(resolve_chat_backend("   "), "hosted_agent")

        config = Mock()
        config.get.return_value = ""
        self.assertEqual(load_chat_backend(config, environment={}), "hosted_agent")
        config.get.assert_called_once_with("CHAT_BACKEND", "", str)

    def test_explicit_hosted_mode_is_supported(self):
        self.assertEqual(resolve_chat_backend(" hosted_agent "), "hosted_agent")

    def test_orchestrator_is_an_explicit_supported_fallback(self):
        self.assertEqual(resolve_chat_backend(" orchestrator "), "orchestrator")

        config = Mock()
        config.get.return_value = "orchestrator"
        self.assertEqual(load_chat_backend(config, environment={}), "orchestrator")

    def test_environment_backend_selection_overrides_app_configuration(self):
        config = Mock()
        config.get.return_value = "orchestrator"
        self.assertEqual(
            load_chat_backend(
                config,
                environment={"CHAT_BACKEND": "hosted_agent"},
            ),
            "hosted_agent",
        )
        config.get.assert_not_called()

    def test_unknown_backend_fails_instead_of_falling_back(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown CHAT_BACKEND"):
            resolve_chat_backend("hosted-or-classic")

    def test_hosted_upload_uses_only_runtime_managed_conversation_id(self):
        self.assertEqual(
            select_upload_conversation_id(
                "hosted_agent",
                classic_conversation_id="fabricated-chainlit-id",
                hosted_conversation_id="managed-conversation-id",
            ),
            "managed-conversation-id",
        )

    def test_new_hosted_upload_never_uses_classic_thread_id(self):
        self.assertEqual(
            select_upload_conversation_id(
                "hosted_agent",
                classic_conversation_id="fabricated-chainlit-id",
                hosted_conversation_id="",
            ),
            "",
        )


class TestInvocationMessages(unittest.TestCase):
    def test_preserves_order_and_current_user_ask(self):
        history = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow-up"},
        ]
        messages = build_invocation_messages(history, "follow-up")
        self.assertEqual([item.to_payload() for item in messages], history)

    def test_appends_current_ask_when_chat_context_does_not_include_it(self):
        messages = build_invocation_messages(
            [{"role": "user", "content": "first"}],
            "second",
        )
        self.assertEqual(
            [item.to_payload() for item in messages],
            [
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
            ],
        )

    def test_rejects_non_runtime_roles(self):
        with self.assertRaisesRegex(HostedAgentProtocolError, "unsupported role"):
            build_invocation_messages(
                [{"role": "system", "content": "untrusted"}],
                "hello",
            )


class TestSseParsing(unittest.TestCase):
    def test_parses_real_response_delta(self):
        event = _parse_sse_block(
            'event: response.output_text.delta\n'
            'data: {"type":"response.output_text.delta","delta":"hello"}'
        )
        self.assertIsNotNone(event)
        self.assertEqual(event.event_type, "response.output_text.delta")
        self.assertEqual(event.data["delta"], "hello")

    def test_uses_data_type_when_event_field_is_absent(self):
        event = _parse_sse_block(
            'data: {"type":"response.completed","response":{"status":"completed"}}'
        )
        self.assertEqual(event.event_type, "response.completed")

    def test_invalid_json_is_a_protocol_error(self):
        with self.assertRaises(HostedAgentProtocolError):
            _parse_sse_block("event: error\ndata: not-json")


class TestHostedAgentClient(unittest.IsolatedAsyncioTestCase):
    settings = HostedAgentSettings(
        base_url="https://agent.example.com/protocol",
        resource_scope="api://hosted-agent/.default",
        idle_timeout_seconds=12.0,
        # These tests exercise HTTP/SSE protocol handling, not identity
        # resolution, and never pass user_access_token. Pin auth_mode to the
        # explicit, non-default service_identity opt-out so StubCredential
        # continues to satisfy token acquisition without needing every test
        # to supply a user token. The default user_delegated (OBO) path is
        # covered separately below in TestUserDelegatedAuth.
        auth_mode="service_identity",
    )

    async def _client_for_body(
        self,
        body: str,
        *,
        status_code: int = 200,
        content_type: str = "text/event-stream",
        credential: StubCredential | None = None,
        capture: list[dict] | None = None,
    ) -> HostedAgentClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            if capture is not None:
                capture.append(
                    {
                        "method": request.method,
                        "url": str(request.url),
                        "headers": dict(request.headers),
                        "json": json.loads((await request.aread()).decode()),
                    }
                )
            return httpx.Response(
                status_code,
                headers={"content-type": content_type},
                content=body.encode(),
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        return HostedAgentClient(
            self.settings,
            credential=credential or StubCredential(),
            http_client=http_client,
        )

    async def _collect(self, client: HostedAgentClient, messages, **kwargs):
        return [item async for item in client.stream(messages, **kwargs)]

    async def test_real_frames_surface_text_citation_tool_and_completion(self):
        body = (
            _created("conv-managed")
            + _frame(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "response_id": "resp_123",
                    "output_index": 0,
                    "item": {
                        "id": "item_123",
                        "type": "message",
                        "role": "assistant",
                        "status": "in_progress",
                        "content": [],
                    },
                },
            )
            + _frame(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": "item_123",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
            )
            + _frame(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "item_123",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "Hello ",
                },
            )
            + _frame(
                "response.output_text.annotation.added",
                {
                    "type": "response.output_text.annotation.added",
                    "item_id": "item_123",
                    "output_index": 0,
                    "content_index": 0,
                    "annotation": {
                        "type": "url_citation",
                        "citation_id": "src-1",
                        "title": "Policy",
                        "url": "documents/policy.pdf",
                        "snippet": "Relevant text",
                    },
                },
            )
            + _frame(
                "response.function_call_arguments.delta",
                {
                    "type": "response.function_call_arguments.delta",
                    "call_id": "call-1",
                    "name": "search",
                    "delta": "",
                    "status": "started",
                },
            )
            + _frame(
                "response.function_call_arguments.done",
                {
                    "type": "response.function_call_arguments.done",
                    "call_id": "call-1",
                    "name": "search",
                    "status": "completed",
                },
            )
            + _frame(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "item_123",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "world",
                },
            )
            + _completed()
        )
        capture: list[dict] = []
        credential = StubCredential()
        client = await self._client_for_body(
            body,
            credential=credential,
            capture=capture,
        )

        results = await self._collect(
            client,
            [InvocationMessage("user", "What is the policy?")],
            question_id="question-1",
            correlation_id="correlation-1",
        )

        self.assertEqual([text for text, _ in results if text], ["Hello ", "world"])
        self.assertEqual(
            [meta["conversation_id"] for _, meta in results if meta.get("conversation_id")][0],
            "conv-managed",
        )
        self.assertEqual(
            [meta["citation"]["citation_id"] for _, meta in results if meta.get("citation")],
            ["src-1"],
        )
        self.assertEqual(
            [
                meta["tool_activity"]["event_type"]
                for _, meta in results
                if meta.get("tool_activity")
            ],
            [
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
            ],
        )
        self.assertTrue(any(meta.get("completed") for _, meta in results))
        self.assertEqual(
            credential.scopes,
            [("api://hosted-agent/.default",)],
        )
        self.assertEqual(capture[0]["method"], "POST")
        self.assertEqual(
            capture[0]["url"],
            "https://agent.example.com/protocol/invocations?api-version=v1",
        )
        transmitted_authorization = capture[0]["headers"]["authorization"]
        self.assertEqual(
            transmitted_authorization,
            f"Bearer {credential.token}",
        )
        self.assertNotEqual(transmitted_authorization, "******")
        self.assertEqual(
            capture[0]["json"],
            {
                "messages": [
                    {"role": "user", "content": "What is the policy?"}
                ],
                "metadata": {
                    "question_id": "question-1",
                    "correlation_id": "correlation-1",
                },
            },
        )

    async def test_transmits_acquired_data_plane_token_with_bearer_scheme(self):
        capture: list[dict] = []
        credential = StubCredential(token="fake-acquired-data-plane-token")
        client = await self._client_for_body(
            _created("conv-managed") + _completed(),
            credential=credential,
            capture=capture,
        )

        await self._collect(client, [InvocationMessage("user", "hello")])

        transmitted_authorization = capture[0]["headers"]["authorization"]
        self.assertEqual(
            transmitted_authorization,
            f"Bearer {credential.token}",
        )
        self.assertNotEqual(transmitted_authorization, "******")

    async def test_two_turn_continuity_resends_ordered_history_and_only_managed_id(self):
        captures: list[dict] = []
        response_bodies = iter(
            [
                _created("conv-managed")
                + _frame(
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "delta": "first answer"},
                )
                + _completed(),
                _created("conv-managed")
                + _frame(
                    "response.output_text.delta",
                    {"type": "response.output_text.delta", "delta": "second answer"},
                )
                + _completed(),
            ]
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            captures.append(json.loads((await request.aread()).decode()))
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=next(response_bodies).encode(),
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        client = HostedAgentClient(
            self.settings,
            credential=StubCredential(),
            http_client=http_client,
        )

        first = build_invocation_messages(
            [{"role": "user", "content": "first question"}],
            "first question",
        )
        first_results = await self._collect(
            client,
            first,
            question_id="q-1",
            correlation_id="corr-1",
        )
        conversation_id = next(
            meta["conversation_id"]
            for _, meta in first_results
            if meta.get("conversation_id")
        )
        second = build_invocation_messages(
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "follow-up"},
            ],
            "follow-up",
        )
        await self._collect(
            client,
            second,
            conversation_id=conversation_id,
            question_id="q-2",
            correlation_id="corr-2",
        )

        self.assertEqual(
            captures[1],
            {
                "messages": [
                    {"role": "user", "content": "first question"},
                    {"role": "assistant", "content": "first answer"},
                    {"role": "user", "content": "follow-up"},
                ],
                "conversation_id": "conv-managed",
                "metadata": {
                    "question_id": "q-2",
                    "correlation_id": "corr-2",
                },
            },
        )
        for forbidden in (
            "question",
            "ask",
            "thread_id",
            "client_principal_id",
            "client_principal_name",
            "access_token",
        ):
            self.assertNotIn(forbidden, captures[1])

    async def test_accepts_canonical_conversation_object(self):
        body = _frame(
            "response.created",
            {"response": {"conversation": {"id": "conv-managed"}}},
        ) + _completed()
        client = await self._client_for_body(body)
        results = await self._collect(client, [InvocationMessage("user", "hello")])
        self.assertIn(("", {"conversation_id": "conv-managed"}), results)
        self.assertTrue(any(meta.get("completed") for _, meta in results))

    async def test_accepts_matching_canonical_and_legacy_identifiers(self):
        body = _frame(
            "response.created",
            {
                "response": {
                    "conversation": {"id": "conv-managed"},
                    "conversation_id": "conv-managed",
                }
            },
        ) + _completed()
        client = await self._client_for_body(body)
        results = await self._collect(client, [InvocationMessage("user", "hello")])
        self.assertIn(("", {"conversation_id": "conv-managed"}), results)

    async def test_rejects_malformed_conversation_identifiers(self):
        invalid_responses = [
            {},
            {"conversation_id": ""},
            {"conversation_id": "   "},
            {"conversation_id": 42},
            {"conversation": "conv-managed"},
            {"conversation": {}},
            {"conversation": {"id": ""}},
            {"conversation": {"id": "   "}},
            {"conversation": {"id": 42}},
            {"conversation": {"id": None}},
        ]
        for response in invalid_responses:
            with self.subTest(response=response):
                client = await self._client_for_body(
                    _frame("response.created", {"response": response}) + _completed()
                )
                with self.assertRaises(HostedAgentProtocolError):
                    await self._collect(client, [InvocationMessage("user", "hello")])

    async def test_rejects_conflicting_conversation_identifiers(self):
        body = _frame(
            "response.created",
            {
                "response": {
                    "conversation": {"id": "conv-managed"},
                    "conversation_id": "another-conversation",
                }
            },
        ) + _completed()
        client = await self._client_for_body(body)
        with self.assertRaisesRegex(HostedAgentProtocolError, "conflicting"):
            await self._collect(client, [InvocationMessage("user", "hello")])

    async def test_error_frame_raises_typed_error(self):
        body = _created("conv-1") + _frame(
            "error",
            {
                "type": "error",
                "code": "tool_failed",
                "message": "Search failed.",
                "retryable": True,
            },
        )
        client = await self._client_for_body(body)
        with self.assertRaises(HostedAgentResponseError) as context:
            await self._collect(client, [InvocationMessage("user", "hello")])
        self.assertEqual(context.exception.code, "tool_failed")
        self.assertTrue(context.exception.retryable)

    async def test_cancelled_frame_raises_typed_error(self):
        body = _created("conv-1") + _frame(
            "response.cancelled",
            {"type": "response.cancelled", "reason": "client_cancelled"},
        )
        client = await self._client_for_body(body)
        with self.assertRaisesRegex(HostedAgentCancelledError, "client_cancelled"):
            await self._collect(client, [InvocationMessage("user", "hello")])

    async def test_stream_without_terminal_frame_is_rejected(self):
        body = _created("conv-1") + _frame(
            "response.output_text.delta",
            {"type": "response.output_text.delta", "delta": "partial"},
        )
        client = await self._client_for_body(body)
        with self.assertRaisesRegex(HostedAgentProtocolError, "terminal"):
            await self._collect(client, [InvocationMessage("user", "hello")])

    async def test_non_sse_response_is_rejected(self):
        client = await self._client_for_body(
            "{}",
            content_type="application/json",
        )
        with self.assertRaisesRegex(HostedAgentProtocolError, "text/event-stream"):
            await self._collect(client, [InvocationMessage("user", "hello")])

    async def test_http_error_is_not_retried_or_fallen_back(self):
        capture: list[dict] = []
        client = await self._client_for_body(
            '{"detail":"denied"}',
            status_code=403,
            content_type="application/json",
            capture=capture,
        )
        with self.assertRaisesRegex(HostedAgentHTTPError, "403"):
            await self._collect(client, [InvocationMessage("user", "hello")])
        self.assertEqual(len(capture), 1)

    async def test_token_failure_happens_before_http_request(self):
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(500)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        client = HostedAgentClient(
            self.settings,
            credential=StubCredential(
                error=ClientAuthenticationError(message="identity unavailable")
            ),
            http_client=http_client,
        )
        with self.assertRaises(HostedAgentAuthenticationError):
            await self._collect(client, [InvocationMessage("user", "hello")])
        self.assertEqual(calls, 0)

    async def test_read_timeout_propagates(self):
        class TimeoutStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise httpx.ReadTimeout("SSE idle timeout reached")
                yield b""  # pragma: no cover

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=TimeoutStream(),
            )

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(
                connect=10,
                read=self.settings.idle_timeout_seconds,
                write=30,
                pool=10,
            ),
        )
        self.addAsyncCleanup(http_client.aclose)
        client = HostedAgentClient(
            self.settings,
            credential=StubCredential(),
            http_client=http_client,
        )
        with self.assertRaisesRegex(httpx.ReadTimeout, "idle timeout"):
            await self._collect(client, [InvocationMessage("user", "hello")])

    async def test_task_cancellation_closes_stream_and_propagates(self):
        class BlockingStream(httpx.AsyncByteStream):
            def __init__(self):
                self.started = asyncio.Event()
                self.closed = False

            async def __aiter__(self):
                self.started.set()
                await asyncio.Event().wait()
                yield b""  # pragma: no cover

            async def aclose(self):
                self.closed = True

        stream = BlockingStream()

        async def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=stream,
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        client = HostedAgentClient(
            self.settings,
            credential=StubCredential(),
            http_client=http_client,
        )
        generator = client.stream([InvocationMessage("user", "hello")])
        pending = asyncio.create_task(anext(generator))
        await stream.started.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await generator.aclose()
        self.assertTrue(stream.closed)


class TestUserDelegatedAuth(unittest.IsolatedAsyncioTestCase):
    """Covers the default (ADR-0001-required) on-behalf-of identity path.

    These tests prove: the exact delegated (OBO) token is transmitted as the
    literal Authorization bearer; a missing/invalid signed-in user identity
    fails *before* any network call and never falls back to a service
    identity; the raw user assertion and delegated token never appear in
    logs or exception text; and the explicit service_identity opt-out
    requires deliberate configuration (never selected implicitly).
    """

    # Default settings: auth_mode intentionally omitted so it resolves to the
    # required default "user_delegated".
    settings = HostedAgentSettings(
        base_url="https://agent.example.com/protocol",
        resource_scope="api://hosted-agent/.default",
        idle_timeout_seconds=12.0,
    )

    def test_default_auth_mode_is_user_delegated_not_service_identity(self):
        self.assertEqual(self.settings.auth_mode, "user_delegated")
        self.assertEqual(_validate_auth_mode(None), "user_delegated")
        self.assertEqual(_validate_auth_mode(""), "user_delegated")

    def test_service_identity_requires_explicit_config_value(self):
        # The opt-out is only ever selected by an explicit, exact config
        # value -- never inferred, and never used as a fallback.
        self.assertEqual(
            _validate_auth_mode("service_identity"), "service_identity"
        )
        with self.assertRaises(HostedAgentConfigError):
            _validate_auth_mode("something-else")

    async def test_default_mode_does_not_construct_service_credential(self):
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)

        with patch("gpt_rag_ui.clients.hosted_agent_client._default_credential") as default_credential:
            client = HostedAgentClient(self.settings, http_client=http_client)
            with self.assertRaises(HostedAgentAuthenticationError):
                async for _ in client.stream(
                    [InvocationMessage("user", "hello")],
                    user_access_token=None,
                ):
                    pass

        default_credential.assert_not_called()
        self.assertEqual(requests, [])

    async def _client(
        self, *, capture: list[dict] | None = None, credential=None
    ) -> HostedAgentClient:
        async def handler(request: httpx.Request) -> httpx.Response:
            if capture is not None:
                capture.append(
                    {
                        "headers": dict(request.headers),
                        "json": json.loads((await request.aread()).decode()),
                    }
                )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(_created("conv-1") + _completed()).encode(),
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        return HostedAgentClient(
            self.settings,
            credential=credential or StubCredential(),
            http_client=http_client,
        )

    async def test_exact_delegated_token_is_sent_as_bearer_and_no_mi_used(self):
        capture: list[dict] = []
        credential = StubCredential(token="service-identity-token-should-not-be-used")
        client = await self._client(capture=capture, credential=credential)

        with patch(
            "gpt_rag_ui.clients.hosted_agent_client._acquire_obo_token",
            new=AsyncMock(return_value="delegated-data-plane-token"),
        ) as mocked_obo:
            results = [
                item
                async for item in client.stream(
                    [InvocationMessage("user", "hello")],
                    user_access_token="signed-in-user-access-token",
                )
            ]

        self.assertTrue(any(meta.get("completed") for _, meta in results))
        mocked_obo.assert_awaited_once_with(
            "signed-in-user-access-token", "api://hosted-agent/.default"
        )
        transmitted_authorization = capture[0]["headers"]["authorization"]
        self.assertEqual(
            transmitted_authorization, "Bearer delegated-data-plane-token"
        )
        # The service/managed identity credential must never be consulted on
        # the default (user_delegated) path.
        self.assertEqual(credential.scopes, [])

    async def test_missing_user_token_fails_before_any_http_request(self):
        capture: list[dict] = []
        credential = StubCredential()
        client = await self._client(capture=capture, credential=credential)

        with self.assertRaises(HostedAgentAuthenticationError):
            async for _ in client.stream(
                [InvocationMessage("user", "hello")], user_access_token=None
            ):
                pass

        self.assertEqual(capture, [])
        self.assertEqual(credential.scopes, [])

    async def test_blank_user_token_fails_before_any_http_request(self):
        capture: list[dict] = []
        client = await self._client(capture=capture)

        with self.assertRaises(HostedAgentAuthenticationError):
            async for _ in client.stream(
                [InvocationMessage("user", "hello")], user_access_token="   "
            ):
                pass

        self.assertEqual(capture, [])

    async def test_obo_failure_never_logs_or_raises_with_token_material(self):
        client = await self._client()
        user_token = "super-secret-user-assertion-value"

        async def _fake_obo(user_access_token, resource_scope):
            # Simulate an MSAL failure path: only a non-sensitive error code
            # should ever be logged, never the raw assertion/delegated token.
            logger = __import__("logging").getLogger(
                "gpt_rag_ui.hosted_agent_client"
            )
            logger.warning(
                "Hosted-agent on-behalf-of token exchange failed: error=%s",
                "invalid_grant",
            )
            raise HostedAgentAuthenticationError(
                "Unable to exchange the signed-in user's token for a "
                "hosted-agent data-plane token (on-behalf-of exchange failed)."
            )

        with (
            self.assertLogs("gpt_rag_ui.hosted_agent_client", level="WARNING") as logs,
            patch("gpt_rag_ui.clients.hosted_agent_client._acquire_obo_token", new=_fake_obo),
        ):
            with self.assertRaises(HostedAgentAuthenticationError) as ctx:
                async for _ in client.stream(
                    [InvocationMessage("user", "hello")],
                    user_access_token=user_token,
                ):
                    pass

        self.assertNotIn(user_token, str(ctx.exception))
        for record in logs.output:
            self.assertNotIn(user_token, record)

    async def test_service_identity_mode_still_available_when_explicitly_gated(self):
        gated_settings = HostedAgentSettings(
            base_url="https://agent.example.com/protocol",
            resource_scope="api://hosted-agent/.default",
            idle_timeout_seconds=12.0,
            auth_mode="service_identity",
        )
        capture: list[dict] = []
        credential = StubCredential(token="explicit-service-identity-token")

        async def handler(request: httpx.Request) -> httpx.Response:
            capture.append({"headers": dict(request.headers)})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(_created("conv-1") + _completed()).encode(),
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        client = HostedAgentClient(
            gated_settings, credential=credential, http_client=http_client
        )

        # No user_access_token supplied -- the explicit opt-out must still
        # work standalone, proving #591 requires a deliberate config change
        # rather than silently degrading into the service-identity path.
        results = [
            item
            async for item in client.stream([InvocationMessage("user", "hello")])
        ]
        self.assertTrue(any(meta.get("completed") for _, meta in results))
        self.assertEqual(
            capture[0]["headers"]["authorization"],
            "Bearer explicit-service-identity-token",
        )
        self.assertEqual(credential.scopes, [("api://hosted-agent/.default",)])


if __name__ == "__main__":
    unittest.main()
