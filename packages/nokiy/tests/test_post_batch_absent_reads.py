"""A post-batch verifier may name files its batch creates."""
import unittest

import test_post_batch_verifier as fixture
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import local_context

CREATED = "member_created_output.py"


class PostBatchAbsentReadTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.PostBatchVerifierTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.command = self.f.contract["verifier_commands"][0]

    def compile(self, action):
        return local_context.compile_context(self.f.workspace, action,
            artifact_root=self.f.final.artifact_root, post_batch_verifier=True)

    def test_absent_reads_are_dropped_with_a_note_and_then_compile(self):
        action = fixture.readonly_action(list(self.f.outputs) + [CREATED], self.command)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "read input missing"):
            self.compile(action)
        adapted, repairs = local_context.absent_post_batch_reads_to_note(action, self.f.workspace)
        self.assertEqual(repairs, [{"path": "action.read_scopes", "rule": "post_batch_absent_reads_to_note",
                                    "paths": [CREATED]}])
        self.assertEqual(set(adapted["read_scopes"]), set(self.f.outputs))
        self.assertIn(CREATED, adapted["context_summary"])
        self.compile(adapted)

    def test_present_reads_and_ordinary_read_only_actions_are_unchanged(self):
        action = fixture.readonly_action(list(self.f.outputs), self.command)
        self.assertEqual(local_context.absent_post_batch_reads_to_note(action, self.f.workspace), (action, []))
        absent = fixture.readonly_action(list(self.f.outputs) + [CREATED], self.command)
        self.assertEqual(local_context.absent_reads_to_note(absent, self.f.workspace), (absent, []))

    def test_a_final_gate_prepared_with_plain_prepare_drops_absent_reads(self):
        action = fixture.readonly_action(list(self.f.outputs) + [CREATED], self.command)
        adapted, repairs = stack._absent_gate_reads(action, self.f.workspace, False)
        self.assertEqual([row["rule"] for row in repairs], ["post_batch_absent_reads_to_note"])
        self.assertEqual(set(adapted["read_scopes"]), set(self.f.outputs))

    def test_a_member_created_file_is_read_through_its_existing_directory(self):
        package = self.f.workspace / "pkg"
        package.mkdir()
        (package / "existing.py").write_text("EXISTING = 1\n")
        action = fixture.readonly_action(list(self.f.outputs) + ["pkg/new_module.py"], self.command)
        adapted, repairs = local_context.absent_post_batch_reads_to_note(action, self.f.workspace)
        self.assertEqual(repairs, [{"path": "action.read_scopes", "rule": "gate_absent_reads_to_directories",
                                    "paths": ["pkg/new_module.py"]}])
        self.assertIn("pkg/**", adapted["read_scopes"])
        self.assertNotIn("pkg/new_module.py", adapted["read_scopes"])
        self.assertEqual(adapted["read_directories"], ["pkg"])
        self.assertIn("pkg/new_module.py", adapted["context_summary"])
        self.compile(adapted)

    def test_exploratory_and_writing_actions_are_not_gates(self):
        explore = fixture.readonly_action(list(self.f.outputs) + [CREATED], self.command)
        explore.pop("verifier_commands")
        self.assertEqual(stack._absent_gate_reads(explore, self.f.workspace, False), (explore, []))
        writing = fixture.readonly_action(list(self.f.outputs) + [CREATED], self.command)
        writing.update(operations=["read", "command", "modify"], write_scopes=[self.f.outputs[0]])
        self.assertEqual(stack._absent_gate_reads(writing, self.f.workspace, False), (writing, []))


if __name__ == "__main__":
    unittest.main()
