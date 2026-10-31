"""Opt-in actual ALZ producer -> UI client -> runtime worker contract.

Set NATIVE_CONTRACT_ALZ_ROOT, NATIVE_CONTRACT_RUNTIME_ROOT and
NATIVE_CONTRACT_RUNTIME_PYTHON to the coordinated checkouts and published-wheel
runtime environment. No source extraction, string rewriting, model or Azure I/O.
Unconfigured component-only CI explicitly skips this cross-repository gate.
"""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import grpc
from azure.identity import ManagedIdentityCredential
from durabletask.azuremanaged.client import DurableTaskSchedulerClient

from gpt_rag_ui.clients.native_durable import NativeDurableClient, scheduler_options
from gpt_rag_ui.config.errors import ConfigurationError
from gpt_rag_ui.config.native import load_native_settings
from test_native_ui import base_config, OTHER


RUNTIME_PROBE = r"""
import importlib.metadata
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import grpc
from azure.identity import ManagedIdentityCredential
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker
import native.durable as runtime

request = json.load(sys.stdin)
assert Path(runtime.__file__).resolve() == Path(request["source"]).resolve()
assert importlib.metadata.version("durabletask") == "1.12.0"
assert importlib.metadata.version("agent-framework-durabletask") == "1.0.0b261029"
binding = request["binding"]
parsed = runtime.scheduler_binding(binding)
# The worker keeps its explicit local SDK credential-chain choice. It is not
# produced by ALZ and is not accepted by the deployed UI consumer.
assert runtime.scheduler_binding(
    binding.replace("Authentication=ManagedIdentity", "Authentication=DefaultAzure")
) == parsed
invalid = [
    *(binding + ";" + field + "=invalid" for field in ("Secret", "SharedAccessKey", "BearerToken")),
]
for value in invalid:
    try:
        runtime.scheduler_binding(value)
    except ValueError:
        pass
    else:
        raise AssertionError("Runtime accepted a secret-bearing binding.")
callbacks = []
constructed = []
identity_factory = Mock(wraps=ManagedIdentityCredential)
config = SimpleNamespace(open=AsyncMock(), view=AsyncMock(return_value={}), close=AsyncMock())
graph = SimpleNamespace(start=lambda: callbacks[0](None, None), stop=lambda: None)

with grpc.insecure_channel("127.0.0.1:1") as channel:
    def construct_worker(**kwargs):
        assert isinstance(kwargs["token_credential"], ManagedIdentityCredential), (
            "Deployed DTS worker must use ManagedIdentityCredential, not DefaultAzureCredential."
        )
        assert kwargs["secure_channel"] is True
        assert kwargs["host_address"] == parsed["host_address"]
        assert kwargs["taskhub"] == parsed["taskhub"]
        worker = DurableTaskSchedulerWorker(**kwargs, channel=channel)
        constructed.append(type(worker).__name__)
        return worker

    with (
        patch.dict(os.environ, {
            "DURABLE_TASK_SCHEDULER_CONNECTION_STRING": binding,
            "AZURE_CLIENT_ID": request["client_id"],
        }, clear=True),
        patch.object(runtime, "NativeConfig", return_value=config),
        patch.object(runtime, "configure_worker_telemetry"),
        patch.object(runtime, "build_worker", return_value=graph),
        patch.object(runtime.signal, "signal", side_effect=lambda sig, fn: callbacks.append(fn)),
        patch.object(runtime, "DurableTaskSchedulerWorker", side_effect=construct_worker),
        patch("azure.identity.ManagedIdentityCredential", identity_factory),
        patch.object(runtime, "ManagedIdentityCredential", identity_factory, create=True),
    ):
        runtime.main()
assert constructed == ["DurableTaskSchedulerWorker"]
assert identity_factory.called
assert identity_factory.call_args.kwargs.get("client_id") == request["client_id"], (
    "The runtime must select its configured workload managed identity."
)
print(json.dumps({"host_address": parsed["host_address"], "taskhub": parsed["taskhub"]}))
"""


class NativeBindingIntegrationTests(unittest.TestCase):
    def test_actual_producer_binding_constructs_ui_and_runtime_with_managed_identity(self):
        keys = (
            "NATIVE_CONTRACT_ALZ_ROOT", "NATIVE_CONTRACT_RUNTIME_ROOT",
            "NATIVE_CONTRACT_RUNTIME_PYTHON",
        )
        paths = [os.environ.get(key, "") for key in keys]
        if not any(paths):
            self.skipTest("Cross-repository gate requires all NATIVE_CONTRACT_* paths.")
        self.assertTrue(all(paths), "Partial cross-repository configuration is not a passing gate.")
        alz, runtime, python = map(Path, paths)
        source = alz / "config" / "deployment" / "native.py"
        self.assertTrue(source.is_file())
        self.assertTrue((runtime / "src" / "native" / "durable.py").is_file())
        self.assertTrue(python.is_file())
        spec = importlib.util.spec_from_file_location("alz_native_binding_contract", source)
        producer = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {spec.name: producer}):
            spec.loader.exec_module(producer)
        # Synthetic resource outputs, but the actual provision-handoff producer.
        emitted = producer.scheduler_settings({"HOSTED_AGENT_DEPLOYMENT": json.dumps({
            "durableTask": {
                "endpoint": "https://contract.durabletask.io",
                "taskHubName": "contract-test",
                "resourceId": "/subscriptions/00000000-0000-0000-0000-000000000000/"
                "resourceGroups/contract/providers/Microsoft.DurableTask/schedulers/contract",
            },
        })})
        binding = emitted["DURABLE_TASK_SCHEDULER_CONNECTION_STRING"]
        # Do not replace Authentication or bypass the UI parser with an injected client.
        with (
            patch.dict(os.environ, {"AZURE_CLIENT_ID": OTHER}, clear=True),
            grpc.insecure_channel("127.0.0.1:1") as channel,
            patch("grpc.secure_channel", return_value=channel) as transport,
        ):
            settings = load_native_settings(base_config(DURABLE_TASK_SCHEDULER_CONNECTION_STRING=binding))
            client = NativeDurableClient(settings)
            try:
                self.assertIsInstance(client.client, DurableTaskSchedulerClient)
                self.assertIsInstance(client._credential, ManagedIdentityCredential)
                self.assertEqual(transport.call_args.args[0], "contract.durabletask.io")
                options = scheduler_options(binding)
                for field in ("Secret", "SharedAccessKey", "BearerToken"):
                    with self.subTest(field=field), self.assertRaises(ConfigurationError):
                        scheduler_options(binding + ";" + field + "=invalid")
                with self.assertRaises(ConfigurationError):
                    scheduler_options(binding.replace("Authentication=ManagedIdentity", "Authentication=DefaultAzure"))
            finally:
                asyncio.run(client.aclose())
        env = os.environ.copy()
        env["PYTHONPATH"] = str(runtime / "src")
        result = subprocess.run(
            [str(python), "-c", RUNTIME_PROBE], input=json.dumps({
                "binding": binding, "source": str(runtime / "src" / "native" / "durable.py"),
                "client_id": OTHER,
            }), env=env, cwd=runtime, capture_output=True, text=True, timeout=45,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        worker = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(worker["taskhub"], options["taskhub"])
        self.assertEqual(worker["host_address"], "https://" + options["host_address"])
        self.assertIn(";Authentication=ManagedIdentity", binding)


if __name__ == "__main__":
    unittest.main()
