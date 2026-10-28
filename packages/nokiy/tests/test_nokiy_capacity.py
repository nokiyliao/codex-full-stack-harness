"""Bounded Ultra capacity and scheduling; no provider calls."""
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


class _CapacityFixture(unittest.TestCase):
    write_scopes = fixtures.BatchFixture.write_scopes
    save_plan = fixtures.BatchFixture.save_plan
    mocks = fixtures.BatchFixture.mocks
    terminal = staticmethod(fixtures.BatchFixture.terminal)

    def setUp(self):
        fixtures.BatchFixture.setUp(self)
        self.config = self.plan.parent / "config.toml"
        self.enterContext(patch.dict(os.environ, {"CODEX_HOME": str(self.plan.parent)}))

    def set_parallel(self, value):
        document = json.loads(self.plan.read_text())
        document["max_parallel_workers"] = value
        self.plan.write_text(json.dumps(document))
        self.digest = hashlib.sha256(self.plan.read_bytes()).hexdigest()


class CapacityTests(_CapacityFixture):
    member_count = 5

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
        self.config.write_text("[agents]\nmax_concurrent_threads_per_session=5\n")
        self.assertEqual(batch.worker_capacity(), 5)
        barrier = threading.Barrier(5)

        def execute(request):
            barrier.wait(timeout=3)
            return self.terminal(request)

        _, execution = self.mocks(execute=execute)
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
            for text, expected in [("", 20), ("[agents]", 20),
                    ("[agents]\nmax_threads=3", 3),
                    ("[agents]\nmax_concurrent_threads_per_session=5", 5),
                    ("[agents]\nmax_threads=20", 20),
                    ("[agents]\nmax_concurrent_threads_per_session=20", 20),
                    ("[agents]\nmax_threads=3\nmax_concurrent_threads_per_session=20", 20),
                    ("[agents]\nenabled=false", 1)]:
                config.write_text(text)
                with patch.dict(os.environ, {"CODEX_HOME": root}):
                    self.assertEqual(batch.worker_capacity(), expected)
            for setting in ("max_threads", "max_concurrent_threads_per_session"):
                for value in ("true", "0", "-1", "21", "64", "1.5", '"5"'):
                    with self.subTest(setting=setting, value=value):
                        config.write_text(f"[agents]\n{setting}={value}")
                        with self.assertRaisesRegex(EmbeddedNokiyError, "1 through 20"):
                            batch.worker_capacity(config)


class Capacity20Tests(_CapacityFixture):
    member_count = 20

    def assert_parallel_schedule(self, parallel):
        lock = threading.Lock()
        barrier = threading.Barrier(parallel)
        active = maximum = 0

        def execute(request):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                barrier.wait(timeout=10)
            finally:
                with lock:
                    active -= 1
            return self.terminal(request)

        preparation, execution = self.mocks(execute=execute)
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertEqual(preparation.call_count, self.member_count)
        self.assertEqual(execution.call_count, self.member_count)
        self.assertEqual(maximum, parallel)
        self.assertEqual(active, 0)
        expected = [request.request_id for request in self.requests]
        self.assertEqual([row["request_id"] for row in result["members"]], expected)
        self.assertCountEqual([call.args[0].request_id for call in execution.call_args_list], expected)
        return execution

    def test_twenty_workers_are_concurrent_with_omitted_config_and_parallelism(self):
        self.assertFalse(self.config.exists())
        self.assertNotIn("max_parallel_workers", json.loads(self.plan.read_text()))
        self.assertEqual(batch.worker_capacity(), 20)
        self.assert_parallel_schedule(20)

    def test_explicit_twenty_workers_are_scheduled(self):
        self.config.write_text("[agents]\nmax_concurrent_threads_per_session=20\n")
        self.set_parallel(20)
        self.assert_parallel_schedule(20)

    def test_explicit_one_queues_all_twenty_members(self):
        self.set_parallel(1)
        execution = self.assert_parallel_schedule(1)
        self.assertEqual([call.args[0].request_id for call in execution.call_args_list],
                         [request.request_id for request in self.requests])

    def test_explicit_two_queues_all_twenty_members(self):
        self.set_parallel(2)
        self.assert_parallel_schedule(2)

    def test_invalid_selection_rejected_before_preflight(self):
        preparation, execution = self.mocks()
        for value in (0, -1, 21, True, "20", None, 20.5):
            with self.subTest(value=value):
                self.set_parallel(value)
                with self.assertRaisesRegex(EmbeddedNokiyError, "max_parallel_workers"):
                    batch.run_batch(self.plan, self.digest)
        preparation.assert_not_called()
        execution.assert_not_called()

    def test_configured_lower_five_rejects_twenty_even_with_serial_selection(self):
        self.config.write_text("[agents]\nmax_concurrent_threads_per_session=5\n")
        self.set_parallel(1)
        preparation, execution = self.mocks()
        with self.assertRaisesRegex(EmbeddedNokiyError, "configured worker capacity is 5"):
            batch.run_batch(self.plan, self.digest)
        preparation.assert_not_called()
        execution.assert_not_called()

    def test_capacity_is_rechecked_after_preflight_before_execution(self):
        preparation, execution = self.mocks()
        with patch.object(batch, "worker_capacity", side_effect=(20, 5)):
            with self.assertRaisesRegex(EmbeddedNokiyError, "current configured worker capacity"):
                batch.run_batch(self.plan, self.digest)
        self.assertEqual(preparation.call_count, 20)
        execution.assert_not_called()

    def test_run_appearing_during_preflight_recovers_before_capacity_recheck(self):
        def preflight(request):
            if request is self.requests[0]:
                (request.artifact_root / request.request_id).mkdir()
            return {"status": "READY"}

        preparation, execution = self.mocks(preflight=preflight)
        with patch.object(batch, "worker_capacity", side_effect=(
                20, AssertionError("recovery is not admission"))) as capacity:
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(capacity.call_count, 1)
        self.assertEqual(preparation.call_count, 20)
        execution.assert_not_called()

    def test_long_twenty_member_plan_over_old_byte_limit_is_scheduled(self):
        long_root = self.plan.parent / ("long-" + "p" * 120)
        long_root.mkdir()
        for member, request in zip(self.members, self.requests):
            path = Path(member["request_path"])
            path = path.rename(long_root / path.name)
            root = request.artifact_root.rename(long_root / request.artifact_root.name)
            member.update(request_path=str(path), artifact_root=str(root))
            request.artifact_root = root
        self.save_plan()
        self.assertGreater(len(self.plan.read_bytes()), 8192)
        self.assertLess(len(self.plan.read_bytes()), 32768)
        self.assert_parallel_schedule(20)

    def test_plan_byte_limit_accepts_32768_and_rejects_32769_before_preflight(self):
        raw = self.plan.read_bytes()
        self.assertLess(len(raw), 32768)
        raw = raw.ljust(32768, b" ")
        self.plan.write_bytes(raw)
        self.digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(len(batch.load_plan(self.plan, self.digest)), 20)
        raw += b" "
        self.plan.write_bytes(raw)
        self.digest = hashlib.sha256(raw).hexdigest()
        preparation, execution = self.mocks()
        with self.assertRaisesRegex(EmbeddedNokiyError, "byte limit"):
            batch.run_batch(self.plan, self.digest)
        preparation.assert_not_called()
        execution.assert_not_called()


class Capacity21Tests(_CapacityFixture):
    member_count = 21

    def test_twenty_one_members_rejected_before_preflight_at_any_parallelism(self):
        self.assertEqual(batch.worker_capacity(), 20)
        preparation, execution = self.mocks()
        for parallel in (None, 1, 2, 20, 21):
            with self.subTest(parallel=parallel):
                if parallel is not None:
                    self.set_parallel(parallel)
                with self.assertRaisesRegex(EmbeddedNokiyError, "21 members.*capacity is 20"):
                    batch.run_batch(self.plan, self.digest)
        preparation.assert_not_called()
        execution.assert_not_called()


class HistoricalCapacityTests(_CapacityFixture):
    member_count = 64

    def test_historical_sixty_four_member_plan_recovers_before_current_capacity(self):
        self.config.write_text("[agents]\nmax_concurrent_threads_per_session=5\n")
        request = self.requests[0]
        (request.artifact_root / request.request_id).mkdir()
        for member in self.members:
            Path(member["request_path"]).unlink()
        preparation, execution = self.mocks()
        with patch.object(batch, "worker_capacity", side_effect=AssertionError("recovery is not admission")):
            result = batch.run_batch(self.plan, self.digest)
            recovered = batch.read_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(len(result["members"]), 64)
        self.assertEqual(len(recovered["members"]), 64)
        preparation.assert_not_called()
        execution.assert_not_called()
