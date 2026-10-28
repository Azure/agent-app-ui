"""Native Search delegation, Work IQ assertions and safe auth recovery."""

import importlib
import asyncio
import base64
import json
import os
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import AsyncMock, Mock, patch

import httpx
from azure.core.credentials import AccessToken

from gpt_rag_ui.clients import hosted_agent_client as hosted
from gpt_rag_ui.auth import oauth
from gpt_rag_ui.auth.auth_common import DelegationError, validated_claims_challenge
from gpt_rag_ui.auth.entra_token import EntraTokenError
from gpt_rag_ui.telemetry.monitoring import Telemetry


class ProtectedRetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, requests, *, text=None):
        async def handler(request):
            requests.append(request)
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                text=text or (
                    'event: response.created\n'
                    'data: {"response":{"conversation_id":"synthetic-conversation"}}\n\n'
                    'event: response.completed\ndata: {}\n\n'
                ),
            )
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        return hosted.HostedAgentClient(
            hosted.HostedAgentSettings(
                base_url="https://agent.example.com/protocol",
                resource_scope="https://ai.azure.com/.default",
            ), http_client=http_client,
        )

    async def collect(self, client, assertion="synthetic-api-assertion"):
        return [item async for item in client.stream(
            [hosted.InvocationMessage("user", "question")], user_access_token=assertion,
        )]

    async def test_native_search_forwards_search_token_without_changing_foundry_identity(self):
        requests = []

        async def handler(request):
            requests.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=(
                    'event: response.created\n'
                    'data: {"response":{"conversation_id":"synthetic-conversation"}}\n\n'
                    'event: response.completed\n'
                    'data: {}\n\n'
                ),
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        client = hosted.HostedAgentClient(
            hosted.HostedAgentSettings(
                base_url="https://agent.example.com/protocol",
                resource_scope="https://ai.azure.com/.default",
            ),
            http_client=http_client,
        )

        async def exchange(assertion, scope):
            self.assertEqual("synthetic-api-assertion", assertion)
            return {
                "https://ai.azure.com/.default": "synthetic-foundry-user-token",
                "https://search.azure.com/.default": "synthetic-search-user-token",
            }[scope]

        with (
            patch.dict(os.environ, {"RETRIEVAL_AUTH_MODE": "native_search"}),
            patch.object(hosted, "_acquire_obo_token", new=AsyncMock(side_effect=exchange)),
            patch.object(hosted, "_default_credential") as workload_credential,
        ):
            result = [
                item async for item in client.stream(
                    [hosted.InvocationMessage("user", "question")],
                    user_access_token="synthetic-api-assertion",
                )
            ]
        workload_credential.assert_not_called()
        self.assertTrue(result[-1][1]["completed"])
        self.assertEqual(1, len(requests))
        headers = requests[0].headers
        self.assertEqual("Bearer synthetic-foundry-user-token", headers["authorization"])
        self.assertNotIn("x-ms-user-identity", headers)
        self.assertNotIn("x-client-retrieval-context", headers)
        self.assertNotIn("synthetic-api-assertion", requests[0].content.decode())
        self.assertEqual("synthetic-search-user-token", headers.get("x-client-search-token"))

    async def test_native_first_turn_allocates_an_opaque_retrieval_scope(self):
        requests = []
        client = await self.client(requests)
        with patch.object(hosted, "_acquire_obo_token", new=AsyncMock(return_value="synthetic-token")):
            await self.collect(client)
            await self.collect(client)
        scopes = [json.loads(request.content)["conversation_id"] for request in requests]
        self.assertEqual(2, len(scopes))
        self.assertNotEqual(scopes[0], scopes[1])
        for scope in scopes:
            self.assertEqual(scope, str(uuid.UUID(scope)))

    async def test_native_existing_retrieval_scope_is_preserved(self):
        requests = []
        client = await self.client(requests)
        with patch.object(hosted, "_acquire_obo_token", new=AsyncMock(return_value="synthetic-token")):
            result = [item async for item in client.stream(
                [hosted.InvocationMessage("user", "question")],
                conversation_id="authorized-existing-scope",
                user_access_token="synthetic-api-assertion",
            )]
        self.assertTrue(result[-1][1]["completed"])
        self.assertEqual("authorized-existing-scope", json.loads(requests[0].content)["conversation_id"])

    async def test_resource_scope_is_exchanged_with_the_original_api_assertion(self):
        msal_app = Mock()
        msal_app.acquire_token_on_behalf_of.return_value = {
            "access_token": "synthetic-downstream-token",
        }
        with (
            patch.object(
                hosted, "_resolve_confidential_client_config",
                return_value=("synthetic-client", "synthetic-secret", "synthetic-tenant"),
            ),
            patch.object(
                hosted.msal, "ConfidentialClientApplication", return_value=msal_app,
            ),
        ):
            token = await hosted.acquire_obo_token(
                "synthetic-api-assertion", "https://graph.microsoft.com/User.Read",
            )
        self.assertEqual("synthetic-downstream-token", token)
        msal_app.acquire_token_on_behalf_of.assert_called_once_with(
            user_assertion="synthetic-api-assertion",
            scopes=["https://graph.microsoft.com/User.Read"],
        )

    async def test_obo_interaction_required_preserves_claims_for_bff_recovery(self):
        # A structured challenge must reach the auth recovery path, never logs
        # or generic user-facing exception text.
        challenge = '{"access_token":{"acrs":{"essential":true,"value":"synthetic"}}}'
        private_detail = "synthetic-provider-private-detail"
        msal_app = Mock()
        msal_app.acquire_token_on_behalf_of.return_value = {
            "error": "interaction_required",
            "error_description": private_detail,
            "claims": challenge,
        }
        with (
            patch.object(
                hosted, "_resolve_confidential_client_config",
                return_value=("synthetic-client", "synthetic-secret", "synthetic-tenant"),
            ),
            patch.object(
                hosted.msal, "ConfidentialClientApplication", return_value=msal_app,
            ),
            self.assertLogs(hosted.logger, level="WARNING") as logs,
        ):
            with self.assertRaises(hosted.HostedAgentAuthenticationError) as raised:
                await hosted.acquire_obo_token(
                    "synthetic-api-assertion", "https://graph.microsoft.com/User.Read",
                )
        diagnostic = str(raised.exception) + "\n" + "\n".join(logs.output)
        self.assertNotIn(private_detail, diagnostic)
        self.assertNotIn(challenge, diagnostic)
        self.assertEqual(challenge, getattr(raised.exception, "claims_challenge", None))

    async def test_http_failure_does_not_expose_provider_body(self):
        private_detail = "synthetic-forwarded-credential-or-protected-excerpt"

        async def handler(_request):
            return httpx.Response(403, text=private_detail)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        credential = SimpleNamespace(
            get_token=AsyncMock(return_value=AccessToken("synthetic-workload-token", 0)),
        )
        client = hosted.HostedAgentClient(
            hosted.HostedAgentSettings(
                base_url="https://agent.example.com/protocol",
                resource_scope="https://ai.azure.com/.default",
                auth_mode="service_identity",
            ),
            credential=credential,
            http_client=http_client,
        )
        with self.assertRaises(hosted.HostedAgentHTTPError) as raised:
            async for _ in client.stream([hosted.InvocationMessage("user", "question")]):
                pass
        self.assertIn("403", str(raised.exception))
        self.assertNotIn(private_detail, str(raised.exception))

    async def test_auth_info_returns_the_refreshed_assertion_on_the_same_turn(self):
        with (
            patch.object(Telemetry, "configure_monitoring"),
            patch.dict(
                os.environ,
                {"CHAT_BACKEND": "orchestrator", "CHAINLIT_AUTH_SECRET": "synthetic-secret"},
            ),
        ):
            chat = importlib.import_module("gpt_rag_ui.services.chat")
        user = SimpleNamespace(metadata={
            "auth_source": "oauth",
            "authorized": True,
            "access_token": "synthetic-old-api-assertion",
        })

        async def refresh(current_user, *, min_ttl_seconds):
            self.assertIs(user, current_user)
            self.assertEqual(120, min_ttl_seconds)
            current_user.metadata["access_token"] = "synthetic-new-api-assertion"
            return True

        session = SimpleNamespace(get=lambda _key: user, set=Mock())
        with (
            patch.object(chat, "cl", SimpleNamespace(user_session=session)),
            patch.object(
                chat, "auth_oauth",
                SimpleNamespace(ensure_fresh_user_access_token=AsyncMock(side_effect=refresh)),
                create=True,
            ),
        ):
            auth_info = await chat.get_auth_info()
        self.assertTrue(auth_info["authorized"])
        session.set.assert_called_once_with("user", user)
        self.assertEqual("synthetic-new-api-assertion", auth_info["access_token"])

    async def test_search_failure_stops_dispatch_without_service_retry(self):
        requests = []
        client = await self.client(requests)

        async def exchange(assertion, scope):
            if scope == hosted.SEARCH_USER_SCOPE:
                raise hosted.HostedAgentAuthenticationError(
                    "synthetic-private-provider-body", code="consent_required",
                )
            return "synthetic-foundry-token"

        with (
            patch.object(hosted, "_acquire_obo_token", new=AsyncMock(side_effect=exchange)) as obo,
            patch.object(hosted, "_default_credential") as workload,
        ):
            with self.assertRaises(hosted.HostedAgentAuthenticationError):
                await self.collect(client)
        self.assertEqual(2, obo.await_count)
        workload.assert_not_called()
        self.assertEqual([], requests)

    async def test_missing_and_invalid_tokens_never_dispatch(self):
        for token in (None, "", " ", "synthetic\r\ninjection", "s" * 8193):
            with self.subTest(token_length=len(token or "")):
                requests = []
                client = await self.client(requests)
                with patch.object(hosted, "_acquire_obo_token", new=AsyncMock()) as obo:
                    with self.assertRaises(hosted.HostedAgentAuthenticationError):
                        await self.collect(client, token)
                obo.assert_not_awaited()
                self.assertEqual([], requests)

    async def test_invalid_downstream_token_never_dispatches(self):
        for token in ("", "s\r\ninjection", "s" * 8193):
            with self.subTest(token_length=len(token)):
                requests = []
                client = await self.client(requests)
                with patch.object(
                    hosted, "_acquire_obo_token",
                    new=AsyncMock(side_effect=["synthetic-foundry-token", token]),
                ):
                    with self.assertRaises(hosted.HostedAgentAuthenticationError):
                        await self.collect(client)
                self.assertEqual([], requests)

    async def test_unknown_retrieval_mode_stops_dispatch(self):
        requests = []
        client = await self.client(requests)
        with (
            patch.dict(os.environ, {"RETRIEVAL_AUTH_MODE": "foundry_toolbox"}),
            patch.object(hosted, "_acquire_obo_token", new=AsyncMock()) as obo,
        ):
            with self.assertRaises(hosted.HostedAgentConfigError):
                await self.collect(client)
        obo.assert_not_awaited()
        self.assertEqual([], requests)

    async def test_concurrent_users_keep_request_local_resource_tokens(self):
        requests = []
        client = await self.client(requests)

        async def exchange(assertion, scope):
            await asyncio.sleep(0)
            return assertion + ("-search" if scope == hosted.SEARCH_USER_SCOPE else "-foundry")

        with patch.object(hosted, "_acquire_obo_token", new=AsyncMock(side_effect=exchange)):
            results = await asyncio.gather(
                self.collect(client, "synthetic-user-a"),
                self.collect(client, "synthetic-user-b"),
            )
            await self.collect(client, "synthetic-user-a")
        for request in requests:
            search = request.headers["x-client-search-token"]
            principal = search.removesuffix("-search")
            self.assertEqual(f"Bearer {principal}-foundry", request.headers["authorization"])
            self.assertNotIn(principal, request.content.decode())
        self.assertEqual(3, len(requests))
        self.assertNotIn("synthetic-user", json.dumps(results))
        self.assertNotIn("synthetic-user", repr(client.__dict__))

    def work_iq_settings(self):
        return {
            "WORK_IQ_ENABLED": "true",
            "WORK_IQ_APPLICATION_ID": "11111111-1111-4111-8111-111111111111",
            "OAUTH_AZURE_AD_CLIENT_ID": "11111111-1111-4111-8111-111111111111",
            "OAUTH_AZURE_AD_TENANT_ID": "22222222-2222-4222-8222-222222222222",
        }

    async def test_work_iq_forwards_only_original_validated_app_assertion(self):
        requests = []
        client = await self.client(requests)
        validator = Mock()
        validator.validate = AsyncMock(return_value={"scp": "access_as_user"})
        with (
            patch.dict(os.environ, self.work_iq_settings()),
            patch.object(hosted, "EntraTokenValidator", return_value=validator) as factory,
            patch.object(
                hosted, "_acquire_obo_token",
                new=AsyncMock(side_effect=["synthetic-foundry-token", "synthetic-search-token"]),
            ),
        ):
            await self.collect(client)
        factory.assert_called_once_with(
            tenant_id="22222222-2222-4222-8222-222222222222",
            audience="11111111-1111-4111-8111-111111111111",
            required_scope="access_as_user",
        )
        validator.validate.assert_awaited_once_with("synthetic-api-assertion")
        self.assertEqual(
            "synthetic-api-assertion", requests[0].headers["x-client-work-iq-assertion"],
        )
        self.assertEqual("synthetic-search-token", requests[0].headers["x-client-search-token"])
        self.assertNotIn("synthetic-api-assertion", requests[0].content.decode())

    async def test_work_iq_invalid_assertion_fails_before_obo_and_dispatch(self):
        for failure in (EntraTokenError("invalid scope"), httpx.ConnectError("synthetic-private")):
            with self.subTest(failure_type=type(failure).__name__):
                requests = []
                client = await self.client(requests)
                validator = Mock(validate=AsyncMock(side_effect=failure))
                with (
                    patch.dict(os.environ, self.work_iq_settings()),
                    patch.object(hosted, "EntraTokenValidator", return_value=validator),
                    patch.object(hosted, "_acquire_obo_token", new=AsyncMock()) as obo,
                ):
                    with self.assertRaises(hosted.HostedAgentAuthenticationError) as raised:
                        await self.collect(client)
                self.assertNotIn("synthetic-private", str(raised.exception))
                obo.assert_not_awaited()
                self.assertEqual([], requests)

    async def test_work_iq_missing_or_mismatched_application_is_not_omitted(self):
        for change in (
            {"WORK_IQ_APPLICATION_ID": ""},
            {"WORK_IQ_APPLICATION_ID": "33333333-3333-4333-8333-333333333333"},
            {"WORK_IQ_TENANT_ID": "33333333-3333-4333-8333-333333333333"},
        ):
            with self.subTest(change=change):
                requests = []
                client = await self.client(requests)
                with (
                    patch.dict(os.environ, {**self.work_iq_settings(), **change}),
                    patch.object(hosted, "_acquire_obo_token", new=AsyncMock()) as obo,
                ):
                    with self.assertRaises(hosted.HostedAgentConfigError):
                        await self.collect(client)
                obo.assert_not_awaited()
                self.assertEqual([], requests)

    async def test_disabled_work_iq_does_not_forward_or_validate_an_assertion(self):
        requests = []
        client = await self.client(requests)
        with (
            patch.dict(os.environ, {"WORK_IQ_ENABLED": "false"}),
            patch.object(hosted, "EntraTokenValidator") as validator,
            patch.object(hosted, "_acquire_obo_token", new=AsyncMock(return_value="synthetic-token")),
        ):
            await self.collect(client)
        validator.assert_not_called()
        self.assertNotIn("x-client-work-iq-assertion", requests[0].headers)

    async def test_terminal_failure_message_code_and_cancel_reason_are_redacted(self):
        for event_type, data, error in (
            (
                "error", {"message": "synthetic-private-token", "code": "synthetic-private-token"},
                hosted.HostedAgentResponseError,
            ),
            (
                "response.cancelled", {"reason": "synthetic-private-token"},
                hosted.HostedAgentCancelledError,
            ),
        ):
            requests = []
            text = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            client = await self.client(requests, text=text)
            with patch.object(hosted, "_acquire_obo_token", new=AsyncMock(return_value="synthetic-token")):
                with self.assertRaises(error) as raised:
                    await self.collect(client)
            self.assertNotIn("synthetic-private-token", str(raised.exception))
            self.assertNotIn("synthetic-private-token", getattr(raised.exception, "code", ""))

    async def test_oauth_refresh_redacts_provider_error_and_preserves_safe_reason(self):
        user = SimpleNamespace(identifier="synthetic-user", metadata={
            "access_token": "synthetic-old", "refresh_token": "synthetic-refresh",
        })
        with (
            patch.object(oauth, "_is_access_token_expiring", return_value=True),
            patch.object(oauth, "refresh_access_token", new=AsyncMock(return_value={
                "error": "invalid_grant", "error_codes": [65001],
                "error_description": "synthetic-private-token",
            })),
            self.assertLogs(oauth.logger, level="WARNING") as logs,
        ):
            with self.assertRaises(DelegationError) as raised:
                await oauth.ensure_fresh_user_access_token(user)
        self.assertEqual("consent_required", raised.exception.code)
        self.assertNotIn("synthetic-private-token", str(raised.exception) + "\n".join(logs.output))

    def test_auth_failure_classification_does_not_use_or_render_provider_descriptions(self):
        for result, code in (
            ({"error": "invalid_grant", "error_codes": [65001]}, "consent_required"),
            ({"error": "interaction_required"}, "interaction_required"),
            ({"error": "invalid_client"}, "configuration_error"),
            ({"error": "temporarily_unavailable"}, "authentication_unavailable"),
            ({"error": "synthetic-private"}, "authentication_required"),
        ):
            result["error_description"] = "synthetic-private"
            error = DelegationError.from_msal(result)
            self.assertEqual(code, error.code)
            self.assertNotIn("synthetic-private", str(error))
        for value in ("not-json", '{"access_token":"synthetic"}', "s" * 8193, "{}"):
            self.assertIsNone(validated_claims_challenge(value))

    def test_header_capture_cannot_disable_credential_sanitization(self):
        config = Mock()
        config.get_value.return_value = None
        with (
            patch.dict(os.environ, {
                "OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS": "existing-private",
            }),
            patch.object(Telemetry, "configure_logging"),
        ):
            Telemetry.configure_monitoring(config, "synthetic-connection-setting", "ui")
            fields = os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"]
        for header in (
            "authorization", "x-client-.*", "x-ms-query-source-authorization",
            "x-ms-query-work-iq-source-authorization", "existing-private",
        ):
            self.assertIn(header, fields)

    def test_http_claims_are_bounded_structured_and_not_diagnostic_text(self):
        challenge = '{"access_token":{"acrs":{"value":"synthetic-private"}}}'
        encoded = base64.urlsafe_b64encode(challenge.encode()).decode()
        response = httpx.Response(401, headers={
            "www-authenticate": f'Bearer error="insufficient_claims", claims="{encoded}"',
        })
        error = hosted.HostedAgentHTTPError(
            401, claims_challenge=hosted._http_claims_challenge(response),
        )
        self.assertEqual(challenge, error.claims_challenge)
        self.assertEqual("interaction_required", error.code)
        self.assertNotIn("synthetic-private", repr(error))
        for header in (
            'Bearer claims="not-valid"',
            f'Bearer claims="{encoded}", claims="{encoded}"',
            "Bearer claims=" + "s" * 16385,
        ):
            self.assertIsNone(hosted._http_claims_challenge(
                httpx.Response(401, headers={"www-authenticate": header}),
            ))

    async def test_configured_native_route_rejects_service_only_authentication(self):
        requests = []
        client = await self.client(requests)
        client.settings = hosted.HostedAgentSettings(
            base_url=client.settings.base_url,
            resource_scope=client.settings.resource_scope,
            auth_mode="service_identity",
        )
        with (
            patch.dict(os.environ, {"RETRIEVAL_AUTH_MODE": "native_search"}),
            patch.object(client, "_acquire_data_plane_token", new=AsyncMock()) as acquire,
        ):
            with self.assertRaises(hosted.HostedAgentConfigError):
                await self.collect(client)
        acquire.assert_not_awaited()
        self.assertEqual([], requests)
