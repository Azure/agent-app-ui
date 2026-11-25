import os
import unittest
from unittest.mock import patch

from gpt_rag_ui.config.hosted_continuity_config import (
    HostedContinuityConfigError,
    load_hosted_continuity_settings,
)


class TestHostedContinuityConfig(unittest.TestCase):
    # Explicit capability-mode fixture: capability mode needs no protocol
    # version/impersonation gate, so it remains the simplest fixture for
    # exercising the history/store validation shared by both modes.
    _VALID_VALUES = {
        "HOSTED_CONTINUITY_ENABLED": "true",
        "HOSTED_CONVERSATION_OWNER_BINDING": "capability",
        "HOSTED_CONVERSATION_CAPABILITY_KEY": "a" * 32,
        "HOSTED_CONVERSATION_CAPABILITY_KEY_ID": "key-1",
        "HOSTED_CONVERSATION_STORE_BASE_URL": "https://agent.example.com/openai/v1",
        "HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE": "api://hosted-agent/.default",
    }

    # Delegated-mode fixture: the preferred/default mode once the live
    # evidence gate (Azure/GPT-RAG#591, "OQ-OWN") is attested.
    _VALID_DELEGATED_VALUES = {
        "HOSTED_CONTINUITY_ENABLED": "true",
        "HOSTED_CONVERSATION_OWNER_BINDING": "delegated",
        "HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED": "true",
        "HOSTED_AGENT_PROTOCOL_VERSION": "2.0.0",
        "HOSTED_CONVERSATION_STORE_BASE_URL": "https://agent.example.com/openai/v1",
        "HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE": "api://hosted-agent/.default",
    }

    def _load(self, values: dict[str, str]):
        def get_value(key, default=None, _type=str):
            return values.get(key, default)

        config = type("Config", (), {"get": staticmethod(get_value)})()
        with patch.dict(os.environ, {}, clear=True):
            return load_hosted_continuity_settings(config)

    def test_disabled_by_default_and_ignores_other_settings(self):
        settings = self._load({})
        self.assertFalse(settings.enabled)
        self.assertEqual(settings.owner_binding, "capability")
        self.assertFalse(settings.uses_capability_binding)
        self.assertFalse(settings.uses_delegated_binding)

    def test_disabled_ignores_invalid_values_of_other_settings(self):
        # Preserves current behavior when the feature flag is false: no
        # other setting is even parsed, let alone validated.
        settings = self._load(
            {
                "HOSTED_CONTINUITY_ENABLED": "false",
                "HOSTED_HISTORY_MAX_ITEMS": "not-a-number",
                "HOSTED_CONVERSATION_OWNER_BINDING": "nonsense",
            }
        )
        self.assertFalse(settings.enabled)

    def test_enabled_with_valid_values_returns_capability_defaults(self):
        settings = self._load(dict(self._VALID_VALUES))
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.owner_binding, "capability")
        self.assertTrue(settings.uses_capability_binding)
        self.assertFalse(settings.uses_delegated_binding)
        self.assertEqual(settings.capability_ttl_seconds, 900)
        self.assertEqual(settings.history_max_items, 40)
        self.assertEqual(settings.history_max_tokens, 8000)
        self.assertEqual(settings.history_truncation, "drop_oldest")
        self.assertEqual(
            settings.store_base_url, "https://agent.example.com/openai/v1"
        )
        self.assertEqual(settings.store_resource_scope, "api://hosted-agent/.default")

    def test_store_resource_scope_falls_back_to_hosted_agent_scope(self):
        values = dict(self._VALID_VALUES)
        del values["HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE"]
        values["HOSTED_AGENT_RESOURCE_SCOPE"] = "api://hosted-agent-fallback/.default"
        settings = self._load(values)
        self.assertEqual(
            settings.store_resource_scope, "api://hosted-agent-fallback/.default"
        )

    def test_delegated_is_the_default_owner_binding_when_enabled(self):
        # Preferred/default per the OQ-OWN pivot: leaving
        # HOSTED_CONVERSATION_OWNER_BINDING unset while enabling continuity
        # resolves to 'delegated', not 'capability'.
        values = dict(self._VALID_DELEGATED_VALUES)
        del values["HOSTED_CONVERSATION_OWNER_BINDING"]
        settings = self._load(values)
        self.assertEqual(settings.owner_binding, "delegated")
        self.assertTrue(settings.uses_delegated_binding)

    def test_delegated_binding_is_inert_until_explicitly_validated(self):
        values = dict(self._VALID_DELEGATED_VALUES)
        del values["HOSTED_CONVERSATION_OWNER_BINDING_VALIDATED"]
        with self.assertRaisesRegex(HostedContinuityConfigError, "inert"):
            self._load(values)

    def test_delegated_binding_requires_protocol_version(self):
        values = dict(self._VALID_DELEGATED_VALUES)
        del values["HOSTED_AGENT_PROTOCOL_VERSION"]
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_AGENT_PROTOCOL_VERSION"
        ):
            self._load(values)

    def test_delegated_binding_rejects_protocol_version_below_minimum(self):
        values = dict(self._VALID_DELEGATED_VALUES)
        values["HOSTED_AGENT_PROTOCOL_VERSION"] = "1.9.9"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_AGENT_PROTOCOL_VERSION"
        ):
            self._load(values)

    def test_delegated_binding_rejects_malformed_protocol_version(self):
        values = dict(self._VALID_DELEGATED_VALUES)
        values["HOSTED_AGENT_PROTOCOL_VERSION"] = "not-a-version"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_AGENT_PROTOCOL_VERSION"
        ):
            self._load(values)

    def test_delegated_binding_accepts_higher_protocol_version(self):
        values = dict(self._VALID_DELEGATED_VALUES)
        values["HOSTED_AGENT_PROTOCOL_VERSION"] = "2.1.0"
        settings = self._load(values)
        self.assertTrue(settings.uses_delegated_binding)

    def test_delegated_binding_activates_once_validated_flag_and_protocol_set(self):
        settings = self._load(dict(self._VALID_DELEGATED_VALUES))
        self.assertTrue(settings.uses_delegated_binding)
        self.assertFalse(settings.uses_capability_binding)
        self.assertEqual(settings.protocol_version, "2.0.0")

    def test_delegated_binding_does_not_require_capability_key(self):
        # Delegated mode never mints/validates a signed capability, so it
        # must not require the capability signing key/key id at all.
        values = dict(self._VALID_DELEGATED_VALUES)
        settings = self._load(values)
        self.assertEqual(settings.capability_key, "")
        self.assertEqual(settings.capability_key_id, "")

    def test_capability_binding_remains_selectable_as_explicit_fallback(self):
        settings = self._load(dict(self._VALID_VALUES))
        self.assertEqual(settings.owner_binding, "capability")
        self.assertTrue(settings.uses_capability_binding)
        self.assertFalse(settings.uses_delegated_binding)
        # Capability mode does not require the delegated-mode gate at all.
        self.assertEqual(settings.protocol_version, "")

    def test_rejects_unknown_owner_binding(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_CONVERSATION_OWNER_BINDING"] = "trust-me"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_CONVERSATION_OWNER_BINDING"
        ):
            self._load(values)

    def test_capability_binding_requires_key(self):
        values = dict(self._VALID_VALUES)
        del values["HOSTED_CONVERSATION_CAPABILITY_KEY"]
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_CONVERSATION_CAPABILITY_KEY"
        ):
            self._load(values)

    def test_capability_binding_requires_long_enough_key(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_CONVERSATION_CAPABILITY_KEY"] = "short"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_CONVERSATION_CAPABILITY_KEY"
        ):
            self._load(values)

    def test_capability_binding_requires_key_id(self):
        values = dict(self._VALID_VALUES)
        del values["HOSTED_CONVERSATION_CAPABILITY_KEY_ID"]
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_CONVERSATION_CAPABILITY_KEY_ID"
        ):
            self._load(values)

    def test_rejects_out_of_bounds_ttl(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_CONVERSATION_CAPABILITY_TTL_SECONDS"] = "10"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_CONVERSATION_CAPABILITY_TTL_SECONDS"
        ):
            self._load(values)

    def test_rejects_out_of_bounds_max_items(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_HISTORY_MAX_ITEMS"] = "0"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_HISTORY_MAX_ITEMS"
        ):
            self._load(values)

    def test_rejects_out_of_bounds_max_tokens(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_HISTORY_MAX_TOKENS"] = "10"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_HISTORY_MAX_TOKENS"
        ):
            self._load(values)

    def test_rejects_unsupported_truncation_strategy(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_HISTORY_TRUNCATION"] = "drop_newest"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_HISTORY_TRUNCATION"
        ):
            self._load(values)

    def test_requires_store_base_url(self):
        values = dict(self._VALID_VALUES)
        del values["HOSTED_CONVERSATION_STORE_BASE_URL"]
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HOSTED_CONVERSATION_STORE_BASE_URL"
        ):
            self._load(values)

    def test_rejects_non_https_store_base_url(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_CONVERSATION_STORE_BASE_URL"] = "http://agent.example.com"
        with self.assertRaisesRegex(
            HostedContinuityConfigError, "HTTPS"
        ):
            self._load(values)

    def test_allows_local_http_store_base_url(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_CONVERSATION_STORE_BASE_URL"] = "http://localhost:8000"
        settings = self._load(values)
        self.assertEqual(settings.store_base_url, "http://localhost:8000")

    def test_rejects_arm_store_resource_scope(self):
        values = dict(self._VALID_VALUES)
        for scope in (
            "https://management.azure.com/.default",
            "https://MANAGEMENT.AZURE.COM/.default",
            "https://user@management.azure.com/.default",
        ):
            values["HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE"] = scope
            with self.subTest(scope=scope), self.assertRaisesRegex(HostedContinuityConfigError, "not Azure ARM"):
                self._load(values)

    def test_arm_name_outside_hostname_does_not_reject_store_scope(self):
        values = dict(self._VALID_VALUES)
        for scope in (
            "https://management.azure.com.example.com/.default",
            "https://example.com/management.azure.com/.default",
            "https://management.azure.com@example.com/.default",
            "api://management.azure.com-agent/.default",
        ):
            values["HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE"] = scope
            with self.subTest(scope=scope):
                self.assertEqual(scope, self._load(values).store_resource_scope)

    def test_requires_explicit_default_scope_suffix(self):
        values = dict(self._VALID_VALUES)
        values["HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE"] = "api://hosted-agent"
        with self.assertRaisesRegex(HostedContinuityConfigError, "/.default"):
            self._load(values)


if __name__ == "__main__":
    unittest.main()
