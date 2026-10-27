"""Offline image acceptance; run explicitly, not through test_*.py discovery."""

import argparse
import http.client
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest

LOGGER = logging.getLogger("container-smoke")
EXCLUDED = {"test_quality_policy.py", "test_module_compatibility.py", "test_installed_package.py"}
MINIMUM_BEHAVIORAL_TESTS = 410


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def assert_installed_origins():
    site = Path(sysconfig.get_paths()["purelib"]).resolve()
    for name, module in list(sys.modules.items()):
        if name == "gpt_rag_ui" or name.startswith("gpt_rag_ui."):
            filename = getattr(module, "__file__", None)
            if filename:
                require(Path(filename).resolve().is_relative_to(site), f"Non-installed canonical owner: {name}")


def clean_environment(root):
    env = dict(os.environ)
    for name in list(env):
        if name.startswith(("OAUTH_", "AZURE_", "HOSTED_", "PANEL_", "CHAINLIT_COPILOT_")) or name in {
            "PYTHONPATH", "PYTHONHOME", "APP_CONFIG_ENDPOINT", "ALLOW_ANONYMOUS_EFFECTIVE",
            "APPLICATIONINSIGHTS_CONNECTION_STRING", "APPLICATION_INSIGHTS_CONNECTION_STRING",
        }:
            env.pop(name, None)
    env.update(CHAINLIT_APP_ROOT=str(root), CHAT_BACKEND="orchestrator", ALLOW_ANONYMOUS="true",
               CHAINLIT_AUTH_SECRET="container-smoke-test-secret", PYTHONDONTWRITEBYTECODE="1")
    return env


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def listener_smoke(root, *, connected):
    port = free_port()
    if connected:
        # Substitute only the existing App Configuration boundary; Uvicorn still
        # resolves the shipped main:app entrypoint and opens a real loopback listener.
        code = f"""
import os, sys, sysconfig
from pathlib import Path
class AuthOrderGuard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "chainlit" and os.environ.get("ALLOW_ANONYMOUS_EFFECTIVE") != "true":
            raise RuntimeError("Chainlit loaded before effective authentication")
        return None
sys.meta_path.insert(0, AuthOrderGuard())
from gpt_rag_ui.config import dependencies
class Config:
    connected = True
    def get(self, key, default=None, type=str):
        return {{"CHAT_BACKEND": "orchestrator", "ALLOW_ANONYMOUS": True}}.get(key, default)
    def get_value(self, key, default=None, allow_none=False, type=str):
        return self.get(key, default, type)
dependencies.__dict__["__config"] = Config()
from uvicorn.importer import import_from_string
application = import_from_string("main:app")
from chainlit.config import APP_ROOT
if Path(APP_ROOT).resolve() != Path(os.environ["CHAINLIT_APP_ROOT"]).resolve():
    raise RuntimeError("Wrong staged asset root")
if any(name in sys.modules for name in (
    "gpt_rag_ui.clients.panel_cosmos", "gpt_rag_ui.clients.hosted_conversation_store"
)):
    raise RuntimeError("Classic startup eagerly loaded hosted/panel storage")
site = Path(sysconfig.get_paths()["purelib"]).resolve()
for name, module in list(sys.modules.items()):
    if name == "gpt_rag_ui" or name.startswith("gpt_rag_ui."):
        filename = getattr(module, "__file__", None)
        if filename and not Path(filename).resolve().is_relative_to(site):
            raise RuntimeError("Canonical module resolved outside the non-editable installation: " + name)
import uvicorn
uvicorn.run("main:app", host="127.0.0.1", port={port}, log_level="warning")
"""
        command = [sys.executable, "-c", code]
    else:
        command = [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
                   "--port", str(port), "--log-level", "warning"]
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        process = subprocess.Popen(command, cwd=root, env=clean_environment(root), stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    output.seek(0)
                    raise RuntimeError("Uvicorn exited before listening:\n" + output.read()[-6000:])
                try:
                    status, body = request(port, "/")
                    break
                except (ConnectionError, TimeoutError, http.client.RemoteDisconnected):
                    if time.monotonic() >= deadline:
                        output.seek(0)
                        raise RuntimeError("Uvicorn listener deadline exceeded:\n" + output.read()[-6000:]) from None
                    time.sleep(0.2)
            require(status == (200 if connected else 503), f"Unexpected startup HTTP status: {status}")
            if connected:
                status, body = request(port, "/version-footer")
                require(status == 200, "VERSION footer unavailable")
                require(json.loads(body)["gpt_rag_ui_release"] == "v" + (root / "VERSION").read_text().strip(),
                        "VERSION did not come from the staged root")
                status, body = request(port, "/public/custom.css")
                require(status == 200 and body == (root / "public" / "custom.css").read_bytes(),
                        "Public CSS did not come from the staged root")
            else:
                status, body = request(port, "/healthz")
                require(status == 503 and body == b"not-ready", "Disconnected configuration failed open")
            LOGGER.info("Uvicorn main:app listener passed (%s)", "ready" if connected else "not-ready")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def behavioral_suite(tests_root):
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for filename in sorted(tests_root.glob("test_*.py")):
        if filename.name not in EXCLUDED:
            suite.addTests(loader.discover(str(tests_root), pattern=filename.name))
    require(not loader.errors, "Behavioral test discovery failed: " + "; ".join(loader.errors))
    require(suite.countTestCases() >= MINIMUM_BEHAVIORAL_TESTS, "Existing behavioral coverage was omitted")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    require(result.wasSuccessful(), "Installed behavioral suite failed")
    require(not result.skipped, "Installed behavioral tests were skipped")
    assert_installed_origins()
    LOGGER.info("Installed behavioral cases passed: %s", result.testsRun)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-root", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    environment = clean_environment(root)
    os.environ.clear()
    os.environ.update(environment)
    for name in (".chainlit/config.toml", "public/custom.css", "chainlit.config.yaml", "chainlit.md", "VERSION"):
        require((root / name).is_file(), f"Missing staged asset: {name}")
    import gpt_rag_ui
    require(gpt_rag_ui.__file__ is not None, "Canonical package has no installed origin")
    assert_installed_origins()
    listener_smoke(root, connected=False)
    listener_smoke(root, connected=True)
    behavioral_suite(args.tests_root.resolve())
    LOGGER.info("Offline installed image acceptance passed")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())
