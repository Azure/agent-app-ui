import json
import copy
import hashlib
from pathlib import Path
import shutil
import subprocess
import importlib.util
import sys
import unittest

from gpt_rag_ui.config.resources import get_asset_root
from gpt_rag_ui.services.native_presentation import read_presentation, presentations, rest_projection, text_projection, SCHEMA


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / "fixtures/inline-presentation/presentation-valid.json").read_text())

    def read(self):
        return read_presentation(json.dumps(self.fixture), admitted=True)

    def test_reviewed_fixture_exact_units_and_indicators(self):
        blocks = self.read()["contributions"][0]["blocks"]
        self.assertEqual([b["type"] for b in blocks], ["cards", "table", "chart", "suggestions"])
        self.assertEqual(blocks[0]["cards"][0]["value"]["value"], "9.750")
        self.assertEqual(blocks[0]["cards"][0]["trend"]["value"], "down")
        self.assertEqual(blocks[1]["rows"][1]["values"]["revenue"], "12.500")
        self.assertEqual(blocks[2]["series"][0]["unit"], "USD")

    def test_unknown_kind_and_nested_optional_properties_not_spread(self):
        blocks = self.fixture["contributions"][0]["blocks"]
        blocks.append({"type": "future", "dangerouslySetInnerHTML": {"__html": "unsafe"}})
        blocks[0]["cards"][0]["binding"]["url"] = "javascript:inert"
        result = self.read()
        self.assertEqual(len(result["contributions"][0]["blocks"]), 4)
        self.assertNotIn("url", result["contributions"][0]["blocks"][0]["cards"][0]["binding"])

    def test_invalid_known_unit_preserves_siblings_and_fallback(self):
        self.fixture["contributions"][0]["blocks"][1]["rows"][0]["values"]["revenue"] = 10.125
        result = self.read()
        self.assertEqual(len(result["contributions"][0]["blocks"]), 3)
        self.assertEqual(result["markdown"], self.fixture["markdown"])

    def test_all_null_and_repeated_category_chart_is_not_success(self):
        chart = self.fixture["contributions"][0]["blocks"][2]
        chart["series"][0]["values"] = [None] * 3
        self.assertNotIn("chart", [b["type"] for b in self.read()["contributions"][0]["blocks"]])
        chart["series"][0]["values"] = ["10.125", "12.500", "9.750"]
        chart["categories"][1]["sourceRowId"] = "jan"
        self.assertNotIn("chart", [b["type"] for b in self.read()["contributions"][0]["blocks"]])

    def test_cross_contribution_source_not_accepted(self):
        self.fixture["contributions"][0]["blocks"][2]["sourceId"] = "other-source"
        self.assertEqual(len(self.read()["contributions"][0]["blocks"]), 3)

    def test_aggregate_count_rejects_entire_visual(self):
        blocks = self.fixture["contributions"][0]["blocks"]
        blocks.extend(copy.deepcopy(blocks[0]) for _ in range(7))
        self.assertEqual(self.read()["contributions"], [])

    def test_nonassistant_nested_and_progress_carriers_are_inert(self):
        raw = json.dumps(self.fixture)
        for role in ("user", "tool", "system"):
            carrier = {"role": role, "contents": [{"type": "text", "text": raw}],
                       "output": {"role": "assistant", "contents": [{"type": "text", "text": raw}]}}
            self.assertEqual(presentations(carrier), [])
        self.assertEqual(presentations({"type": "progress", "text": raw}), [])

    def test_optional_rest_projection_preserves_native_identity_and_disabled_wire(self):
        ordinary = {"response": {"messages": [{"role": "assistant", "contents": [{"type": "text", "text": "ordinary"}]}]}}
        self.assertIs(rest_projection(ordinary), ordinary)
        native = {"native_ref": "native-turn", "response": {"messages": [{
            "role": "assistant", "contents": [{"type": "text", "text": json.dumps(self.fixture)}],
        }]}}
        result = rest_projection(native)
        self.assertEqual(result["native_ref"], "native-turn")
        self.assertEqual(result["presentation_text"], self.fixture["markdown"])
        self.assertEqual(len(result["presentations"]), 1)

    def test_new_text_only_readers_keep_narration_exact_fallback_and_native_identity(self):
        for container, contents in (("output", "content"), ("messages", "contents")):
            with self.subTest(container=container):
                native = {"native_ref": "turn", "response": {container: [
                    {"role": "assistant", contents: [{"type": "output_text", "text": "Authorized narration"}]},
                    {"role": "assistant", contents: [{"type": "output_text", "text": json.dumps(self.fixture)}]},
                ]}}
                projected = rest_projection(native)
                expected = "Authorized narration\n\n" + self.fixture["markdown"]
                self.assertEqual(projected["presentation_text"], expected)
                self.assertEqual(text_projection(native), expected)
                self.assertNotIn("agent_lz_presentation", expected)
                self.assertEqual(projected["native_ref"], "turn")
                self.assertEqual(projected["response"], native["response"])

    def test_text_only_future_version_and_workflow_pending_completed_nonvisual(self):
        future = {"role": "assistant", "content": [{"type": "output_text", "text": json.dumps({
            "kind": "agent_lz_presentation", "schemaVersion": 2, "markdown": "Exact 9007199254740993.001",
        })}]}
        self.assertEqual(rest_projection(future)["presentation_text"], "Exact 9007199254740993.001")
        for status in (None, "approved", "rejected"):
            result = {"topic": "test", "draft": "Narration\n\n" + self.fixture["markdown"],
                      "configuration_revision": "rev", "presentation": self.fixture}
            if status:
                result["status"] = status
                wire = {"native_ref": "workflow", "output": result}
            else:
                wire = {"native_ref": "workflow", "pending": [{"data": result}]}
            projected = rest_projection(wire)
            self.assertIn(self.fixture["markdown"], projected["presentation_text"])
            self.assertIn("Narration", projected["presentation_text"])
            self.assertEqual(projected["native_ref"], "workflow")
        ordinary = {"output": {"status": "approved", "draft": "ordinary text"}}
        self.assertIs(rest_projection(ordinary), ordinary)
        self.assertEqual(text_projection(ordinary), "approved\n\nordinary text")

    def test_schema_package_bytes_match_reviewed_metadata(self):
        schemas = list(SCHEMA.parent.glob("*.schema.json"))
        self.assertEqual(len(schemas), 3)
        for schema in schemas:
            data = schema.read_bytes()
            self.assertNotIn(b"\r", data)
            expected = schema.with_name(schema.name + ".sha256").read_text().split()[0]
            self.assertEqual(hashlib.sha256(data).hexdigest(), expected)

    def test_oversized_carrier_never_admitted(self):
        self.fixture["markdown"] = "x" * 262145
        self.assertIsNone(self.read())

    def test_renderer_exact_numeric_comparison_and_extreme_geometry(self):
        node = shutil.which("node")
        if node is None:
            package = importlib.util.find_spec("playwright")
            if package and package.submodule_search_locations:
                candidate = Path(package.submodule_search_locations[0]) / "driver" / (
                    "node.exe" if sys.platform == "win32" else "node"
                )
                if candidate.is_file():
                    node = str(candidate)
        if node is None:
            self.skipTest("Existing Node or Playwright driver required for JS helper validation")
        asset = (get_asset_root() / "public/elements/Presentation.jsx").read_text()
        helpers = asset[asset.index("function parts("):asset.index("function Table(")]
        program = helpers + """
const assert = require('node:assert/strict');
assert(compareDecimal('9007199254740993.001','9007199254740993.002') < 0);
assert(compareDecimal('-100.000','-9.000') < 0);
assert.equal(compareDecimal('1.00','1.000'), 0);
assert.equal(compareDecimal('-0.00','0'), 0);
assert(compareDecimal('0.001','0.0009') > 0);
const huge = parts('9'.repeat(1000)), tiny = parts('0.'+'0'.repeat(1000)+'1');
assert(Number.isFinite(huge.coefficient));
assert(Number.isFinite(tiny.coefficient));
assert.equal(huge.exponent, 999);
assert.equal(tiny.exponent, -1001);
"""
        result = subprocess.run([node, "-e", program], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_renderer_has_no_active_html_urls_or_remote_dependencies(self):
        asset = (get_asset_root() / "public/elements/Presentation.jsx").read_text()
        for forbidden in ("dangerouslySetInnerHTML", "fetch(", "eval(", "foreignObject", "xlinkHref", "href=", "http://", "https://"):
            self.assertNotIn(forbidden, asset)
        self.assertIn("sendUserMessage(prompt)", asset)
        self.assertIn("if (sending.current) return", asset)

    def test_unadmitted_carriers_are_inert(self):
        self.assertIsNone(read_presentation('{"kind":"agent_lz_presentation","markdown":"saved"}', admitted=False))

    def test_unknown_version_fallback(self):
        result = read_presentation(json.dumps({
            "kind": "agent_lz_presentation", "schemaVersion": 2, "markdown": "saved exact 1.001",
        }), admitted=True)
        self.assertEqual(result["markdown"], "saved exact 1.001")
        self.assertEqual(result["contributions"], [])

    def test_duplicate_and_incomplete_carriers_rejected(self):
        self.assertIsNone(read_presentation('{"kind":"agent_lz_presentation","kind":"spoof"}', admitted=True))
        self.assertIsNone(read_presentation('{"kind":"agent_lz_presentation"', admitted=True))
