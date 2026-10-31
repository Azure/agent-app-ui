"""Native private input, session ownership and actual SDK content envelopes."""

import base64
import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
import grpc
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent_framework import AgentResponse, Content, Message
from durabletask.client import TaskHubGrpcClient
from durabletask.azuremanaged.client import DurableTaskSchedulerClient

from gpt_rag_ui.clients.native_attachments import (
    InlineAttachment, MAX_ATTACHMENT_BYTES, MAX_INLINE_TURN_BYTES, validate_inline_turn,
)
from gpt_rag_ui.clients.native_durable import NativeDurableClient, BoundedRpc
from gpt_rag_ui.clients import native_durable
from gpt_rag_ui.clients.native_metadata import MemoryMetadata
from gpt_rag_ui.config.errors import ConfigurationError
from gpt_rag_ui.config.native import load_native_settings
from gpt_rag_ui.services.native import NativeService, NativeNotFound, NativeUnavailable
from test_native_ui import settings, base_config, NativePrincipal, TENANT, OID, OTHER


def attachment(name="note.txt", content_type="text/plain", data=b"private text"):
    return InlineAttachment(name=name, content_type=content_type, data_base64=base64.b64encode(data).decode())


class NativeAttachmentValidationTests(unittest.TestCase):
    def test_formats_and_limits(self):
        for name, mime, data in (
            ("note.txt", "text/plain", b"hello"), ("note.md", "text/markdown", b"# hello"),
            ("note.pdf", "application/pdf", b"%PDF-test"),
            ("image.png", "image/png", b"\x89PNG\r\n\x1a\n"),
            ("image.jpeg", "image/jpeg", b"\xff\xd8\xff"),
        ):
            with self.subTest(name=name):
                value = attachment(name, mime, data)
                self.assertEqual(value.decode(), data)
                self.assertIn(value.hosted_content()["type"], {"input_file", "input_image"})
                self.assertIsInstance(value.durable_content(), Content)
        self.assertEqual(len(attachment(data=b"a" * MAX_ATTACHMENT_BYTES).decode()), MAX_ATTACHMENT_BYTES)

    def test_reject_paths_types_signatures_urls_malformed_and_large_input(self):
        for name, mime, data in (
            ("../note.txt", "text/plain", b"hi"), ("other\\note.txt", "text/plain", b"hi"),
            ("note.exe", "text/plain", b"hi"), ("note.pdf", "text/plain", b"hi"),
            ("note.pdf", "application/pdf", b"not PDF"), ("note.txt", "text/plain", b"\xff"),
            ("note.txt", "text/plain", b""), ("image.png", "image/png", b"not PNG"),
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                attachment(name, mime, data).decode()
        with self.assertRaises(ValueError):
            InlineAttachment(name="note.txt", content_type="text/plain", data_base64="!").decode()
        with self.assertRaises(ValueError):
            attachment(data=b"a" * (MAX_ATTACHMENT_BYTES + 1)).decode()
        with self.assertRaises(ValueError):
            InlineAttachment(name="note.txt", content_type="text/plain", data_base64="YQ==", file_url="https://untrusted.invalid")

    def test_upload_flag_does_not_require_ingestion_blob_or_sql(self):
        with patch.dict(os.environ, {}, clear=True):
            value = load_native_settings(base_config(
                ENABLE_PRIVATE_ATTACHMENTS="true", DEPLOY_INGESTION_CONTAINER_APP="false",
            ))
            self.assertTrue(value.private_uploads)
            with self.assertRaises(ConfigurationError):
                load_native_settings(base_config(ENABLE_PRIVATE_ATTACHMENTS="true", APPLICATION_MODE="workflow"))

    def test_native_serialized_turn_budget_counts_base64_and_json_text_expansion(self):
        pdf = attachment("note.pdf", "application/pdf", b"%PDF-" + b"a" * (MAX_ATTACHMENT_BYTES - 5))
        self.assertEqual(len(validate_inline_turn("describe", [pdf])), 1)
        with self.assertRaises(ValueError):
            validate_inline_turn("describe", [pdf, pdf, pdf])
        with self.assertRaises(ValueError):
            validate_inline_turn("describe", [attachment(data=b"\x00" * MAX_ATTACHMENT_BYTES)])
        self.assertEqual(MAX_INLINE_TURN_BYTES, 256 * 1024)

    def test_rpc_deadline_uses_supported_grpc_interceptor(self):
        details = SimpleNamespace(
            method="/method", timeout=None, metadata=[], credentials=None, wait_for_ready=False, compression=None,
        )
        continuation = Mock()
        BoundedRpc().intercept_unary_unary(continuation, details, "request")
        self.assertEqual(continuation.call_args.args[0].timeout, 30)
        deadline = native_durable._agent_deadline.set(102)
        try:
            with patch("gpt_rag_ui.clients.native_durable.time.monotonic", return_value=100):
                BoundedRpc().intercept_unary_unary(continuation, details, "request")
            self.assertEqual(continuation.call_args.args[0].timeout, 2)
            with patch("gpt_rag_ui.clients.native_durable.time.monotonic", return_value=103):
                with self.assertRaises(TimeoutError):
                    BoundedRpc().intercept_unary_unary(continuation, details, "request")
        finally:
            native_durable._agent_deadline.reset(deadline)

    def test_scheduler_uses_configured_service_mi_not_user_identity(self):
        hub = Mock(spec=TaskHubGrpcClient)
        with (
            patch.dict(os.environ, {"AZURE_CLIENT_ID": OTHER}),
            patch("gpt_rag_ui.clients.native_durable.ManagedIdentityCredential") as identity,
            patch("gpt_rag_ui.clients.native_durable.DurableTaskSchedulerClient", return_value=hub) as scheduler,
        ):
            NativeDurableClient(settings())
        identity.assert_called_once_with(client_id=OTHER)
        self.assertTrue(scheduler.call_args.kwargs["secure_channel"])
        self.assertEqual(scheduler.call_args.kwargs["taskhub"], "ui")
        self.assertIsInstance(scheduler.call_args.kwargs["interceptors"][0], BoundedRpc)

    def test_actual_published_scheduler_constructor_accepts_explicit_managed_identity(self):
        credential_type = native_durable.ManagedIdentityCredential
        # Only the public gRPC transport factory is replaced. Both the Azure
        # credential and official DTS/MAF client constructors are real.
        with (
            patch.dict(os.environ, {"AZURE_CLIENT_ID": OTHER}, clear=True),
            grpc.insecure_channel("127.0.0.1:1") as channel,
            patch("grpc.secure_channel", return_value=channel) as transport,
            patch.object(native_durable, "ManagedIdentityCredential", wraps=credential_type) as identity,
        ):
            client = NativeDurableClient(settings())
            try:
                self.assertIsInstance(client.client, DurableTaskSchedulerClient)
                self.assertIsInstance(client._credential, credential_type)
                identity.assert_called_once_with(client_id=OTHER)
                self.assertEqual(transport.call_args.args[0], "scheduler.example.invalid")
            finally:
                asyncio.run(client.aclose())


class NativePrivateOwnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = MemoryMetadata()
        self.durable = SimpleNamespace(
            converse=AsyncMock(return_value={"messages": [{"role": "assistant", "contents": [{"type": "text", "text": "done"}]}]}),
        )
        self.service = NativeService(settings(private_uploads=True), self.store, durable=self.durable)
        self.owner = NativePrincipal(TENANT, OID, "authenticated")
        self.other = NativePrincipal(TENANT, OTHER, "authenticated")
        self.row = await self.service.create(self.owner, "conversational")

    async def test_owner_check_precedes_content_or_native_access(self):
        with self.assertRaises(NativeNotFound):
            await self.service.converse(self.other, self.row["id"], "test", [attachment()])
        self.durable.converse.assert_not_awaited()
        self.assertEqual(await self.store.list(self.other.owner, "attachment"), [])

    async def test_private_native_content_and_descriptor_only_product_storage(self):
        file = attachment()
        await self.service.converse(self.owner, self.row["id"], "test", [file])
        self.durable.converse.assert_awaited_once_with(
            self.row["id"], "test", options={}, attachments=[file],
        )
        stored = (await self.store.list(self.owner.owner, "attachment"))[0]
        self.assertEqual(stored["size"], len(b"private text"))
        self.assertNotIn("data_base64", stored)
        self.assertNotIn("content", stored)
        self.assertNotIn("token", stored)

    async def test_disabled_private_upload_is_not_a_silent_success(self):
        disabled = NativeService(settings(), self.store, durable=self.durable)
        with self.assertRaises(NativeUnavailable):
            await disabled.converse(self.owner, self.row["id"], "test", [attachment()])
        self.durable.converse.assert_not_awaited()

    async def test_invalid_batch_and_state_budget_fail_before_metadata_or_backend(self):
        pdf = attachment("note.pdf", "application/pdf", b"%PDF-" + b"a" * (MAX_ATTACHMENT_BYTES - 5))
        for files in ([attachment(), attachment("bad.pdf", "application/pdf", b"invalid")], [pdf, pdf, pdf]):
            with self.subTest(count=len(files)), self.assertRaises(ValueError):
                await self.service.converse(self.owner, self.row["id"], "describe", files)
            self.assertEqual(await self.store.list(self.owner.owner, "attachment"), [])
            self.durable.converse.assert_not_awaited()
            self.assertFalse((await self.service.owned(self.owner, self.row["id"]))["busy"])

    async def test_actual_durable_agent_receives_native_content_not_lossy_text_only(self):
        client = NativeDurableClient(settings(), client=Mock(spec=TaskHubGrpcClient))
        agent = Mock()
        agent.run.return_value = AgentResponse(messages=[Message("assistant", [Content.from_text("done")])])
        with patch.object(client.agents, "get_agent", return_value=agent):
            await client.converse("session-1", "test", options={}, attachments=[
                attachment("note.pdf", "application/pdf", b"%PDF-test"),
            ])
        args = agent.run.call_args.kwargs
        self.assertEqual(args["session"].durable_session_id.entity_version, 2)
        native = Message.from_dict(args["context_messages"][0])
        self.assertEqual(native.contents[1].media_type, "application/pdf")
        self.assertTrue(native.contents[1].uri.startswith("data:application/pdf;base64,"))
        self.assertEqual(len(args["context_message_ids"]), 1)

    async def test_published_durable_agent_serializes_pdf_and_image_at_scheduler_boundary(self):
        class SchedulerBoundary(RuntimeError):
            pass

        hub = Mock(spec=TaskHubGrpcClient)
        hub.signal_entity.side_effect = SchedulerBoundary()
        client = NativeDurableClient(settings(), client=hub)
        # No mock agent or SDK executor: stop only at the scheduler transport.
        with self.assertRaises(SchedulerBoundary):
            await client.converse("fixture", "describe", options={}, attachments=[
                attachment("test.pdf", "application/pdf", b"%PDF-test"),
                attachment("test.png", "image/png", b"\x89PNG\r\n\x1a\n"),
            ])
        payload = json.dumps(hub.signal_entity.call_args.args[2])
        self.assertIn("data:application/pdf;base64,", payload)
        self.assertIn("data:image/png;base64,", payload)
        self.assertEqual(hub.signal_entity.call_count, 1)
        self.assertLess(len(payload.encode("utf-8")), MAX_INLINE_TURN_BYTES)

    async def test_chainlit_private_path_requires_session_registry(self):
        from gpt_rag_ui.api.native_chat import read_session_attachments
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            own = root / "own"
            other = root / "other"
            own.mkdir()
            other.mkdir()
            file = own / "note.txt"
            foreign = other / "note.txt"
            file.write_bytes(b"private text")
            foreign.write_bytes(b"other text")
            session = SimpleNamespace(files_dir=own, files={
                "own-file": {"name": "note.txt", "type": "text/plain", "path": file},
            })
            with patch("chainlit.context.context", SimpleNamespace(session=session)):
                result = await read_session_attachments([SimpleNamespace(path=str(file))])
                self.assertEqual(result[0].decode(), b"private text")
                with self.assertRaises(PermissionError):
                    await read_session_attachments([SimpleNamespace(path=str(foreign))])


class NativeCallbackTests(unittest.TestCase):
    def test_native_callback_wiring_does_not_register_classic_history_or_feedback(self):
        import sys
        spec = importlib.util.find_spec("gpt_rag_ui.api.callbacks")
        callbacks = importlib.util.module_from_spec(spec)
        services = ModuleType("gpt_rag_ui.services")
        services.chat = SimpleNamespace(
            NATIVE_SETTINGS=settings(), OAUTH_CONFIGURED=True,
            get_auth_info=AsyncMock(), CHAT_BACKEND="native",
        )
        native = ModuleType("gpt_rag_ui.api.native_chat")
        native.register = Mock()
        oauth = ModuleType("gpt_rag_ui.api.oauth")
        oauth.register_oauth_callback = Mock()
        history = ModuleType("gpt_rag_ui.api.history")
        history.register_data_layer = Mock()
        feedback = ModuleType("gpt_rag_ui.api.feedback")
        feedback.register_feedback_handlers = Mock()
        with patch.dict(sys.modules, {
            "gpt_rag_ui.services": services, "gpt_rag_ui.api.native_chat": native,
            "gpt_rag_ui.api.oauth": oauth, "gpt_rag_ui.api.history": history,
            "gpt_rag_ui.api.feedback": feedback,
        }):
            spec.loader.exec_module(callbacks)
            callbacks.register_callbacks()
            callbacks.register_callbacks()
        native.register.assert_called_once_with(services.chat.get_auth_info)
        oauth.register_oauth_callback.assert_called_once()
        history.register_data_layer.assert_not_called()
        feedback.register_feedback_handlers.assert_not_called()


if __name__ == "__main__":
    unittest.main()
