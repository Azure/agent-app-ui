import json
import time
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from gpt_rag_ui.auth.entra_token import EntraTokenError, EntraTokenValidator


TENANT_ID = "11111111-2222-3333-4444-555555555555"
AUDIENCE = "api://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
PORTAL_CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class EntraTokenValidatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )
        public_jwk = json.loads(RSAAlgorithm.to_jwk(self.private_key.public_key()))
        public_jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
        self.jwks = {"keys": [public_jwk]}

        async def load_jwks():
            return self.jwks

        self.validator = EntraTokenValidator(
            tenant_id=TENANT_ID,
            audience=AUDIENCE,
            clock_skew_seconds=0,
            jwks_loader=load_jwks,
        )

    def create_token(self, **overrides):
        kid = overrides.pop("_kid", "test-key")
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "tid": TENANT_ID,
            "oid": "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            "sub": "subject",
            "name": "Portal User",
            "preferred_username": "portal.user@example.com",
            "scp": "openid user_impersonation",
            "ver": "2.0",
            "azp": PORTAL_CLIENT_ID,
            "iat": now,
            "nbf": now - 1,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="RS256",
            headers={"kid": kid},
        )

    async def test_accepts_valid_v2_access_token(self):
        claims = await self.validator.validate(self.create_token())

        self.assertEqual(TENANT_ID, claims["tid"])
        self.assertEqual(
            "bbbbbbbb-cccc-dddd-eeee-ffffffffffff",
            claims["oid"],
        )

    async def test_work_iq_assertion_requires_own_app_audience_and_access_as_user(self):
        validator = EntraTokenValidator(
            tenant_id=TENANT_ID,
            audience=PORTAL_CLIENT_ID,
            required_scope="access_as_user",
            clock_skew_seconds=0,
            jwks_loader=self.validator._jwks_loader,
        )
        valid = self.create_token(
            aud=PORTAL_CLIENT_ID, scp="user_impersonation access_as_user",
        )
        self.assertEqual(TENANT_ID, (await validator.validate(valid))["tid"])
        for overrides in (
            {"aud": "https://search.azure.com"},
            {"scp": "user_impersonation"},
            {"scp": None},
            {"exp": int(time.time()) - 10},
            {"tid": "99999999-8888-7777-6666-555555555555"},
        ):
            claims = {"aud": PORTAL_CLIENT_ID, "scp": "access_as_user", **overrides}
            with self.subTest(overrides=overrides), self.assertRaises(EntraTokenError):
                await validator.validate(self.create_token(**claims))

    async def test_rejects_wrong_audience(self):
        with self.assertRaises(EntraTokenError):
            await self.validator.validate(
                self.create_token(aud="api://different")
            )

    async def test_rejects_wrong_issuer_or_tenant(self):
        for overrides in (
            {"iss": "https://issuer.example.com"},
            {"tid": "99999999-8888-7777-6666-555555555555"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(EntraTokenError):
                    await self.validator.validate(self.create_token(**overrides))

    async def test_rejects_expired_token(self):
        with self.assertRaises(EntraTokenError):
            await self.validator.validate(
                self.create_token(exp=int(time.time()) - 10)
            )

    async def test_rejects_token_without_object_id_even_when_sub_exists(self):
        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "tid": TENANT_ID,
                "sub": "subject-only-is-not-accepted",
                "scp": "user_impersonation",
                "ver": "2.0",
                "azp": PORTAL_CLIENT_ID,
                "iat": now,
                "nbf": now - 1,
                "exp": now + 300,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaisesRegex(EntraTokenError, "oid"):
            await self.validator.validate(token)

    async def test_rejects_token_without_required_delegated_scope(self):
        for scope_claim in ("other_scope", None):
            with self.subTest(scope_claim=scope_claim):
                with self.assertRaisesRegex(EntraTokenError, "required"):
                    await self.validator.validate(
                        self.create_token(scp=scope_claim)
                    )

    async def test_unknown_key_ids_do_not_force_repeated_jwks_refreshes(self):
        loader_calls = 0

        async def counted_loader():
            nonlocal loader_calls
            loader_calls += 1
            return self.jwks

        validator = EntraTokenValidator(
            tenant_id=TENANT_ID,
            audience=AUDIENCE,
            jwks_loader=counted_loader,
        )

        for kid in ("unknown-one", "unknown-two"):
            with self.assertRaisesRegex(EntraTokenError, "signing key"):
                await validator.validate(self.create_token(_kid=kid))

        self.assertEqual(1, loader_calls)

    async def test_unknown_key_throttle_does_not_starve_rotation_refresh(self):
        loader_calls = 0
        monotonic_time = 100.0

        async def counted_loader():
            nonlocal loader_calls
            loader_calls += 1
            return self.jwks

        validator = EntraTokenValidator(
            tenant_id=TENANT_ID,
            audience=AUDIENCE,
            unknown_key_refresh_interval_seconds=60,
            clock_skew_seconds=0,
            jwks_loader=counted_loader,
        )

        with patch(
            "gpt_rag_ui.auth.entra_token.time.monotonic",
            side_effect=lambda: monotonic_time,
        ):
            with self.assertRaisesRegex(EntraTokenError, "signing key"):
                await validator.validate(
                    self.create_token(_kid="unknown-one")
                )

            monotonic_time = 110.0
            with self.assertRaisesRegex(EntraTokenError, "signing key"):
                await validator.validate(
                    self.create_token(_kid="unknown-two")
                )

            rotated_jwk = dict(self.jwks["keys"][0])
            rotated_jwk["kid"] = "rotated-key"
            self.jwks = {"keys": [rotated_jwk]}
            monotonic_time = 161.0

            claims = await validator.validate(
                self.create_token(_kid="rotated-key")
            )

        self.assertEqual(TENANT_ID, claims["tid"])
        self.assertEqual(2, loader_calls)

    async def test_rejects_algorithm_confusion(self):
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "tid": TENANT_ID,
                "oid": "user",
                "scp": "user_impersonation",
                "ver": "2.0",
                "azp": PORTAL_CLIENT_ID,
                "exp": int(time.time()) + 300,
            },
            "not-a-public-key-but-long-enough-for-hs256",
            algorithm="HS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaisesRegex(EntraTokenError, "RS256"):
            await self.validator.validate(token)

    async def test_rejects_v1_token_even_with_appid(self):
        with self.assertRaisesRegex(EntraTokenError, "v2.0"):
            await self.validator.validate(
                self.create_token(
                    ver="1.0",
                    azp=None,
                    appid=PORTAL_CLIENT_ID,
                )
            )


if __name__ == "__main__":
    unittest.main()
