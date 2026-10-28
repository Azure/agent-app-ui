import os
import unittest
from unittest.mock import patch

from gpt_rag_ui.clients import ingestion_client
from gpt_rag_ui.clients.ingestion_client import _build_ingest_documents_payload


class IngestionSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingestion_acl_uses_validated_bare_object_id(self):
        object_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with (
            patch.dict(os.environ, {"CHAT_BACKEND": "hosted_agent"}),
            patch(
                "gpt_rag_ui.clients.ingestion_client._read_file_bytes",
                return_value=b"hello",
            ),
        ):
            payload = await _build_ingest_documents_payload(
                "thread",
                "question",
                [{"path": "upload.txt", "name": "upload.txt"}],
                {
                    "principal_id": (
                        "11111111-2222-3333-4444-555555555555:"
                        f"{object_id}"
                    ),
                    "object_id": object_id,
                    "client_principal_id": (
                        "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
                    ),
                },
            )

        self.assertEqual([object_id], payload["securityUserIds"])
        self.assertNotIn(":", payload["securityUserIds"][0])

    async def test_ingestion_omits_malformed_object_id(self):
        with (
            patch.dict(os.environ, {"CHAT_BACKEND": "orchestrator"}),
            patch(
                "gpt_rag_ui.clients.ingestion_client._read_file_bytes",
                return_value=b"hello",
            ),
        ):
            payload = await _build_ingest_documents_payload(
                "thread",
                "question",
                [{"path": "upload.txt", "name": "upload.txt"}],
                {
                    "object_id": "tenant:object",
                    "client_principal_id": (
                        "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                    ),
                },
            )

        self.assertNotIn("securityUserIds", payload)

    async def test_protected_upload_rejects_missing_or_invalid_oid_before_io(self):
        for backend in ("", "hosted_agent"):
            for oid in (None, "", "anonymous", "no-auth", "tenant:object", 123,
                        "00000000-0000-0000-0000-000000000000"):
                with (
                    self.subTest(backend=backend, oid=oid),
                    patch.dict(os.environ, {"CHAT_BACKEND": backend}),
                    patch.object(ingestion_client, "load_ingestion_uploads_enabled", return_value=True),
                    patch.object(ingestion_client, "_read_file_bytes") as read,
                    patch.object(ingestion_client.httpx, "AsyncClient") as http,
                ):
                    with self.assertRaisesRegex(RuntimeError, "valid object ID"):
                        await ingestion_client.ingest_files_session(
                            "conversation", "question",
                            {
                                "authorized": True, "object_id": oid,
                                "client_principal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                                "access_token": "synthetic-api-assertion",
                            },
                            [{"path": "upload.pdf", "name": "upload.pdf"}],
                        )
                    read.assert_not_called()
                    http.assert_not_called()

    async def test_authenticated_classic_upload_cannot_omit_acl(self):
        with patch.dict(os.environ, {"CHAT_BACKEND": "orchestrator"}):
            with self.assertRaisesRegex(RuntimeError, "valid object ID"):
                await _build_ingest_documents_payload(
                    "conversation", "question", [],
                    {"authorized": True, "access_token": "synthetic-api-assertion"},
                )


if __name__ == "__main__":
    unittest.main()
