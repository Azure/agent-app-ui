"""Non-editable wheel acceptance outside the checkout with staged writable assets."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class InstalledPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="gpt-rag-ui-installed-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.environment = cls.root / "environment"
        venv.EnvBuilder(with_pip=True).create(cls.environment)
        cls.python = cls.environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        cls.run_command([
            str(cls.python), "-m", "pip", "install", "--quiet",
            "-r", str(ROOT / "requirements.txt"),
        ], timeout=600)
        cls.run_command([str(cls.python), "-m", "pip", "check"])
        cls.wheels = cls.root / "wheels"
        cls.wheels.mkdir()
        cls.run_command([sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(cls.wheels), str(ROOT)])
        cls.wheel = next(cls.wheels.glob("gpt_rag_ui-*.whl"))
        cls.run_command([str(cls.python), "-m", "pip", "install", "--no-deps", "--ignore-installed", str(cls.wheel)])
        cls.assets = cls.root / "assets"
        cls.assets.mkdir()
        for name in (".chainlit", "public"):
            shutil.copytree(ROOT / name, cls.assets / name)
        for name in ("chainlit.config.yaml", "chainlit.md", "VERSION"):
            shutil.copy2(ROOT / name, cls.assets / name)
        cls.cwd = cls.root / "unrelated-cwd"
        cls.cwd.mkdir()
        cls.inventory = json.loads((ROOT / ".quality/migration.json").read_text(encoding="utf-8"))
        cls.behavioral_tests = cls.root / "behavioral-tests"
        # Source/build and deployment-tooling contracts run in the checkout.
        # Reuse runtime behavior here without copying application source.
        shutil.copytree(
            ROOT / "tests",
            cls.behavioral_tests,
            ignore=shutil.ignore_patterns(
                "__pycache__", "test_installed_package.py",
                "test_module_compatibility.py", "test_quality_policy.py",
                "deployment",
            ),
        )

    @classmethod
    def run_command(cls, command, *, timeout=300, **kwargs):
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout, **kwargs)
        if result.returncode:
            raise AssertionError(f"Command failed: {command[:4]}\n{result.stdout}\n{result.stderr}")
        return result

    def run_installed(self, code, *, environment=None, cwd=None):
        env = dict(os.environ)
        for name in (
            "PYTHONPATH", "PYTHONHOME", "APP_CONFIG_ENDPOINT",
            "AZURE_APPCONFIG_CONNECTION_STRING", "DEPLOY_ADMINISTRATIVE_PANEL",
            "DATABASE_ACCOUNT_NAME", "DATABASE_NAME", "CHAINLIT_URL",
            "CHAINLIT_ALLOWED_ORIGINS", "CHAINLIT_COOKIE_SECURE",
            "CHAINLIT_COOKIE_SAMESITE",
        ):
            env.pop(name, None)
        for name in list(env):
            if name.startswith(("OAUTH_", "HOSTED_", "PANEL_", "CHAINLIT_COPILOT_")) or name == "ALLOW_ANONYMOUS_EFFECTIVE":
                env.pop(name, None)
        env.update(CHAINLIT_APP_ROOT=str(self.assets), CHAT_BACKEND="orchestrator",
                   ALLOW_ANONYMOUS="true", CHAINLIT_AUTH_SECRET="test-secret-for-installed-package")
        env.update(environment or {})
        prelude = f"""
from pathlib import Path
import importlib, os, sys
checkout = Path({str(ROOT)!r}).resolve()
installed = Path({str(self.environment)!r}).resolve()
assert not Path.cwd().is_relative_to(checkout)
assert Path(sys.prefix).resolve() == installed
assert sys.prefix != sys.base_prefix
assert all(not Path(path).resolve().is_relative_to(checkout) for path in sys.path)
def assert_installed(module):
    path = Path(module.__file__).resolve()
    assert path.is_relative_to(installed), (module.__name__, str(path))
    assert not path.is_relative_to(checkout)
"""
        return self.run_command([str(self.python), "-I", "-c", prelude + code],
                                cwd=cwd or self.cwd, env=env)

    def test_wheel_contains_every_package_and_legacy_module(self):
        with zipfile.ZipFile(self.wheel) as wheel:
            files = set(wheel.namelist())
        for entry in self.inventory["modules"]:
            self.assertIn(entry["source_path"], files)
            self.assertIn(entry["path"].removeprefix("src/"), files)
        self.assertIn("gpt_rag_ui/config/resources.py", files)
        self.assertFalse(any(path.startswith((".chainlit/", "public/")) for path in files))

    def test_behavioral_suite_uses_the_installed_distribution(self):
        self.run_installed(f"""
import unittest
suite = unittest.defaultTestLoader.discover({str(self.behavioral_tests)!r})
assert suite.countTestCases() >= 410, suite.countTestCases()
result = unittest.TextTestRunner(verbosity=1).run(suite)
assert result.wasSuccessful()
assert not result.skipped, result.skipped
for name, module in list(sys.modules.items()):
    if (name == "gpt_rag_ui" or name.startswith("gpt_rag_ui.")) and getattr(module, "__file__", None):
        assert_installed(module)
""")

    def test_both_import_orders_have_one_owner_and_one_registration(self):
        pairs = [(m["id"], m["import_name"]) for m in self.inventory["modules"] if m["id"] != "connectors"]
        for reverse in (False, True):
            with self.subTest(canonical_first=reverse):
                self.run_installed(f"""
os.environ.update(OAUTH_AZURE_AD_CLIENT_ID="client", OAUTH_AZURE_AD_TENANT_ID="tenant",
                  OAUTH_AZURE_AD_CLIENT_SECRET="test-secret")
from unittest.mock import patch
import chainlit as cl
with patch.object(cl, "on_chat_start", wraps=cl.on_chat_start) as start, \\
     patch.object(cl, "on_chat_resume", wraps=cl.on_chat_resume) as resume, \\
     patch.object(cl, "on_message", wraps=cl.on_message) as message, \\
     patch.object(cl, "data_layer", wraps=cl.data_layer) as data, \\
     patch.object(cl, "oauth_callback", wraps=cl.oauth_callback) as oauth:
    for legacy, canonical in {pairs!r}:
        order = (canonical, legacy) if {reverse!r} else (legacy, canonical)
        for name in order:
            assert_installed(importlib.import_module(name))
    from gpt_rag_ui.api.callbacks import register_callbacks
    register_callbacks()
    register_callbacks()
    assert start.call_count == resume.call_count == message.call_count == 1
    from gpt_rag_ui.services import chat
    assert data.call_count == int(chat.CHAT_BACKEND == "orchestrator")
    assert oauth.call_count == 1
import dependencies
from gpt_rag_ui.config import dependencies as owner
assert dependencies.get_config is owner.get_config
assert dependencies.get_config() is owner.get_config()
import main
from gpt_rag_ui import bootstrap
assert main.app is bootstrap.app
import hosted_agent_client
from gpt_rag_ui.clients import hosted_agent_client as client_owner
assert hosted_agent_client.call_hosted_agent_stream is client_owner.call_hosted_agent_stream
""")

    def test_real_main_startup_uses_assets_and_auth_precedes_chainlit(self):
        self.run_installed("""
assert "chainlit" not in sys.modules
class AuthOrderGuard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "chainlit":
            assert os.environ.get("ALLOW_ANONYMOUS_EFFECTIVE") == "true"
            assert os.environ.get("CHAINLIT_AUTH_SECRET")
        return None
sys.meta_path.insert(0, AuthOrderGuard())
from gpt_rag_ui.config import dependencies
class Config:
    connected = True
    def get(self, key, default=None, type=str):
        return {"CHAT_BACKEND": "orchestrator", "ALLOW_ANONYMOUS": True}.get(key, default)
    def get_value(self, key, default=None, allow_none=False, type=str):
        return self.get(key, default, type)
dependencies.__dict__["__config"] = Config()
assert "chainlit" not in sys.modules
import main
assert_installed(main)
from uvicorn.importer import import_from_string
assert import_from_string("main:app") is main.app
assert os.environ["ALLOW_ANONYMOUS_EFFECTIVE"] == "true"
assert "gpt_rag_ui.clients.panel_cosmos" not in sys.modules
assert "gpt_rag_ui.clients.hosted_conversation_store" not in sys.modules
from chainlit.config import APP_ROOT, config
assert Path(APP_ROOT).resolve() == Path(os.environ["CHAINLIT_APP_ROOT"]).resolve()
from fastapi.testclient import TestClient
with TestClient(main.app) as client:
    footer = client.get("/version-footer")
    assert footer.status_code == 200
    expected = Path(APP_ROOT, "VERSION").read_text().strip()
    assert footer.json()["gpt_rag_ui_release"] == "v" + expected
    response = client.get("/public/custom.css")
    assert response.status_code == 200, response.status_code
    assert client.get("/").status_code == 200
""")

    def test_installed_startup_preserves_documented_failure_boundaries(self):
        self.run_installed("""
from unittest import TestCase
from unittest.mock import patch
from gpt_rag_ui.config import dependencies
class Config:
    connected = True
    def get(self, key, default=None, type=str):
        return {"CHAT_BACKEND": "orchestrator", "ALLOW_ANONYMOUS": True,
                "DOCUMENTS_STORAGE_CONTAINER": "documents"}.get(key, default)
    def get_value(self, key, default=None, allow_none=False, type=str):
        return self.get(key, default, type)
dependencies.__dict__["__config"] = Config()
recovered_schema = {"openapi": "3.0.0", "info": {"title": "Recovered", "version": "test"}, "paths": {"/ready": {}}}
with patch("fastapi.openapi.utils.get_openapi", side_effect=[
    RuntimeError("schema failure"), RuntimeError("schema failure again"), recovered_schema,
]) as generate:
    import main
assert_installed(main)
from chainlit.server import app as chainlit_app
with TestCase().assertLogs(level="ERROR"), TestCase().assertRaisesRegex(RuntimeError, "schema failure"):
    chainlit_app.openapi()
assert chainlit_app.openapi_schema is None
from fastapi.testclient import TestClient
with TestClient(chainlit_app, raise_server_exceptions=False) as schema_client, TestCase().assertLogs(level="ERROR"):
    failed_schema = schema_client.get("/openapi.json")
assert failed_schema.status_code == 500
assert "schema failure" not in failed_schema.text
assert '"openapi"' not in failed_schema.text
assert chainlit_app.openapi_schema is None
assert chainlit_app.openapi() is recovered_schema
assert chainlit_app.openapi() is recovered_schema
assert generate.call_count == 3
from gpt_rag_ui.clients.blob import BlobClient
from azure.core.exceptions import ResourceNotFoundError
from fastapi.testclient import TestClient
with TestClient(main.app) as client, patch.object(BlobClient, "__init__", return_value=None):
    for failure, expected_status, body in (
        (ResourceNotFoundError("private-backend"), 404, "Blob not found."),
        (RuntimeError("private-backend BlobNotFound"), 500, "Internal server error."),
        (RuntimeError("private-backend"), 500, "Internal server error."),
    ):
        with patch.object(BlobClient, "download_blob", side_effect=failure), TestCase().assertLogs(level="ERROR") as logs:
            response = client.get("/api/download/documents/synthetic.pdf")
        assert response.status_code == expected_status, response.status_code
        assert response.text == body
        assert "private-backend" not in response.text
        assert "private-backend" not in str(logs.output)
    assert client.get("/api/download/not-allowed/synthetic.pdf").status_code == 404
""")

    def test_hosted_continuity_and_panel_activation_share_one_coordinator(self):
        for active in (False, True):
            with self.subTest(panel_active=active):
                self.run_installed(f"""
from gpt_rag_ui.config import dependencies
class Config:
    connected = True
    def get(self, key, default=None, type=str):
        return default
    def get_value(self, key, default=None, allow_none=False, type=str):
        return default
dependencies.__dict__["__config"] = Config()
import main, app
from gpt_rag_ui.services import chat
assert app.CHAT_BACKEND == "hosted_agent"
assert app.HOSTED_CONTINUITY_ENABLED
first = chat.get_hosted_continuity_coordinator()
assert first is chat.get_hosted_continuity_coordinator()
assert chat.PANEL_SETTINGS.user_surfaces_active is {active!r}
assert (first._on_conversation_created is not None) is {active!r}
assert_installed(chat)
assert "gpt_rag_ui.clients.panel_cosmos" in sys.modules
from fastapi.testclient import TestClient
with TestClient(main.app) as client:
    response = client.get("/panel/conversations")
    assert response.status_code == {401 if active else 503}, response.status_code
""", environment={
                    "CHAT_BACKEND": "",
                    "CHAINLIT_URL": "https://ui.example.com",
                    "HOSTED_AGENT_BASE_URL": "https://agent.example.com/protocol",
                    "HOSTED_AGENT_RESOURCE_SCOPE": "api://hosted-agent/.default",
                    "OAUTH_AZURE_AD_CLIENT_ID": "test-client",
                    "OAUTH_AZURE_AD_TENANT_ID": "11111111-2222-3333-4444-555555555555",
                    "OAUTH_AZURE_AD_CLIENT_SECRET": "test-client-secret",
                    "HOSTED_CONTINUITY_ENABLED": "true",
                    "HOSTED_CONVERSATION_OWNER_BINDING": "capability",
                    "HOSTED_CONVERSATION_CAPABILITY_KEY": "k" * 32,
                    "HOSTED_CONVERSATION_CAPABILITY_KEY_ID": "test",
                    "HOSTED_CONVERSATION_STORE_BASE_URL": "https://agent.example.com/openai/v1",
                    "HOSTED_CONVERSATION_STORE_RESOURCE_SCOPE": "api://hosted-agent/.default",
                    "DEPLOY_ADMINISTRATIVE_PANEL": str(active).lower(),
                    "PANEL_HISTORY_ENABLED": str(active).lower(),
                    "PANEL_CONVERSATIONS_TOKEN_AUDIENCE": "api://panel/.default",
                    "PANEL_CONVERSATIONS_TENANT_ID": "11111111-2222-3333-4444-555555555555",
                    "DATABASE_ACCOUNT_NAME": "test-account",
                    "DATABASE_NAME": "test-database",
                })

    def test_real_entra_startup_keeps_uploads_in_the_writable_asset_root(self):
        for copilot in (False, True):
            with self.subTest(copilot=copilot):
                environment = {
                    "ALLOW_ANONYMOUS": "false",
                    "OAUTH_AZURE_AD_CLIENT_ID": "test-client",
                    "OAUTH_AZURE_AD_TENANT_ID": "11111111-2222-3333-4444-555555555555",
                    "OAUTH_AZURE_AD_CLIENT_SECRET": "test-client-secret",
                }
                if copilot:
                    environment.update({
                        "CHAINLIT_COPILOT_ENABLED": "true",
                        "CHAINLIT_COPILOT_AUTH_MODE": "entra",
                        "CHAINLIT_URL": "https://chat.example.com",
                        "CHAINLIT_ALLOWED_ORIGINS": "https://portal.example.com",
                        "CHAINLIT_COPILOT_ENTRA_TENANT_ID": environment["OAUTH_AZURE_AD_TENANT_ID"],
                        "CHAINLIT_COPILOT_ENTRA_AUDIENCE": "api://test",
                    })
                self.run_installed("""
import asyncio, tomllib
from uuid import uuid4
assert "chainlit" not in sys.modules
class AuthOrderGuard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "chainlit":
            assert os.environ.get("ALLOW_ANONYMOUS_EFFECTIVE") == "false"
            assert os.environ.get("CHAINLIT_AUTH_SECRET")
        return None
sys.meta_path.insert(0, AuthOrderGuard())
from gpt_rag_ui.config import dependencies
class Config:
    connected = True
    def get(self, key, default=None, type=str):
        return default
    def get_value(self, key, default=None, allow_none=False, type=str):
        return default
dependencies.__dict__["__config"] = Config()
import main
assert_installed(main)
assert "chainlit" in sys.modules
from gpt_rag_ui import bootstrap
assert main.app is bootstrap.app
assert_installed(bootstrap)
from chainlit.config import APP_ROOT, FILES_DIRECTORY, config
asset_root = Path(os.environ["CHAINLIT_APP_ROOT"]).resolve()
assert Path(APP_ROOT).resolve() == asset_root
assert FILES_DIRECTORY.resolve() == asset_root / ".files"
assert not FILES_DIRECTORY.resolve().is_relative_to(installed)
assert config.features.spontaneous_file_upload.enabled
saved_config = tomllib.loads((asset_root / ".chainlit" / "config.toml").read_text())
assert saved_config["features"]["spontaneous_file_upload"]["enabled"]
from chainlit.session import HTTPSession
async def persist_upload():
    session = HTTPSession(
        id="installed-" + uuid4().hex, client_type="webapp",
        thread_id=None, user=None, token=None, user_env=None, environ=None,
    )
    try:
        reference = await session.persist_file(
            name="synthetic.txt", mime="text/plain", content=b"synthetic installed upload",
        )
        path = session.files[reference["id"]]["path"].resolve()
        assert path.is_relative_to(FILES_DIRECTORY.resolve())
        assert path.read_bytes() == b"synthetic installed upload"
    finally:
        await session.delete()
    assert not session.files_dir.exists()
asyncio.run(persist_upload())
from fastapi.testclient import TestClient
with TestClient(main.app) as client:
    assert client.get("/version-footer").status_code == 200
    assert client.get("/public/custom.css").status_code == 200
""", environment=environment)

    def test_missing_required_oauth_preserves_auth_required_readiness(self):
        self.run_installed("""
from gpt_rag_ui.config import dependencies
class Config:
    connected = True
    def get(self, key, default=None, type=str):
        return default
    def get_value(self, key, default=None, allow_none=False, type=str):
        return default
dependencies.__dict__["__config"] = Config()
import main
assert_installed(main)
from fastapi.testclient import TestClient
with TestClient(main.app) as client:
    response = client.get("/")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.headers["x-app-mode"] == "auth-required"
assert "chainlit" not in sys.modules
""", environment={"ALLOW_ANONYMOUS": "false"})

    def test_invalid_copilot_configuration_fails_only_when_activated(self):
        for active in (False, True):
            with self.subTest(copilot_active=active):
                self.run_installed(f"""
import unittest
from gpt_rag_ui.config import dependencies
from gpt_rag_ui.config.embed_config import EmbedConfigError
class Config:
    connected = True
    def get(self, key, default=None, type=str):
        return default
    def get_value(self, key, default=None, allow_none=False, type=str):
        return default
dependencies.__dict__["__config"] = Config()
if {active!r}:
    with unittest.TestCase().assertRaises(EmbedConfigError):
        importlib.import_module("main")
    assert "chainlit" not in sys.modules
else:
    import main
    assert_installed(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        assert client.get("/version-footer").status_code == 200
""", environment={
                    "CHAINLIT_COPILOT_ENABLED": str(active).lower(),
                    "CHAINLIT_COPILOT_AUTH_MODE": "entra",
                    "CHAINLIT_URL": "invalid-url",
                })

    def test_disconnected_configuration_is_not_ready_and_missing_version_is_optional(self):
        self.run_installed("""
import main
from fastapi.testclient import TestClient
with TestClient(main.app) as client:
    assert client.get("/").status_code == 503
    assert client.get("/healthz").status_code == 503
from gpt_rag_ui import bootstrap
from unittest.mock import patch
with patch.object(bootstrap, "_local_version_file_path", return_value="missing-version"):
    assert bootstrap._read_local_ui_version() is None
assert "chainlit" not in sys.modules
""")

    def test_cwd_asset_handoff_and_missing_config_warning(self):
        self.run_installed("""
os.environ.pop("CHAINLIT_APP_ROOT")
import main
from gpt_rag_ui.config.resources import get_asset_root
assert get_asset_root() == Path.cwd().resolve()
from gpt_rag_ui import bootstrap
from unittest.mock import patch
from types import SimpleNamespace
with patch.object(bootstrap, "get_asset_root", return_value=Path.cwd() / "absent"), \\
     patch.object(bootstrap.logger, "warning") as warning:
    bootstrap._sync_chainlit_spontaneous_file_upload(SimpleNamespace(allow_anonymous=True))
    warning.assert_called_once()
""", cwd=self.assets)


if __name__ == "__main__":
    unittest.main()
