"""Native Chainlit handlers and real ASGI composition, offline dependencies."""

import asyncio
import json
import inspect
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from durabletask.client import TaskHubGrpcClient, OrchestrationStatus
from gpt_rag_ui.api import native_chat
from gpt_rag_ui.auth.auth_common import DelegationError
from gpt_rag_ui.clients.native_durable import NativeDurableClient
from gpt_rag_ui.clients.native_hosted import HostedConsentRequired, HostedToolFailure
from gpt_rag_ui.clients.native_metadata import MemoryMetadata
from gpt_rag_ui.services.native import NativeService, ResearchReviewStart
from test_native_ui import settings, NativePrincipal, TENANT, OID, OTHER


class NativePresentationTests(unittest.IsolatedAsyncioTestCase):
    def test_text_output_preserves_existing_public_typed_signature(self):
        self.assertEqual(list(inspect.signature(native_chat.text_output).parameters), ["value"])

    async def asyncSetUp(self):
        self.orchestrator_root = os.environ.get("INLINE_ORCH_TEST_ROOT")
        self.orchestrator_python = os.environ.get("INLINE_ORCH_TEST_PYTHON")
        self.orchestrator_environment = {
            key: value for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}
        }
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
            patch.object(native_chat.cl, "CustomElement", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)),
            patch.object(native_chat, "replace_source_reference_links", side_effect=lambda text, *args, **kwargs: text),
            patch.object(native_chat, "format_hosted_citation_sources", return_value=""),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    async def test_text_only_handler_uses_shared_readable_projection_without_visual_elements(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json").read_text())
        row = await self.service.create(self.owner, "conversational")
        wire = {"response": {"messages": [
            {"role": "assistant", "contents": [{"type": "text", "text": "Authorized narration"}]},
            {"role": "assistant", "contents": [{"type": "text", "text": json.dumps(fixture)}]},
        ]}}
        expected = "Authorized narration\n\n" + fixture["markdown"]
        self.assertEqual(native_chat.text_output(wire), expected)
        self.assertEqual(await native_chat.display_output(row["id"], wire), expected)
        self.assertEqual(self.messages, [])
        self.assertNotIn("agent_lz_presentation", expected)
        completed = json.dumps({"status": "approved", "draft": expected})
        self.assertEqual(native_chat.text_output(completed), "approved\n\n" + expected)

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

    async def test_admitted_assistant_envelope_displays_exact_fallback_and_shared_element(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json").read_text())
        self.client.converse.return_value = {"messages": [{
            "role": "assistant", "contents": [{"type": "text", "text": json.dumps(fixture)}],
        }]}
        self.session["chat_profile"] = "Conversar"
        await native_chat.on_start()
        await native_chat.handle_message(SimpleNamespace(content="compare", elements=[]))
        self.assertEqual(self.messages[-1]["content"], fixture["markdown"])
        self.assertEqual(len(self.messages[-1]["elements"]), 1)
        element = self.messages[-1]["elements"][0]
        self.assertEqual(element.name, "Presentation")
        self.assertEqual(element.props["contributions"][0]["blocks"][0]["cards"][0]["value"]["value"], "9.750")

    async def test_optional_business_fields_omit_nonvisual_and_keep_saved_values(self):
        from gpt_rag_ui.services.native import ReviewRequest, ResearchReviewResult
        ordinary = ReviewRequest(topic="test", draft="readable", configuration_revision="rev")
        self.assertNotIn("presentation", json.loads(ordinary.model_dump_json()))
        fixture = json.loads((Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json").read_text())
        request = ReviewRequest(**ordinary.model_dump(), presentation=fixture)
        result = ResearchReviewResult(status="approved", review_comment="", **request.model_dump())
        self.assertEqual(result.presentation["contributions"][0]["blocks"][0]["cards"][0]["value"]["value"], "9.750")

    async def test_element_construction_failure_keeps_authorized_readable_answer(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json").read_text())
        self.client.converse.return_value = {"messages": [{
            "role": "assistant", "contents": [{"type": "text", "text": json.dumps(fixture)}],
        }]}
        self.session["chat_profile"] = "Conversar"
        await native_chat.on_start()
        with patch.object(native_chat.cl, "CustomElement", side_effect=ValueError("private fixture failure")):
            await native_chat.handle_message(SimpleNamespace(content="compare", elements=[]))
        self.assertEqual(self.messages[-1]["content"], fixture["markdown"])
        self.assertEqual(self.messages[-1]["elements"], [])

    async def test_history_citations_preserved_only_in_admitted_assistant_messages(self):
        self.session["chat_profile"] = "Conversar"
        await native_chat.on_start()
        id = self.session["native_session_id"]
        good = {"type": "url_citation", "url": "https://source.example.invalid/authorized", "title": "Reviewed"}
        bad = {"type": "url_citation", "url": "https://source.example.invalid/untrusted", "title": "Spoof"}
        value = {"items": [{"messages": [
            {"role": "assistant", "contents": [{"$type": "text", "text": "Authorized narration", "annotations": [good]}]},
            {"role": "user", "contents": [{"type": "text", "text": "Inert user", "annotations": [bad]}]},
            {"role": "tool", "messages": [{"role": "assistant", "contents": [{"type": "text", "annotations": [bad]}]}]},
        ]}]}
        with patch.object(native_chat, "format_hosted_citation_sources", return_value="\nReviewed source") as formatter:
            rendered = await native_chat.display_output(id, value)
            self.assertIn("Authorized narration", rendered)
            self.assertIn("Reviewed source", rendered)
            self.assertEqual(formatter.call_args.args[0], [good])
            self.assertEqual(formatter.call_args.kwargs["principal_id"], self.owner.owner)

    async def test_local_history_revocation_and_new_action_fail_before_native_access(self):
        from dataclasses import replace
        from gpt_rag_ui.services.native import NativeNotFound, NativeUnavailable
        self.service.settings = replace(self.service.settings, history=True)
        self.session["chat_profile"] = "Conversar"
        await native_chat.on_start()
        id = self.session["native_session_id"]
        await self.service.converse(self.owner, id, "Authorized turn")
        fixture = (Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json").read_text()
        self.client.history = AsyncMock(return_value=[{
            "role": "assistant", "contents": [{"type": "text", "text": fixture}],
        }])
        restored = await self.service.history(self.owner, id)
        self.assertEqual(native_chat.presentations(restored)[0]["contributions"][0]["agentId"], "agent-lz-conversation")
        self.client.converse.reset_mock()
        self.client.history.reset_mock()
        with patch.object(self.service.metadata, "get", new=AsyncMock(return_value=None)):
            with self.assertRaises(NativeNotFound):
                await self.service.history(self.owner, id)
            with self.assertRaises(NativeNotFound):
                await self.service.converse(self.owner, id, "Inspect revenue for month mar")
        self.client.history.assert_not_awaited()
        self.client.converse.assert_not_awaited()
        unavailable = replace(self.service.settings, history=False)
        with patch.object(self.service, "current", new=AsyncMock(return_value=unavailable)):
            with self.assertRaises(NativeUnavailable):
                await self.service.history(self.owner, id)
        self.client.history.assert_not_awaited()

    @unittest.skipUnless(os.environ.get("INLINE_ORCH_TESTS"), "Cross-component source paths explicitly required")
    async def test_actual_middleware_handler_disabled_deadline_spoof_and_native_restore(self):
        """Actual tool invocation/producer/handlers, controlled model and state IO."""
        if not self.orchestrator_root or not self.orchestrator_python:
            raise AssertionError("Qualified orchestrator source and isolated test Python are required")
        orchestrator = Path(self.orchestrator_root).resolve(strict=True)
        from agent_framework_durabletask import (
            DurableAgentState, DurableAgentStateResponse, DurableAgentStateMessage, DurableAgentStateTextContent,
        )
        agent_name = "agent-lz-conversation"
        for scenario in ("enabled", "disabled", "deadline", "spoof"):
            with self.subTest(scenario=scenario):
                counts, saved, guidance = {}, [], []
                async def converse(session_key, text, **kwargs):
                    process = await asyncio.create_subprocess_exec(
                        self.orchestrator_python, "-I",
                        str(Path(__file__).with_name("native_presentation_probe.py")),
                        str(orchestrator), scenario,
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, env=self.orchestrator_environment,
                    )
                    payload = json.dumps({
                        "text": text, "spoof": (
                            Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json"
                        ).read_text(),
                    }).encode()
                    try:
                        output, errors = await asyncio.wait_for(process.communicate(payload), timeout=60)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    self.assertEqual(process.returncode, 0, errors.decode(errors="replace"))
                    result = json.loads(output)
                    counts.update(result["counts"])
                    guidance.extend(result["guidance"])
                    saved[:] = result["response"]["messages"]
                    return result["response"]
                self.client.converse.side_effect = converse
                self.session["chat_profile"] = "Conversar"
                await native_chat.on_start()
                await native_chat.handle_message(SimpleNamespace(content="Compare", elements=[]))
                live = self.messages[-1]
                self.assertEqual(len(live["elements"]), 1 if scenario == "enabled" else 0)
                if scenario != "spoof":
                    self.assertEqual(counts, {agent_name: 1})
                    self.assertIn("authorized narration", live["content"])
                if scenario == "disabled":
                    self.assertFalse(guidance)
                if scenario == "enabled":
                    self.assertEqual(live["elements"][0].props["contributions"][0]["blocks"][0]["rows"][0]["values"]["c0"],
                                     "9007199254740993.001")
                    native_state = DurableAgentState()
                    native_state.data.conversation_history.append(DurableAgentStateResponse(
                        correlation_id="fixture-turn", created_at=None, messages=[
                            DurableAgentStateMessage(
                                role=m["role"], message_id=m.get("message_id", m.get("id")),
                                contents=[DurableAgentStateTextContent(text=c["text"]) for c in m["contents"]
                                          if c.get("type") == "text"],
                            ) for m in saved if m["role"] == "assistant" and any(c.get("type") == "text" for c in m["contents"])
                        ],
                    ))
                    state = native_state.to_dict()
                    hub = Mock(spec=TaskHubGrpcClient)
                    hub.get_entity.return_value = SimpleNamespace(get_state=lambda: json.loads(json.dumps(state)))
                    fresh = NativeService(settings(history=True), self.service.metadata,
                                          durable=NativeDurableClient(settings(), client=hub))
                    id = self.session["native_session_id"]
                    await fresh.history(self.owner, id)
                    self.client.converse.reset_mock()
                    with patch.object(native_chat, "get_service", return_value=fresh):
                        await native_chat.handle_message(SimpleNamespace(content=f"/reabrir {id}", elements=[]))
                        restored = self.messages[-1]
                        self.assertEqual(len(restored.get("elements", [])), 1, restored["content"])
                        self.assertEqual(restored["elements"][0].props, live["elements"][0].props)
                        self.client.converse.assert_not_awaited()
                        hub.get_entity.reset_mock()
                        self.auth.return_value["object_id"] = OTHER
                        await native_chat.handle_message(SimpleNamespace(content=f"/reabrir {id}", elements=[]))
                        hub.get_entity.assert_not_called()
                        self.auth.return_value["object_id"] = OID
                sealed = (live["content"], [json.dumps(e.props, sort_keys=True) for e in live["elements"]])
                await asyncio.sleep(0.3 if scenario == "deadline" else 0)
                self.assertEqual(sealed, (live["content"], [json.dumps(e.props, sort_keys=True) for e in live["elements"]]))

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

    async def test_noninteractive_service_failures_are_visible_without_a_signin_action(self):
        error = DelegationError("authentication_unavailable")
        with patch.object(native_chat, "offer_auth_recovery", AsyncMock(return_value=False)) as recovery:
            row = await self.service.create(self.owner, "conversational")
            self.session["native_session_id"] = row["id"]
            self.client.converse.side_effect = error
            await native_chat.handle_message(SimpleNamespace(content="hello", elements=[]))
            self.assertEqual(self.messages[-1], {"content": str(error)})
            with patch.object(self.service, "progress", AsyncMock(side_effect=error)):
                await native_chat.handle_action(SimpleNamespace(name="native_refresh", payload={"id": row["id"]}))
            self.assertEqual(self.messages[-1], {"content": str(error)})
            self.assertEqual(recovery.await_count, 2)

    async def test_managed_consent_and_tool_failure_have_visible_safe_noninteractive_results(self):
        for error, expected in (
            (HostedConsentRequired(), "approved Microsoft Foundry consent"),
            (HostedToolFailure(), "check its authorization"),
            (HostedToolFailure(auth_mode="service_identity"), "enable OBO"),
            (DelegationError("user_delegation_required"), "OBO is disabled"),
        ):
            row = await self.service.create(self.owner, "conversational")
            self.session["native_session_id"] = row["id"]
            self.client.converse.side_effect = error
            await native_chat.handle_message(SimpleNamespace(content="question", elements=[]))
            self.assertIn(expected, self.messages[-1]["content"])
            self.assertEqual({"content"}, set(self.messages[-1]))
            self.assertNotIn("synthetic-provider-private", self.messages[-1]["content"])

    async def test_restored_session_consent_is_visible_without_selecting_or_resuming_it(self):
        self.session["native_session_id"] = "unchanged"
        with patch.object(self.service, "resume", AsyncMock(side_effect=HostedConsentRequired())):
            await native_chat.on_resume({"id": "owned-session-selector"})
        self.assertIn("approved Microsoft Foundry consent", self.messages[-1]["content"])
        self.assertEqual({"content"}, set(self.messages[-1]))
        self.assertEqual("unchanged", self.session["native_session_id"])

    async def test_refresh_reads_json_native_completed_product_result(self):
        hub = Mock(spec=TaskHubGrpcClient)
        result = {
            "status": "approved", "topic": "test", "draft": "final draft",
            "review_comment": "", "configuration_revision": "revision-1",
        }
        state = SimpleNamespace(
            name="dafx-v2-agent-lz-research-review", runtime_status=OrchestrationStatus.COMPLETED,
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
