"""Opt-in actual runtime failure propagation; fake credentials and loopback only."""
import json
from pathlib import Path
import unittest

import test_responses_evidence_handoff as fixture
from codex_collaboration_harness import embedded_nokiy as caller


class ProviderFailureAdoptionTests(unittest.TestCase):
    def test_failed_response_error_survives_null_details_in_actual_runtime(self):
        helper = fixture.RuntimeEvidenceHandoffTests()
        self.addCleanup(helper.doCleanups)
        helper.setUp()
        with fixture._loopback_provider([{"fixture_failed_response": True}] * 4) as (url, calls, errors, _):
            request = helper._request(url)
            with fixture._offline_runtime_environment(helper.native_environment, url, zero_backoff=True) as fences:
                terminal = caller.execute(request)
            self.assertTrue(fences)
            self.assertEqual(len(calls), 4, {key: terminal.get(key) for key in (
                "status", "first_typed_blocker", "cleanup_pass", "provider_failure_diagnostic")})
            self.assertEqual(errors, [])
        self.assertEqual(terminal["status"], "BLOCKED")
        self.assertEqual(terminal["first_typed_blocker"], "NOKIY_EMBEDDED_PROVIDER_EXECUTION_FAILED")
        self.assertTrue(terminal["cleanup_pass"])
        events = [json.loads(line) for line in (request.artifact_root / request.request_id / "core.jsonl").read_bytes().splitlines()]
        failures = [r for r in events if r.get("type") == "turn.completed" and r.get("status") == "failed"]
        self.assertEqual(len(failures), 1)
        self.assertIn("fixture_provider_error", failures[0]["error"])
        self.assertIn("local failure detail", failures[0]["error"])
        self.assertEqual(failures[0]["usage"]["model_usage_coverage"]["status"], "incomplete")
        self.assertFalse((helper.scratch / "runs").exists())
        self.assertEqual((Path(request.workspace) / "answer.txt").read_text(), "broken\n")
