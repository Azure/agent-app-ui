"""Dependency failures retain public outcomes without hiding programming errors."""

import base64
import importlib
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from azure.appconfiguration.provider import load
from azure.core.exceptions import AzureError, ResourceNotFoundError
from tenacity import Future, RetryError, stop_after_attempt, wait_none

from gpt_rag_ui.auth import oauth
from gpt_rag_ui.clients import blob, hosted_agent_client, orchestrator_client
from gpt_rag_ui.config import appconfig, hosted_continuity_config, panel_config
from gpt_rag_ui.config.errors import ConfigurationError
from gpt_rag_ui.telemetry.monitoring import Telemetry


class FailureContractTests(unittest.TestCase):
    def client(self, values):
        with patch.dict(os.environ, {}, clear=True):
            client = appconfig.AppConfigClient()
        client.client = values
        client.get_config_with_retry = client.get_config_with_retry.retry_with(
            wait=wait_none(), stop=stop_after_attempt(2),
        ).__get__(client)
        return client

    def test_real_retry_missing_setting_uses_default(self):
        client = self.client({})
        self.assertEqual("fallback", client.get("missing", "fallback"))
        self.assertIsNone(client.get_value("missing", allow_none=True))
        with self.assertRaises(ConfigurationError):
            client.get("missing")

    def test_real_retry_azure_failure_uses_observable_default(self):
        values = Mock()
        values.__getitem__ = Mock(side_effect=AzureError("provider unavailable"))
        client = self.client(values)
        with self.assertLogs("gpt_rag_ui.appconfig", level="WARNING"):
            self.assertEqual("fallback", client.get("setting", "fallback"))
        self.assertEqual(2, values.__getitem__.call_count)

    def test_real_retry_does_not_hide_unexpected_provider_failure(self):
        values = Mock()
        failure = RuntimeError("provider defect")
        values.__getitem__ = Mock(side_effect=failure)
        client = self.client(values)
        with self.assertRaises(RuntimeError) as raised:
            client.get("setting", "fallback")
        self.assertIs(failure, raised.exception)
        self.assertEqual(1, values.__getitem__.call_count)

    def test_provider_internal_retry_error_is_not_our_azure_exhaustion(self):
        attempt = Future(1)
        attempt.set_exception(RuntimeError("provider internal defect"))
        failure = RetryError(attempt)
        values = Mock()
        values.__getitem__ = Mock(side_effect=failure)
        client = self.client(values)
        with self.assertRaises(RetryError) as raised:
            client.get("setting", "fallback")
        self.assertIs(failure, raised.exception)
        self.assertEqual(1, values.__getitem__.call_count)

    def test_provider_auth_failure_keeps_disconnected_without_connection_fallback(self):
        with (
            patch.dict(os.environ, {"APP_CONFIG_ENDPOINT": "https://config.example.invalid"}, clear=True),
            patch.object(appconfig, "load", side_effect=AzureError("unavailable")) as provider,
        ):
            client = appconfig.AppConfigClient()
        self.assertFalse(client.connected)
        self.assertEqual({}, client.client)
        provider.assert_called_once()
        self.assertEqual(["agent-lz", "agent-lz"],
                         [selector.label_filter for selector in provider.call_args.kwargs["selects"]])
        self.assertEqual(["ORCHESTRATION_HOST", "APPLICATION_MODE"],
                         [selector.key_filter for selector in provider.call_args.kwargs["selects"]])

    def test_provider_invalid_endpoint_then_real_malformed_connection_is_disconnected(self):
        def provider(**kwargs):
            if "endpoint" in kwargs:
                raise ValueError("invalid endpoint")
            return load(**kwargs)

        with (
            patch.dict(os.environ, {
                "APP_CONFIG_ENDPOINT": "invalid",
                "AZURE_APPCONFIG_CONNECTION_STRING": "malformed",
            }, clear=True),
            patch.object(appconfig, "load", side_effect=provider),
        ):
            client = appconfig.AppConfigClient()
        self.assertFalse(client.connected)
        self.assertEqual({}, client.client)

    def test_provider_unexpected_load_failure_does_not_become_disconnected_success(self):
        with (
            patch.dict(os.environ, {"APP_CONFIG_ENDPOINT": "https://config.example.invalid"}, clear=True),
            patch.object(appconfig, "load", side_effect=RuntimeError("provider defect")),
        ):
            with self.assertRaisesRegex(RuntimeError, "provider defect"):
                appconfig.AppConfigClient()

    def test_optional_setting_readers_keep_missing_default_but_propagate_defects(self):
        client = self.client({})
        readers = (
            lambda: panel_config._read_setting(client, {}, "setting", "fallback"),
            lambda: hosted_continuity_config._read_setting(client, {}, "setting", "fallback"),
            lambda: hosted_agent_client._get_config_value("setting", "fallback"),
            lambda: orchestrator_client._get_config_value("setting", default="fallback"),
            lambda: oauth.get_env_var("setting", "fallback"),
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(hosted_agent_client, "config", client),
            patch.object(orchestrator_client, "config", client),
            patch.object(oauth, "config", client),
        ):
            for read in readers:
                self.assertEqual("fallback", read())
            with patch.object(client, "get_value", side_effect=RuntimeError("config defect")):
                for read in readers:
                    with self.subTest(reader=read), self.assertRaisesRegex(RuntimeError, "config defect"):
                        read()

    def test_debug_decoders_reject_malformed_payloads_not_programming_errors(self):
        with patch.object(Telemetry, "configure_monitoring"), patch.dict(
            os.environ, {"CHAT_BACKEND": "orchestrator", "CHAINLIT_AUTH_SECRET": "test-secret"}
        ):
            chat = importlib.import_module("gpt_rag_ui.services.chat")
        for module in (oauth, orchestrator_client, chat):
            with self.subTest(module=module.__name__):
                for token in ("missing", "x.a.x", "x._w.x", "x.ew.x", "x.W10.x"):
                    self.assertIsNone(module._decode_jwt_unverified(token))
                encoded = base64.urlsafe_b64encode(json.dumps({"exp": 10}).encode()).decode()
                self.assertEqual({"exp": 10}, module._decode_jwt_unverified(f"x.{encoded}.x"))
                with patch.object(module.json, "loads", side_effect=RuntimeError("decoder defect")):
                    with self.assertRaisesRegex(RuntimeError, "decoder defect"):
                        module._decode_jwt_unverified(f"x.{encoded}.x")

    def test_debug_expiry_rejects_json_non_integer_values(self):
        for value in ("bad", [], {}, float("inf")):
            with self.subTest(value=value), patch.object(oauth, "_decode_jwt_unverified", return_value={"exp": value}):
                self.assertIsNone(oauth._jwt_exp_unverified("token"))

    def blob_client(self):
        with patch.object(blob, "BlobServiceClient") as sdk:
            client = blob.BlobClient("https://storage.example.invalid/docs/a%20b.pdf", credential=Mock())
        return client, sdk.return_value

    def test_blob_download_preserves_not_found_and_translates_azure_failure(self):
        client, sdk = self.blob_client()
        target = sdk.get_blob_client.return_value
        missing = ResourceNotFoundError("not found")
        for method in (client.download_blob, client.download_blob_chunks):
            with self.subTest(method=method.__name__):
                target.download_blob.side_effect = missing
                with self.assertRaises(ResourceNotFoundError) as raised:
                    method()
                self.assertIs(missing, raised.exception)
                target.download_blob.side_effect = AzureError("transport failed")
                with self.assertRaisesRegex(Exception, "Blob client error") as raised:
                    method()
                self.assertIsInstance(raised.exception.__cause__, AzureError)

    def test_blob_download_unexpected_errors_propagate_unchanged(self):
        client, sdk = self.blob_client()
        failure = RuntimeError("sdk defect")
        sdk.get_blob_client.return_value.download_blob.side_effect = failure
        for method in (client.download_blob, client.download_blob_chunks):
            with self.subTest(method=method.__name__), self.assertRaises(RuntimeError) as raised:
                method()
            self.assertIs(failure, raised.exception)

    def test_sas_azure_failure_keeps_encoded_unsigned_fallback_with_log(self):
        client, sdk = self.blob_client()
        sdk.get_user_delegation_key.side_effect = AzureError("delegation unavailable")
        with self.assertLogs(level="ERROR"):
            self.assertEqual("https://storage.example.invalid/docs/a%20b.pdf", client.generate_sas_url())

    def test_sas_unexpected_errors_do_not_become_unsigned_url(self):
        client, sdk = self.blob_client()
        sdk.get_user_delegation_key.side_effect = RuntimeError("sdk defect")
        with self.assertRaisesRegex(RuntimeError, "sdk defect"):
            client.generate_sas_url()

    def test_invalid_blob_url_is_translated_without_catching_implementation_defects(self):
        with self.assertRaises(OSError):
            blob.BlobClient("https://[invalid", credential=Mock())
        with patch.object(blob, "urlparse", side_effect=RuntimeError("parser defect")):
            with self.assertRaisesRegex(RuntimeError, "parser defect"):
                blob.BlobClient("https://storage.example.invalid/docs/blob", credential=Mock())

    def test_optional_telemetry_falls_back_for_dictconfig_errors_not_programming_errors(self):
        config = Mock()
        config.get_value.return_value = None
        with (
            patch.object(Telemetry, "configure_logging", side_effect=ValueError("invalid logging config")),
            patch.object(Telemetry, "configure_basic") as basic,
            self.assertLogs(level="WARNING"),
        ):
            Telemetry.configure_monitoring(config, "APPLICATIONINSIGHTS_CONNECTION_STRING", "ui")
        basic.assert_called_once_with(config)
        with (
            patch.object(Telemetry, "configure_logging", side_effect=RuntimeError("logging defect")),
            patch.object(Telemetry, "configure_basic") as basic,
        ):
            with self.assertRaisesRegex(RuntimeError, "logging defect"):
                Telemetry.configure_monitoring(config, "APPLICATIONINSIGHTS_CONNECTION_STRING", "ui")
        basic.assert_not_called()


class ConversationFailureContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(
            orchestrator_client, "_build_orchestrator_service_url",
            return_value=("https://orchestrator.example.invalid/conversations", {"mode": "fixture"}),
        ))

    async def test_transport_failures_keep_history_contract_but_defects_propagate(self):
        operations = (
            ("get", orchestrator_client.call_orchestrator_list_conversations, (),
             {"conversations": [], "has_more": False, "skip": 0, "limit": 10}),
            ("get", orchestrator_client.call_orchestrator_get_conversation, ("conversation",), None),
            ("patch", orchestrator_client.call_orchestrator_update_conversation, ("conversation", "name"), False),
            ("delete", orchestrator_client.call_orchestrator_delete_conversation, ("conversation",), False),
        )
        for verb, operation, args, expected in operations:
            with self.subTest(operation=operation.__name__):
                client = AsyncMock()
                client.__aenter__.return_value = client
                with patch.object(orchestrator_client.httpx, "AsyncClient", return_value=client):
                    getattr(client, verb).side_effect = httpx.ReadTimeout("timeout")
                    with self.assertLogs(orchestrator_client.logger, level="ERROR"):
                        self.assertEqual(expected, await operation("test-token", *args))
                    failure = RuntimeError("client defect")
                    getattr(client, verb).side_effect = failure
                    with self.assertRaises(RuntimeError) as raised:
                        await operation("test-token", *args)
                    self.assertIs(failure, raised.exception)

    async def test_malformed_history_json_keeps_existing_failure_outcomes(self):
        for operation, args, expected in (
            (orchestrator_client.call_orchestrator_list_conversations, (),
             {"conversations": [], "has_more": False, "skip": 0, "limit": 10}),
            (orchestrator_client.call_orchestrator_get_conversation, ("conversation",), None),
        ):
            with self.subTest(operation=operation.__name__):
                client = AsyncMock()
                client.__aenter__.return_value = client
                client.get.return_value = httpx.Response(200, text="not json")
                with (
                    patch.object(orchestrator_client.httpx, "AsyncClient", return_value=client),
                    self.assertLogs(orchestrator_client.logger, level="ERROR"),
                ):
                    self.assertEqual(expected, await operation("test-token", *args))
