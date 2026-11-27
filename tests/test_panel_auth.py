import time
import unittest
from unittest.mock import Mock

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from gpt_rag_ui.auth.entra_token import EntraTokenValidator
from gpt_rag_ui.auth.panel_auth import (
    PanelAuthError,
    PanelForbiddenError,
    validate_panel_bearer,
)

TENANT_ID = "11111111-2222-3333-4444-555555555555"
AUDIENCE = "api://panel/.default"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
OID = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"


class PanelBearerValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_jwk = __import__("json").loads(
            RSAAlgorithm.to_jwk(self.private_key.public_key())
        )
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

    def _token(self, **overrides) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "tid": TENANT_ID,
            "oid": OID,
            "scp": "user_impersonation",
            "ver": "2.0",
            "iat": now,
            "nbf": now - 1,
            "exp": now + 300,
        }
        claims.update(overrides)
        return jwt.encode(
            claims, self.private_key, algorithm="RS256", headers={"kid": "test-key"}
        )

    def _request(self, token: str | None):
        request = Mock()
        headers = {}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request.headers = headers
        return request

    async def test_valid_delegated_token_resolves_principal(self):
        principal = await validate_panel_bearer(
            self._request(self._token()), self.validator
        )
        self.assertEqual(principal.oid, OID)
        self.assertEqual(principal.tenant_id, TENANT_ID)

    async def test_missing_authorization_header_is_401(self):
        with self.assertRaises(PanelAuthError):
            await validate_panel_bearer(self._request(None), self.validator)

    async def test_malformed_authorization_header_is_401(self):
        request = Mock()
        request.headers = {"Authorization": "Basic abc123"}
        with self.assertRaises(PanelAuthError):
            await validate_panel_bearer(request, self.validator)

    async def test_wrong_audience_is_401(self):
        with self.assertRaises(PanelAuthError):
            await validate_panel_bearer(
                self._request(self._token(aud="api://different")), self.validator
            )

    async def test_expired_token_is_401(self):
        with self.assertRaises(PanelAuthError):
            await validate_panel_bearer(
                self._request(self._token(exp=int(time.time()) - 10)),
                self.validator,
            )

    async def test_app_only_token_missing_scp_is_403_not_401(self):
        """The reference fail-closed pattern (gpt-rag-ingestion
        ``validate_delegated_user_bearer``): an app-only token is a
        well-formed, trusted token that simply is not delegated -- 403, not
        401."""
        with self.assertRaises(PanelForbiddenError):
            await validate_panel_bearer(
                self._request(self._token(scp=None)), self.validator
            )

    async def test_idtyp_app_is_403(self):
        with self.assertRaises(PanelForbiddenError):
            await validate_panel_bearer(
                self._request(self._token(idtyp="app")), self.validator
            )

    async def test_idtyp_user_is_accepted(self):
        principal = await validate_panel_bearer(
            self._request(self._token(idtyp="user")), self.validator
        )
        self.assertEqual(principal.oid, OID)

    async def test_token_without_oid_is_401_from_validator(self):
        """gpt_rag_ui.auth.entra_token.EntraTokenValidator itself requires GUID tid/oid
        claims and raises before returning; this is indistinguishable from
        any other malformed-token failure here, so it maps to 401 (not the
        403 defense-in-depth branch in ``validate_panel_bearer``, which is
        unreachable in practice because the shared validator already
        guarantees a normalized oid on any successful ``validate()``)."""
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": AUDIENCE,
                "tid": TENANT_ID,
                "sub": "subject-only",
                "scp": "user_impersonation",
                "ver": "2.0",
                "exp": int(time.time()) + 300,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaises(PanelAuthError):
            await validate_panel_bearer(self._request(token), self.validator)


if __name__ == "__main__":
    unittest.main()
