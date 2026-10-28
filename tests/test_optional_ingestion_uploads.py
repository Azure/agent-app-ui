"""Offline behavioral coverage for the independent ingestion upload capability."""

from contextlib import ExitStack
import importlib
import json
import os
from pathlib import Path
import tempfile
import tomllib
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from gpt_rag_ui.config.appconfig import (
    AppConfigClient,
    ATTACHMENTS_UNAVAILABLE_MESSAGE,
    load_ingestion_uploads_enabled,
)
from gpt_rag_ui.config.errors import ConfigurationError


def configuration(value=None):
    # Exercise the real provider lookup/conversion, without creating Azure clients.
    config = AppConfigClient.__new__(AppConfigClient)
    config.client = {} if value is None else {"DEPLOY_INGESTION_CONTAINER_APP": value}
    config.connected = False
    return config


class UploadConfigurationTests(unittest.TestCase):
    def test_real_config_missing_and_valid_boolean_values(self):
        for value, expected in (
            (None, True), ("true", True), (True, True), (" false ", False),
            (False, False), ("1", True), ("0", False), ("yes", True), ("off", False),
        ):
            with self.subTest(value=value):
                self.assertIs(expected, load_ingestion_uploads_enabled(configuration(value)))

    def test_malformed_values_fail_actionably(self):
        for value in ("", " ", "enabled", "tru", 2, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(
                ConfigurationError, "DEPLOY_INGESTION_CONTAINER_APP.*true or false",
            ):
                load_ingestion_uploads_enabled(configuration(value))

    def test_startup_writes_real_chainlit_controls_independent_of_backend(self):
        with patch("gpt_rag_ui.config.dependencies.get_config", return_value=configuration()):
            bootstrap = importlib.import_module("gpt_rag_ui.bootstrap")
        for backend in ("orchestrator", "hosted_agent"):
            for value in (None, "true", "false"):
                for anonymous in (False, True):
                    with self.subTest(backend=backend, value=value, anonymous=anonymous):
                        with tempfile.TemporaryDirectory() as directory:
                            root = Path(directory)
                            (root / ".chainlit").mkdir()
                            path = root / ".chainlit" / "config.toml"
                            path.write_text(
                                "[features.spontaneous_file_upload]\nenabled = true\n",
                                encoding="utf-8",
                            )
                            with (
                                patch.dict(os.environ, {"CHAT_BACKEND": backend}),
                                patch.object(bootstrap, "get_asset_root", return_value=root),
                            ):
                                bootstrap._sync_chainlit_spontaneous_file_upload(
                                    SimpleNamespace(
                                        allow_anonymous=anonymous,
                                        allow_anonymous_source="test",
                                    ),
                                    ingestion_uploads_enabled=load_ingestion_uploads_enabled(
                                        configuration(value),
                                    ),
                                )
                            settings = tomllib.loads(path.read_text(encoding="utf-8"))
                            self.assertIs(
                                value != "false" and not anonymous,
                                settings["features"]["spontaneous_file_upload"]["enabled"],
                            )

    def test_invalid_flag_stops_classic_hosted_and_copilot_startup(self):
        with patch("gpt_rag_ui.config.dependencies.get_config", return_value=configuration()):
            bootstrap = importlib.import_module("gpt_rag_ui.bootstrap")
        from gpt_rag_ui.config.embed_config import EmbedSettings

        for backend in ("orchestrator", "hosted_agent"):
            for copilot in (False, True):
                with (
                    self.subTest(backend=backend, copilot=copilot),
                    patch.dict(os.environ, {"CHAT_BACKEND": backend}),
                    patch.object(bootstrap, "_configure_auth_environment") as auth,
                    self.assertRaisesRegex(ConfigurationError, "DEPLOY_INGESTION_CONTAINER_APP"),
                ):
                    bootstrap._create_chainlit_app(
                        configuration("invalid"), embed_settings=EmbedSettings(enabled=copilot),
                    )
                auth.assert_not_called()

    def test_startup_true_restores_controls_after_disabled(self):
        with patch("gpt_rag_ui.config.dependencies.get_config", return_value=configuration()):
            bootstrap = importlib.import_module("gpt_rag_ui.bootstrap")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".chainlit").mkdir()
            path = root / ".chainlit" / "config.toml"
            path.write_text("[features.spontaneous_file_upload]\nenabled = false\n", encoding="utf-8")
            with patch.object(bootstrap, "get_asset_root", return_value=root):
                bootstrap._sync_chainlit_spontaneous_file_upload(
                    SimpleNamespace(allow_anonymous=False, allow_anonymous_source="test"),
                    ingestion_uploads_enabled=load_ingestion_uploads_enabled(configuration("true")),
                )
            self.assertTrue(tomllib.loads(path.read_text())["features"]["spontaneous_file_upload"]["enabled"])

    def test_startup_enforces_in_memory_controls_even_without_toml_write(self):
        with patch("gpt_rag_ui.config.dependencies.get_config", return_value=configuration()):
            bootstrap = importlib.import_module("gpt_rag_ui.bootstrap")
        from chainlit.config import config as chainlit_config

        class StartupObserved(RuntimeError):
            pass

        for backend in ("orchestrator", "hosted_agent"):
            for value in (None, "true", "false"):
                expected = value != "false"

                def observe_controls(*args):
                    self.assertIs(expected, chainlit_config.features.spontaneous_file_upload.enabled)
                    raise StartupObserved()

                with (
                    self.subTest(backend=backend, value=value),
                    patch.dict(os.environ, {
                        "CHAT_BACKEND": backend,
                        "CHAINLIT_URL": "https://ui.example.com",
                        "CHAINLIT_AUTH_SECRET": "offline-upload-startup-secret-32-bytes",
                    }),
                    patch.object(chainlit_config.features.spontaneous_file_upload, "enabled", not expected),
                    patch.object(bootstrap, "_configure_embed_environment"),
                    patch.object(bootstrap, "_configure_auth_environment"),
                    patch.object(bootstrap, "_sync_chainlit_spontaneous_file_upload"),
                    patch.object(bootstrap, "configure_chainlit_allowed_origins", side_effect=observe_controls),
                    self.assertRaises(StartupObserved),
                ):
                    bootstrap._create_chainlit_app(
                        configuration(value), auth_state=SimpleNamespace(allow_anonymous=False),
                    )


class UploadBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_copilot_project_settings_do_not_override_capability(self):
        from gpt_rag_ui.auth import embed_security

        for value in (None, "true", "false"):
            for mode in ("entra", "anonymous"):
                for originally_enabled in (False, True):
                    with self.subTest(value=value, mode=mode, original=originally_enabled):
                        send = AsyncMock()
                        with patch.object(embed_security, "get_config", return_value=configuration(value)):
                            wrapped = embed_security._copilot_project_settings_send(send, mode)
                        await wrapped({"type": "http.response.start", "status": 200, "headers": []})
                        await wrapped({
                            "type": "http.response.body",
                            "body": json.dumps({
                                "features": {"spontaneous_file_upload": {"enabled": originally_enabled}},
                                "dataPersistence": True,
                            }).encode(),
                        })
                        payload = json.loads(send.await_args_list[-1].args[0]["body"])
                        self.assertIs(
                            mode == "entra" and value != "false",
                            payload["features"]["spontaneous_file_upload"]["enabled"],
                        )
                        self.assertIs(mode == "entra", payload["dataPersistence"])

        with (
            patch.object(embed_security, "get_config", return_value=configuration("invalid")),
            self.assertRaisesRegex(ConfigurationError, "DEPLOY_INGESTION_CONTAINER_APP"),
        ):
            embed_security._copilot_project_settings_send(AsyncMock(), "entra")

    async def test_client_false_and_invalid_reject_before_both_target_fallbacks(self):
        from gpt_rag_ui.clients import ingestion_client as client

        for value in ("false", "invalid"):
            # Both derived Container App URL and Dapr fallback configurations.
            for orchestrator_url in ("https://example-orchestrator.example.com", None):
                with self.subTest(value=value, orchestrator_url=orchestrator_url), ExitStack() as stack:
                    stack.enter_context(patch.object(client, "get_config", return_value=configuration(value)))
                    stack.enter_context(patch.dict(os.environ, {}, clear=True))
                    build_url = stack.enter_context(patch.object(client, "_build_ingestion_url"))
                    base_url = stack.enter_context(patch.object(client, "_get_ingestion_base_url"))
                    orchestrator = stack.enter_context(patch.object(
                        client, "_get_orchestrator_base_url", return_value=orchestrator_url,
                    ))
                    config_lookup = stack.enter_context(patch.object(client, "_get_config_value"))
                    payload = stack.enter_context(patch.object(client, "_build_ingest_documents_payload"))
                    http = stack.enter_context(patch.object(client.httpx, "AsyncClient"))
                    with self.assertRaisesRegex(
                        RuntimeError if value == "false" else ConfigurationError,
                        "Attachments are unavailable" if value == "false" else "DEPLOY_INGESTION_CONTAINER_APP",
                    ):
                        await client.ingest_files_session("conversation", "question", {}, [{"path": "forged.pdf"}])
                    for mock in (build_url, base_url, orchestrator, config_lookup, payload, http):
                        mock.assert_not_called()

    async def test_client_missing_and_true_keep_existing_resolution(self):
        from gpt_rag_ui.clients import ingestion_client as client

        for value in (None, "true"):
            with (
                self.subTest(value=value),
                patch.object(client, "get_config", return_value=configuration(value)),
                patch.object(client, "_build_ingestion_url", side_effect=RuntimeError("target reached")) as target,
                self.assertRaisesRegex(RuntimeError, "target reached"),
            ):
                await client.ingest_files_session("conversation", "question", {}, [])
            target.assert_called_once()

    async def test_handler_attachment_matrix_preserves_auth_and_conversation_checks(self):
        with (
            patch.dict(os.environ, {"CHAT_BACKEND": "orchestrator", "CHAINLIT_AUTH_SECRET": "test-secret"}),
            patch("gpt_rag_ui.config.dependencies.get_config", return_value=configuration()),
            patch("gpt_rag_ui.telemetry.monitoring.Telemetry.configure_monitoring"),
        ):
            chat = importlib.import_module("gpt_rag_ui.services.chat")

        for backend in ("orchestrator", "hosted_agent"):
            for mode in (None, "entra", "anonymous"):
                for value in (None, "true", "false"):
                    for has_conversation in (False, True):
                        with self.subTest(
                            backend=backend, mode=mode, value=value, conversation=has_conversation,
                        ), ExitStack() as stack:
                            state = {}
                            if has_conversation:
                                state.update(conversation_id="classic-id", hosted_agent_conversation_id="hosted-id")
                            response = AsyncMock()
                            response.content = ""
                            ingest = AsyncMock(return_value=True)
                            owner = AsyncMock(return_value={"id": "classic-id"})
                            turns = []

                            async def stream(*args, **kwargs):
                                turns.append((args, kwargs))
                                if False:
                                    yield

                            for name, replacement in {
                                "cl": SimpleNamespace(
                                    File=SimpleNamespace, Message=Mock(return_value=response),
                                    user_session=SimpleNamespace(
                                        get=state.get, set=lambda key, val: state.__setitem__(key, val),
                                    ),
                                    chat_context=SimpleNamespace(to_openai=lambda: []),
                                ),
                                "config": configuration(value), "tracer": MagicMock(), "CHAT_BACKEND": backend,
                                "HOSTED_CONTINUITY_ENABLED": False, "SHOW_STATISTICS": False, "ENABLE_FEEDBACK": False,
                                "get_auth_info": AsyncMock(return_value={"authorized": True, "copilot_auth_mode": mode}),
                                "ingest_files_session": ingest, "get_owned_conversation": owner,
                                "call_orchestrator_stream": stream, "call_hosted_agent_stream": stream,
                                "build_invocation_messages": Mock(return_value=[]),
                            }.items():
                                stack.enter_context(patch.object(chat, name, replacement, create=True))
                            await chat.handle_message(SimpleNamespace(
                                id="upload-reference", content="A text question",
                                elements=[SimpleNamespace(
                                    mime="application/pdf", name="file.pdf", path="forged.pdf", size=1,
                                )],
                            ))
                            eligible = value != "false" and mode != "anonymous" and (
                                backend == "orchestrator" or has_conversation
                            )
                            self.assertEqual(int(eligible), ingest.await_count)
                            self.assertEqual(
                                int(eligible and backend == "orchestrator" and has_conversation),
                                owner.await_count,
                            )
                            self.assertEqual(1, len(turns), "Text chat must remain available")
                            notices = "".join(call.args[0] for call in response.stream_token.await_args_list)
                            if value == "false":
                                self.assertIn(ATTACHMENTS_UNAVAILABLE_MESSAGE, notices)
                                self.assertNotIn("uploaded_docs", state)
                            if eligible:
                                self.assertEqual(["file.pdf"], state["uploaded_docs"])
                                if has_conversation:
                                    self.assertEqual(
                                        "hosted-id" if backend == "hosted_agent" else "classic-id",
                                        ingest.await_args.kwargs["conversation_id"],
                                    )

                            # Attachment-only forged messages must not reach either chat backend.
                            if value == "false":
                                turns.clear()
                                await chat.handle_message(SimpleNamespace(
                                    id="forged-only", content="", elements=[SimpleNamespace()],
                                ))
                                self.assertEqual([], turns)
                                self.assertIn(ATTACHMENTS_UNAVAILABLE_MESSAGE, response.content)


if __name__ == "__main__":
    unittest.main()
