"""Fresh native readers; transport/state boundaries only, no UI history cache."""

import copy
import json
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import AsyncOpenAI, APIStatusError
from durabletask.client import TaskHubGrpcClient, OrchestrationStatus
from agent_framework_durabletask import (
    DurableAgentState, DurableAgentStateResponse, DurableAgentStateMessage,
    DurableAgentStateTextContent,
)

from gpt_rag_ui.clients.native_durable import NativeDurableClient
from gpt_rag_ui.clients.native_hosted import NativeHostedClient
from gpt_rag_ui.clients.native_metadata import MemoryMetadata
from gpt_rag_ui.services.native import NativeService, NativeNotFound, ReviewDecision, MetadataConflict
from gpt_rag_ui.services.native_presentation import presentations, text_projection
from test_native_ui import settings, NativePrincipal, TENANT, OID, OTHER


FRESH_HISTORY_CASES = tuple(
    (f"H{index:02d}", host, application,
     "multi_agent" if application == "workflow" and scenario == "visual" else scenario)
    for index, (host, application, scenario) in enumerate(
        ((host, application, scenario)
         for host in ("hosted", "durable")
         for application in ("conversational", "workflow")
         for scenario in ("visual", "legacy", "future_block", "future_version", "interrupted")),
        start=1,
    )
)


class NativeHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.owner = NativePrincipal(TENANT, OID, "local-fixture")
        self.other = NativePrincipal(TENANT, OTHER, "local-fixture")
        self.fixture = json.loads((Path(__file__).parent /
                                   "fixtures/inline-presentation/presentation-valid.json").read_text())

    async def test_fresh_H01_hosted_conversational_visual(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[0])

    async def test_fresh_H02_hosted_conversational_legacy(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[1])

    async def test_fresh_H03_hosted_conversational_future_block(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[2])

    async def test_fresh_H04_hosted_conversational_future_version(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[3])

    async def test_fresh_H05_hosted_conversational_interrupted(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[4])

    async def test_fresh_H06_hosted_workflow_multi_agent(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[5])

    async def test_fresh_H07_hosted_workflow_legacy(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[6])

    async def test_fresh_H08_hosted_workflow_future_block(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[7])

    async def test_fresh_H09_hosted_workflow_future_version(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[8])

    async def test_fresh_H10_hosted_workflow_interrupted(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[9])

    async def test_fresh_H11_durable_conversational_visual(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[10])

    async def test_fresh_H12_durable_conversational_legacy(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[11])

    async def test_fresh_H13_durable_conversational_future_block(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[12])

    async def test_fresh_H14_durable_conversational_future_version(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[13])

    async def test_fresh_H15_durable_conversational_interrupted(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[14])

    async def test_fresh_H16_durable_workflow_multi_agent(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[15])

    async def test_fresh_H17_durable_workflow_legacy(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[16])

    async def test_fresh_H18_durable_workflow_future_block(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[17])

    async def test_fresh_H19_durable_workflow_future_version(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[18])

    async def test_fresh_H20_durable_workflow_interrupted(self):
        await self.fresh_history_case(*FRESH_HISTORY_CASES[19])

    async def fresh_history_case(self, case_id, host, application, scenario):
        """New native state, owner mapping, service and public SDK readers per case."""
        envelope = copy.deepcopy(self.fixture)
        envelope["presentationId"] = f"presentation-{case_id}"
        if scenario == "multi_agent":
            extra = copy.deepcopy(envelope["contributions"][0])
            extra["agentId"], extra["workflowOrdinal"] = "finance", 1
            extra["contributionId"] += "-finance"

            def distinct_source(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if key == "sourceId":
                            value[key] = item + "-finance"
                        else:
                            distinct_source(item)
                elif isinstance(value, list):
                    for item in value:
                        distinct_source(item)

            distinct_source(extra)
            for block in extra["blocks"]:
                block["blockId"] += "-finance"
            envelope["contributions"].append(extra)
        if scenario == "future_block":
            envelope["contributions"][0]["blocks"].append({
                "type": "future", "blockId": f"future-{case_id}",
                "dangerouslySetInnerHTML": {"__html": "<script>untrusted()</script>"},
            })
        if scenario == "future_version":
            envelope["schemaVersion"] = 2
        draft = {"topic": f"Saved topic {case_id}", "draft": f"Readable review {case_id}",
                 "configuration_revision": f"saved-{case_id}"}
        if scenario != "legacy":
            draft["presentation"] = envelope
        completed = dict(draft, status="approved", review_comment="Reviewed")
        text = (json.dumps(completed) if application == "workflow" else
                f"Legacy answer {case_id}" if scenario == "legacy" else json.dumps(envelope))
        configured = settings(host=host, history=True,
                              agent_url="https://host.example.invalid/v1",
                              workflow_url="https://host.example.invalid/v1")
        metadata = MemoryMetadata()
        requests = []
        hub = Mock(spec=TaskHubGrpcClient)
        if host == "hosted":
            def transport(request):
                requests.append((request.method, request.url.path))
                if request.method != "GET":
                    return httpx.Response(403, json={"error": {"message": "New source action denied"}})
                if request.url.path.endswith("/input_items"):
                    return httpx.Response(200, json={"object": "list", "data": [
                        {"id": f"msg_input_{case_id}", "type": "message", "role": "user",
                         "content": [{"type": "input_text", "text": f"Saved question {case_id}"}]},
                    ], "has_more": False, "first_id": f"msg_input_{case_id}",
                        "last_id": f"msg_input_{case_id}"})
                return httpx.Response(200, json={
                    "id": f"resp_{case_id}", "object": "response", "created_at": 1,
                    "model": "fixture", "status": "incomplete" if scenario == "interrupted" else "completed",
                    "output": [] if scenario == "interrupted" else [{
                        "id": f"msg_output_{case_id}", "type": "message", "role": "assistant",
                        "status": "completed", "content": [{"type": "output_text", "text": text,
                            "annotations": [{"type": "url_citation",
                                             "url": "https://source.example.invalid/saved",
                                             "title": "Saved source", "start_index": 0, "end_index": 5}]}],
                    }], "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                })

            def fresh():
                client = NativeHostedClient(configured)

                @asynccontextmanager
                async def sdk(token, *, workflow):
                    self.assertEqual(workflow, application == "workflow")
                    async with AsyncOpenAI(api_key="local-fixture",
                                           base_url="https://host.example.invalid/v1",
                                           http_client=httpx.AsyncClient(
                                               transport=httpx.MockTransport(transport)),
                                           max_retries=0) as public:
                        yield public
                client._client = sdk
                return NativeService(configured, metadata, hosted=client)
        else:
            if application == "conversational":
                state = DurableAgentState()
                messages = [DurableAgentStateMessage(role="user", message_id=f"question-{case_id}",
                            contents=[DurableAgentStateTextContent(text=f"Saved question {case_id}")])]
                if scenario != "interrupted":
                    messages.append(DurableAgentStateMessage(role="assistant", message_id=f"answer-{case_id}",
                                    contents=[DurableAgentStateTextContent(text=text)]))
                state.data.conversation_history.append(DurableAgentStateResponse(
                    correlation_id=f"turn-{case_id}", created_at=None, messages=messages))
                sealed_state = json.dumps(state.to_dict())
                hub.get_entity.side_effect = lambda *args: SimpleNamespace(
                    get_state=lambda: json.loads(sealed_state))
            else:
                state = SimpleNamespace(
                    name="dafx-v2-agent-lz-research-review",
                    runtime_status=(OrchestrationStatus.RUNNING if scenario == "interrupted"
                                    else OrchestrationStatus.COMPLETED),
                    serialized_output=None if scenario == "interrupted" else json.dumps([completed]),
                    serialized_custom_status=json.dumps({"pending_requests": {}}),
                )
                hub.get_orchestration_state.return_value = state
                hub.wait_for_orchestration_completion.return_value = state

            def fresh():
                return NativeService(configured, metadata,
                                     durable=NativeDurableClient(configured, client=hub))

        writer = fresh()
        row = await writer.create(self.owner, application)
        await writer._commit(await writer._lock(row), f"resp_{case_id}" if host == "hosted" else row["id"])
        saved = await fresh().history(self.owner, row["id"])
        projected = presentations(saved)
        expected = scenario in ("visual", "multi_agent", "future_block")
        self.assertEqual(len(projected), int(expected or scenario == "future_version"),
                         (case_id, host, application, scenario))
        if expected:
            self.assertEqual(projected[0]["presentationId"], envelope["presentationId"])
            self.assertEqual(projected[0]["markdown"], self.fixture["markdown"])
            self.assertEqual(len(projected[0]["contributions"][0]["blocks"]), 4)
            self.assertIn("9.750", json.dumps(projected))
            self.assertNotIn("untrusted()", json.dumps(projected))
        if scenario == "multi_agent":
            self.assertEqual(projected[0]["contributions"], envelope["contributions"])
            self.assertEqual([c["workflowOrdinal"] for c in projected[0]["contributions"]], [0, 1])
            self.assertEqual(len({c["sourceRefs"][0]["sourceId"]
                                  for c in projected[0]["contributions"]}), 2)
        if scenario == "future_version":
            self.assertEqual(projected[0]["contributions"], [])
            self.assertIn(self.fixture["markdown"], text_projection(saved))
        if scenario == "legacy":
            self.assertIn(f"Readable review {case_id}" if application == "workflow"
                          else f"Legacy answer {case_id}", text_projection(saved))
        if host == "hosted" and scenario != "interrupted":
            self.assertEqual(saved[-1]["content"][0]["annotations"][0]["title"], "Saved source")
        self.assertEqual(await fresh().history(self.owner, row["id"]), saved)
        before = len(requests) if host == "hosted" else len(hub.mock_calls)
        with self.assertRaises(NativeNotFound):
            await fresh().history(self.other, row["id"])
        with patch.object(metadata, "get", return_value=None):
            with self.assertRaises(NativeNotFound):
                await fresh().history(self.owner, row["id"])
        self.assertEqual(len(requests) if host == "hosted" else len(hub.mock_calls), before)
        if host == "hosted":
            self.assertTrue(all(method == "GET" and path.startswith("/v1/responses/resp_" + case_id)
                                for method, path in requests))
        else:
            hub.schedule_new_orchestration.assert_not_called()
            hub.raise_orchestration_event.assert_not_called()
        print(f"T057 {case_id} {host} {application} {scenario}: PASS fresh SDK/state, exact projection, owner/deletion denial")

    def test_completed_business_message_requires_assistant_and_complete_typed_result(self):
        business = {"status": "approved", "topic": "Topic", "draft": "Readable review",
                    "review_comment": "", "configuration_revision": "rev", "presentation": self.fixture}
        for role in ("assistant", "user", "tool"):
            message = {"id": "native-message", "role": role, "type": "message",
                       "content": [{"type": "output_text", "text": json.dumps(business)}]}
            self.assertEqual(len(presentations(message)), 1 if role == "assistant" else 0)
            if role == "assistant":
                self.assertEqual(text_projection(message),
                                 "approved\n\nReadable review\n\n" + self.fixture["markdown"])
                self.assertEqual(presentations([message, message])[0]["nativeMessageId"], "native-message")
                self.assertEqual(len(presentations([message, message])), 1)
        for invalid in (dict(business, status="running"), dict(business, topic=42)):
            message["role"] = "assistant"
            message["content"][0]["text"] = json.dumps(invalid)
            self.assertEqual(presentations(message), [])

    async def test_durable_workflow_review_completed_saved_snapshot_and_terminal_action_denial(self):
        for completed in (False, True):
            with self.subTest(completed=completed):
                configured = settings(history=True)
                draft = {"topic": "Saved topic", "draft": "Readable review\n\n" + self.fixture["markdown"],
                         "configuration_revision": "saved-revision", "presentation": self.fixture}
                result = dict(draft, status="approved", review_comment="Reviewed")
                state = SimpleNamespace(
                    name="dafx-v2-agent-lz-research-review",
                    runtime_status=OrchestrationStatus.COMPLETED if completed else OrchestrationStatus.RUNNING,
                    serialized_output=json.dumps([result]) if completed else None,
                    serialized_custom_status=json.dumps({"pending_requests": {} if completed else {
                        "review-1": {"request_id": "review-1", "source_executor_id": "human-review",
                                     "data": draft, "response_type": "ReviewDecision"}}}),
                )
                hub = Mock(spec=TaskHubGrpcClient)
                hub.get_orchestration_state.return_value = state
                hub.wait_for_orchestration_completion.return_value = state
                metadata = MemoryMetadata()
                service = NativeService(configured, metadata,
                                        durable=NativeDurableClient(configured, client=hub))
                row = await service.create(self.owner, "workflow")
                await service._commit(await service._lock(row), row["id"])
                fresh = NativeService(configured, metadata,
                                      durable=NativeDurableClient(configured, client=hub))
                saved = await fresh.history(self.owner, row["id"])
                self.assertIn("Readable review", text_projection(saved))
                self.assertEqual(presentations(saved)[0]["markdown"], self.fixture["markdown"])
                self.assertEqual(await fresh.history(self.owner, row["id"]), saved)
                hub.schedule_new_orchestration.assert_not_called()
                hub.raise_orchestration_event.assert_not_called()
                if completed:
                    with self.assertRaises(MetadataConflict):
                        await fresh.reply(self.owner, row["id"], "review-1", "stale",
                                          ReviewDecision(decision="approve"))
                    hub.raise_orchestration_event.assert_not_called()
                hub.get_orchestration_state.reset_mock()
                with self.assertRaises(NativeNotFound):
                    await fresh.history(self.other, row["id"])
                hub.get_orchestration_state.assert_not_called()

    async def test_hosted_fresh_sdk_inputs_outputs_legacy_future_owner_and_revocation(self):
        for scenario in ("visual", "legacy", "future", "workflow", "interrupted"):
            with self.subTest(scenario=scenario):
                envelope = copy.deepcopy(self.fixture)
                if scenario == "future":
                    envelope["contributions"][0]["blocks"].append({"type": "future", "blockId": "future"})
                text = "Legacy narration" if scenario == "legacy" else json.dumps(envelope)
                if scenario == "workflow":
                    text = json.dumps({"status": "approved", "topic": "Saved topic",
                                       "draft": "Readable review\n\n" + envelope["markdown"],
                                       "review_comment": "Reviewed", "configuration_revision": "saved",
                                       "presentation": envelope})
                citation = {"type": "url_citation", "url": "https://source.example.invalid/saved",
                            "title": "Saved source", "start_index": 0, "end_index": 5}
                output = {"id": "msg_saved", "type": "message", "role": "assistant",
                          "status": "completed", "content": [
                              {"type": "output_text", "text": text, "annotations": [citation]}]}
                requests = []

                def transport(request):
                    requests.append((request.method, request.url.path))
                    if request.method != "GET":
                        return httpx.Response(403, json={"error": {"message": "New source action denied"}})
                    if request.url.path.endswith("/input_items"):
                        return httpx.Response(200, json={"object": "list", "data": [
                            {"id": "msg_input", "type": "message", "role": "user",
                             "content": [{"type": "input_text", "text": "Saved question"}]},
                        ], "has_more": False, "first_id": "msg_input", "last_id": "msg_input"})
                    return httpx.Response(200, json={
                        "id": "resp_saved", "object": "response", "created_at": 1,
                        "model": "fixture", "status": "incomplete" if scenario == "interrupted" else "completed",
                        "output": [] if scenario == "interrupted" else [output],
                        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
                    })

                configured = settings(host="hosted", history=True,
                                      agent_url="https://host.example.invalid/v1",
                                      workflow_url="https://host.example.invalid/v1")
                metadata = MemoryMetadata()
                writer = NativeService(configured, metadata, hosted=NativeHostedClient(configured))
                row = await writer.create(self.owner, "workflow" if scenario == "workflow" else "conversational")
                await writer._commit(await writer._lock(row), "resp_saved")
                # Brand-new service/client and SDK instance for every read.
                def fresh():
                    client = NativeHostedClient(configured)

                    @asynccontextmanager
                    async def sdk(token, *, workflow):
                        async with AsyncOpenAI(api_key="local-fixture",
                                               base_url="https://host.example.invalid/v1",
                                               http_client=httpx.AsyncClient(
                                                   transport=httpx.MockTransport(transport)),
                                               max_retries=0) as public:
                            yield public
                    client._client = sdk
                    return NativeService(configured, metadata, hosted=client)

                saved = await fresh().history(self.owner, row["id"])
                if scenario != "interrupted":
                    self.assertEqual(saved[-1]["content"][0]["annotations"], [citation])
                self.assertEqual(saved[0]["content"][0]["text"], "Saved question")
                self.assertEqual(len(presentations(saved)), 0 if scenario in ("legacy", "interrupted") else 1)
                if scenario not in ("legacy", "interrupted"):
                    self.assertEqual(presentations(saved)[0]["markdown"], self.fixture["markdown"])
                    self.assertEqual(len(presentations(saved)[0]["contributions"][0]["blocks"]), 4)
                # A revoked source denies new work but does not trigger a separate
                # visual-history source ACL lookup on the retained saved response.
                if scenario != "workflow":
                    with self.assertRaises(APIStatusError) as denied:
                        await fresh().converse(self.owner, row["id"], "Inspect saved source")
                    self.assertEqual(denied.exception.status_code, 403)
                self.assertEqual(await fresh().history(self.owner, row["id"]), saved)
                self.assertTrue(all(path.startswith("/v1/responses") for _, path in requests))
                before = len(requests)
                with self.assertRaises(NativeNotFound):
                    await fresh().history(self.other, row["id"])
                with patch.object(metadata, "get", return_value=None):
                    with self.assertRaises(NativeNotFound):
                        await fresh().history(self.owner, row["id"])
                self.assertEqual(len(requests), before)

    async def test_durable_fresh_sdk_state_completed_interrupted_and_missing(self):
        for scenario in ("complete", "interrupted", "legacy", "deleted"):
            with self.subTest(scenario=scenario):
                configured = settings(history=True)
                state = DurableAgentState()
                if scenario != "deleted":
                    messages = [DurableAgentStateMessage(role="user", message_id="question",
                                contents=[DurableAgentStateTextContent(text="Saved question")])]
                    if scenario != "interrupted":
                        messages.append(DurableAgentStateMessage(role="assistant", message_id="answer",
                                        contents=[DurableAgentStateTextContent(
                                            text="Legacy answer" if scenario == "legacy"
                                            else json.dumps(self.fixture))]))
                    state.data.conversation_history.append(DurableAgentStateResponse(
                        correlation_id="saved-turn", created_at=None, messages=messages))
                hub = Mock(spec=TaskHubGrpcClient)
                hub.get_entity.return_value = None if scenario == "deleted" else SimpleNamespace(
                    get_state=lambda: json.loads(json.dumps(state.to_dict())))
                metadata = MemoryMetadata()
                service = NativeService(configured, metadata,
                                        durable=NativeDurableClient(configured, client=hub))
                row = await service.create(self.owner, "conversational")
                await service._commit(await service._lock(row), row["id"])
                fresh = NativeService(configured, metadata,
                                      durable=NativeDurableClient(configured, client=hub))
                saved = await fresh.history(self.owner, row["id"])
                self.assertEqual(len(presentations(saved)), 1 if scenario == "complete" else 0)
                if scenario == "complete":
                    self.assertEqual(presentations(saved)[0]["markdown"], self.fixture["markdown"])
                if scenario == "interrupted":
                    self.assertIn("Saved question", text_projection(saved))
                    self.assertNotIn(self.fixture["markdown"], text_projection(saved))
                if scenario == "deleted":
                    self.assertEqual(saved, [])
                hub.get_entity.reset_mock()
                with self.assertRaises(NativeNotFound):
                    await fresh.history(self.other, row["id"])
                hub.get_entity.assert_not_called()
