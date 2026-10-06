import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "paired_recovery_fixture.py"
SPEC = importlib.util.spec_from_file_location("paired_recovery_fixture", SCRIPT)
fixture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixture)
DARWIN_SANDBOX = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


class PairedRecoveryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name).resolve() / "pair"
        self.protocol = fixture.prepare(fixture.ROOT, self.output, "same-model", "high")
        self.protocol_sha256 = fixture.digest((self.output / "protocol.json").read_bytes())

    def _reviewed(self):
        return {name: fixture.bounded_file_digest(self.output / name, fixture.CORE)
                or fixture.BASE_CORE_SHA256 for name in ("native", "nokiy")}

    def _score(self):
        return fixture.score(self.output / "protocol.json", self.protocol_sha256,
                             Path(sys.executable), self._reviewed())

    def _supplemental(self, reviewed_sha256=None):
        source = self.output / "nokiy" / fixture.CORE
        return fixture.supplemental_functional(
            self.output / "protocol.json", self.protocol_sha256, Path(sys.executable),
            source, reviewed_sha256 or fixture.digest(source.read_bytes()))

    def test_prepare_uses_same_frozen_tree_without_oracle_or_history(self):
        self.assertEqual(self.protocol["base_commit"], fixture.BASE)
        self.assertEqual(self.protocol["requested_effort"], "high")
        for arm in ("native", "nokiy"):
            root = self.output / arm
            self.assertEqual(fixture.digest((root / fixture.CORE).read_bytes()),
                             fixture.BASE_CORE_SHA256)
            self.assertFalse((root / ".git").exists())
            self.assertFalse((root / "verifier").exists())
        self.assertFalse((self.output / "verifier").exists())

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_historical_fix_passes_heldout_oracle_and_base_does_not(self):
        fixed_core = fixture.pinned_blob(fixture.ROOT, fixture.FIX, fixture.CORE)
        (self.output / "nokiy" / fixture.CORE).write_bytes(fixed_core)
        report = self._score()
        self.assertFalse(report["pair_quality_pass"])
        self.assertTrue(report["results"]["native"]["original"]["passed"])
        self.assertFalse(report["results"]["native"]["heldout"]["passed"])
        self.assertTrue(report["results"]["nokiy"]["quality_pass"])
        self.assertEqual(report["usage"], "UNKNOWN")
        self.assertEqual(report["causal_gain"], "NOT_ESTABLISHED")

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_test_weakening_fails_scope_even_if_core_is_fixed(self):
        fixed_core = fixture.pinned_blob(fixture.ROOT, fixture.FIX, fixture.CORE)
        (self.output / "nokiy" / fixture.CORE).write_bytes(fixed_core)
        (self.output / "nokiy" / fixture.TEST).write_text("# weakened\n", encoding="utf-8")
        report = self._score()
        self.assertIsNone(report["results"]["nokiy"]["heldout"])
        self.assertFalse(report["results"]["nokiy"]["scope_pass"])
        self.assertFalse(report["results"]["nokiy"]["quality_pass"])

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_symlinked_candidate_source_is_rejected_before_execution(self):
        outside = self.output / "outside.py"
        outside.write_text("SECRET = 'outside'\n", encoding="utf-8")
        core = self.output / "native" / fixture.CORE
        core.unlink()
        core.symlink_to(outside)
        report = self._score()
        row = report["results"]["native"]
        self.assertFalse(row["scope_pass"])
        self.assertIsNone(row["core_sha256"])
        self.assertIsNone(row["heldout"])

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_extra_binary_source_is_rejected_before_execution(self):
        (self.output / "nokiy" / "src" / "extension.so").write_bytes(b"not a library")
        report = self._score()
        row = report["results"]["nokiy"]
        self.assertFalse(row["scope_pass"])
        self.assertIn("src/extension.so", row["unexpected_paths"])
        self.assertIsNone(row["heldout"])

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_unreviewed_core_digest_is_rejected_before_execution(self):
        reviewed = self._reviewed()
        reviewed["nokiy"] = "0" * 64
        report = fixture.score(self.output / "protocol.json", self.protocol_sha256,
                               Path(sys.executable), reviewed)
        row = report["results"]["nokiy"]
        self.assertEqual(row["verification_status"], "REJECTED_UNREVIEWED")
        self.assertIsNone(row["heldout"])

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_import_time_stdout_forgery_is_not_accepted_as_test_result(self):
        core = self.output / "nokiy" / fixture.CORE
        with core.open("ab") as stream:
            stream.write(b'\nimport os as _fixture_os\n'
                         b'print("{\\"passed\\": true}", flush=True)\n'
                         b'_fixture_os._exit(0)\n')
        with self.assertRaisesRegex(fixture.FixtureError, "test-count or result evidence"):
            self._score()

    def test_protocol_inventory_cannot_be_rewritten_to_hide_a_source_edit(self):
        protocol = dict(self.protocol)
        protocol["tracked_sha256"] = dict(protocol["tracked_sha256"])
        protocol["tracked_sha256"][fixture.TEST] = "0" * 64
        fixture.write_json(self.output / "forged.json", protocol)
        with self.assertRaisesRegex(fixture.FixtureError, "inventory differs"):
            fixture.score(self.output / "forged.json",
                          fixture.digest((self.output / "forged.json").read_bytes()),
                          Path(sys.executable), self._reviewed())

    def test_skipped_verifier_test_cannot_pass_quality_gate(self):
        source = (b"class Tests:\n"
                  b"    def test_one(self): pass\n"
                  b"    def test_two(self): pass\n")
        output = {"tests_run": 2, "failures": 0, "errors": 0, "skipped": 1,
                  "passed": True, "detail": "Ran 2 tests\nOK (skipped=1)"}
        completed = subprocess.CompletedProcess([], 0, json.dumps(output).encode(), b"")
        with mock.patch.object(fixture.subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(fixture.FixtureError, "test-count or result evidence"):
                fixture.run_suite(Path(sys.executable), self.output / "native", source,
                                  "unused-profile", self.output / "native")

    def test_changed_protocol_is_rejected_before_scoring(self):
        with (self.output / "protocol.json").open("ab") as stream:
            stream.write(b" ")
        with self.assertRaisesRegex(fixture.FixtureError, "frozen reference"):
            fixture.score(self.output / "protocol.json", self.protocol_sha256,
                          Path(sys.executable), self._reviewed())

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_supplemental_replays_reviewed_core_without_changing_the_scored_arm(self):
        source = self.output / "nokiy" / fixture.CORE
        fixed_core = fixture.pinned_blob(fixture.ROOT, fixture.FIX, fixture.CORE)
        source.write_bytes(fixed_core)
        (self.output / "nokiy" / ".git").mkdir()
        (self.output / "nokiy" / ".tura").mkdir()
        source_sha256 = fixture.digest(fixed_core)
        with mock.patch.object(fixture, "materialize", wraps=fixture.materialize) as materialize:
            report = self._supplemental(source_sha256)
        self.assertFalse(materialize.call_args.args[0].exists())
        self.assertEqual(report["status"], "SUPPLEMENTAL_FUNCTIONAL_ONLY")
        self.assertEqual(report["protocol_sha256"], self.protocol_sha256)
        self.assertEqual(report["source_path"], str(source))
        self.assertEqual(report["source_core_sha256"], source_sha256)
        self.assertEqual(report["replay_source"],
                         "PINNED_BASE_ARCHIVE_PLUS_REVIEWED_CORE_BYTES")
        self.assertEqual(report["base_commit"], fixture.BASE)
        self.assertEqual(report["base_tree"], fixture.BASE_TREE)
        self.assertTrue(report["original"]["passed"])
        self.assertTrue(report["heldout"]["passed"])
        self.assertTrue(report["supplemental_functional_pass"])
        self.assertEqual(report["original_pair_score"], "UNCHANGED_NOT_REASSESSED")
        self.assertEqual(report["arm_scope"], "NOT_REASSESSED")
        self.assertEqual(report["effect_audit"], "NOT_REASSESSED")
        self.assertEqual(report["verifier_os_sandbox"], "DARWIN_SEATBELT")
        self.assertEqual(report["acceptance"], "NOT_ESTABLISHED")
        self.assertEqual(report["causal_gain"], "NOT_ESTABLISHED")
        self.assertEqual(report["usage"], "UNKNOWN")
        self.assertEqual(source.read_bytes(), fixed_core)
        self.assertTrue((self.output / "nokiy" / ".git").is_dir())
        self.assertTrue((self.output / "nokiy" / ".tura").is_dir())
        self.assertFalse(any(path.name.startswith(".verifier-")
                             for path in (self.output / "nokiy").iterdir()))

    def test_supplemental_rejects_unreviewed_core_before_replay(self):
        with mock.patch.object(fixture, "run_suite") as run_suite:
            with self.assertRaisesRegex(fixture.FixtureError, "reviewed core SHA-256"):
                self._supplemental("0" * 64)
        run_suite.assert_not_called()

    def test_supplemental_rejects_symlinked_core_before_replay(self):
        source = self.output / "nokiy" / fixture.CORE
        outside = self.output / "outside.py"
        outside.write_bytes(source.read_bytes())
        source.unlink()
        source.symlink_to(outside)
        with mock.patch.object(fixture, "run_suite") as run_suite:
            with self.assertRaisesRegex(fixture.FixtureError, "symlink"):
                self._supplemental(fixture.BASE_CORE_SHA256)
        run_suite.assert_not_called()

    def test_supplemental_rejects_symlinked_ancestor_before_replay(self):
        source = self.output / "nokiy" / fixture.CORE
        package = source.parent
        moved = package.with_name("source-package")
        package.rename(moved)
        package.symlink_to(moved, target_is_directory=True)
        with mock.patch.object(fixture, "run_suite") as run_suite:
            with self.assertRaisesRegex(fixture.FixtureError, "symlink"):
                self._supplemental(fixture.BASE_CORE_SHA256)
        run_suite.assert_not_called()

    def test_supplemental_rejects_protocol_tamper_before_replay(self):
        with (self.output / "protocol.json").open("ab") as stream:
            stream.write(b" ")
        with mock.patch.object(fixture, "run_suite") as run_suite:
            with self.assertRaisesRegex(fixture.FixtureError, "frozen reference"):
                self._supplemental(fixture.BASE_CORE_SHA256)
        run_suite.assert_not_called()

    def test_supplemental_rejects_other_source_path_before_replay(self):
        with mock.patch.object(fixture, "run_suite") as run_suite:
            with self.assertRaisesRegex(fixture.FixtureError, "protocol's Nokiy core path"):
                fixture.supplemental_functional(
                    self.output / "protocol.json", self.protocol_sha256,
                    Path(sys.executable), self.output / "native" / fixture.CORE,
                    fixture.BASE_CORE_SHA256)
        run_suite.assert_not_called()

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_score_cli_uses_frozen_protocol_digest(self):
        completed = subprocess.run(
            [sys.executable, str(SCRIPT), "score", "--protocol",
             str(self.output / "protocol.json"), "--protocol-sha256",
             self.protocol_sha256, "--native-reviewed-core-sha256",
             self._reviewed()["native"], "--nokiy-reviewed-core-sha256",
             self._reviewed()["nokiy"]], capture_output=True, text=True, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertFalse(report["pair_quality_pass"])
        self.assertEqual(report["requested_model"], "same-model")

    @unittest.skipUnless(DARWIN_SANDBOX, "requires Darwin sandbox-exec")
    def test_verifier_fence_denies_other_reads_writes_network_and_spawn(self):
        arm = self.output / "native"
        secret = SCRIPT
        original_sha256 = fixture.digest(secret.read_bytes())
        alternate_secret = Path("/System/Volumes/Data") / secret.relative_to("/")
        self.assertTrue(alternate_secret.is_file())
        outside_write = self.output / "outside-write.txt"
        with tempfile.TemporaryDirectory(dir=arm) as directory:
            scratch = Path(directory)
            profile = fixture.sandbox_profile(arm, scratch)
            (scratch / "outside-link").symlink_to(outside_write)
            probe = """
import json, os, socket, subprocess, sys
from pathlib import Path
results = {}
for name, operation in (
    ("outside_read", lambda: Path(sys.argv[1]).read_text()),
    ("alternate_outside_read", lambda: Path(sys.argv[5]).read_text()),
    ("outside_write", lambda: Path(sys.argv[2]).write_text("bad")),
    ("network_bind", lambda: socket.socket().bind(("127.0.0.1", 0))),
    ("scratch_write", lambda: Path(sys.argv[3]).write_text("ok")),
    ("symlink_write", lambda: Path(sys.argv[4]).write_text("bad")),
    ("process_spawn", lambda: subprocess.run(["/bin/true"], check=True)),
):
    try:
        operation()
        results[name] = "ALLOWED"
    except OSError:
        results[name] = "DENIED"
results["secret_env_absent"] = "YES" if "BENCH_SECRET_CANARY" not in os.environ else "NO"
print(json.dumps(results))
"""
            with mock.patch.dict("os.environ", {"BENCH_SECRET_CANARY": "host-secret"}):
                completed = subprocess.run(
                    ["/usr/bin/sandbox-exec", "-p", profile, sys.executable,
                     "-I", "-S", "-B", "-c", probe, str(secret), str(outside_write),
                     str(scratch / "inside.txt"), str(scratch / "outside-link"),
                     str(alternate_secret)],
                    capture_output=True, text=True, cwd=arm, check=False,
                    env=fixture.verifier_environment(scratch),
                )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {
                "outside_read": "DENIED", "alternate_outside_read": "DENIED",
                "outside_write": "DENIED",
                "network_bind": "DENIED", "scratch_write": "ALLOWED",
                "symlink_write": "DENIED", "process_spawn": "DENIED",
                "secret_env_absent": "YES",
            })
        self.assertFalse(outside_write.exists())
        self.assertEqual(fixture.digest(secret.read_bytes()), original_sha256)

    def test_verifier_fails_closed_when_sandbox_is_unavailable(self):
        arm = self.output / "native"
        with tempfile.TemporaryDirectory(dir=arm) as directory:
            with mock.patch.object(fixture.sys, "platform", "linux"):
                with self.assertRaisesRegex(fixture.FixtureError, "sandbox-exec is required"):
                    fixture.sandbox_profile(arm, Path(directory))


if __name__ == "__main__":
    unittest.main()
