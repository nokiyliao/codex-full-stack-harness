"""V431 parent-executed read-only shell: policy binding, execution and local-context admission."""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack
from codex_collaboration_harness import local_context as local
from codex_collaboration_harness import parent_shell as ps
from codex_collaboration_harness import result_inspection as inspection

MISSION = {"mission_id": "m", "task_id": "t", "mode": "fix", "objective": "o", "current_predicate": "p"}


@unittest.skipUnless(Path("/usr/bin/sandbox-exec").is_file(), "macOS Seatbelt required")
class ParentShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "input.txt").write_text("needle\n")
        self.artifacts = root / "artifacts"
        self.artifacts.mkdir()
        self.action = {"mission": dict(MISSION), "context_summary": "check input",
                       "operations": ["read", "command"], "read_scopes": ["input.txt"], "source_read": True}

    def tearDown(self):
        self.temp.cleanup()

    def contract(self):
        return local.compile_context(self.workspace, self.action, artifact_root=self.artifacts)[1]

    def test_local_context_binds_the_policy_and_verifies_its_profile(self):
        contract = self.contract()
        policy = contract["parent_shell"]
        self.assertEqual((policy["schema_version"], policy["network"]), (ps.POLICY_SCHEMA, "inherit"))
        profile = Path(policy["evidence_root"]).parent / "profile.sb"
        self.assertEqual(hashlib.sha256(profile.read_bytes()).hexdigest(), policy["profile_sha256"])
        self.assertTrue(Path(policy["evidence_root"]).is_relative_to(self.artifacts / "parent-shell"))
        self.assertEqual(ps.verify_policy(contract, self.artifacts), profile)
        # Compiling the same action again reuses the bound profile.
        self.assertEqual(self.contract()["authorization_semantic_sha256"], contract["authorization_semantic_sha256"])
        with self.assertRaises(caller.EmbeddedNokiyError):
            ps.verify_policy(contract, self.workspace)
        os.chmod(profile, 0o600)
        profile.write_text(profile.read_text() + "(allow file-write*)\n")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "PROFILE_CHANGED"):
            ps.verify_policy(contract, self.artifacts)

    def test_no_parent_shell_without_an_artifact_root_or_a_read_operation(self):
        self.assertNotIn("parent_shell", local.compile_context(self.workspace, self.action)[1])
        create = {"mission": dict(MISSION), "context_summary": "create", "operations": ["create"],
                  "write_scopes": ["new.txt"], "target_paths": ["new.txt"]}
        contract = local.compile_context(self.workspace, create, artifact_root=self.artifacts)[1]
        self.assertNotIn("parent_shell", contract)
        self.assertNotIn("command", contract["allowed_operations"])

    def test_shell_lines_run_read_only_and_settle_their_process_group(self):
        shell = ps.ParentShell(self.workspace, self.contract(), self.artifacts)
        self.addCleanup(shell.close)
        cancel = threading.Event()

        def run(line, workdir=None, timeout=20):
            return shell.handle_request({"line": line, "workdir": workdir, "timeout_seconds": timeout}, "c", cancel)

        found = run("grep -c needle input.txt")
        self.assertEqual((found["exit_code"], found["stdout"].strip(), found["outcome"]), (0, "1", "known"))
        self.assertEqual(run("grep -c absent input.txt")["exit_code"], 1)
        self.assertNotEqual(run("echo changed > input.txt")["exit_code"], 0)
        self.assertEqual((self.workspace / "input.txt").read_text(), "needle\n")
        evidence = run('echo e > "$NOKIY_PARENT_SHELL_EVIDENCE/e.txt" && cat "$NOKIY_PARENT_SHELL_EVIDENCE/e.txt"')
        self.assertEqual(evidence["stdout"].strip(), "e")
        self.assertEqual(run("ps -o pid= -p $$ >/dev/null && echo ps-ok")["stdout"].strip(), "ps-ok")
        secrets = run("test -d ~/.ssh && (ls ~/.ssh >/dev/null 2>&1 && echo READ || echo DENIED) || echo ABSENT")
        self.assertNotEqual(secrets["stdout"].strip(), "READ")
        background = run("sleep 30 & echo started")
        self.assertEqual((background["exit_code"], background["process_group_empty"], background["outcome"]),
                         (0, True, "known"))
        started = time.monotonic()
        timed_out = run("sleep 30", timeout=1)
        self.assertEqual(timed_out["exit_code"], 124)
        self.assertIn("PARENT_SHELL_TIMEOUT", timed_out["stderr"])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(run("pwd", workdir="/")["exit_code"], 126)
        self.assertEqual(shell.calls, 9)

    def test_result_inspection_resolves_only_closed_parent_shell_failures(self):
        receipt = {**inspection._CLOSED_SHELL_FAILURE_RECEIPT, "termination_origin": "parent_shell",
                   "exit_code": 1, "pid": None}
        output = {"exit_code": 1, "stdout": "", "stderr": "", "message": "Exit code: 1",
                  "terminal_receipt": receipt, "terminal_receipt_path": "/receipt.json"}
        item = {"command_type": "zsh"}
        self.assertTrue(inspection._parent_shell_failure(item, output))
        self.assertFalse(inspection._inspect_shell_failure(item, output))
        for change in ({"pid": 7}, {"termination_origin": "workload"}, {"process_group_empty": False},
                       {"apply_patch_changes": ["input.txt"]}, {"timed_out": True}):
            self.assertFalse(inspection._parent_shell_failure(item, {**output, "terminal_receipt": {**receipt, **change}}),
                             change)
        contract = self.contract()
        self.assertTrue(inspection._admitted_parent_shell_contract(contract))
        self.assertFalse(inspection._admitted_parent_shell_contract({**contract, "allowed_operations": ["read"]}))
        self.assertFalse(inspection._admitted_parent_shell_contract({k: v for k, v in contract.items() if k != "parent_shell"}))

    def test_dcf_actions_get_the_shell_only_from_dcf_code_that_binds_it(self):
        code = self.workspace.parent / "dcf-code"
        (code / "scripts/ops/dcf").mkdir(parents=True)
        compiler = code / "scripts/ops/dcf/jspace.py"
        compiler.write_text('PARENT_SHELL_SCHEMA_VERSION = "nokiy_parent_shell_v1"\n')
        draft = {"artifact_root": str(self.artifacts)}
        action = {"mission": dict(MISSION), "operations": ["read"], "read_scopes": ["input.txt"]}
        with patch.object(full_stack, "_dcf_code_root", return_value=code):
            shaped = full_stack._with_parent_shell(action, self.workspace, draft)
            self.assertEqual(shaped["operations"], ["command", "read"])
            self.assertEqual(shaped["parent_shell"]["schema_version"], ps.POLICY_SCHEMA)
            self.assertEqual(action["operations"], ["read"], "the input action is not mutated")
            for unchanged in (dict(action, forbidden_effects=["command"]), dict(action, operations=["modify"]),
                              dict(action, inspect={})):
                self.assertIs(full_stack._with_parent_shell(unchanged, self.workspace, draft), unchanged)
            self.assertIs(full_stack._with_parent_shell(action, self.workspace, {}), action)
        compiler.write_text("# older DCF without the policy\n")
        with patch.object(full_stack, "_dcf_code_root", return_value=code):
            self.assertIs(full_stack._with_parent_shell(action, self.workspace, draft), action)


if __name__ == "__main__":
    unittest.main()

