"""Bound closeout uses only supervised local fixture commands and temporary files."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from codex_collaboration_harness import deployment as d
from codex_collaboration_harness import embedded_nokiy as c

THREAD = "019fd83e-861a-7b62-a628-0e0ad2f88a27"
TASK = "fixture-task"
COMMIT = "a" * 40
DEPLOY = '''import json, os, sys
from pathlib import Path
stage = sys.argv[1]
p = json.loads(Path("plan.json").read_text())
r = {k:p[k] for k in ("action_id", "target", "release")}
r["plan_sha256"] = os.environ["NOKIY_DEPLOYMENT_PLAN_SHA256"]
with Path("order").open("a") as f: f.write(stage + "\\n")
mode = Path("mode").read_text()
if stage == "preflight": r["admitted"] = mode != "deny"
if stage == "preflight" and mode == "lease-drift": Path("leases/active.json").write_text("changed")
if stage == "apply": Path("version").write_text("v2")
if stage == "verify":
    r.update(verified=True, healthy=mode != "unhealthy", observed_release=Path("version").read_text())
    if mode == "static-drift": Path("finish.py").write_text("changed")
print(json.dumps(r))
'''
FINISH = '''import json
from pathlib import Path
p = json.loads(Path("plan.json").read_text())["task_closeout"]
mode = Path("finish-mode").read_text()
with Path("order").open("a") as f: f.write("closeout\\n")
if mode == "nonzero": raise SystemExit(9)
r = {"ok": mode != "not-ok", **p["expected_receipt"], "project_extra": {"allowed": True}}
if mode == "wrong-thread": r["thread_id"] = "other"
if mode == "wrong-task": r["task_id"] = "other"
if mode == "wrong-commits": r["commit_attribution"]["task_commit_ids"] = ["b" * 40]
if mode == "unexpected-attribution": r["commit_attribution"] = {"task_commit_ids": ["b" * 40]}
if mode == "unexpected-paths": r["committed_paths"] = ["src/change.py"]
if mode == "wrong-base": r["base_commit"] = "b" * 40
if mode == "wrong-head": r["head_commit"] = "b" * 40
if mode == "dirty-task-paths": r["dirty_task_paths"] = ["src/change.py"]
if mode in ("task_owned_dirty_paths", "task_owned_new_dirty_paths", "committed_paths_outside_write_scopes"):
    r[mode] = ["src/change.py"]
if mode == "new-source-changes": r["new_source_changes"] = ["src/change.py"]
Path(p["completion_path"]).write_text(json.dumps(r))
if mode != "lease-remains": Path(p["active_lease"]["path"]).unlink()
'''


def ident(path):
    return {"path": str(path), "sha256": c._file_sha256(path)}


class CloseoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name).resolve()
        env = patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD})
        env.start()
        self.addCleanup(env.stop)
        (root / "artifacts").mkdir()
        (root / "leases").mkdir()
        (root / "deploy.py").write_text(DEPLOY)
        (root / "finish.py").write_text(FINISH)
        (root / "grant").write_text("approved fixture")
        (root / "mode").write_text("ok")
        (root / "finish-mode").write_text("ok")
        (root / "version").write_text("v1")
        lease = root / "leases" / "active.json"
        lease.write_text(json.dumps({"task_id": TASK, "thread_id": THREAD}))
        python = Path(sys.executable).resolve()
        def command(script, arg=None):
            return {"argv": [str(python), "-B", str(script)] + ([arg] if arg else []),
                    "files": [ident(python), ident(script)], "timeout_seconds": 10}
        self.plan = {"schema_version": d.SCHEMA, "action_id": "fixture-closeout", "native_thread_id": THREAD,
                     "workspace": str(root), "artifact_root": str(root / "artifacts"),
                     "target": "fixture", "release": "v2", "expires_at": time.time() + 120,
                     "authorization_ref": ident(root / "grant"),
                     "commands": {stage: command(root / "deploy.py", stage) for stage in d.STAGES},
                     "task_closeout": {"task_id": TASK, "active_lease": ident(lease),
                         "completion_path": str(root / "leases" / "complete.json"),
                         "command": command(root / "finish.py"),
                         "expected_receipt": {"task_id": TASK, "thread_id": THREAD,
                             "finish_state": "committed", "commit_attribution": {"task_commit_ids": [COMMIT]}}}}
        self.path = root / "plan.json"

    def execute(self):
        self.path.write_text(json.dumps(self.plan))
        return d.execute(self.path, c._canonical_sha256(self.plan))

    def reject(self, code):
        with self.assertRaisesRegex(c.EmbeddedNokiyError, code):
            self.execute()
        self.assertFalse((self.root / "order").exists())

    def external(self):
        """Lease schema uses the actual repository root, not the deployment workspace."""
        repo = self.root / "repo"
        repo.mkdir()
        cache = self.root / "cache"
        runtime = self.root / "runtime"
        publication = self.root / "publication"
        cache.mkdir()
        runtime.mkdir()
        publication.mkdir()
        lease = self.root / "leases" / "active.json"
        lease.write_text(json.dumps({"ok": True, "violations": [], "task_id": TASK,
                                     "thread_id": THREAD, "root": str(repo),
                                     "base_commit": COMMIT,
                                     "write_scopes": [str(cache), str(runtime), str(publication)]}))
        spec = self.plan["task_closeout"]
        spec["mode"] = "external_deployment"
        spec["active_lease"] = ident(lease)
        spec["expected_receipt"] = {"task_id": TASK, "thread_id": THREAD,
                                    "finish_state": "committed", "base_commit": COMMIT,
                                    "head_commit": COMMIT, "committed_paths": [],
                                    "commit_attribution": {}}

    def update_lease(self, **changes):
        lease = self.root / "leases" / "active.json"
        data = json.loads(lease.read_text())
        data.update(changes)
        lease.write_text(json.dumps(data))
        self.plan["task_closeout"]["active_lease"] = ident(lease)

    def test_verified_then_closeout_once_and_historical_read(self):
        result = self.execute()
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertTrue(result["deployment_verified"])
        self.assertEqual(result["lease_closeout"]["status"], "VERIFIED")
        self.assertEqual(result["lease_closeout"]["receipt_sha256"], c._file_sha256(self.root / "leases" / "complete.json"))
        self.assertEqual((self.root / "order").read_text(), "preflight\napply\nverify\ncloseout\n")
        (self.root / "deploy.py").unlink()
        (self.root / "finish.py").unlink()
        (self.root / "grant").unlink()
        with patch.object(d.time, "time", return_value=self.plan["expires_at"] + 1):
            self.assertEqual(self.execute(), result)
            self.assertEqual(d.read_result(self.root / "artifacts", self.plan["action_id"]), result)
        self.assertEqual((self.root / "order").read_text().count("closeout"), 1)

    def test_external_deployment_verified_once_and_historical_read(self):
        self.external()
        result = self.execute()
        self.assertEqual(result["status"], "VERIFIED", result)
        self.assertTrue(result["deployment_verified"])
        self.assertEqual(result["lease_closeout"]["status"], "VERIFIED")
        completion = self.root / "leases" / "complete.json"
        self.assertEqual(result["lease_closeout"]["receipt_sha256"], c._file_sha256(completion))
        self.assertEqual({k: json.loads(completion.read_text())[k] for k in
                          ("base_commit", "head_commit", "committed_paths", "commit_attribution")},
                         {"base_commit": COMMIT, "head_commit": COMMIT,
                          "committed_paths": [], "commit_attribution": {}})
        self.assertFalse((self.root / "leases" / "active.json").exists())
        self.assertEqual((self.root / "order").read_text(), "preflight\napply\nverify\ncloseout\n")
        (self.root / "deploy.py").unlink()
        (self.root / "finish.py").unlink()
        (self.root / "grant").unlink()
        with patch.object(d.time, "time", return_value=self.plan["expires_at"] + 1):
            self.assertEqual(self.execute(), result)
            self.assertEqual(d.read_result(self.root / "artifacts", self.plan["action_id"]), result)
        self.assertEqual((self.root / "order").read_text().count("closeout"), 1)

    def test_source_mode_still_requires_commits_and_unknown_mode_rejected(self):
        self.plan["task_closeout"]["expected_receipt"]["commit_attribution"] = {}
        self.reject("CLOSEOUT_COMMITS_REQUIRED")
        self.plan["task_closeout"]["mode"] = "source"
        self.reject("CLOSEOUT_COMMITS_REQUIRED")
        self.plan["task_closeout"]["mode"] = "other"
        self.reject("CLOSEOUT_MODE_INVALID")

    def test_explicit_source_mode_keeps_source_closeout(self):
        self.plan["task_closeout"]["mode"] = "source"
        self.assertEqual(self.execute()["status"], "VERIFIED")

    def test_external_expected_zero_source_assertions_and_commit_flags_required(self):
        for key, value in (("finish_state", "no_changes"), ("base_commit", "b" * 40),
                           ("base_commit", "a" * 8),
                           ("head_commit", "b" * 40), ("committed_paths", ["change.py"]),
                           ("commit_attribution", {"task_commit_ids": [COMMIT]})):
            with self.subTest(key=key), self.fixture() as fixture:
                fixture.external()
                fixture.plan["task_closeout"]["expected_receipt"][key] = value
                fixture.reject("CLOSEOUT_EXTERNAL_SOURCE_IDENTITY_INVALID")
        for key in ("base_commit", "head_commit", "committed_paths", "commit_attribution"):
            with self.subTest(missing=key), self.fixture() as fixture:
                fixture.external()
                del fixture.plan["task_closeout"]["expected_receipt"][key]
                fixture.reject("CLOSEOUT_EXTERNAL_SOURCE_IDENTITY_INVALID")
        for flag in ("--task-commit", "--task-commit-id=" + COMMIT,
                     "--task-commits=" + COMMIT):
            with self.subTest(flag=flag), self.fixture() as fixture:
                fixture.external()
                fixture.plan["task_closeout"]["command"]["argv"].append(flag)
                fixture.reject("CLOSEOUT_EXTERNAL_TASK_COMMIT_FLAG")

    def test_external_lease_owner_sha_and_source_identity_rejected(self):
        for change in ({"thread_id": "other"}, {"task_id": "other"}):
            with self.subTest(change=change), self.fixture() as fixture:
                fixture.external()
                fixture.update_lease(**change)
                fixture.reject("CLOSEOUT_LEASE_OWNER_MISMATCH")
        self.external()
        self.update_lease(base_commit="b" * 40)
        self.reject("CLOSEOUT_EXTERNAL_LEASE_INVALID")
        self.plan["task_closeout"]["active_lease"]["sha256"] = "0" * 64
        self.reject("CLOSEOUT_LEASE_DRIFT")

    def test_external_lease_scopes_and_attestation_rejected(self):
        for changes in ({"ok": False}, {"violations": ["denied"]}, {"root": None},
                        {"root": "repo"}, {"root": "/tmp/../repo"},
                        {"write_scopes": []}, {"write_scopes": ["cache"]},
                        {"write_scopes": ["/tmp/../cache"]},
                        {"write_scopes": ["/tmp/cache*"]}):
            with self.subTest(changes=changes), self.fixture() as fixture:
                fixture.external()
                fixture.update_lease(**changes)
                fixture.reject("CLOSEOUT_EXTERNAL_")
        for scope in ("repo", "repo/src", "."):
            with self.subTest(scope=scope), self.fixture() as fixture:
                fixture.external()
                fixture.update_lease(write_scopes=[str(fixture.root / scope)])
                fixture.reject("CLOSEOUT_EXTERNAL_SCOPE_INVALID")
        with self.fixture() as fixture:
            fixture.external()
            route = fixture.root / "route"
            route.symlink_to(fixture.root / "repo", target_is_directory=True)
            fixture.update_lease(write_scopes=[str(route / "src")])
            fixture.reject("CLOSEOUT_EXTERNAL_SCOPE_INVALID")

    def test_external_no_closeout_on_unsuccessful_verify(self):
        self.external()
        (self.root / "mode").write_text("unhealthy")
        result = self.execute()
        self.assertEqual(result["status"], "EFFECT_UNCERTAIN", result)
        self.assertEqual(result["lease_closeout"]["status"], "NOT_ATTEMPTED")
        self.assertTrue((self.root / "leases" / "active.json").exists())
        self.assertEqual((self.root / "order").read_text(), "preflight\napply\nverify\n")

    def test_external_receipt_and_lease_absence_required(self):
        for mode in ("not-ok", "wrong-thread", "wrong-task", "unexpected-attribution",
                     "unexpected-paths", "wrong-base", "wrong-head", "dirty-task-paths",
                     "new-source-changes", "lease-remains", "task_owned_dirty_paths",
                     "task_owned_new_dirty_paths", "committed_paths_outside_write_scopes"):
            with self.subTest(mode=mode), self.fixture() as fixture:
                fixture.external()
                (fixture.root / "finish-mode").write_text(mode)
                result = fixture.execute()
                self.assertEqual(result["status"], "CLOSEOUT_BLOCKED", result)
                self.assertTrue(result["deployment_verified"])
                self.assertEqual(result["lease_closeout"]["status"], "BLOCKED")
                self.assertIn("CLOSEOUT_LEASE_STILL_ACTIVE" if mode == "lease-remains"
                              else "CLOSEOUT_RECEIPT_MISMATCH", result["first_typed_blocker"])
                self.assertEqual(fixture.execute(), result)

    def test_no_closeout_on_preflight_or_verify_failure(self):
        for mode, expected in (("deny", "BLOCKED_BEFORE_APPLY"), ("unhealthy", "EFFECT_UNCERTAIN")):
            with self.subTest(mode=mode):
                with self.fixture() as fixture:
                    (fixture.root / "mode").write_text(mode)
                    result = fixture.execute()
                    self.assertEqual(result["status"], expected, result)
                    self.assertEqual(result["lease_closeout"]["status"], "NOT_ATTEMPTED")
                    self.assertTrue((fixture.root / "leases" / "active.json").is_file())
                    self.assertNotIn("closeout", (fixture.root / "order").read_text())

    def fixture(self):
        # Another independent TestCase fixture in the same subtest.
        from contextlib import contextmanager
        @contextmanager
        def context():
            case = CloseoutTests()
            case.setUp()
            try:
                yield case
            finally:
                case.doCleanups()
        return context()

    def test_lease_owner_and_cas(self):
        lease = self.root / "leases" / "active.json"
        lease.write_text(json.dumps({"task_id": TASK, "thread_id": "other"}))
        self.plan["task_closeout"]["active_lease"] = ident(lease)
        self.reject("CLOSEOUT_LEASE_OWNER_MISMATCH")
        lease.write_text(json.dumps({"task_id": TASK, "thread_id": THREAD}, indent=2))
        self.reject("CLOSEOUT_LEASE_DRIFT")

    def test_lease_drift_after_preflight_blocks_apply(self):
        (self.root / "mode").write_text("lease-drift")
        result = self.execute()
        self.assertEqual(result["status"], "BLOCKED_BEFORE_APPLY", result)
        self.assertIn("CLOSEOUT_LEASE_DRIFT", result["first_typed_blocker"])
        self.assertEqual((self.root / "order").read_text(), "preflight\n")

    def test_finish_failures_never_mark_verified(self):
        for mode in ("nonzero", "not-ok", "wrong-thread", "wrong-task", "wrong-commits", "lease-remains"):
            with self.subTest(mode=mode), self.fixture() as fixture:
                (fixture.root / "finish-mode").write_text(mode)
                result = fixture.execute()
                self.assertEqual(result["status"], "CLOSEOUT_BLOCKED", result)
                self.assertTrue(result["deployment_verified"])
                self.assertIsNotNone(result["first_typed_blocker"])
                self.assertEqual((fixture.root / "order").read_text().count("closeout"), 1)
                self.assertEqual(fixture.execute(), result)

    def test_static_input_drift_before_finish(self):
        (self.root / "mode").write_text("static-drift")
        result = self.execute()
        self.assertEqual(result["status"], "CLOSEOUT_BLOCKED", result)
        self.assertIn("IDENTITY_DRIFT", result["first_typed_blocker"])
        self.assertNotIn("closeout", (self.root / "order").read_text())

    def test_explicit_null_invalid_and_commits_required(self):
        self.plan["task_closeout"] = None
        self.reject("CLOSEOUT_FIELDS_INVALID")

    def test_missing_commit_attribution_invalid(self):
        del self.plan["task_closeout"]["expected_receipt"]["commit_attribution"]
        self.reject("CLOSEOUT_COMMITS_REQUIRED")

    def test_leaf_symlink_rejected(self):
        lease = self.root / "leases" / "active.json"
        alias = self.root / "leases" / "alias.json"
        alias.symlink_to(lease)
        self.plan["task_closeout"]["active_lease"] = ident(alias)
        self.reject("CLOSEOUT_LEAF_SYMLINK")

    def test_completion_leaf_symlink_rejected(self):
        (self.root / "leases" / "complete.json").symlink_to(self.root / "leases" / "missing")
        self.reject("CLOSEOUT_LEAF_SYMLINK")

    def test_routed_parent_accepted(self):
        routed = self.root / "routed"
        routed.symlink_to(self.root / "leases", target_is_directory=True)
        spec = self.plan["task_closeout"]
        spec["active_lease"] = ident(routed / "active.json")
        spec["completion_path"] = str(routed / "complete.json")
        self.assertEqual(self.execute()["status"], "VERIFIED")

    def test_routed_artifact_parent_keeps_existing_canonical_policy(self):
        (self.root / "artifacts" / "output").mkdir()
        (self.root / "output-route").symlink_to(self.root / "artifacts", target_is_directory=True)
        self.plan["artifact_root"] = str(self.root / "output-route" / "output")
        self.reject("CANONICAL_PATH_REQUIRED")

    def test_retargeted_parent_rejected_before_effect(self):
        routed = self.root / "routed"
        routed.symlink_to(self.root / "leases", target_is_directory=True)
        spec = self.plan["task_closeout"]
        spec["active_lease"] = ident(routed / "active.json")
        spec["completion_path"] = str(routed / "complete.json")
        other = self.root / "other"
        other.mkdir()
        original = d.save
        def retarget(path, value):
            original(path, value)
            if path.name == "closeout.paths.json":
                routed.unlink()
                routed.symlink_to(other, target_is_directory=True)
        with patch.object(d, "save", side_effect=retarget):
            result = self.execute()
        self.assertEqual(result["status"], "BLOCKED_BEFORE_APPLY", result)
        self.assertFalse((self.root / "order").exists())


if __name__ == "__main__":
    unittest.main()
