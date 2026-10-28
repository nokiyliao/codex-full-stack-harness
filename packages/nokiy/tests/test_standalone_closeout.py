"""Source-only closeout bypasses deployment and models, not ownership checks."""
import json
import io
from contextlib import redirect_stdout
import unittest
from unittest.mock import patch

from codex_collaboration_harness import deployment as d
from codex_collaboration_harness import embedded_nokiy as c
import test_task_closeout as fixture


class StandaloneCloseoutTests(unittest.TestCase):
    def setUp(self):
        fixture.CloseoutTests.setUp(self)
        self.plan["schema_version"] = d.CLOSEOUT_SCHEMA
        for field in ("commands", "target", "release"):
            del self.plan[field]

    def execute(self, *, check_only=False):
        self.path.write_text(json.dumps(self.plan))
        with (patch.object(d, "run_stages", side_effect=AssertionError("No deployment stages")),
              patch("codex_collaboration_harness.full_stack.query_surface_candidates",
                    side_effect=AssertionError("Closeout must not require a DCF generation")),
              patch("codex_collaboration_harness.full_stack.prepare_request",
                    side_effect=AssertionError("Closeout must not prepare a worker"))):
            return d.execute_closeout(self.path, c._canonical_sha256(self.plan), check_only=check_only)

    def test_check_then_closeout_once_and_expired_historical_read(self):
        self.assertEqual(self.execute(check_only=True)["status"], "READY")
        self.assertFalse((self.root / "order").exists())
        result = self.execute()
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertEqual(result["schema_version"], d.CLOSEOUT_TERMINAL)
        self.assertTrue(result["cleanup_pass"])
        self.assertFalse(result["provider_execution_started"])
        self.assertNotIn("deployment_verified", result)
        self.assertEqual((self.root / "order").read_text(), "closeout\n")
        self.assertFalse((self.root / "leases/active.json").exists())
        self.assertEqual(result["lease_closeout"]["receipt_sha256"],
                         c._file_sha256(self.root / "leases/complete.json"))
        (self.root / "finish.py").unlink()
        (self.root / "grant").unlink()
        with patch.object(d.time, "time", return_value=self.plan["expires_at"] + 1):
            self.assertEqual(self.execute(), result)
            self.assertEqual(d.read_result(self.root / "artifacts", self.plan["action_id"],
                                           closeout_only=True), result)
        self.assertEqual((self.root / "order").read_text(), "closeout\n")

    def test_missing_owner_not_filled_and_foreign_owner_rejected(self):
        lease = self.root / "leases/active.json"
        for owner in (None, "other"):
            data = {"task_id": self.plan["task_closeout"]["task_id"]}
            if owner is not None:
                data["thread_id"] = owner
            lease.write_text(json.dumps(data))
            self.plan["task_closeout"]["active_lease"] = fixture.ident(lease)
            with self.assertRaisesRegex(c.EmbeddedNokiyError, "CLOSEOUT_LEASE_OWNER_MISMATCH"):
                self.execute()
            self.assertEqual(json.loads(lease.read_text()), data)
            self.assertFalse((self.root / "order").exists())

    def test_digest_source_authorization_and_lease_drift_rejected(self):
        self.path.write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(c.EmbeddedNokiyError, "APPROVAL_DIGEST_MISMATCH"):
            d.execute_closeout(self.path, "0" * 64)
        for path in ("grant", "finish.py", "leases/active.json"):
            with self.subTest(path=path):
                file = self.root / path
                before = file.read_bytes()
                file.write_bytes(before + b"\n")
                with self.assertRaisesRegex(c.EmbeddedNokiyError, "IDENTITY_DRIFT|CLOSEOUT_LEASE_DRIFT"):
                    self.execute()
                file.write_bytes(before)
        with patch.object(d.time, "time", return_value=self.plan["expires_at"] + 1):
            with self.assertRaisesRegex(c.EmbeddedNokiyError, "AUTHORIZATION_EXPIRED"):
                self.execute()
        self.assertFalse((self.root / "order").exists())

    def test_uncertain_attempt_and_changed_plan_never_replayed(self):
        run = self.root / "artifacts" / ("closeout-" + self.plan["action_id"])
        run.mkdir()
        (run / "plan.json").write_text(json.dumps(self.plan))
        with self.assertRaisesRegex(c.EmbeddedNokiyError, "UNCERTAIN_PRIOR_ATTEMPT"):
            self.execute()
        self.plan["expires_at"] += 1
        with self.assertRaisesRegex(c.EmbeddedNokiyError, "ACTION_ID_CONFLICT"):
            self.execute()
        self.assertFalse((self.root / "order").exists())

    def test_cli_uncertain_original_result_does_not_claim_zero_effects(self):
        run = self.root / "artifacts" / ("closeout-" + self.plan["action_id"])
        run.mkdir()
        (run / "plan.json").write_text(json.dumps(self.plan))
        (run / "closeout.started.json").write_text("{}")
        self.path.write_text(json.dumps(self.plan))
        for args in (["closeout", "--plan", str(self.path), "--approved-plan-sha256",
                      c._canonical_sha256(self.plan)],
                     ["read-closeout-result", "--artifact-root", str(self.root / "artifacts"),
                      "--action-id", self.plan["action_id"]]):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(c.main(args), 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "EFFECT_UNCERTAIN", result)
            self.assertEqual(result["authority_effect"], "unknown")
            self.assertEqual(result["action_id"], self.plan["action_id"])
            self.assertEqual(result["artifact_root"], str(self.root / "artifacts"))
            self.assertFalse(result["automatic_retry_allowed"])
        self.assertFalse((self.root / "order").exists())

    def test_cli_uncertainty_survives_mutable_receipt_paths(self):
        run = self.root / "artifacts" / ("closeout-" + self.plan["action_id"])
        run.mkdir()
        (run / "plan.json").write_text(json.dumps(self.plan))
        (run / "closeout.started.json").write_text("{}")
        self.path.write_text(json.dumps(self.plan))
        completion = self.root / "leases/complete.json"
        completion.symlink_to(self.root / "missing")
        for removed_parent in (False, True):
            if removed_parent:
                completion.unlink()
                (self.root / "leases/active.json").unlink()
                completion.parent.rmdir()
            for args in (["closeout", "--plan", str(self.path), "--approved-plan-sha256",
                          c._canonical_sha256(self.plan)],
                         ["read-closeout-result", "--artifact-root", str(self.root / "artifacts"),
                          "--action-id", self.plan["action_id"]]):
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(c.main(args), 2)
                result = json.loads(output.getvalue())
                self.assertEqual(result["status"], "EFFECT_UNCERTAIN", result)
                self.assertEqual(result["authority_effect"], "unknown")
                self.assertFalse(result["automatic_retry_allowed"])

    def test_nonzero_and_receipt_mismatch_never_verified_or_retried(self):
        for mode in ("nonzero", "wrong-thread", "wrong-task", "wrong-commits", "lease-remains"):
            with self.subTest(mode=mode):
                case = StandaloneCloseoutTests()
                case.setUp()
                try:
                    (case.root / "finish-mode").write_text(mode)
                    result = case.execute()
                    self.assertEqual(result["status"], "EFFECT_UNCERTAIN", result)
                    self.assertTrue(result["closeout_attempted"])
                    self.assertIsNotNone(result["first_typed_blocker"])
                    self.assertEqual(case.execute(), result)
                    self.assertEqual((case.root / "order").read_text(), "closeout\n")
                finally:
                    case.doCleanups()

    def test_preexisting_receipt_blocks_before_effect(self):
        completion = self.root / "leases/complete.json"
        completion.write_text("{}")
        result = self.execute()
        self.assertEqual(result["status"], "BLOCKED_BEFORE_CLOSEOUT", result)
        self.assertIn("RECEIPT_ALREADY_EXISTS", result["first_typed_blocker"])
        self.assertFalse((self.root / "order").exists())

    def test_parent_retarget_during_consumption_blocks_before_effect(self):
        route = self.root / "route"
        route.symlink_to(self.root / "leases", target_is_directory=True)
        spec = self.plan["task_closeout"]
        spec["active_lease"] = fixture.ident(route / "active.json")
        spec["completion_path"] = str(route / "complete.json")
        other = self.root / "other"
        other.mkdir()
        save = d.save
        def retarget(path, value):
            save(path, value)
            if path.name == "closeout.paths.json":
                route.unlink()
                route.symlink_to(other, target_is_directory=True)
        with patch.object(d, "save", side_effect=retarget):
            result = self.execute()
        self.assertEqual(result["status"], "BLOCKED_BEFORE_CLOSEOUT", result)
        self.assertIn("CLOSEOUT_PARENT_RETARGETED", result["first_typed_blocker"])
        self.assertFalse((self.root / "order").exists())

    def test_supervision_and_terminal_durability_failure_are_uncertain(self):
        cleanup = {"process_stopped": False, "unsettled_descendant_pids": [123]}
        with patch.object(c, "_run_process", return_value=(0, 0.01, None, cleanup)):
            result = self.execute()
        self.assertEqual(result["status"], "EFFECT_UNCERTAIN", result)
        self.assertFalse(result["cleanup_pass"])
        self.assertEqual(self.execute(), result)
        case = StandaloneCloseoutTests()
        case.setUp()
        save = d.save
        def no_terminal(path, value):
            if path.name == "terminal.json":
                raise OSError("durability fixture")
            save(path, value)
        try:
            with patch.object(d, "save", side_effect=no_terminal):
                result = case.execute()
            self.assertEqual(result["status"], "EFFECT_UNCERTAIN", result)
            with self.assertRaisesRegex(c.EmbeddedNokiyError, "UNCERTAIN_PRIOR_ATTEMPT"):
                case.execute()
            self.assertEqual((case.root / "order").read_text(), "closeout\n")
        finally:
            case.doCleanups()

    def test_current_thread_and_source_mode_required(self):
        with patch.dict(d.os.environ, {"CODEX_THREAD_ID": "other"}):
            with self.assertRaisesRegex(c.EmbeddedNokiyError, "PARENT_THREAD_MISMATCH"):
                self.execute()
        self.plan["task_closeout"]["mode"] = "external_deployment"
        with self.assertRaises(c.EmbeddedNokiyError):
            self.execute()
        self.assertFalse((self.root / "order").exists())

    def test_cli_closeout_not_prepare_and_result_thread_binding(self):
        self.path.write_text(json.dumps(self.plan))
        with patch.object(d, "execute_closeout", return_value={"status": "READY"}) as execute:
            self.assertEqual(c.main(["check-closeout", "--plan", str(self.path),
                                     "--approved-plan-sha256", c._canonical_sha256(self.plan)]), 0)
            execute.assert_called_once_with(self.path, c._canonical_sha256(self.plan), check_only=True)
        self.execute()
        with patch.dict(d.os.environ, {"CODEX_THREAD_ID": "other"}):
            with self.assertRaisesRegex(c.EmbeddedNokiyError, "RESULT_IDENTITY_MISMATCH"):
                d.read_result(self.root / "artifacts", self.plan["action_id"], closeout_only=True)


if __name__ == "__main__":
    unittest.main()
