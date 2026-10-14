import logging
import unittest

from gpt_rag_ui.telemetry.boundary_diagnostics import log_boundary_failure


class BoundaryDiagnosticsTests(unittest.TestCase):
    def test_dependency_payload_chain_and_locals_are_not_logged(self):
        logger = logging.getLogger("tests.boundary_diagnostics")
        private_value = "private-token-cookie-claim-url-sentinel"
        with self.assertLogs(logger, level="ERROR") as logs:
            try:
                try:
                    raise ValueError(private_value)
                except ValueError as cause:
                    raise RuntimeError(private_value) from cause
            except RuntimeError:
                log_boundary_failure(logger, "Boundary failed: reference=%s", "reference-123")
        output = "\n".join(logs.output)
        self.assertNotIn(private_value, output)
        self.assertIn("reference-123", output)
        self.assertIn("exception_type=RuntimeError", output)
        self.assertIn("test_boundary_diagnostics.py:", output)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertIsNone(logs.records[0].exc_text)
        self.assertEqual(__name__, logs.records[0].module)

    def test_warning_diagnostic_preserves_level_without_error_payload(self):
        logger = logging.getLogger("tests.boundary_diagnostics")
        with self.assertLogs(logger, level="WARNING") as logs:
            try:
                raise ValueError("private-refresh-token")
            except ValueError:
                log_boundary_failure(logger, "Refresh denied", level=logging.WARNING)
        self.assertEqual(logging.WARNING, logs.records[0].levelno)
        self.assertNotIn("private-refresh-token", "\n".join(logs.output))

    def test_no_active_exception_is_explicit(self):
        logger = logging.getLogger("tests.boundary_diagnostics")
        with self.assertLogs(logger, level="ERROR") as logs:
            log_boundary_failure(logger, "Failure without active exception")
        self.assertIn("exception_type=unknown failure_site=unknown", logs.output[0])
