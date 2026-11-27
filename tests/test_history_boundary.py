"""History operations have explicit context; Chainlit owns the request bridge."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from chainlit.context import ChainlitContextException, context_var
from chainlit.data.base import BaseDataLayer
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User

from gpt_rag_ui.api import history as api
from gpt_rag_ui.services import history as service


class HistoryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.principal = (
            "11111111-2222-3333-4444-555555555555:"
            "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        )
        self.metadata = {
            "principal_id": self.principal,
            "tenant_id": self.principal.split(":")[0],
            "object_id": self.principal.split(":")[1],
            "authorized": True,
        }

    async def test_adapter_and_factory_have_one_api_owner(self):
        self.assertEqual(api.__name__, api.OrchestratorDataLayer.__module__)
        self.assertEqual(api.__name__, api.get_data_layer.__module__)
        self.assertIsInstance(api.get_data_layer(), BaseDataLayer)
        self.assertIsNot(api.get_data_layer(), api.get_data_layer())
        self.assertFalse(issubclass(service.HistoryService, BaseDataLayer))
        self.assertFalse(hasattr(service, "_request_user_metadata"))
        self.assertFalse(hasattr(api, "_users"))

    async def test_service_uses_explicit_context_without_chainlit_session(self):
        with (
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "resolve_access_token", AsyncMock(return_value="token")),
            patch.object(service, "call_orchestrator_list_conversations",
                         AsyncMock(return_value={"conversations": [{"id": "thread"}]})) as backend,
            patch.object(api.cl.user_session, "get", side_effect=AssertionError("implicit session")),
        ):
            result = await service.HistoryService().list_threads(
                service.HistoryOperationContext(metadata=self.metadata),
                Pagination(first=7, cursor="2"), ThreadFilter(),
            )
        self.assertEqual(self.principal, result.data[0]["userId"])
        backend.assert_awaited_once_with(access_token="token", skip=2, limit=7)

    async def test_invalid_cursor_uses_first_page_and_logs_without_cursor(self):
        with (
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "resolve_access_token", AsyncMock(return_value="token")),
            patch.object(service, "call_orchestrator_list_conversations",
                         AsyncMock(return_value={"conversations": []})) as backend,
            self.assertLogs(service.logger, level="WARNING") as logs,
        ):
            result = await service.HistoryService().list_threads(
                service.HistoryOperationContext(metadata=self.metadata),
                Pagination(first=7, cursor="invalid-private-cursor"), ThreadFilter(),
            )
        backend.assert_awaited_once_with(access_token="token", skip=0, limit=7)
        self.assertEqual("0", result.pageInfo.startCursor)
        self.assertNotIn("invalid-private-cursor", "\n".join(logs.output))

    async def test_invalid_thread_timestamp_is_unset_and_logged(self):
        with (
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "get_owned_conversation", AsyncMock(return_value={
                "id": "thread", "_ts": {"invalid": "private-value"},
            })),
            self.assertLogs(service.logger, level="WARNING") as logs,
        ):
            thread = await service.HistoryService().get_thread(
                service.HistoryOperationContext(metadata=self.metadata), "thread"
            )
        self.assertIsNone(thread["createdAt"])
        self.assertNotIn("private-value", "\n".join(logs.output))

    async def test_request_metadata_is_consumed_once_and_task_local(self):
        async def consume(value):
            api._request_user_metadata.set(value)
            await asyncio.sleep(0)
            self.assertIs(value, api._get_session_metadata())
            self.assertIsNone(api._get_session_metadata())

        with patch.object(api.cl.user_session, "get", side_effect=ChainlitContextException()):
            await asyncio.gather(consume(self.metadata), consume({"principal_id": "other"}))

    async def test_live_session_precedes_request_metadata_without_consuming_it(self):
        live = {"principal_id": "live"}
        marker = api._request_user_metadata.set(self.metadata)
        token = context_var.set(SimpleNamespace(session=SimpleNamespace(user=SimpleNamespace(metadata=live))))
        try:
            self.assertIs(live, api._get_session_metadata())
            self.assertIs(self.metadata, api._request_user_metadata.get())
        finally:
            context_var.reset(token)
            api._request_user_metadata.reset(marker)

    async def test_user_cache_is_shared_across_factory_instances(self):
        service._users.clear()
        self.addCleanup(service._users.clear)
        marker = api._request_user_metadata.set(None)
        self.addCleanup(api._request_user_metadata.reset, marker)
        with (
            patch.object(api, "get_request_copilot_session", return_value=None),
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
        ):
            created = await api.get_data_layer().create_user(
                User(identifier=self.principal, metadata=self.metadata)
            )
            self.assertIs(created, await api.get_data_layer().get_user(self.principal))
        self.assertIs(created, service._users[self.principal])
        self.assertIs(created.metadata, api._request_user_metadata.get())

    async def test_session_update_occurs_after_ownership_before_token_resolution(self):
        events = []

        async def owned(*args):
            events.append("owned")
            return {"id": "thread"}

        async def token(*args):
            events.append("token")
            return "token"

        with (
            patch.object(api, "_get_session_metadata", return_value=self.metadata),
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "get_owned_conversation", side_effect=owned),
            patch.object(api.cl.user_session, "set", side_effect=lambda *args: events.append("session")),
            patch.object(service, "resolve_access_token", side_effect=token),
            patch.object(service, "call_orchestrator_update_conversation", AsyncMock(return_value=True)) as update,
        ):
            await api.get_data_layer().update_thread("thread", name=" renamed ")
        self.assertEqual(["owned", "session", "token"], events)
        update.assert_awaited_once_with(access_token="token", conversation_id="thread", name="renamed")

    async def test_denied_update_never_mutates_session_or_backend(self):
        with (
            patch.object(api, "_get_session_metadata", return_value=self.metadata),
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "get_owned_conversation", AsyncMock(return_value=None)),
            patch.object(api.cl.user_session, "set") as set_session,
            patch.object(service, "call_orchestrator_update_conversation", AsyncMock()) as update,
        ):
            await api.get_data_layer().update_thread("other", name="rename")
        set_session.assert_not_called()
        update.assert_not_awaited()

    async def test_selection_without_rename_keeps_framework_positional_contract(self):
        with (
            patch.object(api, "_get_session_metadata", return_value=self.metadata),
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "get_owned_conversation", AsyncMock(return_value={"id": "thread"})),
            patch.object(api.cl.user_session, "set") as select,
            patch.object(service, "resolve_access_token", AsyncMock()) as token,
        ):
            await api.get_data_layer().update_thread("thread", None, "ignored-user", {}, [])
        select.assert_called_once_with("conversation_id", "thread")
        token.assert_not_awaited()

    async def test_unexpected_framework_failure_is_not_missing_session(self):
        with (
            patch.object(api, "_get_session_metadata", return_value=self.metadata),
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "get_owned_conversation", AsyncMock(return_value={"id": "thread"})),
            patch.object(api.cl.user_session, "set", side_effect=RuntimeError("framework defect")),
            patch.object(service, "call_orchestrator_update_conversation", AsyncMock()) as update,
        ):
            with self.assertRaisesRegex(RuntimeError, "framework defect"):
                await api.get_data_layer().update_thread("thread", name="rename")
        update.assert_not_awaited()

    async def test_missing_chainlit_context_does_not_prevent_authorized_rename(self):
        with (
            patch.object(api, "_get_session_metadata", return_value=self.metadata),
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "get_owned_conversation", AsyncMock(return_value={"id": "thread"})),
            patch.object(service, "resolve_access_token", AsyncMock(return_value="token")),
            patch.object(api.cl.user_session, "set", side_effect=ChainlitContextException()),
            patch.object(service, "call_orchestrator_update_conversation", AsyncMock(return_value=False)) as update,
            self.assertLogs("gpt_rag_ui.datalayer", level="DEBUG") as logs,
        ):
            await api.get_data_layer().update_thread("thread", title="rename")
        update.assert_awaited_once()
        self.assertTrue(any("orchestrator rename failed" in line for line in logs.output))

    async def test_history_backend_failure_propagates(self):
        failure = RuntimeError("backend unavailable")
        with (
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "resolve_access_token", AsyncMock(return_value="token")),
            patch.object(service, "call_orchestrator_list_conversations", AsyncMock(side_effect=failure)),
        ):
            with self.assertRaises(RuntimeError) as raised:
                await service.HistoryService().list_threads(
                    service.HistoryOperationContext(metadata=self.metadata),
                    Pagination(first=10), ThreadFilter(),
                )
        self.assertIs(failure, raised.exception)

    async def test_citation_rendering_receives_explicit_identity_and_session(self):
        metadata = {**self.metadata, "copilot_session_id": "session"}
        with (
            patch.object(service, "is_copilot_session_active", AsyncMock(return_value=True)),
            patch.object(service, "get_owned_conversation", AsyncMock(return_value={
                "id": "thread", "messages": [{"role": "assistant", "text": "answer"}],
            })),
            patch("gpt_rag_ui.services.citations.replace_source_reference_links",
                  Mock(return_value="rendered")) as render,
        ):
            thread = await service.HistoryService().get_thread(
                service.HistoryOperationContext(metadata=metadata), "thread"
            )
        self.assertEqual("rendered", thread["steps"][0]["output"])
        render.assert_called_once_with("answer", conversation_id="thread",
                                       principal_id=self.principal, copilot_session_id="session")
