"""Native downloads authorize the current user, not a citation or chat label."""

import asyncio
import base64
from contextlib import ExitStack
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import FastAPI

from gpt_rag_ui.api.download_routes import register_secure_download_route
from gpt_rag_ui.auth.auth_common import DelegationError
from gpt_rag_ui.clients import blob
from gpt_rag_ui.services import citations, download_security as security

TENANT = "11111111-1111-4111-8111-111111111111"
USER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
LABEL = "opaque-hosted-label"
PATH = f"conversations/{LABEL}/private.pdf"
URL = f"https://synthetic.blob.core.windows.net/uploads/{PATH}"
SETTINGS = {
    "STORAGE_ACCOUNT_NAME": "synthetic",
    "DOCUMENTS_STORAGE_CONTAINER": "documents",
    "DOCUMENTS_IMAGES_STORAGE_CONTAINER": "images",
    "CONVERSATION_DOCUMENTS_STORAGE_CONTAINER": "uploads",
    "SEARCH_SERVICE_QUERY_ENDPOINT": "https://synthetic.search.windows.net",
    "SEARCH_RAG_INDEX_NAME": "ragindex",
    "SEARCH_API_VERSION": "2025-11-01-preview",
}


class Config:
    def get(self, key, default=None, type=str):
        return SETTINGS.get(key, default)


class FileStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"synthetic-file"


def metadata(oid=USER_A):
    return {
        "authorized": True, "auth_source": "oauth", "object_id": oid,
        "tenant_id": TENANT, "principal_id": f"{TENANT}:{oid}",
        "client_principal_id": oid, "access_token": oid + "-assertion",
        "recovery_binding": "synthetic-native-session-aaaaaaaaaaaaaaaaaaaaaa",
    }


def row(oid=USER_A, url=URL):
    return {
        "url": url, "conversationId": LABEL,
        "metadata_security_user_ids": [oid],
        "metadata_security_group_ids": [], "metadata_security_rbac_scope": "",
    }


class NativeDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {"CHAT_BACKEND": "hosted_agent", **SETTINGS}))
        self.manager = security.DownloadTokenManager(
            secret="synthetic-native-test-secret", public_url="https://ui.example.com", native=True,
        )
        self.calls = []
        self.rows = [row()]
        self.storage_status = 200
        self.search_status = 200
        self.source_headers = {}
        self.service = self.stack.enter_context(patch.object(
            blob, "acquire_service_identity_token", AsyncMock(return_value="synthetic-workload"),
        ))

        async def obo(assertion, scope):
            return assertion + ("-search" if "search.azure.com" in scope else "-storage")
        self.obo = self.stack.enter_context(patch.object(
            blob, "acquire_obo_token", AsyncMock(side_effect=obo),
        ))

        async def handler(request):
            self.calls.append(request)
            if request.method == "POST":
                # The remote native permission evaluator is mocked at HTTP.
                allowed = USER_A in request.headers["x-ms-query-source-authorization"]
                return httpx.Response(self.search_status, headers=self.source_headers, json={"value": self.rows if allowed else []})
            status = self.storage_status
            if request.url.path.startswith("/documents/") and USER_A not in request.headers["authorization"]:
                status = 403
            return httpx.Response(status, headers={"content-length": "14", **self.source_headers}, stream=FileStream())
        client = blob.NativeDownloadClient(Config(), transport=httpx.MockTransport(handler))
        self.legacy = Mock(side_effect=AssertionError("legacy reader used"))
        self.owner = AsyncMock(side_effect=AssertionError("classic owner lookup used"))
        self.app = FastAPI()
        register_secure_download_route(
            self.app, manager=self.manager, native_client=client,
            download_blob=self.legacy, allowed_containers={"documents", "images", "uploads"},
            conversation_container="uploads", shared_containers={"documents", "images"},
            sessions=None, conversation_resolver=self.owner,
        )
        self.stack.enter_context(patch.object(
            security, "get_token_from_cookies", side_effect=lambda cookies: cookies.get("auth"),
        ))

        async def authenticate(cookie):
            oid = USER_A if cookie == "a" else USER_B
            return SimpleNamespace(identifier=f"{TENANT}:{oid}", metadata=metadata(oid))
        self.stack.enter_context(patch.object(
            security, "authenticate_user", AsyncMock(side_effect=authenticate),
        ))
        self.refresh = self.stack.enter_context(patch(
            "gpt_rag_ui.auth.oauth.ensure_fresh_user_access_token",
            AsyncMock(return_value=False),
        ))
        self.http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://ui.example.com",
        )
        self.addAsyncCleanup(self.http.aclose)

    def grant(self, *, oid=USER_A, container="documents", path="private.pdf", session="oauth", label=LABEL):
        return self.manager.issue(
            principal_id=f"{TENANT}:{oid}", session_id=session,
            conversation_id=label, container=container, blob_name=path,
        )

    async def fetch(self, url, user="a"):
        headers = {"Cookie": f"auth={user}"} if user else {}
        return await self.http.get(url, headers=headers)

    async def test_shared_blob_uses_current_user_storage_obo_without_workload_or_sas(self):
        with patch.object(citations, "generate_blob_sas_url") as sas:
            response = await self.fetch(self.grant())
        self.assertEqual(200, response.status_code)
        self.assertEqual(b"synthetic-file", response.content)
        self.obo.assert_awaited_once_with(USER_A + "-assertion", "https://storage.azure.com/.default")
        self.service.assert_not_awaited()
        self.legacy.assert_not_called()
        self.owner.assert_not_awaited()
        sas.assert_not_called()
        self.assertEqual("private, no-store", response.headers["cache-control"])

    async def test_missing_auth_other_user_tampered_expired_and_unknown_container(self):
        grant = self.grant()
        self.assertEqual(401, (await self.fetch(grant, user=None)).status_code)
        self.assertEqual(404, (await self.fetch(grant, user="b")).status_code)
        self.assertEqual(404, (await self.fetch(grant + "tampered")).status_code)
        with patch("itsdangerous.timed.TimestampSigner.get_timestamp", return_value=int(time.time()) - 4000):
            expired = self.grant()
        self.assertEqual(404, (await self.fetch(expired)).status_code)
        self.assertEqual(404, (await self.fetch(self.grant(container="other"))).status_code)
        self.assertEqual([], self.calls)
        self.obo.assert_not_awaited()
        self.assertEqual(404, (await self.fetch("/api/download/documents/private.pdf")).status_code)

    async def test_an_own_signed_selector_does_not_authorize_another_users_blob(self):
        response = await self.fetch(self.grant(oid=USER_B), user="b")
        self.assertEqual(403, response.status_code)
        self.service.assert_not_awaited()

    async def test_storage_denied_redirect_or_revoked_never_falls_back(self):
        for status in (301, 302, 401, 403, 404):
            self.storage_status = status
            response = await self.fetch(self.grant())
            self.assertIn(response.status_code, (401, 403))
        self.assertEqual(5, len(self.calls))
        self.service.assert_not_awaited()

    async def test_upload_exact_document_native_gate_precedes_workload_blob(self):
        response = await self.fetch(self.grant(container="uploads", path=PATH))
        self.assertEqual(200, response.status_code)
        self.assertEqual(["POST", "GET"], [r.method for r in self.calls])
        request = self.calls[0]
        self.assertEqual(USER_A + "-assertion-search", request.headers["x-ms-query-source-authorization"])
        self.assertNotIn("x-ms-user-identity", request.headers)
        self.assertIn(LABEL, request.content.decode())
        self.assertIn("not metadata_security_group_ids/any()", request.content.decode())
        self.assertEqual("https://storage.azure.com/.default", self.service.await_args_list[-1].args[0])
        self.owner.assert_not_awaited()
        self.legacy.assert_not_called()
        # Revocation must be evaluated afresh even with exactly the same grant.
        self.rows = []
        response = await self.fetch(self.grant(container="uploads", path=PATH))
        self.assertEqual(403, response.status_code)
        self.assertEqual(["POST", "GET", "POST"], [r.method for r in self.calls])

    async def test_upload_other_user_wrong_document_scope_and_non_uploader_acl_denied(self):
        response = await self.fetch(self.grant(oid=USER_B, container="uploads", path=PATH), user="b")
        self.assertEqual(403, response.status_code)
        for candidate in (
            row(url=URL + ".other"), {**row(), "conversationId": "other"},
            {**row(), "metadata_security_user_ids": [USER_B]},
            {**row(), "metadata_security_group_ids": ["group"]},
            {**row(), "metadata_security_rbac_scope": "rbac"},
        ):
            self.rows = [candidate]
            response = await self.fetch(self.grant(container="uploads", path=PATH))
            self.assertEqual(403, response.status_code)
        self.assertTrue(all(r.method == "POST" for r in self.calls))
        self.assertTrue(all(c.args[0] == "https://search.azure.com/.default" for c in self.service.await_args_list))

    async def test_embedded_grant_stays_session_bound_without_classic_owner_lookup(self):
        from gpt_rag_ui.api import download_routes
        principal = security.DownloadPrincipal(f"{TENANT}:{USER_A}", "session-b", metadata())
        with patch.object(download_routes, "resolve_download_principal", AsyncMock(return_value=principal)):
            self.assertEqual(404, (await self.fetch(self.grant(session="session-a"))).status_code)
            self.assertEqual(200, (await self.fetch(self.grant(session="session-b"))).status_code)
        payload = self.manager.serializer.loads(self.grant(session="session-b").rsplit("/", 1)[-1])
        self.assertNotEqual("session-b", payload["s"])
        self.assertNotEqual(f"{TENANT}:{USER_A}", payload["p"])
        self.owner.assert_not_awaited()

    async def test_fresh_assertion_and_consent_failure_are_not_replayed(self):
        async def refresh(user, **kwargs):
            user.metadata["access_token"] = "fresh-api-assertion"
            return True
        self.refresh.side_effect = refresh
        await self.fetch(self.grant())
        self.assertEqual("fresh-api-assertion", self.obo.await_args.args[0])
        self.refresh.side_effect = DelegationError("consent_required")
        response = await self.fetch(self.grant())
        self.assertEqual(401, response.status_code)
        self.assertEqual(1, len(self.calls))

    def test_native_citations_never_sign_and_restored_links_use_same_safe_route(self):
        with (
            patch.object(citations, "native_downloads_enabled", return_value=True),
            patch.object(citations, "get_download_tokens", return_value=self.manager),
            patch.object(citations, "STORAGE_ACCOUNT_NAME", "synthetic"),
            patch.object(citations, "DOCUMENTS_CONTAINER", "documents"),
            patch.object(citations, "CONVERSATION_DOCUMENTS_CONTAINER", "uploads"),
            patch.object(citations, "generate_blob_sas_url") as sas,
        ):
            args = dict(conversation_id=LABEL, principal_id=f"{TENANT}:{USER_A}", copilot_session_id="")
            for href in (
                "documents/private.pdf",
                "https://synthetic.blob.core.windows.net/documents/private.pdf",
                "https://synthetic.dfs.core.windows.net/documents/private.pdf",
            ):
                url = citations.resolve_reference_href(href, **args)
                self.assertTrue(url.startswith("https://ui.example.com/api/download/"))
                restored = citations.resolve_reference_href(url, **args)
                self.assertTrue(restored.startswith("https://ui.example.com/api/download/"))
            for href in (
                "https://other.blob.core.windows.net/documents/private.pdf",
                "https://synthetic.blob.core.windows.net:443/documents/private.pdf",
                "https://user@synthetic.blob.core.windows.net/documents/private.pdf",
                "https://synthetic.blob.core.windows.net/documents/private.pdf?sig=synthetic",
                "documents/../private.pdf", "other/private.pdf", "/api/download/documents/private.pdf",
            ):
                self.assertIsNone(citations.resolve_reference_href(href, **args), href)
            sas.assert_not_called()

    async def test_bad_path_host_and_search_configuration_fail_before_blob(self):
        client = blob.NativeDownloadClient(Config())
        for path in ("../private.pdf", "/private.pdf", "folder/%2e/file", "folder\\file"):
            with self.assertRaises(blob.NativeDownloadError):
                await client.open(container="documents", blob_name=path, conversation_id=LABEL,
                                  assertion="synthetic", object_id=USER_A)
        for key, value in (("STORAGE_ACCOUNT_NAME", "evil.example/"), ("SEARCH_SERVICE_QUERY_ENDPOINT", "http://evil.example"),
                           ("SEARCH_API_VERSION", "2024-07-01")):
            with patch.dict(os.environ, {key: value}):
                response = await self.fetch(self.grant(container="uploads", path=PATH))
                self.assertIn(response.status_code, (403, 503))
        self.assertEqual([], self.calls)

    async def test_upload_prefix_cannot_be_relabelled_as_shared_storage_access(self):
        response = await self.fetch(self.grant(container="documents", path=PATH))
        self.assertEqual(403, response.status_code)
        self.assertEqual([], self.calls)
        self.service.assert_not_awaited()
        self.obo.assert_not_awaited()

    async def test_storage_size_limit_and_search_denial_exclude_provider_text(self):
        async def handler(request):
            return httpx.Response(200, headers={"content-length": str(101 * 1024 * 1024)},
                                  stream=FileStream())
        client = blob.NativeDownloadClient(Config(), transport=httpx.MockTransport(handler))
        with self.assertRaises(blob.NativeDownloadError) as raised:
            await client.open(container="documents", blob_name="private.pdf", conversation_id=LABEL,
                              assertion="synthetic", object_id=USER_A)
        self.assertEqual(503, raised.exception.status_code)

        async def denied(request):
            return httpx.Response(403, text="synthetic-private-provider-body")
        client = blob.NativeDownloadClient(Config(), transport=httpx.MockTransport(denied))
        with self.assertRaises(blob.NativeDownloadError) as raised:
            await client.open(container="uploads", blob_name=PATH, conversation_id=LABEL,
                              assertion="synthetic", object_id=USER_A)
        self.assertNotIn("synthetic-private", str(raised.exception))
        self.assertTrue(all(c.args[0] != "https://storage.azure.com/.default"
                            for c in self.service.await_args_list))

    async def test_failure_after_output_closes_stream_without_retry_or_provider_text(self):
        requests = []
        closed = []

        class FailingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"x" * 65536
                raise httpx.ReadError("synthetic-private-provider-token")

            async def aclose(self):
                closed.append(True)

        async def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={"content-length": "70000"}, stream=FailingStream())
        client = blob.NativeDownloadClient(Config(), transport=httpx.MockTransport(handler))
        download = await client.open(container="documents", blob_name="private.pdf", conversation_id=LABEL,
                                     assertion="synthetic", object_id=USER_A)
        self.assertEqual(65536, len(await anext(download.chunks)))
        with self.assertRaises(blob.NativeDownloadError) as raised:
            await anext(download.chunks)
        self.assertNotIn("synthetic-private", str(raised.exception))
        self.assertEqual(1, len(requests))
        self.assertEqual([True], closed)
        self.obo.assert_awaited_once()

    async def test_stream_closes_on_cancellation(self):
        closed = AsyncMock()
        from gpt_rag_ui.api.download_routes import _NativeStreamingResponse

        async def chunks():
            yield b"partial"
            raise asyncio.CancelledError
        response = _NativeStreamingResponse(blob.NativeBlobDownload(chunks(), 100, closed))
        async def receive():
            await asyncio.sleep(10)
        with self.assertRaises(asyncio.CancelledError):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, AsyncMock())
        closed.assert_awaited_once()

    async def test_actual_storage_and_search_challenges_offer_same_bound_flow_without_retry(self):
        import re
        from gpt_rag_ui.auth.recovery import verify_recovery
        claims = '{"access_token":{"acrs":{"value":"synthetic-private"}}}'
        header = "Bearer claims=\"" + base64.b64encode(claims.encode()).decode() + "\""
        with patch.dict(os.environ, {
            "CHAINLIT_AUTH_SECRET": "synthetic-native-recovery-secret-32-bytes",
            "CHAINLIT_URL": "https://ui.example.com",
        }):
            for container in ("documents", "uploads"):
                self.calls.clear()
                self.storage_status = 403
                self.search_status = 401 if container == "uploads" else 200
                self.source_headers = {"www-authenticate": header}
                response = await self.fetch(self.grant(container=container, path=PATH if container == "uploads" else "private.pdf"))
                self.assertEqual(401, response.status_code)
                self.assertIn("Continue sign-in", response.text)
                self.assertNotIn("synthetic-private", response.text)
                ticket = re.search(r"ticket=([^\"]+)", response.text).group(1)
                self.assertEqual(claims, verify_recovery(ticket, metadata())["claims"])
                with self.assertRaises(ValueError):
                    verify_recovery(ticket, metadata(USER_B))
                self.assertEqual(1, len(self.calls))
            self.source_headers = {}
            response = await self.fetch(self.grant())
            self.assertEqual(403, response.status_code)
            self.assertNotIn("Continue sign-in", response.text)
            self.assertTrue(all(call.args[0] != "https://storage.azure.com/.default"
                                for call in self.service.await_args_list))

    async def test_refresh_challenge_recovery_still_requires_matching_download_grant(self):
        claims = '{"access_token":{"acrs":{"value":"synthetic-private"}}}'
        self.refresh.side_effect = DelegationError("interaction_required", claims_challenge=claims)
        with patch.dict(os.environ, {
            "CHAINLIT_AUTH_SECRET": "synthetic-native-recovery-secret-32-bytes",
            "CHAINLIT_URL": "https://ui.example.com",
        }):
            self.assertEqual(401, (await self.fetch(self.grant())).status_code)
            self.assertEqual(404, (await self.fetch(self.grant(), user="b")).status_code)
        self.assertEqual([], self.calls)
        self.obo.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
