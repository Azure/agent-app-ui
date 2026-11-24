"""Execute the release guard and gate's fail-closed prerequisite check."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class WorkflowGovernanceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is unavailable")
    def test_release_guard_accepts_only_same_repository_semantic_release_branches(self):
        guard = workflow("release-branch-guard.yml")
        script = guard["jobs"]["release-branch-required"]["steps"][0]["with"]["script"]
        for branch, repository, allowed in (
            ("release/1.2.3", "Azure/agent-app-ui", True),
            ("release/0.0.0", "Azure/agent-app-ui", True),
            ("release/12.345.6789", "Azure/agent-app-ui", True),
            ("release/1.2.3", "fork/agent-app-ui", False),
            ("release/1.2.3", None, False),
            ("feature/example", "Azure/agent-app-ui", False),
            ("develop", "Azure/agent-app-ui", False),
            ("release/v1.2.3", "Azure/agent-app-ui", False),
            ("release/1.2", "Azure/agent-app-ui", False),
            ("release/1.2.3-rc.1", "Azure/agent-app-ui", False),
            ("release/01.2.3", "Azure/agent-app-ui", False),
            ("release/1.2.3/extra", "Azure/agent-app-ui", False),
        ):
            with self.subTest(branch=branch, repository=repository):
                payload = {"pull_request": {
                    "head": {"ref": branch, "repo": {"full_name": repository} if repository else None},
                    "base": {"repo": {"full_name": "Azure/agent-app-ui"}},
                }}
                harness = (
                    f"const context = {{payload: {json.dumps(payload)}}};"
                    "const core = {info: () => {}, setFailed: () => {process.exitCode = 1;}};"
                    f"(async () => {{{script}}})();"
                )
                result = subprocess.run(
                    [shutil.which("node"), "-e", harness], capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(0 if allowed else 1, result.returncode, result.stderr)

    @unittest.skipUnless(shutil.which("bash"), "Bash is unavailable")
    def test_quality_gate_rejects_every_non_successful_or_missing_prerequisite(self):
        jobs = workflow("quality-gate.yml")["jobs"]
        gate = jobs["quality-gate"]
        self.assertEqual(set(jobs) - {"quality-gate"}, set(gate["needs"]))
        script = gate["steps"][0]["run"]
        variables = ("QUALITY_RESULT", "TEST_RESULT", "CONTAINER_RESULT", "AGENTIC_RESULT")
        passing = {name: "success" for name in variables}
        cases = [(passing, 0)]
        for name in variables:
            for status in ("failure", "cancelled", "skipped", ""):
                cases.append(({**passing, name: status}, 1))
            missing = dict(passing)
            del missing[name]
            cases.append((missing, 1))
        clean_env = {key: value for key, value in os.environ.items() if key not in variables}
        for statuses, expected in cases:
            with self.subTest(statuses=statuses):
                result = subprocess.run(
                    [shutil.which("bash"), "-c", script],
                    env={**clean_env, **statuses}, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(expected, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
