"""Native contribution ordering, safe attribution and replay admission."""
import copy
import json
from pathlib import Path
import unittest

from gpt_rag_ui.services.native_presentation import presentations, read_presentation


class InlineMultiAgentTests(unittest.TestCase):
    def test_declared_order_attribution_and_native_replay_deduplication(self):
        fixture = json.loads((Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json").read_text())
        first = fixture["contributions"][0]
        first.update(agentId="sales", workflowOrdinal=1)
        second = copy.deepcopy(first)
        second.update(agentId="finance", workflowOrdinal=0, contributionId="finance-contribution")
        def qualify(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in ("sourceId", "blockId"):
                        value[key] = "finance-" + child
                    else:
                        qualify(child)
            elif isinstance(value, list):
                for child in value:
                    qualify(child)
        qualify(second)
        fixture["contributions"] = [first, second]
        projected = read_presentation(json.dumps(fixture), admitted=True)
        self.assertEqual([c["agentId"] for c in projected["contributions"]], ["finance", "sales"])
        self.assertEqual(projected["contributions"][0]["sourceRefs"], second["sourceRefs"])
        message = {"role": "assistant", "messageId": "native-message", "contents": [
            {"$type": "text", "text": json.dumps(fixture)},
        ]}
        self.assertEqual(len(presentations({"messages": [message, copy.deepcopy(message)]})), 1)
        message["role"] = "tool"
        self.assertEqual(presentations(message), [])
        message["role"] = "user"
        self.assertEqual(presentations(message), [])
        second["blocks"][0]["cards"][0]["binding"]["sourceId"] = first["sourceRefs"][0]["sourceId"]
        scoped = read_presentation(json.dumps(fixture), admitted=True)
        self.assertNotIn("cards", [b["type"] for b in scoped["contributions"][0]["blocks"]])
        self.assertEqual(len(scoped["contributions"][1]["blocks"]), len(first["blocks"]))
