"""Actual legacy imports remain available; private test seams are not adapters."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ModuleCompatibilityTests(unittest.TestCase):
    def test_all_researched_runtime_owners_are_inventoried(self):
        inventory = json.loads((ROOT / ".quality" / "migration.json").read_text(encoding="utf-8"))
        self.assertEqual(32, len(inventory["modules"]))
        for module in inventory["modules"]:
            self.assertTrue((ROOT / module["path"]).is_file(), module["id"])

    def test_root_adapters_contain_no_business_definitions_or_module_proxies(self):
        for path in [*ROOT.glob("*.py"), *ROOT.joinpath("connectors").glob("*.py")]:
            with self.subTest(path=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                self.assertFalse(any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                                     for n in ast.walk(tree)))
                self.assertNotIn("sys.modules", path.read_text(encoding="utf-8"))
                self.assertNotIn("sys.path", path.read_text(encoding="utf-8"))

    def test_legacy_and_canonical_imports_share_public_objects(self):
        # A subprocess isolates real legacy import side effects from tests patching owners.
        code = """
import importlib, json, os
from pathlib import Path
os.environ.update(CHAT_BACKEND="orchestrator", ALLOW_ANONYMOUS="true",
                  OAUTH_AZURE_AD_CLIENT_ID="client", OAUTH_AZURE_AD_TENANT_ID="tenant",
                  OAUTH_AZURE_AD_CLIENT_SECRET="test-secret")
inventory = json.loads(Path(".quality/migration.json").read_text())
splits = {
    ("datalayer", "OrchestratorDataLayer"): "gpt_rag_ui.api.history",
    ("datalayer", "get_data_layer"): "gpt_rag_ui.api.history",
    ("embed_auth", "register_copilot_auth_routes"): "gpt_rag_ui.api.embed_routes",
    ("download_security", "register_secure_download_route"): "gpt_rag_ui.api.download_routes",
    ("feedback", "register_feedback_handlers"): "gpt_rag_ui.api.feedback",
}
citations = {"CONVERSATION_DOCUMENTS_CONTAINER", "DOCUMENTS_CONTAINER", "IMAGES_CONTAINER",
             "IMAGE_EXTENSIONS", "SHARED_DOWNLOAD_CONTAINERS", "STORAGE_ACCOUNT_NAME",
             "format_hosted_citation_sources", "generate_blob_sas_url",
             "replace_source_reference_links", "resolve_reference_href"}
for entry in inventory["modules"]:
    if entry["id"] == "connectors":
        continue
    legacy = importlib.import_module(entry["id"])
    canonical = importlib.import_module(entry["import_name"])
    for name in entry["public_exports"]:
        owner = splits.get((entry["id"], name), entry["import_name"])
        if entry["id"] == "app" and name in citations:
            owner = "gpt_rag_ui.services.citations"
        assert getattr(legacy, name) is getattr(importlib.import_module(owner), name), (entry["id"], name)
"""
        environment = dict(os.environ)
        environment.pop("APP_CONFIG_ENDPOINT", None)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=environment,
                                capture_output=True, text=True, timeout=90)
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
