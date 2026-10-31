"""Native Chainlit handlers and real ASGI composition, offline dependencies."""

import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from durabletask.client import TaskHubGrpcClient, OrchestrationStatus
from gpt_rag_ui.api import native_chat
from gpt_rag_ui.clients.native_durable import NativeDurableClient
from gpt_rag_ui.clients.native_metadata import MemoryMetadata
from gpt_rag_ui.services.native import NativeService, ResearchReviewStart
from test_native_ui import settings, NativePrincipal, TENANT, OID, OTHER


class NativePresentationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = NativePrincipal(TENANT, OID, "authenticated")
        self.session = {}
        self.messages = []
        self.auth = AsyncMock(return_value={
            "authorized": True, "tenant_id": TENANT, "object_id": OID,
            "access_token": "authenticated", "copilot_session_id": "",
        })
        self.client = SimpleNamespace(
            start=AsyncMock(side_effect=lambda input, id: id),
            progress=AsyncMock(return_value={
                "status": "RUNNING", "pending": [{
                    "request_id": "review-1", "source_executor_id": "human-review",
                    "data": {"topic": "test", "draft": "draft", "configuration_revision": "revision-1"},
                }], "output": None,
            }),
            reply=AsyncMock(),
            converse=AsyncMock(return_value={"messages": [{
                "role": "assistant", "contents": [{"type": "text", "text": "native answer"}],
            }]}),
        )
        self.service = NativeService(settings(), MemoryMetadata(), durable=self.client)

        def message(**kwargs):
            self.messages.append(kwargs)
            return SimpleNamespace(send=AsyncMock())

        for manager in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(native_chat, "_get_auth_info", self.auth),
            patch.object(native_chat, "get_service", return_value=self.service),
            patch.object(native_chat.cl.user_session, "get", side_effect=lambda key: self.session.get(key)),
            patch.object(native_chat.cl.user_session, "set", side_effect=self.session.__setitem__),
            patch.object(native_chat.cl, "Message", side_effect=message),
            patch.object(native_chat, "replace_source_reference_links", side_effect=lambda text, *args, **kwargs: text),
            patch.object(native_chat, "format_hosted_citation_sources", return_value=""),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    async def test_conversar_and_named_process_are_ordinary_profiles(self):
        self.session["chat_profile"] = "Conversar"
        await native_chat.on_start()
        id = self.session["native_session_id"]
        self.assertEqual((await self.service.owned(self.owner, id))["application"], "conversational")
        await native_chat.handle_message(SimpleNamespace(content="hello", elements=[]))
        self.assertEqual(self.messages[-1]["content"], "native answer")
        self.session["chat_profile"] = self.service.settings.workflow_title
        await native_chat.on_start()
        await native_chat.handle_message(SimpleNamespace(content="test", elements=[]))
        self.assertIn("draft", self.messages[-1]["content"])
        self.assertEqual({action.name for action in self.messages[-1]["actions"]},
                         {"native_refresh", "native_approve", "native_reject"})
        self.assertIn("/reabrir ", self.messages[-1]["content"])

    async def test_review_action_delivers_without_waiting_ui_coroutine_and_duplicate_is_denied(self):
        row = await self.service.create(self.owner, "workflow")
        progress = await self.service.start(self.owner, row["id"], ResearchReviewStart(topic="test"))
        action = SimpleNamespace(name="native_reject", payload={
            "id": row["id"], "request_id": "review-1", "revision": progress["revision"],
        })
        await native_chat.handle_action(action)
        self.client.reply.assert_awaited_once_with(row["id"], "review-1", {"decision": "reject", "comment": ""})
        await native_chat.handle_action(action)
        self.assertEqual(self.client.reply.await_count, 1)
        self.assertIn("indisponível", self.messages[-1]["content"])

    async def test_workflow_reopen_checks_owner_before_changing_active_session(self):
        row = await self.service.create(self.owner, "workflow")
        await self.service.start(self.owner, row["id"], ResearchReviewStart(topic="test"))
        await native_chat.handle_message(SimpleNamespace(content=f"/reabrir {row['id']}", elements=[]))
        self.assertEqual(self.session["native_session_id"], row["id"])
        self.auth.return_value["object_id"] = OTHER
        self.session["native_session_id"] = "unchanged"
        self.client.progress.reset_mock()
        await native_chat.handle_message(SimpleNamespace(content=f"/reabrir {row['id']}", elements=[]))
        self.assertEqual(self.session["native_session_id"], "unchanged")
        self.client.progress.assert_not_awaited()

    async def test_native_auth_denial_does_not_send_a_turn(self):
        self.auth.return_value["authorized"] = False
        await native_chat.handle_message(SimpleNamespace(content="hello", elements=[]))
        self.client.converse.assert_not_awaited()
        self.assertIn("Entre", self.messages[-1]["content"])

    async def test_refresh_reads_json_native_completed_product_result(self):
        hub = Mock(spec=TaskHubGrpcClient)
        result = {
            "status": "approved", "topic": "test", "draft": "final draft",
            "review_comment": "", "configuration_revision": "revision-1",
        }
        state = SimpleNamespace(
            name="dafx-v2-alz-research-review", runtime_status=OrchestrationStatus.COMPLETED,
            serialized_output=json.dumps([result]), serialized_custom_status="{}",
        )
        hub.get_orchestration_state.return_value = state
        hub.wait_for_orchestration_completion.return_value = state
        row = await self.service.create(self.owner, "workflow")
        await self.service.metadata.replace({**row, "native_id": row["id"]})
        fresh = NativeService(settings(), self.service.metadata, durable=NativeDurableClient(settings(), client=hub))
        progress = await fresh.resume(self.owner, row["id"])
        self.assertEqual(progress["output"], [result])
        await native_chat.render_progress(row["id"], progress)
        self.assertIn("final draft", self.messages[-1]["content"])
        self.assertEqual([action.name for action in self.messages[-1]["actions"]], ["native_refresh"])


class NativeCompositionTests(unittest.TestCase):
    def test_native_asgi_routes_precede_chainlit_and_legacy_panel_and_public_blob_are_absent(self):
        script = "import sys; sys.path.insert(0, " + repr(str(Path(__file__).parent)) + ")\n" + """
import asyncio, os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from test_native_ui import base_config, settings, NativePrincipal, TENANT, OID
from gpt_rag_ui.clients.native_metadata import MemoryMetadata
from gpt_rag_ui.services.native import NativeService
from gpt_rag_ui.auth.panel_auth import PanelPrincipal
from gpt_rag_ui.config.embed_config import EmbedSettings
config = base_config(OAUTH_AZURE_AD_CLIENT_ID=OID, OAUTH_AZURE_AD_CLIENT_SECRET='offline-test-only')
config.connected = False
config.get_value = lambda key, **kwargs: None
service = NativeService(settings(), MemoryMetadata(), durable=SimpleNamespace(aclose=AsyncMock()))
with patch('gpt_rag_ui.config.dependencies.get_config', return_value=config):
    from gpt_rag_ui import bootstrap
    with patch('gpt_rag_ui.api.native_chat.get_service', return_value=service):
        app = bootstrap._create_chainlit_app(config, auth_state=bootstrap._evaluate_auth_state(
            config, EmbedSettings(),
        ), embed_settings=EmbedSettings())
paths = [getattr(route, 'path', '') for route in app.routes]
assert paths[-1] == ''
assert '/api/download/{grant_token}' in paths
assert not any(path.startswith('/panel') for path in paths)
assert not any(path == '/api/download' for path in paths)
with patch('gpt_rag_ui.api.native_chat.get_service', return_value=service), TestClient(app) as client:
    assert client.get('/api/native/sessions').status_code == 401
    assert client.get('/api/download/invalid').status_code == 404
    with patch('gpt_rag_ui.api.native.validate_panel_bearer', AsyncMock(
        return_value=PanelPrincipal(OID, TENANT, 'authenticated'),
    )):
        response = client.post('/api/native/sessions', json={'application':'workflow'})
        assert response.status_code == 201, response.text
        id = response.json()['id']
        assert client.get('/api/native/sessions/'+id+'/progress').json()['status'] == 'not_started'
"""
        env = {key: value for key, value in os.environ.items() if key not in {
            "CHAT_BACKEND", "APP_CONFIG_ENDPOINT", "APPLICATION_MODE", "ORCHESTRATION_HOST",
        }}
        env.update({"CHAINLIT_AUTH_SECRET": "0" * 32, "CHAINLIT_URL": "https://ui.example.invalid", "ALLOW_ANONYMOUS": "false"})
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
