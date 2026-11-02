"""Managed Toolbox outcomes through the official Responses SDK, without Azure."""

import json
import os
import unittest
import warnings
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from openai import AsyncOpenAI

from gpt_rag_ui.api.native import register_native_routes
from gpt_rag_ui.auth.panel_auth import PanelPrincipal
from gpt_rag_ui.auth.auth_common import DelegationError
from gpt_rag_ui.clients.native_hosted import NativeHostedClient, HostedConsentRequired, HostedToolFailure
from gpt_rag_ui.clients.native_metadata import MemoryMetadata
from gpt_rag_ui.services.native import NativeService, NativePrincipal, NativeNotFound
from test_native_ui import settings, TENANT, OID, OTHER


class NativeToolboxOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        manager = patch.dict(os.environ, {}, clear=True)
        manager.start()
        self.addCleanup(manager.stop)
        self.owner = NativePrincipal(TENANT, OID, "authenticated")
        self.settings = settings(
            host="hosted", auth_mode="user_delegated", history=True,
            agent_url="https://agent.example.invalid/openai/v1",
            resource_scope="https://ai.azure.com/.default",
        )
        self.store = MemoryMetadata()
        self.service = self.make_service()
        self.row = await self.service.create(self.owner, "conversational")
        self.requests = []
        self.body = self.consent("https://untrusted.example.invalid/consent?private=synthetic")

        def transport(request):
            self.requests.append(request)
            return httpx.Response(200, json=self.body)

        def sdk(**kwargs):
            return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

        self.obo = AsyncMock(return_value="delegated")
        self.workload = AsyncMock(return_value="workload")
        for manager in (
            patch("gpt_rag_ui.clients.native_hosted.AsyncOpenAI", side_effect=sdk),
            patch("gpt_rag_ui.clients.native_hosted.acquire_obo_token", self.obo),
            patch("gpt_rag_ui.clients.native_hosted.acquire_service_identity_token", self.workload),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    def make_service(self):
        return NativeService(self.settings, self.store, hosted=NativeHostedClient(self.settings))

    @staticmethod
    def consent(link):
        return {
            "id": "resp_owned", "object": "response", "created_at": 1,
            "status": "incomplete", "model": "hosted", "output": [{
                "type": "oauth_consent_request", "id": "oacr_1", "response_id": "resp_owned",
                "consent_link": link, "server_label": "knowledge-base",
            }],
        }

    async def test_consent_is_actionable_without_accepting_any_link_or_losing_owner_binding(self):
        for link in (
            "https://untrusted.example.invalid/consent?private=synthetic",
            "https://account.services.ai.azure.com/api/projects/project/consent/start?private=synthetic",
            "javascript:alert('synthetic')", None,
        ):
            self.body = self.consent(link)
            with warnings.catch_warnings(record=True) as emitted:
                warnings.simplefilter("always")
                with self.assertRaises(HostedConsentRequired) as error:
                    await self.service.converse(self.owner, self.row["id"], "question")
            self.assertEqual([], emitted)
            self.assertEqual(error.exception.code, "consent_required")
            self.assertIsNone(error.exception.claims_challenge)
            self.assertIn("approved Microsoft Foundry consent", str(error.exception))
            self.assertNotIn("synthetic", str(error.exception))
            row = await self.service.owned(self.owner, self.row["id"])
            self.assertEqual(row["native_id"], "resp_owned")
            self.assertFalse(row["busy"])
        self.assertNotIn("consent_link", json.dumps(list(self.store.rows.values())))
        self.workload.assert_not_awaited()
        self.assertEqual(len(self.requests), 4)
        for request in self.requests:
            self.assertNotIn("x-ms-user-identity", request.headers)
            self.assertNotIn("x-ms-query-source-authorization", request.headers)
            self.assertNotIn("x-agent-user-id", request.headers)
        previous_calls = len(self.requests)
        other = replace(self.owner, object_id=OTHER)
        for operation in (
            self.service.converse(other, self.row["id"], "continue"),
            self.service.history(other, self.row["id"]), self.service.resume(other, self.row["id"]),
        ):
            with self.assertRaises(NativeNotFound):
                await operation
        self.assertEqual(previous_calls, len(self.requests))

    async def test_consent_progress_and_history_cannot_be_rendered_as_empty_success(self):
        await self.store.replace({**self.row, "native_id": "resp_owned"})
        for operation in (
            self.service.progress(self.owner, self.row["id"]),
            self.service.history(self.owner, self.row["id"]),
            self.service.resume(self.owner, self.row["id"]),
        ):
            with self.assertRaises(HostedConsentRequired):
                await operation
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all("/input_items" not in request.url.path for request in self.requests))

    async def test_service_consent_denies_without_obo_or_interactive_fallback(self):
        self.settings = replace(self.settings, auth_mode="service_identity")
        self.service = self.make_service()
        row = await self.service.create(self.owner, "conversational")
        with self.assertRaises(DelegationError) as error:
            await self.service.converse(self.owner, row["id"], "question")
        self.assertEqual(error.exception.code, "user_delegation_required")
        self.assertIn("OBO is disabled", str(error.exception))
        self.obo.assert_not_awaited()
        self.workload.assert_awaited_once_with("https://ai.azure.com/.default")
        self.assertEqual(len(self.requests), 1)

    async def test_service_source_denial_is_actionable_without_obo_retry(self):
        self.settings = replace(self.settings, auth_mode="service_identity")
        self.service = self.make_service()
        row = await self.service.create(self.owner, "conversational")
        app = FastAPI()
        register_native_routes(app, self.service)
        with patch("gpt_rag_ui.api.native.validate_panel_bearer", AsyncMock(
            return_value=PanelPrincipal(OID, TENANT, "authenticated"),
        )):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://ui.example.invalid",
            ) as client:
                route = f"/api/native/sessions/{row['id']}/turns"
                response = await client.post(route, json={"text": "question"})
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["detail"]["code"], "user_delegation_required")
                self.assertNotIn("untrusted", response.text)
                self.body = {
                    **self.body, "status": "failed", "output": [],
                    "error": {"code": "server_error", "message": "provider-private"},
                }
                response = await client.post(route, json={"text": "question"})
                self.assertEqual(response.status_code, 502)
                self.assertEqual(response.json()["detail"]["code"], "tool_failed")
                self.assertIn("OBO", response.json()["detail"]["message"])
                self.assertNotIn("provider-private", response.text)
        self.obo.assert_not_awaited()
        self.assertEqual(len(self.requests), 2)

    async def test_explicit_tool_failure_suppresses_success_text_and_provider_details(self):
        message = {
            "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "must not appear as success", "annotations": []}],
        }
        for tool in (
            {"type": "mcp_call", "id": "mcp_1", "name": "retrieve", "arguments": "{}",
             "server_label": "kb", "status": "failed", "error": None},
            {"type": "mcp_call", "id": "mcp_1", "name": "retrieve", "arguments": "{}",
             "server_label": "kb", "error": {"type": "http_error", "message": "provider-private"}},
            {"type": "mcp_list_tools", "id": "mcp_1", "server_label": "kb",
             "tools": [], "error": "provider-private"},
        ):
            self.body = {**self.consent("untrusted"), "status": "completed", "output": [tool, message]}
            with self.assertRaises(HostedToolFailure) as error:
                await self.service.converse(self.owner, self.row["id"], "question")
            self.assertNotIn("provider-private", str(error.exception))
            self.assertNotIn("must not appear", str(error.exception))
        self.body = {
            **self.consent("untrusted"), "status": "failed", "output": [message],
            "error": {"code": "server_error", "message": "provider-private"},
        }
        with self.assertRaises(HostedToolFailure) as error:
            await self.service.converse(self.owner, self.row["id"], "question")
        self.assertNotIn("provider-private", str(error.exception))
        self.workload.assert_not_awaited()

    async def test_valid_service_response_and_explicit_owned_retry_are_unchanged(self):
        with self.assertRaises(HostedConsentRequired):
            await self.service.converse(self.owner, self.row["id"], "question")
        self.body = {
            "id": "resp_retry", "object": "response", "created_at": 1, "status": "completed",
            "model": "hosted", "output": [{
                "type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "ordinary answer", "annotations": []}],
            }],
        }
        result = await self.service.converse(self.owner, self.row["id"], "retry explicitly")
        self.assertEqual(result["response"]["output"][0]["content"][0]["text"], "ordinary answer")
        self.assertEqual(json.loads(self.requests[-1].content)["previous_response_id"], "resp_owned")
        self.settings = replace(self.settings, auth_mode="service_identity")
        self.service = self.make_service()
        row = await self.service.create(self.owner, "conversational")
        self.obo.reset_mock()
        result = await self.service.converse(self.owner, row["id"], "question")
        self.assertEqual(result["native_ref"], "resp_retry")
        self.obo.assert_not_awaited()
        self.workload.assert_awaited_once()

    async def test_bff_returns_explicit_safe_consent_code_and_wrong_owner_continuation_is_denied(self):
        app = FastAPI()
        register_native_routes(app, self.service)
        with patch("gpt_rag_ui.api.native.validate_panel_bearer", AsyncMock(
            return_value=PanelPrincipal(OID, TENANT, "authenticated"),
        )) as validate:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="https://ui.example.invalid",
            ) as client:
                route = f"/api/native/sessions/{self.row['id']}/turns"
                response = await client.post(route, json={"text": "question"})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["detail"]["code"], "consent_required")
                self.assertNotIn("consent_link", response.text)
                self.assertNotIn("untrusted.example.invalid", response.text)
                validate.return_value = PanelPrincipal(OTHER, TENANT, "authenticated")
                self.assertEqual((await client.post(route, json={"text": "continue"})).status_code, 404)
        self.assertEqual(len(self.requests), 1)
