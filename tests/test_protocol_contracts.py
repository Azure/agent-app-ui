"""Runtime compatibility of documented protocol methods."""

import inspect
import unittest

from gpt_rag_ui.auth.embed_auth import TokenValidator
from gpt_rag_ui.clients.hosted_agent_client import AsyncCredential
from gpt_rag_ui.clients.native_metadata import MetadataStore
from gpt_rag_ui.config.appconfig import IngestionUploadConfig
from gpt_rag_ui.config.chat_backend import ChatBackendConfig
from gpt_rag_ui.config.hosted_continuity_config import ContinuityConfig
from gpt_rag_ui.config.native import NativeConfig
from gpt_rag_ui.config.panel_config import PanelConfig


class ProtocolContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_inherited_protocol_methods_keep_no_op_behavior(self):
        cases = (
            (IngestionUploadConfig, "get", ("key", "default", str)),
            (ChatBackendConfig, "get", ("key", "default", str)),
            (ContinuityConfig, "get", ("key", "default", str)),
            (NativeConfig, "get", ("key",)),
            (NativeConfig, "refresh", ()),
            (PanelConfig, "get", ("key", "default", str)),
            (TokenValidator, "validate", ("token",)),
            (AsyncCredential, "get_token", ("scope",)),
            (AsyncCredential, "close", ()),
            (MetadataStore, "get", ("owner", "id")),
            (MetadataStore, "create", ({},)),
            (MetadataStore, "replace", ({},)),
            (MetadataStore, "list", ("owner", "kind")),
            (MetadataStore, "aclose", ()),
        )
        for protocol, method_name, args in cases:
            with self.subTest(protocol=protocol.__name__, method=method_name):
                concrete = type("Concrete", (protocol,), {})()
                method = getattr(concrete, method_name)
                result = method(*args)
                if inspect.isawaitable(result):
                    result = await result
                self.assertIsNone(result)
                self.assertTrue(method.__doc__)

    def test_native_config_defaults_are_unchanged(self):
        parameters = inspect.signature(NativeConfig.get).parameters
        self.assertEqual(parameters["default"].default, "")
        self.assertIs(parameters["type"].default, str)
