"""Real caller preflight for non-DCF preparation and no-downgrade boundaries."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

import test_full_core as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import local_context as local


class LocalContextTests(unittest.TestCase):
    def test_directory_discovery_does_not_require_known_files(self):
        source = self.f.workspace / "src"
        source.mkdir()
        (source / "unknown.txt").write_text("discover me")
        self.action.update(read_scopes=[], target_paths=[], command_templates=[], read_directories=["src"])
        result = self.prepare()
        request = caller.load_request(self.output / "request.json")
        _, capsule, contract = caller._verify_context(request)
        self.assertEqual(contract["read_commands"]["roots"], ["src"])
        self.assertEqual(contract["read_scopes"], ["src/**"])
        self.assertEqual(contract["write_scopes"], [])
        self.assertEqual(capsule["dcf_generation"]["source_snapshot"], {})
        self.assertIn("--no-config", capsule["context_summary"])
        (source / "later.txt").write_text("another valid read")
        caller._verify_context(request)

    def test_directory_discovery_cannot_be_symlink(self):
        source = self.f.workspace / "src"
        source.symlink_to(self.f.root)
        self.action["read_directories"] = ["src"]
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.prepare()

    def test_directory_discovery_rechecks_identity(self):
        source = self.f.workspace / "src"
        source.mkdir()
        self.action["read_directories"] = ["src"]
        self.prepare()
        source.rename(self.f.workspace / "retired")
        source.mkdir()
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "STALE"):
            caller._verify_context(caller.load_request(self.output / "request.json"))

    def setUp(self):
        self.core = fixtures.FullCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.f = self.core.f
        self.draft = json.loads(self.f.request_path.read_text())
        for k in ("context_capsule", "jspace_contract", "execution_profile"):
            self.draft.pop(k, None)
        self.action = {"mission": {"mission_id": "flex", "task_id": "local", "mode": "DELIVERY",
            "objective": "Read exact input", "current_predicate": "input_read"},
            "context_summary": "Only read the named fixture; no deployment or broker effects.",
            "operations": ["read", "command"], "read_scopes": ["input.txt"], "write_scopes": [],
            "target_paths": ["input.txt"], "command_templates": [{"argv": ["cat", "input.txt"],
                "effects": ["read"], "targets": [{"operation": "read", "path": "input.txt", "argv_index": 1}]}]}
        (self.f.workspace / "input.txt").write_text("bounded input\n")
        self.output = self.f.root / "prepared-local"

    def prepare(self, surface=None):
        self.f.request_path.write_text(json.dumps(self.draft))
        action_path = self.f.root / "action-local.json"
        action_path.write_text(json.dumps(self.action))
        return stack.prepare_request(self.f.request_path, action_path, surface, self.output)

    def test_prepare_without_dcf_or_project_python(self):
        with patch.object(caller, "execute") as execute:
            result = self.prepare()
        execute.assert_not_called()
        self.assertEqual(result["context_mode"], local.MODE)
        self.assertEqual(result["execution_profile"], "direct")
        ready, capsule, contract = caller._verify_context(caller.load_request(self.output / "request.json"))
        self.assertEqual(ready["dcf_generation_id"], "")
        self.assertFalse(capsule["dcf_generation"]["dcf_available"])
        self.assertEqual(contract["matched_surface_ids"], [])
        self.assertEqual(contract["command_effect_policy"], "trusted_argv_effects_v1")

    def test_cli_without_surface(self):
        parsed = caller.build_parser().parse_args(["prepare", "--request", "draft.json", "--action", "action.json", "--output-dir", "/tmp/new"])
        self.assertIsNone(parsed.surface_id)

    def test_explicit_missing_surface_is_not_downgraded(self):
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare("invented")
        self.assertFalse(self.output.exists())

    def test_dcf_missing_interpreter_does_not_downgrade(self):
        entry = self.f.workspace / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.write_text("# DCF")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"): self.prepare("s")

    def test_dcf_broken_entry_does_not_downgrade(self):
        entry = self.f.workspace / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.symlink_to("/nonexistent-dcf")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"): self.prepare()

    def test_dcf_ancestor_blocks_subdirectory_downgrade(self):
        entry = self.f.root / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.write_text("# ancestor DCF")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"): self.prepare()

    def test_input_change_rejected_at_execution_boundary(self):
        self.prepare()
        (self.f.workspace / "input.txt").write_text("changed")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_new_dcf_integration_rejected_at_execution_boundary(self):
        self.prepare()
        entry = self.f.workspace / "scripts/ops/dcf.py"
        entry.parent.mkdir(parents=True); entry.write_text("# installed later")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_INVALID"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_local_scope_escape_and_globs_rejected(self):
        for name in ("../outside.txt", "/tmp/outside.txt", "**", ".git/config", "input.txt/../other"):
            with self.subTest(name=name), self.assertRaises(caller.EmbeddedNokiyError):
                local._file(self.f.workspace, name)

    def test_symlink_rejected_even_inside_root(self):
        (self.f.workspace / "link.txt").symlink_to("input.txt")
        with self.assertRaises(caller.EmbeddedNokiyError): local._file(self.f.workspace, "link.txt")

    def test_undeclared_command_read_rejected(self):
        self.action["command_templates"][0]["argv"] = ["cat", "outside.txt"]
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare()

    def test_shell_command_cannot_claim_read_only(self):
        self.action["command_templates"][0]["argv"] = ["sh", "-c", "touch outside"]
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare()

    def test_command_operand_binding_required(self):
        self.action["command_templates"][0]["targets"] = []
        with self.assertRaises(caller.EmbeddedNokiyError): self.prepare()

    def test_workspace_write_needs_explicit_authority(self):
        self.action.update(operations=["read", "create"], command_templates=[],
                           write_scopes=["answer.txt"], target_paths=["input.txt", "answer.txt"])
        self.draft["authority_effect"] = "none"
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "AUTHORITY_MISMATCH"): self.prepare()
        self.assertFalse((self.output / "request.json").exists())

    def test_workspace_write_and_absent_target_bound(self):
        self.action.update(operations=["read", "create"], command_templates=[],
                           write_scopes=["answer.txt"], target_paths=["input.txt", "answer.txt"])
        self.draft["authority_effect"] = "workspace"
        self.prepare()
        (self.f.workspace / "answer.txt").write_text("concurrent owner")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "LOCAL_CONTEXT_STALE"):
            caller.preflight(caller.load_request(self.output / "request.json"))

    def test_expired_local_context_cannot_use_dcf_action_freshness(self):
        capsule, contract = local.compile_context(self.f.workspace, self.action)
        gen = capsule["dcf_generation"]
        gen["generated_at"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        gen["action_freshness"] = {}
        contract["content_sha256"] = caller._canonical_sha256({k:v for k,v in contract.items() if k != "content_sha256"})
        capsule["semantic_sha256"] = caller._canonical_sha256({k:v for k,v in capsule.items() if k != "semantic_sha256"})
        self.f.context.write_text(json.dumps(capsule)); self.f.jspace.write_text(json.dumps(contract))
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "CONTEXT_STALE"):
            caller._verify_context(caller.load_request(self.f._write_request()))


if __name__ == "__main__":
    unittest.main()
