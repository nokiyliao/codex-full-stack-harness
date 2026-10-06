# SPDX-License-Identifier: MIT
"""Bounded diagnostic recovery with mocked execution; no provider or processes."""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core as core


class PhaseTimingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        contract = self.root / "contract.json"
        contract.write_text("{}")
        identity = caller.FileIdentity(
            path=contract, sha256=hashlib.sha256(contract.read_bytes()).hexdigest())
        self.request = caller.EmbeddedNokiyRequest(
            runtime_image=identity, codex=identity, context_capsule=identity,
            jspace_contract=identity, artifact_root=self.root,
            workspace=self.root, prompt="synthetic", timeout_seconds=1,
            max_context_age_seconds=60,
            max_trajectory_bytes=65536, max_result_bytes=4096, require_tool_call=False,
            execution_profile="direct", native_thread_id="synthetic-thread",
            persistence_mode="native_codex_thread_only",
            model="requested-model", reasoning_effort="low",
            model_acceleration=False, allow_provider_network=False, authority_effect="none",
            terminal_delivery="assistant_reply")
        self.run = self.root / self.request.request_id
        self.runtime = SimpleNamespace(image_sha256="b" * 64, build_identity="synthetic",
            artifacts={name: SimpleNamespace(path=Path(name)) for name in
                       ("tura_router", "tura_session_db", "tura_exec")})

    def _progress(self, phases):
        clock = iter(i / 100 for i in range(1, len(phases) + 1))
        with patch.object(core.time, "monotonic", side_effect=lambda: next(clock)):
            timing = core._PhaseTiming(self.request, self.run, 0.0)
            for phase in phases:
                timing.observe(phase)
        return timing.observation

    def _execute(self, phases, failure=None, malformed_supervision=False):
        observation = None

        def execute(*args, **kwargs):
            nonlocal observation
            observation = self._progress(phases)
            engine = {} if failure else {
                "turn_completed": True,
                "final_text": "synthetic result", "phase_timing_observation": observation,
                "phase_timings_ms": core._phase_intervals(observation)}
            if not failure:
                core_path = self.run / "core.jsonl"
                core_path.write_text(json.dumps({
                    "type": "turn.completed", "status": "completed",
                    **core._terminal_identity(self.request)}) + "\n")
                engine["trajectory_artifact"] = core._record_artifact(core_path)
            outcome = {"result": engine, "failure": failure,
                       "scope": {"engine_reaped": True, "no_live_descendants": True}}
            kwargs["stdout_path"].write_text("{" if malformed_supervision else json.dumps(outcome))
            return (2 if failure else 0, 1.0, failure, None)

        with patch.object(caller, "_verify_native_thread_binding"), \
                patch.object(core, "prepare", return_value=(
                    {"status": "READY", "requested_service_tier": "default"}, self.runtime, {}, {})), \
                patch.object(caller, "_run_process", side_effect=execute) as runner, \
                patch("codex_collaboration_harness.source_excerpt.verify_context_excerpt", return_value=None):
            receipt = core.execute_full_core(self.request)
        runner.assert_called_once()
        self.assertEqual(json.loads((self.run / "terminal.json").read_text()), receipt)
        return receipt, observation

    def test_success_preserves_request_bound_observed_intervals_in_terminal(self):
        receipt, observation = self._execute(core._PHASE_CHECKPOINTS[1:])
        self.assertEqual(receipt["status"], "RESULT_AVAILABLE")
        self.assertEqual(receipt["phase_timing_observation"], observation)
        self.assertEqual(observation["request_sha256"], self.request.request_sha256)
        phases = receipt["phase_timings_ms"]
        self.assertEqual(phases, core._phase_intervals(observation))
        self.assertTrue(all(type(value) is int and value >= 0 for value in phases.values()))
        self.assertEqual(sum(value for name, value in phases.items() if name != "engine_total"),
                         phases["engine_total"])
        self.assertTrue(receipt["cleanup_pass"])
        self.assertIsNone(receipt["observed_model"])
        self.assertIsNone(receipt["observed_service_tier"])
        with patch.object(caller, "read_terminal", return_value=receipt) as read, \
                patch.object(caller, "_verify_native_thread_binding"), \
                patch.object(caller, "_run_process") as runner:
            self.assertEqual(core.execute_full_core(self.request), receipt)
        read.assert_called_once_with(self.root, self.request.request_id)
        runner.assert_not_called()

    def test_timeout_recovers_only_completed_intervals_without_engine_output(self):
        receipt, observation = self._execute(core._PHASE_CHECKPOINTS[1:5], "SYNTHETIC_TIMEOUT")
        self.assertEqual(receipt["status"], "BLOCKED")
        self.assertEqual(receipt["first_typed_blocker"], "SYNTHETIC_TIMEOUT")
        self.assertEqual(receipt["phase_timing_observation"], observation)
        self.assertEqual(receipt["phase_timings_ms"]["cli_launch"], 10)
        for name in ("cli_process", "trajectory_parse", "cleanup", "engine_total"):
            self.assertIsNone(receipt["phase_timings_ms"][name])
        # Supervisor cleanup proof must not manufacture an engine cleanup interval.
        self.assertTrue(receipt["cleanup_pass"])

    def test_partial_supervision_does_not_replace_timeout_or_lose_progress(self):
        receipt, observation = self._execute(["prepare"], "SYNTHETIC_TIMEOUT", True)
        self.assertEqual(receipt["first_typed_blocker"], "SYNTHETIC_TIMEOUT")
        self.assertEqual(receipt["phase_timing_observation"], observation)
        self.assertFalse(receipt["cleanup_pass"])

    def test_publication_failure_keeps_previous_snapshot_and_never_retries(self):
        self.run.mkdir()
        timing = core._PhaseTiming(self.request, self.run, core.time.monotonic())
        previous = (self.run / "phase-timing.json").read_bytes()
        with patch.object(core.os, "replace", side_effect=OSError("diagnostic failure")) as replace:
            timing.observe("prepare")
            timing.observe("session_db_ready")
        replace.assert_called_once()
        self.assertEqual((self.run / "phase-timing.json").read_bytes(), previous)
        self.assertLessEqual((self.run / "phase-timing.pending").stat().st_size,
                             core.MAX_PHASE_TIMING_BYTES)
        recovered = core._recover_phase_timing(self.run, self.request, {})
        self.assertEqual(recovered["checkpoints_ms"], {"engine_started": 0})

    def test_recovery_rejects_foreign_malformed_oversized_and_nonregular_evidence(self):
        self.run.mkdir()
        observation = self._progress(["prepare"])
        path = self.run / "phase-timing.json"
        bad_values = [dict(observation, request_id="foreign"),
                      dict(observation, request_sha256="c" * 64),
                      dict(observation, schema_version="unknown")]
        bad_values.extend(dict(observation, checkpoints_ms=marks) for marks in (
            {}, {"engine_started": False}, {"engine_started": 0, "prepare": -1},
            {"engine_started": 0, "router_ready": 10},
            {"engine_started": 0, "prepare": 10, "session_db_ready": 9},
            {"engine_started": 0, "cleanup": 10},
            {"engine_started": 0, "invented": 10}))
        payloads = [json.dumps(value) for value in bad_values]
        payloads += ["{", "[]", '{"request_id":1,"request_id":2}',
                     " " * (core.MAX_PHASE_TIMING_BYTES + 1), "[" * 2000]
        for payload in payloads:
            with self.subTest(payload=payload[:80]):
                path.write_text(payload)
                self.assertIsNone(core._recover_phase_timing(self.run, self.request, {}))
        path.unlink()
        (self.run / "phase-timing.pending").write_text(json.dumps(observation))
        self.assertIsNone(core._recover_phase_timing(self.run, self.request, {}))
        path.symlink_to(self.run / "phase-timing.pending")
        self.assertIsNone(core._recover_phase_timing(self.run, self.request, {}))
        path.unlink()
        path.mkdir()
        self.assertIsNone(core._recover_phase_timing(self.run, self.request, {}))
        self.assertTrue(all(value is None for value in core._phase_intervals(None).values()))

    def test_prepare_failure_persists_without_claiming_prepare_completed(self):
        self.run.mkdir()
        spec = self.run / "execution.json"
        spec.write_text('{"request":{}}')
        original = caller.EmbeddedNokiyError("SYNTHETIC_PREPARE_FAILED", "fixture")
        with patch.object(caller, "decode_request", return_value=self.request), \
                patch.object(core, "prepare", side_effect=original), \
                patch.object(core.subprocess, "Popen") as spawn:
            with self.assertRaises(caller.EmbeddedNokiyError) as caught:
                core._engine(spec)
        self.assertIs(caught.exception, original)
        spawn.assert_not_called()
        recovered = core._recover_phase_timing(self.run, self.request, {})
        phases = core._phase_intervals(recovered)
        self.assertIsNone(phases["prepare"])
        self.assertIsNotNone(phases["cleanup"])

    def test_owned_cleanup_continues_without_masking_first_failure(self):
        self.run.mkdir()
        spec = self.run / "execution.json"
        spec.write_text('{"request":{}}')
        (self.run / "prompt.txt").write_text("synthetic")
        db = Mock(returncode=None)
        router = Mock(returncode=None)
        cli = Mock(returncode=2)
        db.poll.return_value = router.poll.return_value = None
        cli.poll.return_value = 2
        router.terminate.side_effect = OSError("synthetic cleanup failure")

        def spawn(argv, **kwargs):
            name = argv[0]
            if name in ("tura_session_db", "tura_router"):
                endpoints = self.run / "execution-state/session_log"
                endpoints.mkdir(exist_ok=True)
                marker = "service.addr" if name == "tura_session_db" else "router.addr"
                (endpoints / marker).write_text('{"addr":"127.0.0.1:12345"}')
                return db if name == "tura_session_db" else router
            return cli

        with patch.object(caller, "decode_request", return_value=self.request), \
                patch.object(core, "prepare", return_value=({"status": "READY"}, self.runtime, {}, {})), \
                patch.object(core, "_environment", return_value={}), \
                patch.object(core, "_verifier_fd", return_value=()), \
                patch.object(core, "_verify_service_owner"), \
                patch.object(core, "_cli_argv", return_value=["tura_exec"]), \
                patch.object(core.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=b"")), \
                patch.object(core.subprocess, "Popen", side_effect=spawn), \
                patch.object(core, "_events") as events:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "PROVIDER_EXECUTION_FAILED"):
                core._engine(spec)
        events.assert_not_called()
        router.terminate.assert_called_once()
        db.terminate.assert_called_once()
        db.wait.assert_called_once_with(timeout=5)
        phases = core._phase_intervals(core._recover_phase_timing(self.run, self.request, {}))
        self.assertIsNotNone(phases["cli_process"])
        self.assertIsNone(phases["trajectory_parse"])
        self.assertIsNone(phases["cleanup"])
        self.assertIsNone(phases["engine_total"])


if __name__ == "__main__":
    unittest.main()
