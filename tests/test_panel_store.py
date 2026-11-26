import unittest

from gpt_rag_ui.clients.panel_cosmos import PanelStoreError
from gpt_rag_ui.config.panel_config import PanelSettings
from gpt_rag_ui.services.panel_store import (
    PanelValidationError,
    create_feedback,
    delete_feedback_for_conversation,
    delete_owner_index_row,
    get_owner_index_row,
    list_feedback_for_conversation,
    list_owner_index_rows,
    upsert_owner_index_row,
)

OID_A = "11111111-1111-1111-1111-111111111111"
OID_B = "22222222-2222-2222-2222-222222222222"


class FakePanelCosmosClient:
    """In-memory stand-in for PanelCosmosClient; partitions by
    (container, partition_key) exactly like real Cosmos containers."""

    def __init__(self):
        self.settings = PanelSettings(
            deploy_administrative_panel=True,
            history_enabled=True,
            owner_index_container="owner-index",
            feedback_container="feedback",
        )
        self._data: dict[str, dict[tuple[str, str], dict]] = {
            "owner-index": {},
            "feedback": {},
        }
        self.fail_read = False
        self.fail_upsert = False
        self.fail_delete_for: set[str] = set()
        self.fail_query = False

    async def read_item(self, container_name, item_id, partition_key):
        if self.fail_read:
            raise PanelStoreError("simulated read failure")
        return self._data[container_name].get((item_id, partition_key))

    async def upsert_item(self, container_name, body):
        if self.fail_upsert:
            raise PanelStoreError("simulated upsert failure")
        key = (body["id"], body["principal_id"])
        self._data[container_name][key] = dict(body)
        return dict(body)

    async def delete_item(self, container_name, item_id, partition_key):
        if item_id in self.fail_delete_for:
            raise PanelStoreError("simulated delete failure")
        self._data[container_name].pop((item_id, partition_key), None)

    async def query_items(self, container_name, *, query, parameters, partition_key):
        if self.fail_query:
            raise PanelStoreError("simulated query failure")
        params = {p["name"]: p["value"] for p in parameters}
        rows = [
            dict(value)
            for (item_id, pk), value in self._data[container_name].items()
            if pk == partition_key
        ]
        if "@conversation_id" in params:
            rows = [
                row
                for row in rows
                if row.get("conversation_id") == params["@conversation_id"]
            ]
        rows.sort(key=lambda row: row.get("updated_at") or row.get("created_at") or "")
        if container_name == "owner-index":
            rows.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
            skip = params.get("@skip", 0)
            limit = params.get("@limit", len(rows))
            rows = rows[skip : skip + limit]
        return rows


class OwnerIndexRowTests(unittest.IsolatedAsyncioTestCase):
    async def test_upsert_then_get_round_trips(self):
        client = FakePanelCosmosClient()
        await upsert_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1", title="Hi"
        )
        row = await get_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row.title, "Hi")

    async def test_get_owner_index_row_cross_user_returns_none(self):
        client = FakePanelCosmosClient()
        await upsert_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        row = await get_owner_index_row(
            client=client, principal_id=OID_B, conversation_id="conv-1"
        )
        self.assertIsNone(row)

    async def test_get_owner_index_row_missing_returns_none(self):
        client = FakePanelCosmosClient()
        row = await get_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="does-not-exist"
        )
        self.assertIsNone(row)

    async def test_upsert_preserves_created_at_on_refresh(self):
        client = FakePanelCosmosClient()
        first = await upsert_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        second = await upsert_owner_index_row(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-1",
            title="renamed",
        )
        self.assertEqual(first.created_at, second.created_at)
        self.assertEqual(second.title, "renamed")

    async def test_list_only_returns_the_callers_own_rows(self):
        client = FakePanelCosmosClient()
        await upsert_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-a"
        )
        await upsert_owner_index_row(
            client=client, principal_id=OID_B, conversation_id="conv-b"
        )
        rows, has_more = await list_owner_index_rows(
            client=client, principal_id=OID_A, skip=0, limit=20
        )
        self.assertEqual([r.conversation_id for r in rows], ["conv-a"])
        self.assertFalse(has_more)

    async def test_list_reports_has_more_beyond_page(self):
        client = FakePanelCosmosClient()
        for i in range(3):
            await upsert_owner_index_row(
                client=client, principal_id=OID_A, conversation_id=f"conv-{i}"
            )
        rows, has_more = await list_owner_index_rows(
            client=client, principal_id=OID_A, skip=0, limit=2
        )
        self.assertEqual(len(rows), 2)
        self.assertTrue(has_more)

    async def test_delete_owner_index_row_is_idempotent(self):
        client = FakePanelCosmosClient()
        await upsert_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        await delete_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        # Second delete of an already-absent row must not raise.
        await delete_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        row = await get_owner_index_row(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        self.assertIsNone(row)

    async def test_invalid_conversation_id_raises_validation_error(self):
        client = FakePanelCosmosClient()
        with self.assertRaises(PanelValidationError):
            await get_owner_index_row(
                client=client, principal_id=OID_A, conversation_id=""
            )

    async def test_downstream_read_failure_raises_panel_store_error(self):
        client = FakePanelCosmosClient()
        client.fail_read = True
        with self.assertRaises(PanelStoreError):
            await get_owner_index_row(
                client=client, principal_id=OID_A, conversation_id="conv-1"
            )


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_list_feedback_round_trips(self):
        client = FakePanelCosmosClient()
        await create_feedback(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-1",
            feedback_id="fb-1",
            message_ref="msg-1",
            rating=1,
            category="helpful",
            comment="Great answer",
        )
        records = await list_feedback_for_conversation(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].comment, "Great answer")

    async def test_feedback_is_scoped_to_conversation(self):
        client = FakePanelCosmosClient()
        await create_feedback(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-1",
            feedback_id="fb-1",
            message_ref="msg-1",
            rating=1,
            category=None,
            comment=None,
        )
        await create_feedback(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-2",
            feedback_id="fb-2",
            message_ref="msg-1",
            rating=-1,
            category=None,
            comment=None,
        )
        records = await list_feedback_for_conversation(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        self.assertEqual([r.feedback_id for r in records], ["fb-1"])

    async def test_repeated_create_with_same_feedback_id_is_idempotent(self):
        client = FakePanelCosmosClient()
        for _ in range(3):
            await create_feedback(
                client=client,
                principal_id=OID_A,
                conversation_id="conv-1",
                feedback_id="fb-1",
                message_ref="msg-1",
                rating=1,
                category=None,
                comment=None,
            )
        records = await list_feedback_for_conversation(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        self.assertEqual(len(records), 1)

    async def test_out_of_bounds_rating_rejected(self):
        client = FakePanelCosmosClient()
        with self.assertRaises(PanelValidationError):
            await create_feedback(
                client=client,
                principal_id=OID_A,
                conversation_id="conv-1",
                feedback_id="fb-1",
                message_ref="msg-1",
                rating=100,
                category=None,
                comment=None,
            )

    async def test_comment_is_sanitized_and_bounded(self):
        client = FakePanelCosmosClient()
        raw_comment = "hi\x00\x01" + ("z" * 3000)
        record = await create_feedback(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-1",
            feedback_id="fb-1",
            message_ref="msg-1",
            rating=None,
            category=None,
            comment=raw_comment,
        )
        self.assertNotIn("\x00", record.comment)
        self.assertLessEqual(len(record.comment), 2000)

    async def test_missing_feedback_id_rejected(self):
        client = FakePanelCosmosClient()
        with self.assertRaises(PanelValidationError):
            await create_feedback(
                client=client,
                principal_id=OID_A,
                conversation_id="conv-1",
                feedback_id="",
                message_ref="msg-1",
                rating=None,
                category=None,
                comment=None,
            )

    async def test_delete_feedback_for_conversation_removes_all_rows(self):
        client = FakePanelCosmosClient()
        await create_feedback(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-1",
            feedback_id="fb-1",
            message_ref="msg-1",
            rating=1,
            category=None,
            comment=None,
        )
        await delete_feedback_for_conversation(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        records = await list_feedback_for_conversation(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        self.assertEqual(records, [])

    async def test_delete_feedback_partial_failure_raises_panel_store_error(self):
        client = FakePanelCosmosClient()
        await create_feedback(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-1",
            feedback_id="fb-1",
            message_ref="msg-1",
            rating=1,
            category=None,
            comment=None,
        )
        await create_feedback(
            client=client,
            principal_id=OID_A,
            conversation_id="conv-1",
            feedback_id="fb-2",
            message_ref="msg-1",
            rating=1,
            category=None,
            comment=None,
        )
        client.fail_delete_for.add("fb-2")
        with self.assertRaises(PanelStoreError):
            await delete_feedback_for_conversation(
                client=client, principal_id=OID_A, conversation_id="conv-1"
            )
        # fb-1 (which did not fail) must still have been attempted/removed.
        remaining = await list_feedback_for_conversation(
            client=client, principal_id=OID_A, conversation_id="conv-1"
        )
        self.assertEqual([r.feedback_id for r in remaining], ["fb-2"])


if __name__ == "__main__":
    unittest.main()
