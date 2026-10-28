import unittest
import base64
import json

import httpx

from gpt_rag_ui.auth.auth_common import canonical_principal_id, is_user_authorized, http_claims_challenge


TENANT_ID = "11111111-2222-3333-4444-555555555555"
OBJECT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
PRINCIPAL_ID = f"{TENANT_ID}:{OBJECT_ID}"


class FakeConfig:
    def __init__(self, values=None):
        self.values = values or {}

    def get(self, key, default=None, type=str):
        value = self.values.get(key, default)
        return type(value) if value is not None and type is not None else value


class AuthCommonTests(unittest.TestCase):
    def test_http_claims_parser_supports_direct_and_encoded_bearer_challenges(self):
        claims = '{"access_token":{"acrs":{"essential":true,"value":"synthetic"}}}'
        for value in (json.dumps(claims), base64.urlsafe_b64encode(claims.encode()).decode().rstrip("=")):
            with self.subTest(encoded=value != json.dumps(claims)):
                response = httpx.Response(401, headers={"www-authenticate": f"Bearer claims={value}"})
                self.assertEqual(claims, http_claims_challenge(response))

    def test_http_claims_parser_rejects_ambiguous_malformed_or_body_only_claims(self):
        encoded = base64.b64encode(b'{"access_token":{"acrs":{"essential":true}}}').decode()
        for headers in (
            [],
            [("www-authenticate", f"Basic claims={encoded}")],
            [("www-authenticate", "Bearer claims=invalid")],
            [("www-authenticate", f"Bearer claims={encoded}, claims={encoded}")],
            [("www-authenticate", f"Bearer claims={encoded}"), ("www-authenticate", f"Bearer claims={encoded}")],
            [("www-authenticate", "Bearer claims=" + "a" * 16384)],
        ):
            with self.subTest(headers=len(headers)):
                response = httpx.Response(403, headers=headers, json={"claims": encoded})
                self.assertIsNone(http_claims_challenge(response))

    def test_canonical_principal_requires_tenant_and_object(self):
        self.assertEqual(
            PRINCIPAL_ID,
            canonical_principal_id(TENANT_ID.upper(), OBJECT_ID.upper()),
        )
        with self.assertRaises(ValueError):
            canonical_principal_id("", OBJECT_ID)

    def test_allowlist_accepts_canonical_or_legacy_bare_oid(self):
        for allowed_id in (PRINCIPAL_ID.upper(), OBJECT_ID.upper()):
            with self.subTest(allowed_id=allowed_id):
                self.assertTrue(
                    is_user_authorized(
                        FakeConfig({"ALLOWED_USER_PRINCIPALS": allowed_id}),
                        "user@example.com",
                        PRINCIPAL_ID,
                    )
                )

    def test_allowlist_name_is_case_insensitive_and_denies_mismatch(self):
        config = FakeConfig({"ALLOWED_USER_NAMES": "User@Example.com"})
        self.assertTrue(
            is_user_authorized(
                config,
                "user@example.COM",
                PRINCIPAL_ID,
            )
        )
        self.assertFalse(
            is_user_authorized(
                config,
                "other@example.com",
                PRINCIPAL_ID,
            )
        )

if __name__ == "__main__":
    unittest.main()
