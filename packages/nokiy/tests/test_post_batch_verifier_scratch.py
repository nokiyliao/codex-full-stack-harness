"""Postgate-only external scratch binding; ordinary worker authority is unchanged."""
from copy import deepcopy
import hashlib
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

    def test_live_logs_use_hashing_read_and_keep_unsafe_files_metadata_only(self):
        self.prepare()
        contents = {"summary.log": "Checks complete: café 中\n".encode("utf-8"),
                    "notes.txt": b"first\tsecond\r\n", "report.json": b'{"ok":true}\n',
                    "invalid.log": b"invalid\xff", "binary.log": b"binary\x00data",
                    "unsafe.txt": b"\x1b[31mterminal control\n",
                    "large.log": b"x" * (post.INLINE_EVIDENCE_BYTES + 1)}
        observe = self.f.observation
        def produce(*args):
            for name, data in contents.items():
                (self.scratch / name).write_bytes(data)
            outside = self.scratch.parent / "outside.log"
            outside.write_text("outside scratch\n")
            (self.scratch / "alias.log").symlink_to(outside)
            (self.scratch / ".hidden.log").write_text("hidden\n")
            (self.scratch / "nested").mkdir()
            (self.scratch / "nested" / "nested.log").write_text("nested\n")
            return observe(*args)
        self.f.gate_call.side_effect = produce
        with patch.object(post.os, "open", wraps=post.os.open) as opened:
            result = batch.run_batch(self.f.plan, self.f.digest)
        gate = result["post_batch_verifier"]
        self.assertEqual((result["status"], gate["status"]), ("RESULT_AVAILABLE", "PASSED"))
        expected = []
        for name, data in sorted(contents.items()):
            row = {"path": str(self.scratch / name), "bytes": len(data),
                   "sha256": hashlib.sha256(data).hexdigest()}
            if name in {"summary.log", "notes.txt"}:
                row["text"] = data.decode("utf-8")
            expected.append(row)
            self.assertEqual(sum(call.args[0] == row["path"] for call in opened.call_args_list), 1)
        self.assertFalse(any(call.args[0] == str(self.scratch / "alias.log")
                             for call in opened.call_args_list))
        self.assertEqual(gate["verifier_outputs"], {"files": expected, "truncated": False})
        self.assertEqual(gate["verifier_outputs_authority"],
                         "untrusted_verifier_written_files_not_parent_acceptance")
        self.assertEqual(gate["output_preview"], {"stdout": "raw stdout\n", "stderr": "raw stderr\n"})
        self.assertTrue(gate["parent_acceptance_required"])
        self.assertFalse(gate["auto_retry_allowed"])
        before = deepcopy(result)
        self.assertEqual(batch.summarize(result)["post_batch_verifier"], gate)
        self.assertEqual(result, before)
        recovered = batch.read_batch(self.f.plan, self.f.digest)["post_batch_verifier"]
        self.assertEqual(recovered["status"], "UNKNOWN")
        self.assertNotIn("verifier_outputs", recovered)
        self.f.gate_call.assert_called_once()

    def test_log_text_yields_to_gate_stdout_stderr_and_evidence(self):
        data = b"x" * (post.INLINE_EVIDENCE_BYTES // 2)
        path = self.scratch / "summary.log"
        path.write_bytes(data)
        metadata = {"files": [{"path": str(path), "bytes": len(data),
                                "sha256": hashlib.sha256(data).hexdigest()}], "truncated": False}
        observation = {"stdout": "s" * (post.INLINE_EVIDENCE_BYTES // 2), "stderr": "details\n",
                       "exit_code": 0, "outcome": "known", "process_reaped": True,
                       "process_group_empty": True}
        binding = {"command": self.command}
        with patch.object(post, "_verifier_outputs", return_value=metadata):
            expected = post._attach({"status": "RESULT_AVAILABLE"}, "PASSED", self.f.spec,
                                    binding=binding, observation=observation)
        actual = post._attach({"status": "RESULT_AVAILABLE"}, "PASSED", self.f.spec,
                              binding=binding, observation=observation)
        self.assertEqual(actual, expected)
        gate = actual["post_batch_verifier"]
        self.assertEqual(gate["output_preview"], {key: observation[key] for key in ("stdout", "stderr")})
        self.assertEqual(gate["binding"], binding)
        self.assertLessEqual(len(caller._canonical_bytes(gate)), post.INLINE_EVIDENCE_BYTES)

    def test_log_text_is_not_duplicated_or_lost_with_visible_preview(self):
        data = b"diagnostic detail\n"
        path = self.scratch / "summary.log"
        path.write_bytes(data)
        for stdout in ("prefix\n" + data.decode() + "suffix\n",
                       "s" * post.INLINE_EVIDENCE_BYTES):
            with self.subTest(stdout_fits=len(stdout) < post.INLINE_EVIDENCE_BYTES):
                observation = {"stdout": stdout, "stderr": "", "exit_code": 0,
                               "outcome": "known", "process_reaped": True,
                               "process_group_empty": True}
                gate = post._attach({"status": "RESULT_AVAILABLE"}, "PASSED", self.f.spec,
                                    binding={"command": self.command},
                                    observation=observation)["post_batch_verifier"]
                row = gate["verifier_outputs"]["files"][0]
                self.assertEqual(row["sha256"], hashlib.sha256(data).hexdigest())
                if "output_preview" in gate:
                    self.assertEqual(gate["output_preview"]["stdout"], stdout)
                    self.assertNotIn("text", row)
                else:
                    self.assertEqual(row["text"], data.decode())

    def test_postgate_cli_has_no_model_run_option(self):
        arguments = caller.build_parser().parse_args(["prepare-post-batch-verifier", "--request", "draft.json",
            "--action", "action.json", "--output-dir", "out"])
        self.assertFalse(hasattr(arguments, "run"))


if __name__ == "__main__":
    unittest.main()
