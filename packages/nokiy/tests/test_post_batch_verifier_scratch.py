"""Postgate-only external scratch binding; ordinary worker authority is unchanged."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_collaboration_harness import batch, embedded_nokiy as caller, local_context
from codex_collaboration_harness import post_batch_verifier as post
import test_post_batch_verifier as fixture


class SeparatePostgateScratchTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PostBatchVerifierTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.scratch = self.f.plan.parent / "final-output"
        self.scratch.mkdir()
        self.command = dict(self.f.contract["verifier_commands"][0], scratch_root=str(self.scratch))
        self.action = fixture.readonly_action(self.f.outputs, self.command)
        self.members = tuple(batch.Member(Path(m["request_path"]), m["request_file_sha256"],
            m["request_id"], Path(m["artifact_root"])) for m in self.f.members)

    def compile(self, action=None):
        return local_context.compile_context(self.f.workspace, action or self.action,
            artifact_root=self.f.final.artifact_root, post_batch_verifier=True)

    def prepare(self, action=None):
        capsule, contract = self.compile(action)
        self.f.set_contract(contract)
        local_context.verify_context(self.f.workspace, capsule, contract,
            artifact_root=self.f.final.artifact_root, post_batch_verifier=True)
        return post.prepare(self.f.spec, self.f.plan, self.f.digest, self.members,
            self.f.workers, [batch._scopes(r) for r in self.f.workers])

    def test_worker_compile_context_and_scope_reject_external_root(self):
        with self.assertRaises(caller.EmbeddedNokiyError):
            local_context.compile_context(self.f.workspace, self.action,
                                          artifact_root=self.f.final.artifact_root)
        capsule, contract = self.compile()
        self.assertEqual(contract["verifier_artifact_root"], str(self.scratch.parent))
        self.f.set_contract(contract)
        with self.assertRaises(caller.EmbeddedNokiyError):
            local_context.verify_context(self.f.workspace, capsule, contract,
                                         artifact_root=self.f.final.artifact_root)
        with self.assertRaises(caller.EmbeddedNokiyError):
            batch._scopes(self.f.final)
        altered = deepcopy(contract)
        altered["verifier_artifact_root"] = str(self.f.final.artifact_root)
        with self.assertRaises(caller.EmbeddedNokiyError):
            local_context.verify_context(self.f.workspace, capsule, altered,
                artifact_root=self.f.final.artifact_root, post_batch_verifier=True)

    def test_binding_changes_with_exact_output_scope(self):
        _, original = self.compile()
        another = self.scratch.parent / "second-output"
        another.mkdir()
        action = deepcopy(self.action)
        action["verifier_commands"][0]["scratch_root"] = str(another)
        _, changed = self.compile(action)
        self.assertNotEqual(original["authorization_semantic_sha256"], changed["authorization_semantic_sha256"])

    def test_readonly_and_exactly_one_command_required(self):
        changes = [dict(write_scopes=self.f.outputs, target_paths=self.f.outputs,
                        operations=["read", "command", "modify"]),
                   dict(verifier_commands=[self.command, self.command])]
        for changed in changes:
            with self.subTest(changed=changed), self.assertRaises(caller.EmbeddedNokiyError):
                self.compile(dict(self.action, **changed))

    def test_workspace_receipt_and_alias_scratch_reject(self):
        alias = self.scratch.parent / "alias"
        alias.symlink_to(self.scratch, target_is_directory=True)
        for path in (self.f.workspace, self.f.workspace.parent, self.f.final.artifact_root, alias):
            action = deepcopy(self.action)
            action["verifier_commands"][0]["scratch_root"] = str(path)
            with self.subTest(path=path), self.assertRaises(caller.EmbeddedNokiyError):
                self.prepare(action)

    def test_symlinked_parent_and_ancestor_reject_before_admission(self):
        alias = self.scratch.parent / "parent-alias"
        alias.symlink_to(self.scratch.parent, target_is_directory=True)
        nested = self.scratch / "nested"
        nested.mkdir()
        for path in (alias / self.scratch.name, alias / self.scratch.name / "nested"):
            action = deepcopy(self.action)
            action["verifier_commands"][0]["scratch_root"] = str(path)
            with self.subTest(path=path), self.assertRaises(caller.EmbeddedNokiyError):
                self.compile(action)
        self.f.ready.assert_not_called()

    def test_foreign_member_receipts_reject(self):
        for root in (self.f.workers[0].artifact_root, self.f.workers[0].artifact_root / "scratch"):
            root.mkdir(exist_ok=True)
            action = deepcopy(self.action)
            action["verifier_commands"][0]["scratch_root"] = str(root)
            with self.subTest(root=root), self.assertRaisesRegex(caller.EmbeddedNokiyError, "protected member"):
                self.prepare(action)

    def test_member_context_and_runtime_artifact_overlap_reject(self):
        capsule = self.scratch / "member-capsule.json"
        capsule.write_text("{}")
        self.f.workers[0].context_capsule = SimpleNamespace(path=capsule)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "protected member"):
            self.prepare()
        self.f.workers[0].context_capsule = self.f.final.context_capsule
        image = self.f.plan.parent / "other-runtime.json"
        image.write_text("{}")
        self.f.workers[0].runtime_image = SimpleNamespace(path=image)
        with patch.object(caller, "verify_runtime_image", return_value=SimpleNamespace(
                artifacts={"runtime": SimpleNamespace(path=capsule)})), \
                self.assertRaisesRegex(caller.EmbeddedNokiyError, "protected member"):
            self.prepare()

    def test_alias_added_after_claim_prevents_any_verifier_call(self):
        gate = self.prepare()
        self.assertTrue(post.claim(gate, self.f.plan))
        (self.scratch / "aliased-receipt").symlink_to(self.f.workers[0].artifact_root,
                                                    target_is_directory=True)
        self.f.gate_call.reset_mock()
        result = post.finish(gate, {"status": "RESULT_AVAILABLE"})
        self.assertEqual(result["post_batch_verifier"]["status"], "UNKNOWN")
        self.f.gate_call.assert_not_called()

    def test_postgate_cli_has_no_model_run_option(self):
        arguments = caller.build_parser().parse_args(["prepare-post-batch-verifier", "--request", "draft.json",
            "--action", "action.json", "--output-dir", "out"])
        self.assertFalse(hasattr(arguments, "run"))


if __name__ == "__main__":
    unittest.main()
