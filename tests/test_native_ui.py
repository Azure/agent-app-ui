"""Product behavior and real published client shapes, offline service boundaries."""

import asyncio
import importlib.metadata
import json
import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from azure.core import MatchConditions
from azure.core.exceptions import AzureError
from azure.cosmos.exceptions import CosmosHttpResponseError, CosmosResourceNotFoundError
from openai import AsyncOpenAI
from durabletask.client import OrchestrationStatus, TaskHubGrpcClient

from gpt_rag_ui.api.native import register_native_routes
from gpt_rag_ui.auth.panel_auth import PanelPrincipal
from gpt_rag_ui.auth.auth_common import DelegationError
from gpt_rag_ui.clients.native_durable import NativeDurableClient, scheduler_options
from gpt_rag_ui.clients.native_hosted import NativeHostedClient, hosted_pending
from gpt_rag_ui.clients.native_metadata import CosmosMetadata, MemoryMetadata, MetadataConflict
from gpt_rag_ui.config.appconfig import AppConfigClient, BASE_LABEL, NATIVE_UI_KEYS
from gpt_rag_ui.config.errors import ConfigurationError
from gpt_rag_ui.config.native import NativeSettings, load_native_settings
from gpt_rag_ui.services.native import (
    NativeService, NativePrincipal, NativeNotFound, NativeUnavailable,
    ResearchReviewStart, ReviewDecision,
)

TENANT = "11111111-1111-4111-8111-111111111111"
OID = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
BINDING = "Endpoint=https://scheduler.example.invalid;TaskHub=ui;Authentication=ManagedIdentity"


class Config:
    def __init__(self, values=None):
        self.values = values or {}
        self.reads = []
        self.refresh = Mock()

    def get(self, key, default=None, type=str):
        self.reads.append(key)
        return self.values.get(key, default)


def settings(**values):
    return replace(NativeSettings(
        host="container_apps", mode="both", scheduler_binding=BINDING,
        agent_name="alz-conversation", workflow_name="alz-research-review",
        database_account="metadata", database="ui", token_audience="api://ui", tenant_id=TENANT,
    ), **values)


def base_config(**values):
    return Config({
        "ORCHESTRATION_HOST": "container_apps", "APPLICATION_MODE": "both",
        "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": BINDING,
        "DURABLE_AGENT_NAME": "alz-conversation", "DURABLE_WORKFLOW_NAME": "alz-research-review",
        "DATABASE_ACCOUNT_NAME": "metadata", "DATABASE_NAME": "ui",
        "NATIVE_UI_TOKEN_AUDIENCE": "api://ui", "OAUTH_AZURE_AD_TENANT_ID": TENANT, **values,
    })


class NativeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_absent_native_preserves_existing_deployment(self):
        self.assertIsNone(load_native_settings(Config()))

    def test_modes_and_default_conversation(self):
        for mode in ("conversational", "workflow", "both"):
            self.assertEqual(load_native_settings(base_config(APPLICATION_MODE=mode)).mode, mode)
        config = base_config()
        del config.values["APPLICATION_MODE"]
        self.assertEqual(load_native_settings(config).mode, "conversational")

    def test_hosted_workflow_does_not_require_conversation_url(self):
        config = base_config(
            ORCHESTRATION_HOST="hosted", APPLICATION_MODE="workflow",
            DURABLE_TASK_SCHEDULER_CONNECTION_STRING="",
            HOSTED_WORKFLOW_BASE_URL="https://workflow.example.invalid/openai/v1",
            HOSTED_AGENT_RESOURCE_SCOPE="https://ai.azure.com/.default",
        )
        result = load_native_settings(config)
        self.assertEqual(result.agent_url, "")
        self.assertTrue(result.workflow)

    def test_owner_metadata_is_conditional_not_runtime_checkpoint_dependency(self):
        result = load_native_settings(base_config(
            APPLICATION_MODE="conversational", DATABASE_ACCOUNT_NAME="", DATABASE_NAME="",
        ))
        self.assertFalse(result.persistent_metadata)
        self.assertFalse(result.history)
        self.assertFalse(result.feedback)
        self.assertFalse(result.private_uploads)
        for feature in ("ENABLE_CHAT_HISTORY", "ENABLE_USER_FEEDBACK"):
            with self.subTest(feature=feature), self.assertRaises(ConfigurationError):
                load_native_settings(base_config(
                    APPLICATION_MODE="conversational", DATABASE_ACCOUNT_NAME="", **{feature: "true"},
                ))

    def test_reject_invalid_and_unsupported_combinations(self):
        for values in (
            {"APPLICATION_MODE": "sql"}, {"ORCHESTRATION_HOST": "classic"},
            {"ALLOW_ANONYMOUS": "true"}, {"CHAT_BACKEND": "orchestrator"},
            {"ENABLE_CHAT_HISTORY": "maybe"}, {"DURABLE_AGENT_NAME": ""},
            {"HOSTED_AGENT_BASE_URL": "https://other.example.invalid"},
            {"DEPLOY_ADMINISTRATIVE_PANEL": "true"},
            {"ENABLE_PRIVATE_ATTACHMENTS": "true", "APPLICATION_MODE": "workflow"},
        ):
            with self.subTest(values=values), self.assertRaises(ConfigurationError):
                load_native_settings(base_config(**values))

    def test_structural_env_contradiction_is_not_silent_override(self):
        with patch.dict(os.environ, {"ORCHESTRATION_HOST": "hosted"}), self.assertRaises(ConfigurationError):
            load_native_settings(base_config())

    def test_nl2sql_is_not_ui_infrastructure_or_a_configuration_consumer(self):
        config = base_config(NL2SQL_ENABLED="true", NL2SQL_SOURCE_REGISTRY="cosmos")
        load_native_settings(config)
        self.assertNotIn("NL2SQL_ENABLED", config.reads)
        self.assertFalse(any(key.startswith("NL2SQL") for key in NATIVE_UI_KEYS))

    def test_dts_mi_binding_actual_output_and_fail_closed(self):
        self.assertEqual(scheduler_options(BINDING), {
            "host_address": "scheduler.example.invalid", "taskhub": "ui", "client_id": "",
        })
        for value in (
            BINDING.replace("ManagedIdentity", "Key"), BINDING + ";Secret=bad",
            BINDING.replace("https://", "http://"), BINDING + ";TaskHub=duplicate",
            "Endpoint=https://scheduler.example.invalid;Authentication=ManagedIdentity",
        ):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                scheduler_options(value)

    def test_native_appconfig_is_scoped_label_and_uses_official_refresh(self):
        provider = Config({"ORCHESTRATION_HOST": "container_apps"})
        with (
            patch.dict(os.environ, {"APP_CONFIG_ENDPOINT": "https://config.example.invalid"}),
            patch("gpt_rag_ui.config.appconfig.load", return_value=provider) as load,
        ):
            client = AppConfigClient()
            client.refresh()
        self.assertEqual(load.call_count, 2)
        selectors = load.call_args.kwargs["selects"]
        self.assertTrue(all(selector.label_filter == BASE_LABEL for selector in selectors))
        self.assertTrue(all(selector.key_filter != "*" for selector in selectors))
        self.assertEqual(load.call_args.kwargs["refresh_interval"], 30)
        provider.refresh.assert_called_once()


class PublishedClientShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_hosted_history_and_challenge_use_actual_sdk_without_replay_or_provider_body(self):
        calls = []
        denied = False

        def transport(request):
            calls.append(request)
            if denied:
                return httpx.Response(403, json={"error": {"message": "provider-private-detail"}})
            if request.url.path.endswith("/input_items"):
                return httpx.Response(200, json={
                    "object": "list", "has_more": False, "first_id": "item_1", "last_id": "item_1",
                    "data": [{"id": "item_1", "type": "message", "role": "user",
                              "content": [{"type": "input_text", "text": "topic"}]}],
                })
            return httpx.Response(200, json={
                "id": "resp_1", "object": "response", "created_at": 1, "status": "completed",
                "output": [{"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
                            "content": [{"type": "output_text", "text": "answer", "annotations": []}]}],
                "model": "hosted",
            })

        def sdk(**kwargs):
            return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)))

        client = NativeHostedClient(settings(
            host="hosted", agent_url="https://agent.example.invalid/openai/v1",
            resource_scope="https://ai.azure.com/.default",
        ))
        with (
            patch("gpt_rag_ui.clients.native_hosted.AsyncOpenAI", side_effect=sdk),
            patch("gpt_rag_ui.clients.native_hosted.acquire_obo_token", AsyncMock(return_value="delegated")),
        ):
            history = await client.history("authenticated", "resp_1", workflow=False)
            self.assertEqual(history[0]["role"], "user")
            self.assertEqual(history[-1]["role"], "assistant")
            self.assertEqual(calls[-1].url.params["limit"], "100")
            self.assertEqual(calls[-1].url.params["order"], "asc")
            denied = True
            with self.assertRaises(DelegationError) as error:
                await client.retrieve("authenticated", "resp_1", workflow=False)
        self.assertEqual(error.exception.code, "source_denied")
        self.assertNotIn("provider-private-detail", str(error.exception))
        self.assertEqual(len(calls), 3)

    def test_package_versions_are_published_not_editable(self):
        for name, version in (
            ("agent-framework-core", "1.24.0"),
            ("agent-framework-durabletask", "1.0.0b261029"),
            ("openai", "3.38.0"),
        ):
            self.assertEqual(importlib.metadata.version(name), version)
            receipt = importlib.metadata.distribution(name).read_text("direct_url.json")
            self.assertFalse(receipt and json.loads(receipt).get("dir_info", {}).get("editable"))

    async def test_durable_progress_uses_official_workflow_client(self):
        hub = Mock(spec=TaskHubGrpcClient)
        hub.schedule_new_orchestration.return_value = "run-1"
        hub.get_orchestration_state.return_value = SimpleNamespace(
            name="dafx-v2-alz-research-review", runtime_status=OrchestrationStatus.RUNNING,
            serialized_custom_status=json.dumps({"pending_requests": {"review-1": {
                "request_id": "review-1", "source_executor_id": "human-review",
                "data": {"topic": "test", "draft": "draft"}, "response_type": "ReviewDecision",
            }}}),
        )
        client = NativeDurableClient(settings(), client=hub)
        self.assertEqual(await client.start({"topic": "test"}, "run-1"), "run-1")
        hub.schedule_new_orchestration.assert_called_once()
        self.assertEqual(hub.schedule_new_orchestration.call_args.args, ("dafx-v2-alz-research-review",))
        self.assertEqual(hub.schedule_new_orchestration.call_args.kwargs["instance_id"], "run-1")
        progress = await client.progress("run-1")
        self.assertEqual(progress["pending"][0]["request_id"], "review-1")
        await client.reply("run-1", "review-1", {"decision": "reject", "comment": ""})
        hub.raise_orchestration_event.assert_called_once_with(
            "run-1", event_name="review-1", data={"decision": "reject", "comment": ""},
        )
        # A fresh client reads the same SDK backend, no waiting UI coroutine.
        fresh = NativeDurableClient(settings(), client=hub)
        self.assertEqual((await fresh.progress("run-1"))["pending"], progress["pending"])

    async def test_durable_wrong_graph_and_terminal_replies_are_denied_by_sdk(self):
        hub = Mock(spec=TaskHubGrpcClient)
        hub.get_orchestration_state.return_value = SimpleNamespace(
            name="dafx-other", runtime_status=OrchestrationStatus.RUNNING,
            serialized_custom_status="{}",
        )
        client = NativeDurableClient(settings(), client=hub)
        with self.assertRaises(ValueError):
            await client.reply("run-1", "review-1", {"decision": "approve"})
        hub.raise_orchestration_event.assert_not_called()

    async def test_durable_completion_uses_native_output_codec(self):
        hub = Mock(spec=TaskHubGrpcClient)
        state = SimpleNamespace(
            name="dafx-v2-alz-research-review", runtime_status=OrchestrationStatus.COMPLETED,
            serialized_output='[{"status":"rejected","draft":"draft"}]',
            serialized_custom_status="{}",
        )
        hub.get_orchestration_state.return_value = state
        hub.wait_for_orchestration_completion.return_value = state
        result = await NativeDurableClient(settings(), client=hub).progress("run-1")
        self.assertEqual(result["output"][0]["status"], "rejected")
        hub.wait_for_orchestration_completion.assert_called_once_with("run-1", timeout=5)

    async def test_hosted_official_responses_payload_and_native_retrieve(self):
        calls = []

        def transport(request):
            calls.append(request)
            return httpx.Response(200, json={
                "id": "resp_1", "object": "response", "created_at": 1, "status": "completed",
                "output": [], "model": "hosted", "error": None, "incomplete_details": None,
                "instructions": None, "metadata": {}, "parallel_tool_calls": False,
                "temperature": None, "tool_choice": "auto", "tools": [], "top_p": None,
            })

        sdk = AsyncOpenAI(
            base_url="https://workflow.example.invalid/openai/v1", api_key="test",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(transport)), max_retries=0,
        )
        client = NativeHostedClient(settings(
            host="hosted", workflow_url="https://workflow.example.invalid/openai/v1",
            resource_scope="https://ai.azure.com/.default",
        ))
        with (
            patch("gpt_rag_ui.clients.native_hosted.AsyncOpenAI", return_value=sdk),
            patch("gpt_rag_ui.clients.native_hosted.acquire_obo_token", AsyncMock(return_value="delegated")) as obo,
        ):
            result = await client.create(
                "user-assertion", workflow=True, previous_response_id="resp_previous",
                replies=[{"type": "function_call_output", "call_id": "call_1", "output": '{"decision":"reject"}'}],
            )
        self.assertEqual(result["id"], "resp_1")
        obo.assert_awaited_once_with("user-assertion", "https://ai.azure.com/.default")
        payload = json.loads(calls[0].content)
        self.assertTrue(payload["store"])
        self.assertEqual(payload["previous_response_id"], "resp_previous")
        self.assertEqual(payload["input"][0]["call_id"], "call_1")
        self.assertEqual(calls[0].url.path, "/openai/v1/responses")
        self.assertNotIn("x-ms-user-identity", calls[0].headers)
        self.assertNotIn("conversation_id", payload)

    def test_hosted_pending_matches_published_request_info_projection(self):
        response = {"output": [{
            "type": "function_call", "name": "request_info", "call_id": "call_1",
            "arguments": json.dumps({"request_id": "call_1", "request": {"draft": "draft"}}),
        }]}
        self.assertEqual(hosted_pending(response)[0]["request_id"], "call_1")
        response["output"][0]["arguments"] = '{"request_id":"wrong","request":{}}'
        with self.assertRaises(ValueError):
            hosted_pending(response)


class NativeOwnerBehaviorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.owner = NativePrincipal(TENANT, OID, "authenticated")
        self.other = NativePrincipal(TENANT, OTHER, "authenticated")
        self.store = MemoryMetadata()
        self.durable = SimpleNamespace(
            start=AsyncMock(side_effect=lambda input, id: id),
            progress=AsyncMock(return_value={
                "status": "RUNNING", "pending": [{
                    "request_id": "request-1", "source_executor_id": "human-review",
                    "data": {"topic": "test", "draft": "draft", "configuration_revision": "revision-1"},
                }], "output": None,
            }),
            reply=AsyncMock(), history=AsyncMock(return_value=[{"role": "user", "content": "native"}]),
            converse=AsyncMock(return_value={"messages": []}), aclose=AsyncMock(),
        )
        self.service = NativeService(settings(history=True, feedback=True), self.store, durable=self.durable)
        self.row = await self.service.create(self.owner, "workflow")
        self.id = self.row["id"]
        await self.service.start(self.owner, self.id, ResearchReviewStart(topic="test"))

    async def test_wrong_owner_denied_before_native_progress_history_feedback_resume(self):
        self.durable.progress.reset_mock()
        for operation in (
            self.service.owned(self.other, self.id),
            self.service.progress(self.other, self.id),
            self.service.history(self.other, self.id),
            self.service.feedback(self.other, self.id, 1, "", self.id),
            self.service.resume(self.other, self.id),
            self.service.reply(self.other, self.id, "request-1", "bad", ReviewDecision(decision="approve")),
        ):
            with self.assertRaises(NativeNotFound):
                await operation
        self.durable.progress.assert_not_awaited()
        self.durable.reply.assert_not_awaited()

    async def test_reject_then_duplicate_is_not_reported_as_success(self):
        progress = await self.service.progress(self.owner, self.id)
        result = await self.service.reply(
            self.owner, self.id, "request-1", progress["revision"], ReviewDecision(decision="reject"),
        )
        self.assertEqual(result["delivery"], "submitted")
        self.durable.reply.assert_awaited_once_with(
            self.id, "request-1", {"decision": "reject", "comment": ""},
        )
        with self.assertRaises(MetadataConflict):
            await self.service.reply(
                self.owner, self.id, "request-1", progress["revision"], ReviewDecision(decision="approve"),
            )
        self.assertFalse((await self.service.owned(self.owner, self.id))["busy"])

    async def test_stale_wrong_and_not_pending_actions_fail_closed(self):
        progress = await self.service.progress(self.owner, self.id)
        for request_id, rev in (("wrong", progress["revision"]), ("request-1", "stale")):
            with self.assertRaises(MetadataConflict):
                await self.service.reply(self.owner, self.id, request_id, rev, ReviewDecision(decision="approve"))
        self.durable.progress.return_value["pending"] = []
        with self.assertRaises(MetadataConflict):
            await self.service.reply(
                self.owner, self.id, "request-1", progress["revision"], ReviewDecision(decision="approve"),
            )
        self.durable.reply.assert_not_awaited()

    async def test_saved_owner_mapping_fresh_bff_reads_native_progress(self):
        fresh = NativeService(settings(history=True), self.store, durable=self.durable)
        self.assertEqual((await fresh.list_sessions(self.owner))[0]["id"], self.id)
        self.assertEqual((await fresh.progress(self.owner, self.id))["status"], "waiting_for_input")
        row = await self.store.get(self.owner.owner, self.id)
        self.assertNotIn("draft", row)
        self.assertNotIn("pending", row)
        self.assertNotIn("token", row)
        self.assertNotIn("transcript", row)

    async def test_host_switch_or_changed_registration_is_not_state_migration(self):
        fresh = NativeService(settings(workflow_name="new-graph"), self.store, durable=self.durable)
        with self.assertRaises(NativeNotFound):
            await fresh.owned(self.owner, self.id)
        other_hub = NativeService(settings(scheduler_binding=BINDING.replace("TaskHub=ui", "TaskHub=other")), self.store, durable=self.durable)
        with self.assertRaises(NativeNotFound):
            await other_hub.resume(self.owner, self.id)

    async def test_workflow_resume_with_history_off_uses_known_owned_selector(self):
        fresh = NativeService(settings(history=False), self.store, durable=self.durable)
        self.assertEqual((await fresh.resume(self.owner, self.id))["status"], "waiting_for_input")
        with self.assertRaises(NativeUnavailable):
            await fresh.list_sessions(self.owner)

    async def test_revoked_user_and_changed_application_mode_are_denied_before_backend(self):
        config = base_config(ALLOWED_USER_PRINCIPALS=OTHER)
        service = NativeService(settings(), self.store, durable=self.durable, config=config)
        self.durable.progress.reset_mock()
        with self.assertRaises(PermissionError):
            await service.resume(self.owner, self.id)
        config.values["ALLOWED_USER_PRINCIPALS"] = ""
        config.values["APPLICATION_MODE"] = "conversational"
        with self.assertRaises(NativeUnavailable):
            await service.resume(self.owner, self.id)
        self.durable.progress.assert_not_awaited()

    async def test_non_json_and_wrong_review_contracts_do_not_expose_actions(self):
        for data in ({"__pickle__": "worker-module"}, {"draft": "missing fields"}, "draft"):
            self.durable.progress.return_value["pending"][0]["data"] = data
            with self.assertRaises(NativeUnavailable):
                await self.service.progress(self.owner, self.id)
        self.durable.reply.assert_not_awaited()

    async def test_unknown_stored_application_is_not_an_authorization_shortcut(self):
        row = await self.store.get(self.owner.owner, self.id)
        await self.store.replace({**row, "application": "history"})
        with self.assertRaises(NativeNotFound):
            await self.service.owned(self.owner, self.id)

    async def test_flags_and_feedback_writes_are_real(self):
        feedback = await self.service.feedback(self.owner, self.id, 1, "useful", self.id)
        stored = await self.store.get(self.owner.owner, feedback["id"])
        self.assertEqual(stored["comment"], "useful")
        self.assertNotIn("output", stored)
        disabled = NativeService(settings(), self.store, durable=self.durable)
        with self.assertRaises(NativeUnavailable):
            await disabled.feedback(self.owner, self.id, 1, "", self.id)
        with self.assertRaises(NativeUnavailable):
            await disabled.history(self.owner, self.id)
        with self.assertRaises(MetadataConflict):
            await self.service.feedback(self.owner, self.id, 1, "", "wrong-ref")
        with patch.object(self.store, "create", AsyncMock(side_effect=AzureError("unavailable"))):
            with self.assertRaises(AzureError):
                await self.service.feedback(self.owner, self.id, 1, "", self.id)

    async def test_failed_action_remains_uncertain_and_never_reports_success(self):
        progress = await self.service.progress(self.owner, self.id)
        self.durable.reply.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.service.reply(
                self.owner, self.id, "request-1", progress["revision"], ReviewDecision(decision="approve"),
            )
        with self.assertRaises(MetadataConflict):
            await self.service.reply(
                self.owner, self.id, "request-1", progress["revision"], ReviewDecision(decision="approve"),
            )

    async def test_metadata_cas_rejects_concurrent_update(self):
        row = await self.service.owned(self.owner, self.id)
        await self.store.replace({**row, "title": "first"})
        with self.assertRaises(MetadataConflict):
            await self.store.replace({**row, "title": "second"})

    async def test_refresh_rechecks_feature_gates_without_rebuilding(self):
        config = base_config(ENABLE_CHAT_HISTORY="true", ENABLE_USER_FEEDBACK="true")
        service = NativeService(settings(history=True, feedback=True), self.store, durable=self.durable, config=config)
        await service.history(self.owner, self.id)
        config.values["ENABLE_CHAT_HISTORY"] = "false"
        with self.assertRaises(NativeUnavailable):
            await service.history(self.owner, self.id)
        self.assertGreaterEqual(config.refresh.call_count, 2)


class NativeRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryMetadata()
        self.service = NativeService(settings(history=True), self.store, durable=SimpleNamespace(progress=AsyncMock()))
        self.owner = NativePrincipal(TENANT, OID, "authenticated")
        self.row = asyncio.run(self.service.create(self.owner, "workflow"))
        app = FastAPI()
        register_native_routes(app, self.service)
        self.client = TestClient(app)

    def test_missing_bearer_cannot_use_identity_header(self):
        response = self.client.get(
            f"/api/native/sessions/{self.row['id']}/progress", headers={"x-ms-user-identity": OID},
        )
        self.assertEqual(response.status_code, 401)

    def test_validated_wrong_owner_gets_404_before_backend(self):
        with patch("gpt_rag_ui.api.native.validate_panel_bearer", AsyncMock(
            return_value=PanelPrincipal(OTHER, TENANT, "authenticated"),
        )):
            response = self.client.get(f"/api/native/sessions/{self.row['id']}/progress")
        self.assertEqual(response.status_code, 404)
        self.service.durable.progress.assert_not_awaited()

    def test_strict_business_inputs_cannot_supply_owner_or_native_id(self):
        with patch("gpt_rag_ui.api.native.validate_panel_bearer", AsyncMock(
            return_value=PanelPrincipal(OID, TENANT, "authenticated"),
        )):
            response = self.client.post(f"/api/native/sessions/{self.row['id']}/start", json={
                "topic": "test", "owner": OTHER, "native_id": "forged",
            })
        self.assertEqual(response.status_code, 422)

    def test_turn_returns_opaque_feedback_reference_without_client_host_or_id_guessing(self):
        self.service.durable.converse = AsyncMock(return_value={"messages": [{"role": "assistant", "contents": []}]})
        self.service.settings = settings(history=True, feedback=True)
        with patch("gpt_rag_ui.api.native.validate_panel_bearer", AsyncMock(
            return_value=PanelPrincipal(OID, TENANT, "authenticated"),
        )):
            created = self.client.post("/api/native/sessions", json={"application": "conversational"})
            id = created.json()["id"]
            turn = self.client.post(f"/api/native/sessions/{id}/turns", json={"text": "test"})
            self.assertEqual(turn.status_code, 200)
            self.assertEqual(turn.json()["native_ref"], id)
            feedback = self.client.post(f"/api/native/sessions/{id}/feedback", json={
                "native_ref": turn.json()["native_ref"], "rating": 1, "comment": "useful",
            })
        self.assertEqual(feedback.status_code, 201)


class NativeCosmosContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_partition_cas_duplicate_not_found_and_failed_writes_use_real_cosmos_shapes(self):
        container = SimpleNamespace(
            read_item=AsyncMock(side_effect=CosmosResourceNotFoundError()),
            create_item=AsyncMock(side_effect=CosmosHttpResponseError(status_code=409)),
            replace_item=AsyncMock(side_effect=CosmosHttpResponseError(status_code=412)),
        )
        client = Mock()
        client.get_database_client.return_value.get_container_client.return_value = container
        with patch("gpt_rag_ui.clients.native_metadata.CosmosClient", return_value=client):
            store = CosmosMetadata(settings(), credential=Mock())
        owner = NativePrincipal(TENANT, OID, "authenticated").owner
        self.assertIsNone(await store.get(owner, "session-1"))
        container.read_item.assert_awaited_once_with("session-1", partition_key=owner)
        row = {"id": "session-1", "principal_id": owner, "_etag": "etag-1"}
        with self.assertRaises(MetadataConflict):
            await store.create(row)
        with self.assertRaises(MetadataConflict):
            await store.replace(row)
        container.replace_item.assert_awaited_once_with(
            "session-1", row, etag="etag-1", match_condition=MatchConditions.IfNotModified,
        )
        container.create_item.side_effect = CosmosHttpResponseError(status_code=503)
        with self.assertRaises(CosmosHttpResponseError):
            await store.create(row)
        client.get_database_client.assert_called_once_with("ui")
        client.get_database_client.return_value.get_container_client.assert_called_once_with("native-ui-metadata")


if __name__ == "__main__":
    unittest.main()
