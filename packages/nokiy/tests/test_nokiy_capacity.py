"""Configured Ultra capacity; no provider calls."""
import os
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from codex_collaboration_harness import batch
from codex_collaboration_harness.embedded_nokiy import EmbeddedNokiyError
import test_nokiy_batch as fixtures


class CapacityTests(unittest.TestCase):
    member_count = 5
    setUp = fixtures.BatchFixture.setUp
    write_scopes = fixtures.BatchFixture.write_scopes
    save_plan = fixtures.BatchFixture.save_plan
    mocks = fixtures.BatchFixture.mocks
    terminal = staticmethod(fixtures.BatchFixture.terminal)

    def set_parallel(self, value):
        document = json.loads(self.plan.read_text())
        document["max_parallel_workers"] = value
        self.plan.write_text(json.dumps(document))
        self.digest = hashlib.sha256(self.plan.read_bytes()).hexdigest()

    def test_coordinator_selects_two_workers_for_five_members(self):
        self.set_parallel(2)
        lock = threading.Lock()
        pair = threading.Barrier(2)
        active = maximum = started = 0

        def execute(request):
            nonlocal active, maximum, started
            with lock:
                active += 1
                started += 1
                position = started
                maximum = max(maximum, active)
            if position <= 4:
                pair.wait(timeout=3)
            with lock:
                active -= 1
            return self.terminal(request)

        _, execution = self.mocks(execute=execute)
        with patch.object(batch, "worker_capacity", return_value=5):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertEqual(maximum, 2)
        self.assertEqual(execution.call_count, 5)
        self.assertEqual([row["request_id"] for row in result["members"]],
                         [request.request_id for request in self.requests])

    def test_coordinator_can_select_serial_execution(self):
        self.set_parallel(1)
        order = []
        self.mocks(execute=lambda request: order.append(request.request_id) or self.terminal(request))
        with patch.object(batch, "worker_capacity", return_value=5):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertEqual(order, [request.request_id for request in self.requests])

    def test_invalid_selection_rejected_before_preflight(self):
        preparation, execution = self.mocks()
        for value in (0, -1, 6, True, "2", None, 1.5):
            self.set_parallel(value)
            with self.assertRaisesRegex(EmbeddedNokiyError, "max_parallel_workers"):
                batch.run_batch(self.plan, self.digest)
        preparation.assert_not_called()
        execution.assert_not_called()

    def test_selection_is_bound_to_original_plan_digest(self):
        original = self.digest
        self.set_parallel(2)
        preparation, execution = self.mocks()
        with self.assertRaisesRegex(EmbeddedNokiyError, "digest"):
            batch.run_batch(self.plan, original)
        preparation.assert_not_called()
        execution.assert_not_called()

    def test_selected_parallelism_preserves_result_only_reentry(self):
        self.set_parallel(2)
        request = self.requests[0]
        (request.artifact_root / request.request_id).mkdir()
        for member in self.members:
            Path(member["request_path"]).unlink()
        _, execution = self.mocks()
        with patch.object(batch, "worker_capacity", side_effect=AssertionError("recovery is not admission")):
            result = batch.run_batch(self.plan, self.digest)
            batch.read_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        execution.assert_not_called()

    def test_five_workers_are_concurrent_not_queued_in_pairs(self):
        barrier = threading.Barrier(5)

        def execute(request):
            barrier.wait(timeout=3)
            return self.terminal(request)

        _, execution = self.mocks(execute=execute)
        with patch.object(batch, "worker_capacity", return_value=5):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertEqual(execution.call_count, 5)

    def test_lower_capacity_rejects_before_preflight(self):
        preparation, execution = self.mocks()
        with patch.object(batch, "worker_capacity", return_value=4):
            with self.assertRaisesRegex(EmbeddedNokiyError, "capacity"):
                batch.run_batch(self.plan, self.digest)
        preparation.assert_not_called()
        execution.assert_not_called()

    def test_configured_capacity_and_legacy_alias(self):
        with tempfile.TemporaryDirectory() as root:
            config = Path(root) / "config.toml"
            for text, expected in [("[agents]\nmax_threads=3", 3),
                    ("[agents]\nmax_concurrent_threads_per_session=5", 5),
                    ("[agents]\nenabled=false", 1)]:
                config.write_text(text)
                with patch.dict(os.environ, {"CODEX_HOME": root}):
                    self.assertEqual(batch.worker_capacity(), expected)
            config.write_text("[agents]\nmax_threads=true")
            with self.assertRaises(EmbeddedNokiyError):
                batch.worker_capacity(config)
