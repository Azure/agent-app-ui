"""Offline ASGI + real pinned MSAL OAuth flows; provider HTTP is synthetic."""

import asyncio
import base64
from contextlib import ExitStack
import hashlib
import json
import logging
import os
import secrets
import time
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, Mock, patch

import httpx
import jwt
import msal
from chainlit.auth import create_jwt, decode_jwt
from chainlit.user import User
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from fastapi import FastAPI

from gpt_rag_ui.api import auth_recovery as routes
from gpt_rag_ui.api.embed_routes import register_copilot_auth_routes
from gpt_rag_ui.auth import oauth, recovery
from gpt_rag_ui.auth.auth_common import validated_claims_challenge
from gpt_rag_ui.auth.embed_auth import CopilotSessionStore, COPILOT_SESSION_COOKIE
from gpt_rag_ui.auth.entra_token import EntraTokenValidator
from gpt_rag_ui.config.embed_config import EmbedSettings

TENANT = "11111111-2222-4333-8444-555555555555"
ALICE = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
BOB = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
CLIENT = "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa"
UI = "https://ui.example.com"
PORTAL = "https://portal.example.com"
CLAIMS = '{"access_token":{"acrs":{"essential":true,"value":"synthetic-private"}}}'
ENV = {
    "CHAINLIT_AUTH_SECRET": "synthetic-only-recovery-secret-32-bytes",
    "CHAINLIT_URL": UI, "OAUTH_AZURE_AD_CLIENT_ID": CLIENT,
    "OAUTH_AZURE_AD_CLIENT_SECRET": "synthetic-client-secret",
    "OAUTH_AZURE_AD_TENANT_ID": TENANT,
    "OAUTH_AZURE_AD_SCOPES": f"api://{CLIENT}/user_impersonation openid profile offline_access",
}


def metadata(oid=ALICE, binding="synthetic-binding-aaaaaaaaaaaaaaaaaaaaaaaa"):
    return {
        "authorized": True, "auth_source": "oauth", "tenant_id": TENANT, "object_id": oid,
        "principal_id": f"{TENANT}:{oid}", "recovery_binding": binding,
        "access_token": "synthetic-old", "refresh_token": "synthetic-refresh",
    }


def user(oid=ALICE, binding="synthetic-binding-aaaaaaaaaaaaaaaaaaaaaaaa"):
    return User(identifier=f"{TENANT}:{oid}", metadata=metadata(oid, binding))


class Config:
    def get(self, key, default=None, type=str):
        return default


class ProviderHTTP:
    """Implements the MSAL HTTP protocol, never opens a socket."""
    def __init__(self):
        self.nonces = {}
        self.posts = []
        self.code_oid = ALICE
        self.refresh_oid = ALICE
        self.bad_nonce = False
        self.error = None
        self.barrier = None

    def get(self, url, **kwargs):
        return httpx.Response(200, json={
            "authorization_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize",
            "token_endpoint": f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token",
            "issuer": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        })

    def post(self, url, data=None, **kwargs):
        data = dict(data or {})
        self.posts.append(data)
        if self.error:
            return httpx.Response(400, json=self.error)
        is_code = data.get("grant_type") == "authorization_code"
        if is_code and self.barrier:
            self.barrier.wait(timeout=3)
        nonce = self.nonces.get(data.get("code")) if is_code else None
        now = int(time.time())
        claims = {
            "aud": CLIENT, "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
            "sub": "synthetic-subject", "tid": TENANT,
            "oid": self.code_oid if is_code else self.refresh_oid,
            "iat": now, "exp": now + 600,
        }
        if nonce:
            claims["nonce"] = "wrong" if self.bad_nonce else nonce
        # MSAL validates this token received over its trusted token HTTP
        # channel (audience/issuer/nonce). Here that channel is entirely local.
        return httpx.Response(200, json={
            "access_token": "synthetic-new-token", "refresh_token": "synthetic-new-refresh",
            "token_type": "Bearer", "expires_in": 600,
            "id_token": jwt.encode(claims, "synthetic-provider-only-signing-key", algorithm="HS256"),
        })


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, ENV))
        self.stack.enter_context(patch.object(oauth, "get_env_var", side_effect=lambda name, fallback=None, **k: os.getenv(name, fallback)))
        self.stack.enter_context(patch.object(oauth, "config", Config()))
        self.http = ProviderHTTP()
        real_application = msal.ConfidentialClientApplication

        def application(*args, **kwargs):
            return real_application(*args, **kwargs, instance_discovery=False, http_client=self.http)
        self.stack.enter_context(patch.object(msal, "ConfidentialClientApplication", side_effect=application))
        self.app = FastAPI()
        routes.register_auth_recovery_routes(self.app)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=UI)
        self.addAsyncCleanup(self.client.aclose)
        self.login_user()

    def login_user(self, oid=ALICE, binding="synthetic-binding-aaaaaaaaaaaaaaaaaaaaaaaa"):
        self.client.cookies.set("access_token", create_jwt(user(oid, binding)), domain="ui.example.com", path="/")

    def ticket(self, oid=ALICE, binding="synthetic-binding-aaaaaaaaaaaaaaaaaaaaaaaa"):
        return recovery.issue_recovery(metadata(oid, binding), "interaction_required", CLAIMS)

    async def begin(self, ticket=None):
        response = await self.client.get("/api/auth/recovery", params={"ticket": ticket or self.ticket()})
        self.assertEqual(307, response.status_code, response.text)
        params = parse_qs(urlsplit(response.headers["location"]).query)
        return response, params

    async def complete(self, params, code=None):
        code = code or "synthetic-code-" + secrets.token_hex(4)
        self.http.nonces[code] = params["nonce"][0]
        return await self.client.get(routes.CALLBACK_PATH, params={"state": params["state"][0], "code": code})

    async def test_real_msal_authorize_callback_and_refresh_preserve_claims_then_rotate_binding(self):
        ticket = self.ticket()
        response, params = await self.begin(ticket)
        self.assertEqual(CLAIMS, params["claims"][0])
        self.assertEqual([CLIENT], params["client_id"])
        self.assertEqual([UI + routes.CALLBACK_PATH], params["redirect_uri"])
        self.assertEqual({"openid", "profile", "offline_access", f"api://{CLIENT}/user_impersonation"},
                         set(params["scope"][0].split()))
        self.assertEqual("login.microsoftonline.com", urlsplit(response.headers["location"]).hostname)
        self.assertTrue(all("HttpOnly" in c and "Secure" in c for c in response.headers.get_list("set-cookie")
                            if "Max-Age=180" in c))
        self.assertNotIn("synthetic-private", repr(self.client.cookies))
        finished = await self.complete(params)
        self.assertEqual(303, finished.status_code, finished.text)
        self.assertEqual(UI + "/login/callback", finished.headers["location"])
        self.assertEqual(["authorization_code", "refresh_token"], [p["grant_type"] for p in self.http.posts])
        self.assertTrue(all(p["claims"] == CLAIMS for p in self.http.posts))
        verifier = self.http.posts[0]["code_verifier"]
        self.assertEqual(params["code_challenge"][0],
                         base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())
        recovered = decode_jwt(self.client.cookies.get("access_token"))
        self.assertEqual(f"{TENANT}:{ALICE}", recovered.identifier)
        self.assertEqual("synthetic-new-token", recovered.metadata["access_token"])
        self.assertNotIn(CLAIMS, repr(recovered.metadata))
        self.assertNotEqual(metadata()["recovery_binding"], recovered.metadata["recovery_binding"])
        self.assertEqual(401, (await self.client.get("/api/auth/recovery", params={"ticket": ticket})).status_code)
        self.assertEqual(401, (await self.complete(params)).status_code)
        self.assertIsNone(recovery.recovery_context.get())

    async def test_ordinary_login_uses_same_callback_without_graph_and_creates_binding(self):
        self.client.cookies.clear()
        response = await self.client.get("/auth/oauth/azure-ad", params={"resource": "https://evil.example"})
        self.assertEqual(307, response.status_code)
        params = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertNotIn("resource", params)
        self.assertNotIn("claims", params)
        finished = await self.complete(params)
        self.assertEqual(303, finished.status_code)
        self.assertGreaterEqual(len(decode_jwt(self.client.cookies.get("access_token")).metadata["recovery_binding"]), 32)

    async def test_wrong_user_session_missing_binding_logout_and_tamper_are_rejected(self):
        ticket = self.ticket()
        for oid, binding in ((BOB, metadata()["recovery_binding"]), (ALICE, "synthetic-other-session-bbbbbbbbbbbbbbbb"), (ALICE, "")):
            self.login_user(oid, binding)
            self.assertEqual(401, (await self.client.get("/api/auth/recovery", params={"ticket": ticket})).status_code)
        self.client.cookies.clear()
        self.assertEqual(401, (await self.client.get("/api/auth/recovery", params={"ticket": ticket})).status_code)
        self.login_user()
        self.assertEqual(401, (await self.client.get("/api/auth/recovery", params={"ticket": ticket[:-2] + "xx"})).status_code)
        self.assertEqual([], self.http.posts)

    async def test_expired_ticket_and_expired_oauth_flow_fail_closed(self):
        ticket = self.ticket()
        with patch.object(recovery.time, "time", return_value=time.time() + 181):
            self.assertEqual(401, (await self.client.get("/api/auth/recovery", params={"ticket": ticket})).status_code)
        _, params = await self.begin()
        with patch.object(recovery.time, "time", return_value=time.time() + 181):
            self.assertEqual(401, (await self.complete(params)).status_code)
        self.assertEqual([], self.http.posts)

    async def test_callback_after_logout_or_account_switch_does_not_exchange_code(self):
        for switch in ("logout", "account"):
            self.login_user()
            _, params = await self.begin()
            self.client.cookies.clear()
            if switch == "account":
                self.login_user(BOB)
            self.assertEqual(401, (await self.complete(params)).status_code)
        self.assertEqual([], self.http.posts)

    async def test_bad_duplicate_state_and_duplicate_code_consume_browser_context(self):
        for query in (
            {"state": "forged", "code": "code"},
            [("state", "a"), ("state", "a"), ("code", "code")],
            [("state", "a"), ("code", "a"), ("code", "b")],
            {"error": "synthetic-private-provider-token"},
        ):
            _, params = await self.begin()
            result = await self.client.get(routes.CALLBACK_PATH, params=query)
            self.assertEqual(401, result.status_code)
            self.assertNotIn("synthetic-private", result.text)
            self.assertEqual(401, (await self.complete(params)).status_code)
        self.assertEqual([], self.http.posts)

    async def test_nonce_and_authenticated_callback_identity_mismatch_are_safe(self):
        for code_oid, refresh_oid, bad_nonce in ((BOB, ALICE, False), (ALICE, BOB, False), (ALICE, ALICE, True)):
            _, params = await self.begin()
            self.http.code_oid, self.http.refresh_oid, self.http.bad_nonce = code_oid, refresh_oid, bad_nonce
            result = await self.complete(params)
            self.assertEqual(401, result.status_code)
            self.assertNotIn("synthetic-private", result.text)
            self.assertIsNone(recovery.recovery_context.get())

    async def test_two_parallel_contexts_are_isolated_and_last_browser_state_wins(self):
        # Independent browsers are independent; same-browser concurrent login
        # intentionally invalidates the first state rather than mixing claims.
        _, first = await self.begin()
        _, second = await self.begin()
        self.assertNotEqual(first["state"], second["state"])
        self.assertEqual(401, (await self.complete(first)).status_code)
        # Failure consumed context; start a fresh explicit attempt.
        _, third = await self.begin()
        self.assertEqual(303, (await self.complete(third)).status_code)

    async def test_duplicate_auth_cookie_and_foreign_origin_are_rejected(self):
        token = create_jwt(user())
        response = await self.client.get("/api/auth/recovery", params={"ticket": self.ticket()},
                                         headers={"Cookie": f"access_token={token}; access_token={token}"})
        self.assertEqual(401, response.status_code)
        response = await self.client.get("/api/auth/recovery", params={"ticket": self.ticket()},
                                         headers={"Origin": PORTAL})
        self.assertEqual(401, response.status_code)

    async def test_concurrent_browsers_keep_callback_refresh_claims_request_local(self):
        second_claims = CLAIMS.replace("synthetic-private", "synthetic-second")
        binding = "synthetic-other-session-bbbbbbbbbbbbbbbb"
        ticket = recovery.issue_recovery(metadata(binding=binding), "interaction_required", second_claims)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=UI) as second:
            second.cookies.set("access_token", create_jwt(user(binding=binding)), domain="ui.example.com", path="/")
            _, params = await self.begin()
            started = await second.get("/api/auth/recovery", params={"ticket": ticket})
            self.assertEqual(307, started.status_code)
            other_params = parse_qs(urlsplit(started.headers["location"]).query)
            self.http.nonces["second"] = other_params["nonce"][0]
            self.http.barrier = threading.Barrier(2)
            results = await asyncio.gather(
                self.complete(params, code="first"),
                second.get(routes.CALLBACK_PATH, params={"state": other_params["state"][0], "code": "second"}),
            )
        self.assertEqual([303, 303], [r.status_code for r in results])
        self.assertEqual(sorted([CLAIMS, second_claims]), sorted(
            p["claims"] for p in self.http.posts if p["grant_type"] == "refresh_token"))
        self.assertIsNone(recovery.recovery_context.get())

    async def test_source_denial_invalid_claims_and_anonymous_never_offer_interaction(self):
        for code, claims, md in (
            ("source_denied", CLAIMS, metadata()),
            ("interaction_required", '{"id_token":{"acrs":{}}}', metadata()),
            ("interaction_required", None, metadata()),
            ("authentication_required", None, {**metadata(), "auth_source": "anonymous"}),
        ):
            with self.assertRaises(ValueError):
                recovery.issue_recovery(md, code, claims)
        for claims in (
            "not-json", "{}",
            '{"access_token":{},"access_token":{"a":{}}}',
            '{"access_token":{"a":{},"a":{}}}',
            '{"access_token":{"a":{"value":"' + "é" * 4096 + '"}}}',
            '{"access_token":{},"userinfo":{}}',
            '{"access_token":{"a":{"value":NaN}}}', "\ud800",
            '{"access_token":{"a":{"value":"\\ud800"}}}',
        ):
            self.assertIsNone(validated_claims_challenge(claims))

    async def test_sdk_error_body_not_reflected_and_claims_logs_are_redacted(self):
        self.http.error = {"error": "invalid_grant", "error_description": "synthetic-private-provider-token"}
        _, params = await self.begin()
        result = await self.complete(params)
        self.assertEqual(401, result.status_code)
        self.assertNotIn("synthetic-private", result.text + repr(result.headers))
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1,
                                   "GET /auth/oauth/azure-ad/callback?claims=%s", (CLAIMS,), None)
        self.assertTrue(recovery.RecoveryLogFilter().filter(record))
        self.assertNotIn("synthetic-private", record.getMessage())
        record.name = "msal.oauth2cli.oauth2"
        self.assertFalse(recovery.RecoveryLogFilter().filter(record))

    async def test_offered_element_contains_only_encrypted_context_not_claims_or_credentials(self):
        message = Mock()
        message.return_value.send = AsyncMock()
        from types import SimpleNamespace
        with (patch.object(recovery.cl, "Message", message),
              patch.object(recovery.cl, "CustomElement", side_effect=lambda **kw: SimpleNamespace(**kw))):
            self.assertTrue(await recovery.offer_auth_recovery("interaction_required", CLAIMS, user=user()))
        kwargs = message.call_args.kwargs
        props = kwargs["elements"][0].props
        self.assertNotIn("synthetic-private", repr(kwargs))
        self.assertNotIn("synthetic-refresh", repr(kwargs))
        self.assertEqual(CLAIMS, recovery.verify_recovery(props["ticket"], metadata())["claims"])
        self.assertIn("/api/auth/recovery?ticket=", props["href"])

    async def test_encrypted_context_works_with_another_replica_and_secret_rotation_invalidates_it(self):
        ticket = self.ticket()
        # No process-local ticket registry is involved.
        app = FastAPI()
        routes.register_auth_recovery_routes(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=UI) as client:
            client.cookies.set("access_token", create_jwt(user()))
            self.assertEqual(307, (await client.get("/api/auth/recovery", params={"ticket": ticket})).status_code)
        with patch.dict(os.environ, {"CHAINLIT_AUTH_SECRET": "synthetic-rotated-recovery-secret-32"}):
            with self.assertRaises(ValueError):
                recovery.verify_recovery(ticket, metadata())

    async def test_partial_hosted_output_not_replayed_and_claims_reach_recovery_in_both_paths(self):
        from test_boundary_failures import BoundaryFailureTests
        from gpt_rag_ui.clients.hosted_agent_client import HostedAgentResponseError
        from gpt_rag_ui.services.hosted_continuity import ContinuityPersistenceError, ConversationNotFoundError
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        chat = BoundaryFailureTests().chat()
        for continuity in (False, True):
            calls = []

            async def fail(*args, **kwargs):
                calls.append(True)
                yield "already emitted", {}
                raise HostedAgentResponseError("synthetic-private-body", code="interaction_required", claims_challenge=CLAIMS)

            response = AsyncMock()
            response.content = ""
            app_user = user()
            with ExitStack() as stack:
                for name, value in {
                    "cl": SimpleNamespace(
                        Message=Mock(return_value=response),
                        user_session=SimpleNamespace(get=lambda key: app_user if key == "user" else None, set=Mock()),
                        chat_context=SimpleNamespace(to_openai=lambda: []),
                    ),
                    "tracer": MagicMock(), "CHAT_BACKEND": "hosted_agent", "HOSTED_CONTINUITY_ENABLED": continuity,
                    "SHOW_STATISTICS": False, "ENABLE_FEEDBACK": False,
                    "get_auth_info": AsyncMock(return_value={"authorized": True, **metadata()}),
                    "build_invocation_messages": Mock(return_value=[]), "call_hosted_agent_stream": fail,
                    "get_hosted_continuity_coordinator": Mock(return_value=SimpleNamespace(run_turn=fail)),
                    "ContinuityPersistenceError": ContinuityPersistenceError,
                    "ConversationNotFoundError": ConversationNotFoundError,
                    "offer_auth_recovery": AsyncMock(return_value=True),
                }.items():
                    stack.enter_context(patch.object(chat, name, value, create=True))
                await chat.handle_message(SimpleNamespace(id="synthetic", content="question", elements=[]))
                chat.offer_auth_recovery.assert_awaited_once_with("interaction_required", CLAIMS, user=app_user)
            self.assertEqual([True], calls)
            self.assertTrue(any("already emitted" in str(call) for call in response.stream_token.call_args_list))
            self.assertNotIn("synthetic-private", response.content)

    async def test_response_failed_nested_error_preserves_challenge_without_retry(self):
        from gpt_rag_ui.clients.hosted_agent_client import (
            HostedAgentClient, HostedAgentSettings, HostedAgentResponseError, InvocationMessage,
        )
        from azure.core.credentials import AccessToken
        frames = [
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.failed", "response": {"error": {
                "code": "interaction_required", "message": "synthetic-private-body", "claims_challenge": CLAIMS,
            }}},
        ]
        requests_seen = []

        async def handler(request):
            requests_seen.append(request)
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  text="".join("data: " + json.dumps(frame) + "\n\n" for frame in frames))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = HostedAgentClient(HostedAgentSettings(UI, "https://ai.azure.com/.default"), http_client=http)
            with (patch.object(client, "_acquire_data_plane_token", AsyncMock(return_value=AccessToken("synthetic", int(time.time()) + 600))),
                  patch("gpt_rag_ui.clients.hosted_agent_client.acquire_obo_token", AsyncMock(return_value="synthetic")),
                  patch.object(client, "_work_iq_assertion", AsyncMock(return_value=None))):
                stream = client.stream([InvocationMessage(role="user", content="question")], user_access_token="synthetic")
                self.assertEqual("partial", (await anext(stream))[0])
                with self.assertRaises(HostedAgentResponseError) as raised:
                    await anext(stream)
                self.assertEqual(CLAIMS, raised.exception.claims_challenge)
                self.assertNotIn("synthetic-private", str(raised.exception))
        self.assertEqual(1, len(requests_seen))


class EmbeddedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, ENV)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="synthetic", use="sig", alg="RS256")
        self.validator = EntraTokenValidator(
            tenant_id=TENANT, audience=CLIENT, clock_skew_seconds=0,
            jwks_loader=AsyncMock(return_value={"keys": [jwk]}),
        )
        self.sessions = CopilotSessionStore(max_sessions=10, ttl_seconds=600)
        self.session = await self.sessions.replace(
            previous_session_id=None, access_token="synthetic-assertion",
            claims={"tid": TENANT, "oid": ALICE, "exp": int(time.time()) + 600},
            display_name="Synthetic", principal_name="",
        )
        self.settings = EmbedSettings(enabled=True, auth_mode="entra", ui_origin=UI, allowed_origins=(PORTAL,))
        self.app = FastAPI()
        register_copilot_auth_routes(self.app, settings=self.settings, sessions=self.sessions,
                                    validator=self.validator, config=Config())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=UI,
                                       headers={"Origin": PORTAL})
        self.addAsyncCleanup(self.client.aclose)
        self.client.cookies.set(COPILOT_SESSION_COOKIE, self.session.session_id)
        self.ticket = recovery.issue_recovery(self.session.user_metadata(), "interaction_required", CLAIMS)
        self.addAsyncCleanup(self.clear_sessions)

    async def clear_sessions(self):
        for session_id in list(self.sessions._sessions):
            await self.sessions.delete(session_id)

    def token(self, oid=ALICE, **overrides):
        now = int(time.time())
        claims = {
            "tid": TENANT, "oid": oid, "sub": "synthetic",
            "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT,
            "iat": now, "nbf": now - 1, "exp": now + 600,
            "scp": "user_impersonation", "ver": "2.0",
        }
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "synthetic"})

    async def post(self, operation="challenge", token=None, **kwargs):
        return await self.client.post(
            "/copilot/auth/recovery", json={"ticket": self.ticket, "operation": operation},
            headers={"Authorization": "Bearer " + token} if token else None, **kwargs,
        )

    async def test_actual_signed_bootstrap_token_recovery_rotates_and_invalidates_old_ticket(self):
        challenge = await self.post()
        self.assertEqual(200, challenge.status_code)
        self.assertEqual({"claims": CLAIMS}, challenge.json())
        result = await self.post("complete", self.token())
        self.assertEqual(200, result.status_code, result.text)
        self.assertIsNone(await self.sessions.get(self.session.session_id))
        self.assertEqual(401, (await self.post()).status_code)

    async def test_cross_user_wrong_scope_expiry_and_logout_never_replace_session(self):
        for token in (self.token(BOB), self.token(scp="other"), self.token(exp=int(time.time()) - 100)):
            self.assertEqual(401, (await self.post("complete", token)).status_code)
            self.assertIsNotNone(await self.sessions.get(self.session.session_id))
        await self.sessions.delete(self.session.session_id)
        self.assertEqual(401, (await self.post()).status_code)

    async def test_missing_duplicate_foreign_origin_and_missing_cookie_are_denied(self):
        for headers in ({}, {"Origin": "https://evil.example"}, [("Origin", PORTAL), ("Origin", PORTAL)]):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=UI,
                                         headers=headers) as client:
                client.cookies.set(COPILOT_SESSION_COOKIE, self.session.session_id)
                result = await client.post("/copilot/auth/recovery", json={"ticket": self.ticket, "operation": "challenge"})
                self.assertEqual(403, result.status_code)
        self.client.cookies.clear()
        self.assertEqual(401, (await self.post()).status_code)

    async def test_parallel_completions_only_replace_the_current_session_once(self):
        results = await asyncio.gather(self.post("complete", self.token()), self.post("complete", self.token()))
        self.assertEqual([200, 401], sorted(r.status_code for r in results))

    async def test_only_bound_entra_recovery_notification_crosses_existing_bridge_guard(self):
        from types import SimpleNamespace
        from chainlit.session import WebsocketSession
        from gpt_rag_ui.auth import embed_security
        sio = SimpleNamespace(emit=AsyncMock(), call=AsyncMock(return_value={"status": "ready"}), handlers={})
        original_call, original_emit = sio.call, sio.emit
        socket = SimpleNamespace(client_type="copilot", user=User(
            identifier=self.session.principal_id, metadata=self.session.user_metadata(),
        ))
        with ExitStack() as stack:
            for name in ("_copilot_sio", "_copilot_disconnect_socket", "_copilot_socket_registry"):
                stack.enter_context(patch.object(embed_security, name, None))
            stack.enter_context(patch.object(WebsocketSession, "get", side_effect=lambda sid: socket if sid == "socket-a" else None))
            stack.enter_context(patch.object(embed_security, "_has_copilot_sockets", return_value=True))
            embed_security.configure_copilot_bridge_guards(sio, sessions=self.sessions, portal_origins=(PORTAL,))
            payload = {"name": "protectedAuthRecovery", "args": {"ticket": self.ticket}}
            self.assertEqual({"status": "ready"}, await sio.call("call_fn", payload, to="socket-a"))
            await sio.emit("call_fn", payload, to="socket-a")
            original_call.assert_awaited_once()
            original_emit.assert_awaited_once()
            for event, data, target in (
                ("call_fn", {"name": "other", "args": {"ticket": self.ticket}}, "socket-a"),
                ("call_fn", {"name": "protectedAuthRecovery", "args": {"ticket": "forged"}}, "socket-a"),
                ("call_fn", payload, None),
                ("call_fn", payload, "room"),
                ("window_message", payload, "socket-a"),
                ("call_fn", {"name": "protectedAuthRecovery", "args": {"ticket": self.ticket, "claims": CLAIMS}}, "socket-a"),
            ):
                with self.assertRaises(PermissionError):
                    await sio.call(event, data, to=target)
            await self.sessions.delete(self.session.session_id)
            with self.assertRaises(PermissionError):
                await sio.call("call_fn", payload, to="socket-a")
            self.assertEqual(1, original_call.await_count)

    async def test_anonymous_mode_cannot_release_claims_or_switch_to_oauth(self):
        app = FastAPI()
        register_copilot_auth_routes(
            app, settings=EmbedSettings(enabled=True, auth_mode="anonymous", ui_origin=UI, allowed_origins=(PORTAL,)),
            sessions=self.sessions, validator=None, config=Config(),
        )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=UI,
                                     headers={"Origin": PORTAL}) as client:
            client.cookies.set(COPILOT_SESSION_COOKIE, self.session.session_id)
            result = await client.post("/copilot/auth/recovery", json={"ticket": self.ticket, "operation": "challenge"})
            self.assertEqual(401, result.status_code)
        self.assertEqual(404, (await self.client.get("/auth/oauth/azure-ad")).status_code)
