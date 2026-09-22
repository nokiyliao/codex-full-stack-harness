"""Real supervised fixture processes; no broker or system service manager."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_collaboration_harness import deployment as d
from codex_collaboration_harness import embedded_nokiy as c

THREAD = "019fd83e-861a-7b62-a628-0e0ad2f88a27"
SCRIPT = '''import json, os, sys, time
from pathlib import Path
stage = sys.argv[1]
p = json.loads(Path("plan.json").read_text())
result = {key:p[key] for key in ("action_id", "target", "release")}
result["plan_sha256"] = os.environ["NOKIY_DEPLOYMENT_PLAN_SHA256"]
mode = Path("mode").read_text()
if stage == "preflight":
    result["admitted"] = mode != "deny" and Path("version").read_text() == "v1"
    if mode == "drift": Path("grant.txt").write_text("revoked")
elif stage == "apply":
    # Real installers must perform their own fresh authority/CAS checks under lock.
    assert Path("version").read_text() == "v1"
    Path("version").write_text("v2")
    with Path("effects").open("a") as f: f.write("apply\\n")
    if mode == "timeout": time.sleep(30)
    if mode == "fail": sys.exit(7)
    if mode == "leak":
        import subprocess
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
elif stage == "verify":
    result.update(verified=True, healthy=mode != "unhealthy", observed_release=Path("version").read_text())
    if mode == "wrong-release": result["observed_release"] = "v1"
print(json.dumps(result))
'''


def ident(path):
    return {"path": str(path.resolve()), "sha256": c._file_sha256(path)}


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = root = Path(temp.name).resolve()
        environment = patch.dict("os.environ", {"CODEX_THREAD_ID": THREAD})
        environment.start()
        self.addCleanup(environment.stop)
        (root / "artifacts").mkdir()
        (root / "deploy.py").write_text(SCRIPT)
        (root / "grant.txt").write_text("Parent authorizes fixture-service v1 to v2 only; no external services.")
        (root / "version").write_text("v1")
        (root / "mode").write_text("ok")
        python = Path(sys.executable).resolve()
        self.plan = {"schema_version": d.SCHEMA, "action_id": "fixture-v1-v2", "native_thread_id": THREAD,
            "workspace": str(root), "artifact_root": str(root / "artifacts"),
            "target": "fixture-service", "release": "v2", "expires_at": time.time() + 120,
            "authorization_ref": ident(root / "grant.txt"),
            "commands": {stage: {"argv": [str(python), "-B", str(root / "deploy.py"), stage],
                                  "files": [ident(python), ident(root / "deploy.py")], "timeout_seconds": 10}
                         for stage in d.STAGES}}
        self.path = root / "plan.json"
        self.save()

    def save(self):
        self.path.write_text(json.dumps(self.plan))
        return c._canonical_sha256(self.plan)

    def execute(self):
        return d.execute(self.path, self.save())

    def rejected(self, code):
        with self.assertRaisesRegex(c.EmbeddedNokiyError, code):
            self.execute()
        self.assertEqual((self.root / "version").read_text(), "v1")

    def test_exact_execution_and_idempotent_readback(self):
        ready = d.execute(self.path, self.save(), check_only=True)
        self.assertEqual(ready["status"], "READY")
        self.assertFalse(ready["domain_admission_checked"])
        self.assertFalse((self.root / "effects").exists())
        result = self.execute()
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertTrue(result["cleanup_pass"])
        self.assertEqual(result["execution_boundary"], "parent_task_environment_no_added_seatbelt")
        self.assertTrue(all(row["scope"]["sandbox_applied"] is False for row in result["stages"]))
        self.assertFalse(result["provider_execution_started"])
        self.assertEqual((self.root / "version").read_text(), "v2")
        (self.root / "deploy.py").unlink()
        self.assertEqual(self.execute(), result)
        self.assertEqual(d.read_result(self.root / "artifacts", self.plan["action_id"]), result)
        self.assertEqual((self.root / "effects").read_text(), "apply\n")

    def test_wrong_parent(self):
        self.plan["native_thread_id"] = "019fd83e-861a-7b62-a628-0e0ad2f88a28"
        self.rejected("PARENT_THREAD_MISMATCH")

    def test_expiry(self):
        self.plan["expires_at"] = 1
        self.rejected("AUTHORIZATION_EXPIRED")

    def test_inline_shell(self):
        self.plan["commands"]["apply"]["argv"] = ["/bin/sh", "-c", "echo bad"]
        self.rejected("PINNED_ENTRYPOINT_REQUIRED")

    def test_unbound_script(self):
        self.plan["commands"]["apply"]["files"] = self.plan["commands"]["apply"]["files"][:1]
        self.rejected("PINNED_SCRIPT_REQUIRED")

    def test_timeout_type(self):
        self.plan["commands"]["apply"]["timeout_seconds"] = True
        self.rejected("TIMEOUT_INVALID")

    def test_missing_verifier(self):
        del self.plan["commands"]["verify"]
        self.rejected("THREE_STAGES_REQUIRED")

    def test_path_escape(self):
        self.plan["action_id"] = "../escape"
        self.rejected("PLAN_ID_INVALID")

    def test_source_drift(self):
        (self.root / "deploy.py").write_text("changed")
        self.rejected("IDENTITY_DRIFT")

    def test_grant_drift(self):
        (self.root / "grant.txt").write_text("changed")
        self.rejected("IDENTITY_DRIFT")

    def test_approval_hash_not_recomputed(self):
        digest = self.save()
        self.plan["release"] = "unapproved"
        self.save()
        with self.assertRaisesRegex(c.EmbeddedNokiyError, "APPROVAL_DIGEST_MISMATCH"):
            d.execute(self.path, digest)

    def failure(self, mode, status):
        (self.root / "mode").write_text(mode)
        if mode == "timeout": self.plan["commands"]["apply"]["timeout_seconds"] = 1
        result = self.execute()
        self.assertEqual(result["status"], status, result)
        self.assertTrue(result["cleanup_pass"], result)
        self.assertEqual(self.execute(), result)
        self.assertEqual((self.root / "effects").exists(), mode != "deny")
        if mode != "deny": self.assertEqual((self.root / "effects").read_text(), "apply\n")

    def test_admission_denial(self): self.failure("deny", "BLOCKED_BEFORE_APPLY")
    def test_apply_failure(self): self.failure("fail", "EFFECT_UNCERTAIN")
    def test_health_failure(self): self.failure("unhealthy", "EFFECT_UNCERTAIN")
    def test_timeout(self): self.failure("timeout", "EFFECT_UNCERTAIN")
    def test_wrong_observed_version(self): self.failure("wrong-release", "EFFECT_UNCERTAIN")
    def test_no_added_sandbox_process_is_spawned(self):
        original = subprocess.Popen
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(subprocess, "Popen", wraps=original) as spawned:
            result = self.execute()
        self.assertEqual(result["status"], "VERIFIED")
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        commands = [call.args[0] for call in spawned.call_args_list]
        self.assertTrue(commands)
        self.assertTrue(all(Path(command[0]).name != "sandbox-exec" for command in commands))

    def test_authority_rechecked_before_apply(self):
        (self.root / "mode").write_text("drift")
        result = self.execute()
        self.assertEqual(result["status"], "BLOCKED_BEFORE_APPLY")
        self.assertIn("IDENTITY_DRIFT", result["first_typed_blocker"])
        self.assertFalse((self.root / "effects").exists())

    def test_symlink_invocation_preserved_with_resolved_identity(self):
        alias = self.root / "python"
        alias.symlink_to(Path(sys.executable).resolve())
        for spec in self.plan["commands"].values():
            spec["argv"][0] = str(alias)
            spec["files"][0] = {"path": str(alias), "realpath": str(alias.resolve()), "sha256": c._file_sha256(alias)}
        self.assertEqual(self.execute()["status"], "VERIFIED")

    def test_unbound_symlink_rejected(self):
        alias = self.root / "grant-alias"
        alias.symlink_to(self.root / "grant.txt")
        self.plan["authorization_ref"]["path"] = str(alias)
        self.rejected("RESOLVED_IDENTITY_REQUIRED")

    def test_uncertain_attempt_and_conflicting_plan(self):
        run = self.root / "artifacts" / ("deployment-" + self.plan["action_id"])
        run.mkdir()
        c._write_create_only(run / "plan.json", self.plan)
        self.rejected("UNCERTAIN_PRIOR_ATTEMPT")
        self.plan["release"] = "v3"
        self.rejected("ACTION_ID_CONFLICT")

    def test_cancel_after_effect(self):
        (self.root / "mode").write_text("timeout")
        self.plan["commands"]["apply"]["timeout_seconds"] = 30
        process = subprocess.Popen([sys.executable, "-m", "codex_collaboration_harness.embedded_nokiy", "deploy",
            "--plan", str(self.path), "--approved-plan-sha256", self.save()], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not (self.root / "effects").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((self.root / "effects").exists())
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=20)
            result = json.loads(stdout)
            self.assertEqual(result["status"], "EFFECT_UNCERTAIN", (result, stderr))
            self.assertEqual(process.returncode, 2)
            self.assertEqual(d.read_result(self.root / "artifacts", self.plan["action_id"]), result)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    def test_terminal_write_failure(self):
        original = c._write_create_only
        def write(path, value):
            if path.name == "terminal.json": raise OSError("disk full")
            original(path, value)
        with patch.object(c, "_write_create_only", write):
            result = self.execute()
        self.assertEqual(result["status"], "EFFECT_UNCERTAIN")
        self.assertTrue(result["apply_attempted"])
        with self.assertRaisesRegex(c.EmbeddedNokiyError, "UNCERTAIN_PRIOR_ATTEMPT"):
            self.execute()


if __name__ == "__main__":
    unittest.main()
