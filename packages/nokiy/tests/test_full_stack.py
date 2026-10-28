# SPDX-License-Identifier: MIT
"""New-request preparation only; real preflight with a synthetic pinned image."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import test_full_core as fixtures
import test_embedded_nokiy as embedded_fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import local_context


class FullStackTests(unittest.TestCase):
    def setUp(self):
        # DCF mocks must not intercept another module's real runtime probes.
        process_api = patch.object(stack, "subprocess", SimpleNamespace(**vars(subprocess)))
        process_api.start()
        self.addCleanup(process_api.stop)
        # These tests cover exact preparation and its refusals. The read-only fallback
        # that reprepares a refused action as inspect is covered by ReadOnlyRouteTests.
        self.exact_only = patch.object(stack, "_read_commands_as_inspect", return_value=None)
        self.exact_only.start()
        self.addCleanup(self.exact_only.stop)
        self.core = fixtures.FullCoreTests()
        self.core.setUp()
        self.addCleanup(self.core.doCleanups)
        self.f = self.core.f
        self.draft = json.loads(self.f.request_path.read_text())
        for key in ("context_capsule", "jspace_contract", "execution_profile",
                    "model", "reasoning_effort", "service_tier", "model_acceleration"):
            self.draft.pop(key, None)
        self.base_action = {
            "mission": {"mission_id": "fixture-mission", "task_id": "test", "mode": "DELIVERY",
                        "objective": "Inspect bounded fixture inputs", "current_predicate": "fixture_unknown"},
            "context_summary": "Inspect bounded fixture inputs.", "operations": ["read"],
        }
        self.action = self.f.root / "action.json"
        self.action.write_text(json.dumps(self.base_action))
        self.output = self.f.root / "prepared"
        compiler = self.f.workspace / "scripts/ops/dcf.py"
        compiler.parent.mkdir(parents=True)
        compiler.write_text("# synthetic DCF entry\n")
        # DCF checkouts without a J-Space compiler intentionally use local context.
        jspace = compiler.with_suffix("") / "jspace.py"
        jspace.parent.mkdir()
        jspace.write_text("# synthetic J-Space compiler\n")
        python = self.f.workspace / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("synthetic interpreter")
        self.compiled = {"task_context_capsule": json.loads(self.f.context.read_text()),
                         "contract": json.loads(self.f.jspace.read_text())}

    def run_prepare(self, surface_id="research_execution_engine"):
        self.f.request_path.write_text(json.dumps(self.draft))
        return stack.prepare_request(self.f.request_path, self.action, surface_id, self.output)

    def compiler(self):
        return patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(self.compiled), ""))

    def thin_worktree(self):
        local = self.f.workspace / ".venv"
        (local / "bin/python").unlink()
        (local / "bin").rmdir()
        local.rmdir()
        primary = self.f.root / "primary"
        common = primary / ".git"
        git_dir = common / "worktrees/fixture"
        git_dir.mkdir(parents=True)
        (self.f.workspace / ".git").write_text(f"gitdir: {git_dir}\n")
        (git_dir / "gitdir").write_text(f"{self.f.workspace / '.git'}\n")
        (git_dir / "commondir").write_text("../..\n")
        python = primary / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("synthetic shared interpreter")
        return SimpleNamespace(primary=primary, common=common, git_dir=git_dir, python=python,
                               registered=True, primary_common=common)

    def shared_git(self, fixture, worker=None):
        def run(argv, **kwargs):
            if argv[0] != "/usr/bin/git":
                if worker is not None:
                    return worker(argv, **kwargs)
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.compiled), "")
            if argv[1] == "rev-parse":
                root = kwargs["cwd"]
                rows = ((self.f.workspace, fixture.common, fixture.git_dir)
                        if root == self.f.workspace else
                        (fixture.primary, fixture.primary_common, fixture.primary_common))
                return subprocess.CompletedProcess(argv, 0, "\n".join(map(str, rows)) + "\n", "")
            self.assertEqual(argv, ["/usr/bin/git", "worktree", "list", "--porcelain", "-z"])
            self.assertEqual(kwargs["cwd"], fixture.primary)
            rows = [f"worktree {fixture.primary}\0HEAD {'a' * 40}\0\0"]
            if fixture.registered:
                rows.append(f"worktree {self.f.workspace}\0HEAD {'b' * 40}\0detached\0\0")
            return subprocess.CompletedProcess(argv, 0, "".join(rows).encode(), b"")
        return patch.object(stack.subprocess, "run", side_effect=run)

    def test_dcf_interpreter_local_existing_path_is_unchanged(self):
        with patch.object(stack.subprocess, "run") as run:
            self.assertEqual(stack._dcf_interpreter(self.f.workspace),
                             self.f.workspace / ".venv/bin/python")
            run.assert_not_called()

    def test_shared_interpreter_git_proofs_ignore_untrusted_path(self):
        fixture = self.thin_worktree()
        with patch.dict(os.environ, {"PATH": "/untrusted"}), self.shared_git(fixture) as run:
            self.assertEqual(stack._dcf_interpreter(self.f.workspace), fixture.python)
        self.assertEqual(run.call_count, 3)
        self.assertTrue(all(call.args[0][0] == "/usr/bin/git" for call in run.call_args_list))

    def test_registered_worktree_pointer_fifo_is_rejected_without_blocking(self):
        fifo = self.f.root / "pointer-fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "bounded regular file"):
            stack._git_pointer(fifo)

    def test_dcf_interpreter_absent_only_shared_selection_and_backpointer_checks(self):
        fixture = self.thin_worktree()
        with self.shared_git(fixture):
            self.assertEqual(stack._dcf_interpreter(self.f.workspace), fixture.python)
        self.assertFalse((self.f.workspace / ".venv").exists())
        for pointer, text in ((self.f.workspace / ".git", "gitdir: /unrelated\n"),
                              (fixture.git_dir / "gitdir", f"{fixture.primary / '.git'}\n"),
                              (fixture.git_dir / "commondir", "../../unrelated\n")):
            original = pointer.read_text()
            pointer.write_text(text)
            with self.subTest(pointer=pointer), self.shared_git(fixture):
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"):
                    stack._dcf_interpreter(self.f.workspace)
            pointer.write_text(original)

    def test_dcf_interpreter_broken_or_incomplete_local_environment_never_falls_back(self):
        fixture = self.thin_worktree()
        local = self.f.workspace / ".venv"
        local.symlink_to(self.f.root / "missing-environment")
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "no shared fallback"):
                stack._dcf_interpreter(self.f.workspace)
            run.assert_not_called()
        local.unlink()
        local.mkdir()
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "no shared fallback"):
                stack._dcf_interpreter(self.f.workspace)
            run.assert_not_called()
        self.assertTrue(fixture.python.is_file())

    def test_dcf_interpreter_rejects_unrelated_primary_unregistered_and_missing_shared(self):
        fixture = self.thin_worktree()
        unrelated = self.f.root / "unrelated/.git"
        unrelated.mkdir(parents=True)
        fixture.primary_common = unrelated
        with self.shared_git(fixture):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "backpointers"):
                stack._dcf_interpreter(self.f.workspace)
        fixture.primary_common = fixture.common
        fixture.registered = False
        with self.shared_git(fixture):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "currently registered"):
                stack._dcf_interpreter(self.f.workspace)
        fixture.registered = True
        fixture.python.unlink()
        with self.shared_git(fixture):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "primary DCF interpreter unavailable"):
                stack._dcf_interpreter(self.f.workspace)

    def test_shared_interpreter_is_used_by_surface_prepare_and_freshness_callers(self):
        fixture = self.thin_worktree()
        paths = ["src/one.py"]
        self.surface_action(paths)
        calls = []

        def worker(argv, **kwargs):
            calls.append((argv, kwargs))
            payload = (self.surface_response(paths, [["fixture"]])
                       if argv[-1] == stack._SURFACE_QUERY else self.compiled)
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        with self.shared_git(fixture, worker), patch.object(caller, "execute") as execute:
            surface = stack.query_surface_candidates(self.f.workspace, paths)
            prepared = self.run_prepare(surface_id=None)
            stack.verify_action_freshness(self.f.workspace, {})
        execute.assert_not_called()

        self.assertEqual(surface["status"], "SURFACE_CANDIDATES")
        self.assertEqual(prepared["status"], "PREPARED")
        self.assertEqual(len(calls), 4)
        for argv, kwargs in calls:
            self.assertEqual(argv[0], str(fixture.python))
            self.assertEqual(kwargs["cwd"], self.f.workspace)
            # The inherited environment, plus only the DCF implementation root.
            env = dict(kwargs.get("env", os.environ))
            self.assertIn(env.pop("NOKIY_DCF_CODE_ROOT", str(self.f.workspace)), {str(self.f.workspace)})
            self.assertEqual(env, dict(os.environ))
        self.assertEqual(calls[2][0][-1], str(Path(stack.__file__).with_name("dcf_prepare.py")))
        self.assertFalse((self.f.workspace / ".venv").exists())

    def test_thin_worktree_compiles_with_committed_primary_dcf_code(self):
        fixture = self.thin_worktree()
        jspace = fixture.primary / "scripts/ops/dcf/jspace.py"
        jspace.parent.mkdir(parents=True)
        jspace.write_text("# primary J-Space\n")
        (fixture.primary / "scripts/ops/dcf.py").write_text("# primary DCF\n")
        status = []
        with self.shared_git(fixture) as fake:
            git = fake.side_effect

            def run(argv, **kwargs):
                if argv[:2] == ["/usr/bin/git", "status"]:
                    self.assertEqual(kwargs["cwd"], fixture.primary)
                    self.assertEqual(argv[-len(stack._DCF_CODE_PATHS):], list(stack._DCF_CODE_PATHS))
                    return subprocess.CompletedProcess(argv, 0, "".join(status), "")
                return git(argv, **kwargs)

            fake.side_effect = run
            self.assertEqual(stack._dcf_code_root(self.f.workspace), fixture.primary)
            status.append(" M scripts/ops/dcf/jspace.py\n")
            self.assertEqual(stack._dcf_code_root(self.f.workspace), self.f.workspace)
            status.clear()
            jspace.unlink()
            self.assertEqual(stack._dcf_code_root(self.f.workspace), self.f.workspace)
            # A .venv linked to the primary's environment shares it; a link elsewhere does not.
            jspace.write_text("# primary J-Space\n")
            (fixture.primary / ".venv").mkdir(exist_ok=True)
            (self.f.workspace / ".venv").symlink_to(fixture.primary / ".venv", target_is_directory=True)
            self.assertEqual(stack._dcf_code_root(self.f.workspace), fixture.primary)
            (self.f.workspace / ".venv").unlink()
            (self.f.root / "other-venv").mkdir()
            (self.f.workspace / ".venv").symlink_to(self.f.root / "other-venv", target_is_directory=True)
            self.assertEqual(stack._dcf_code_root(self.f.workspace), self.f.workspace)
        self.assertTrue(stack._SURFACE_QUERY.startswith(stack._DCF_PATH))
        self.assertTrue(stack._with_verifier_grammar("pass").startswith(stack._DCF_PATH))
        self.assertEqual(stack._dcf_env(fixture.primary)["NOKIY_DCF_CODE_ROOT"], str(fixture.primary))

    def test_independent_project_keeps_its_own_dcf_code_without_git(self):
        with patch.object(stack.subprocess, "run") as run:
            self.assertEqual(stack._dcf_code_root(self.f.workspace), self.f.workspace)
            run.assert_not_called()

    def test_recursive_reads_and_discovery_roots_pair_both_ways(self):
        for name in ("src", "docs", ".cache", "real"):
            (self.f.workspace / name).mkdir(exist_ok=True)
        (self.f.workspace / "linked").symlink_to(self.f.workspace / "real", target_is_directory=True)
        base = {"operations": ["read", "command"], "source_read": True}
        adapted, repairs = stack.pair_discovery(
            dict(base, read_scopes=["a.py", "src/**"], read_directories=["docs"]), self.f.workspace)
        self.assertEqual(adapted["read_directories"], ["docs", "src"])
        self.assertEqual(adapted["read_scopes"], ["a.py", "src/**", "docs/**"])
        self.assertEqual([r["rule"] for r in repairs],
                         ["recursive_reads_to_discovery", "discovery_roots_to_recursive_reads"])
        for scopes in (["a.py", ".cache/**"], ["a.py", "linked/**"], ["a.py", "missing/**"], ["a.py"]):
            with self.subTest(scopes=scopes):
                action = dict(base, read_scopes=scopes)
                self.assertEqual(stack.pair_discovery(action, self.f.workspace), (action, []))
        many = dict(base, read_scopes=["src/**"], read_directories=[f"d{i}" for i in range(8)])
        self.assertEqual(stack.pair_discovery(many, self.f.workspace), (many, []))
        reads_only = {"operations": ["read"], "read_scopes": ["src/**"]}
        self.assertEqual(stack.pair_discovery(reads_only, self.f.workspace), (reads_only, []))

    def test_missing_source_read_of_a_writing_action_is_dropped_with_a_note(self):
        (self.f.workspace / "src").mkdir(exist_ok=True)
        (self.f.workspace / "src/kept.py").write_text("x = 1\n")
        action = dict(self.base_action, operations=["read", "command", "modify"], source_read=True,
                      read_scopes=["src/kept.py", "src/gone.py"], write_scopes=["src/kept.py"],
                      target_paths=["src/kept.py"])
        error = caller.EmbeddedNokiyError("NOKIY_FULL_STACK_PREPARATION_FAILED", "refused", diagnostics={
            "child_error_code": "JSPACE_SOURCE_READ_FILE_INVALID"})
        adapted, repair = stack._dcf_compile_repair(action, error, self.f.workspace)
        self.assertEqual(adapted["read_scopes"], ["src/kept.py"])
        self.assertIn("src/gone.py", adapted["context_summary"])
        self.assertEqual(repair["path"], "action.read_scopes")
        # A present file refused for another reason (link, permission) has nothing to drop.
        present = dict(action, read_scopes=["src/kept.py"])
        self.assertEqual(stack._dcf_compile_repair(present, error, self.f.workspace), (present, None))

    def test_linked_read_dcf_cannot_map_is_dropped_with_a_note(self):
        # 2026-10-23: output/exact598_reference linked to a sibling worktree whose mapping
        # contract was older, and a one-file test edit was refused as a whole.
        (self.f.workspace / "src").mkdir(exist_ok=True)
        (self.f.workspace / "src/kept.py").write_text("x = 1\n")
        elsewhere = self.f.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "ref.py").write_text("y = 2\n")
        (self.f.workspace / "linked").symlink_to(elsewhere, target_is_directory=True)
        action = dict(self.base_action, operations=["read", "command", "modify"], source_read=True,
                      read_scopes=["src/kept.py", "linked/ref.py"], write_scopes=["src/kept.py"],
                      target_paths=["src/kept.py"])
        error = caller.EmbeddedNokiyError("NOKIY_FULL_STACK_PREPARATION_FAILED", "refused", diagnostics={
            "child_error_code": "MAPPED_SOURCE_READ_CONTRACT_INVALID"})
        adapted, repair = stack._dcf_compile_repair(action, error, self.f.workspace)
        self.assertEqual(adapted["read_scopes"], ["src/kept.py"])
        self.assertIn("linked/ref.py", adapted["context_summary"])
        self.assertEqual(repair["rule"], "unmappable_reads_to_note")
        read_only = dict(action, operations=["read", "command"], write_scopes=[], target_paths=[])
        self.assertEqual(stack._dcf_compile_repair(read_only, error, self.f.workspace), (read_only, None))

    def test_projection_conflict_with_a_null_summary_states_the_mission(self):
        error = caller.EmbeddedNokiyError("NOKIY_FULL_STACK_PREPARATION_FAILED", "refused", diagnostics={
            "child_error_code": "TASK_CONTEXT_PROJECTION_CONFLICT"})
        for summary in (None, "  "):
            with self.subTest(summary=summary):
                adapted, repair = stack._dcf_compile_repair(
                    dict(self.base_action, context_summary=summary), error, self.f.workspace)
                self.assertEqual(repair["rule"], "projection_conflict_to_mission_summary")
                self.assertTrue(adapted["context_summary"].strip())
        stated = dict(self.base_action, context_summary="Stated.")
        self.assertEqual(stack._dcf_compile_repair(stated, error, self.f.workspace), (stated, None))

    def test_null_blank_and_empty_fields_state_omission(self):
        action = dict(self.base_action, context_summary=None, task_projection="  ", verifier_commands=[],
                      source_sections=[], read_scopes=[], focused_verifiers=None)
        adapted, repairs = stack._normalize_action_input(action)
        for key in ("context_summary", "task_projection", "verifier_commands", "source_sections",
                    "focused_verifiers"):
            self.assertNotIn(key, adapted)
        # An empty list keeps its meaning where empty is a valid statement.
        self.assertEqual(adapted["read_scopes"], [])
        self.assertEqual(sorted(r["path"] for r in repairs if r["rule"] == "empty_to_omission"),
                         ["action.context_summary", "action.focused_verifiers", "action.source_sections",
                          "action.task_projection", "action.verifier_commands"])
        self.assertEqual(stack._normalize_action_input(adapted)[1], [])

    def test_source_sections_take_one_spelling_and_stop_at_the_last_line(self):
        (self.f.workspace / "src").mkdir(exist_ok=True)
        (self.f.workspace / "src/short.py").write_text("".join(f"line{i}\n" for i in range(1, 11)))
        action = {"source_sections": [
            {"path": str(self.f.workspace / "src/short.py"), "start_line": 2, "end_line": 4},
            {"path": "src/short.py", "start_line": 8, "end_line": 30},
            {"path": "src/short.py", "start_line": 12, "end_line": 14}]}
        adapted, repairs = stack._source_sections_spelled(action, self.f.workspace)
        self.assertEqual(adapted["source_sections"], [
            {"path": "src/short.py", "start_line": 2, "end_line": 4},
            {"path": "src/short.py", "start_line": 8, "end_line": 10},
            # A range starting past the end names no lines; it keeps its refusal.
            {"path": "src/short.py", "start_line": 12, "end_line": 14}])
        self.assertEqual(repairs, [{"path": "action.source_sections", "rule": "source_sections_spelled",
                                    "fields": ["end_line", "path"]}])
        self.assertEqual(stack._source_sections_spelled(adapted, self.f.workspace), (adapted, []))

    def test_declared_exact_writes_take_their_write_operations(self):
        (self.f.workspace / "src").mkdir(exist_ok=True)
        (self.f.workspace / "src/old.py").write_text("x = 1\n")
        base = dict(self.base_action, operations=["read", "command"])
        for writes, added in ((["src/old.py"], ["modify"]), (["src/new.py"], ["create"]),
                              (["src/old.py", "src/new.py"], ["modify", "create"])):
            with self.subTest(writes=writes):
                adapted, repairs = stack._declared_writes_operable(dict(base, write_scopes=writes),
                                                                   self.f.workspace)
                self.assertEqual(adapted["operations"], ["read", "command", *added])
                self.assertEqual(repairs, [{"path": "action.operations",
                                            "rule": "write_scopes_to_" + "_".join(added)}])
        for changes in ({"write_scopes": ["src/*.py"]}, {"write_scopes": []},
                        {"write_scopes": ["src/old.py"], "forbidden_effects": ["modify"]},
                        {"write_scopes": ["src/old.py"], "forbidden_effects": ["write"]},
                        {"write_scopes": ["src/old.py"], "operations": ["read", "modify"]},
                        {"write_scopes": ["src/old.py"], "inspect": {}}):
            with self.subTest(changes=changes):
                action = dict(base, **changes)
                self.assertEqual(stack._declared_writes_operable(action, self.f.workspace), (action, []))

    def test_unmanaged_surface_query_cli_succeeds_end_to_end(self):
        root = self.f.root / "unmanaged"
        root.mkdir()
        (root / "source.py").write_text("content\n")
        output = io.StringIO()
        with redirect_stdout(output):
            code = caller.main(["query-surfaces", "--workspace", str(root), "--target", "source.py"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "CONTEXT_ROUTE_AVAILABLE")

    def test_shared_interpreter_rechecks_registration_and_content_after_query(self):
        fixture = self.thin_worktree()
        paths = ["src/one.py"]
        self.surface_action(paths)
        original = fixture.python.read_text()
        for drift in ("registration", "interpreter", "backpointer"):
            fixture.registered = True
            fixture.python.write_text(original)
            (fixture.git_dir / "gitdir").write_text(f"{self.f.workspace / '.git'}\n")

            def worker(argv, **kwargs):
                if drift == "registration":
                    fixture.registered = False
                elif drift == "interpreter":
                    fixture.python.write_text("replaced shared interpreter")
                else:
                    (fixture.git_dir / "gitdir").write_text(f"{fixture.primary / '.git'}\n")
                return subprocess.CompletedProcess(argv, 0,
                    json.dumps(self.surface_response(paths, [["fixture"]])), "")

            with self.subTest(drift=drift), self.shared_git(fixture, worker):
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"):
                    stack.query_surface_candidates(self.f.workspace, paths)
            self.assertFalse(self.output.exists())

    def test_shared_interpreter_rechecks_binding_before_preparation_publication(self):
        fixture = self.thin_worktree()
        compile_dcf = stack._compile_dcf

        def drift(*args, **kwargs):
            result = compile_dcf(*args, **kwargs)
            fixture.registered = False
            return result

        with self.shared_git(fixture), patch.object(stack, "_compile_dcf", side_effect=drift):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "currently registered"):
                self.run_prepare()
        self.assertFalse(self.output.exists())

    def compiled_navigation(self, action, navigation):
        from codex_collaboration_harness.dcf_prepare import bind_locators
        amended = {key: value for key, value in action.items()
                   if key not in {"surface_targets", "include_source_excerpt"}}
        navigation = navigation if isinstance(navigation, list) else [navigation]
        locators = []
        for item in navigation:
            row = item["result"]["resolved"][0]
            locators.append({"target": item["target"], "path": row["payload"]["path"],
                             "line": row["payload"]["line"], "generation_id": item["generation_id"]})
        amended = bind_locators(amended, locators, navigation[0]["generation_id"])
        compiled = json.loads(json.dumps(self.compiled))
        capsule = compiled["task_context_capsule"]
        capsule["context_summary"] = amended["context_summary"]
        capsule["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in capsule.items() if key != "semantic_sha256"})
        return dict(compiled, action=amended, navigation_locators=locators)

    def assert_public_action_identity(self, result, action):
        raw = caller._canonical_bytes(action)
        expected = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        summary = action.get("context_summary")
        if isinstance(summary, str):
            expected["context_summary_sha256"] = hashlib.sha256(summary.encode()).hexdigest()
            expected["context_summary_chars"] = len(summary)
        self.assertEqual(result["action"], expected)

    def test_default_direct_full_stack_real_preflight_no_provider(self):
        with self.compiler() as compiler, patch.object(caller, "execute") as execute:
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(compiler.call_count, 1)  # legacy exact ID pays no lookup
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(result["context_mode"], "dcf_jspace_required")
        self.assertEqual(result["execution_profile"], "direct")
        request = caller.load_request(self.output / "request.json")
        self.assertEqual((request.model, request.reasoning_effort, request.service_tier),
                         ("gpt-6.1-sol", "max", "default"))
        self.assertEqual(request.max_trajectory_bytes, self.draft["max_trajectory_bytes"])
        self.assertEqual(caller.preflight(request)["status"], "READY")
        self.assertEqual(compiler.call_args.args[0][-1],
                         str(Path(stack.__file__).with_name("dcf_prepare.py")))
        self.assertEqual(json.loads(compiler.call_args.kwargs["input"]),
                         {"surface_id": "research_execution_engine",
                          "action": json.loads(self.action.read_text())})
        self.assertFalse(result["provider_execution_started"])

    def test_terminal_delivery_is_derived_from_action_only_and_not_sent_to_compiler(self):
        for profile in ("direct", "balanced"):
            with self.subTest(profile=profile):
                self.output = self.f.root / ("prepared-terminal-" + profile)
                self.draft["execution_profile"] = profile
                action = {**self.base_action, "terminal_delivery": "evidence_only"}
                self.action.write_text(json.dumps(action))
                with self.compiler() as compiler, patch.object(caller, "execute") as execute:
                    result = self.run_prepare()
                execute.assert_not_called()
                self.assertEqual(compiler.call_count, 1)
                request = caller.load_request(self.output / "request.json")
                self.assertEqual(request.terminal_delivery, "evidence_only")
                self.assertEqual(request.to_wire()["terminal_delivery"], "evidence_only")
                self.assertNotIn("terminal_delivery", self.draft)
                compiled_action = json.loads((self.output / "preparation.json").read_text())["action"]
                self.assertEqual(compiled_action, {key: value for key, value in action.items()
                                                   if key != "terminal_delivery"})
                self.assertFalse(result["provider_execution_started"])

    def test_terminal_delivery_explicit_default_is_omitted_from_prepared_wire(self):
        self.action.write_text(json.dumps({**self.base_action,
                                          "terminal_delivery": "assistant_reply"}))
        with self.compiler():
            self.run_prepare()
        wire = json.loads((self.output / "request.json").read_text())
        self.assertNotIn("terminal_delivery", wire)
        request = caller.decode_request(wire)
        self.assertEqual(request.request_sha256, caller._canonical_sha256(wire))
        self.assertEqual(request.terminal_delivery, "assistant_reply")

    def test_terminal_delivery_invalid_action_or_draft_does_not_compile_or_publish(self):
        # None and "" state omission, which takes the assistant_reply default
        # (test_null_blank_and_empty_fields_state_omission); other values stay refused.
        for mode in ("unknown", 1, True, {}, []):
            self.action.write_text(json.dumps({**self.base_action,
                                              "terminal_delivery": mode}))
            with self.subTest(mode=mode), self.compiler() as compiler:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "terminal_delivery"):
                    self.run_prepare()
                compiler.assert_not_called()
                self.assertFalse(self.output.exists())
        self.action.write_text(json.dumps(self.base_action))
        self.draft["terminal_delivery"] = "evidence_only"
        with self.compiler() as compiler:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "terminal_delivery belongs in action"):
                self.run_prepare()
            compiler.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_terminal_delivery_action_requires_v3_full_core_profile(self):
        self.action.write_text(json.dumps({**self.base_action,
                                          "terminal_delivery": "evidence_only"}))
        for changes in ({"schema_version": caller.LEGACY_REQUEST_SCHEMA_VERSION},
                        {"schema_version": caller.PREVIOUS_REQUEST_SCHEMA_VERSION},
                        {"execution_profile": "native_once"}):
            original = dict(self.draft)
            self.draft.update(changes)
            with self.subTest(changes=changes), self.compiler() as compiler:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "full-core profile required"):
                    self.run_prepare()
                compiler.assert_not_called()
                self.assertFalse(self.output.exists())
            self.draft = original

    def test_prepare_missing_full_core_trajectory_budget_defaults_to_null(self):
        self.draft.pop("max_trajectory_bytes")
        with self.compiler(), patch.object(caller, "execute") as execute:
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["status"], "PREPARED")
        request = caller.load_request(self.output / "request.json")
        self.assertIsNone(request.max_trajectory_bytes)
        self.assertIsNone(request.to_wire(include_identity=False)["max_trajectory_bytes"])

    def test_explicit_tier_prepare_preflight_and_cli_preserve_unknown_delivery(self):
        # Explicit caller tier support is independent of the retired Fast alias.
        profile = {"model": "gpt-6-sol", "reasoning_effort": "max",
                   "execution_profile": "direct", "service_tier": "priority"}
        self.draft.update(profile)
        with self.compiler(), patch.object(caller, "execute") as execute:
            prepared = self.run_prepare()
        execute.assert_not_called()
        request = caller.load_request(self.output / "request.json")
        self.assertEqual({key: getattr(request, key) for key in profile}, profile)
        ready = prepared["preflight"]
        self.assertEqual(ready["status"], "READY")
        self.assertEqual((ready["model"], ready["reasoning_effort"], ready["execution_profile"],
                          ready["requested_service_tier"], ready["observed_service_tier"]),
                         ("gpt-6-sol", "max", "direct", "priority", None))
        runtime = SimpleNamespace(artifacts={"tura_exec": SimpleNamespace(path=self.f.root / "tura")})
        argv = full_core._cli_argv(request, runtime, self.f.root, "test-address")
        for flag, value in (("-m", "codex/gpt-6-sol"), ("-a", "direct"),
                            ("--model-reasoning-effort", "max"), ("--service-tier", "priority")):
            self.assertEqual(argv[argv.index(flag) + 1], value)

        default = caller.decode_request({**request.to_wire(include_identity=False),
                                         "service_tier": "default"})
        self.assertNotEqual((request.request_id, request.request_sha256),
                            (default.request_id, default.request_sha256))
        self.assertEqual((request.context_capsule, request.jspace_contract),
                         (default.context_capsule, default.jspace_contract))
        self.assertEqual(json.loads((self.output / "preparation.json").read_text())["action"],
                         json.loads(self.action.read_text()))
        self.assertEqual(json.loads(self.action.read_text())["operations"], ["read"])

    def test_dcf_prepare_cli_omits_raw_action_but_preserves_durable_inputs(self):
        summary = "DCF_CONTEXT_SENTINEL_" + "x" * 11900
        projection = "DCF_PROJECTION_SENTINEL_" + "y" * 1000
        action = {**self.base_action,
                  "context_summary": summary, "task_projection": {"note": projection}}
        self.action.write_text(json.dumps(action))
        self.f.request_path.write_text(json.dumps(self.draft))
        capsule = self.compiled["task_context_capsule"]
        capsule["context_summary"] = summary
        capsule["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in capsule.items() if key != "semantic_sha256"})
        stdout = io.StringIO()
        with self.compiler(), redirect_stdout(stdout):
            exit_code = caller.main(["prepare", "--request", str(self.f.request_path),
                                     "--action", str(self.action), "--surface-id",
                                     "research_execution_engine", "--output-dir", str(self.output)])
        self.assertEqual(exit_code, 0)
        public_json = stdout.getvalue()
        result = json.loads(public_json)
        self.assertEqual(result["context_mode"], "dcf_jspace_required")
        self.assert_public_action_identity(result, action)
        self.assertNotIn("DCF_CONTEXT_SENTINEL_", public_json)
        self.assertNotIn("DCF_PROJECTION_SENTINEL_", public_json)
        self.assertEqual(json.loads((self.output / "preparation.json").read_text())["action"], action)
        self.assertEqual(json.loads((self.output / "capsule.json").read_text()), capsule)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), self.compiled["contract"])
        caller.load_request(self.output / "request.json")
        old_public = {**result, "action": action}
        self.public_json_before_bytes = len(json.dumps(old_public, ensure_ascii=True, sort_keys=True).encode()) + 1
        self.public_json_after_bytes = len(public_json.encode())
        self.assertGreater(self.public_json_before_bytes - self.public_json_after_bytes, len(summary))

    def test_local_prepare_omits_raw_action_but_preserves_durable_inputs(self):
        (self.f.workspace / "scripts/ops/dcf.py").unlink()
        (self.f.workspace / "input.txt").write_text("bounded input\n")
        summary = "LOCAL_CONTEXT_SENTINEL_" + "z" * 11900
        action = {"mission": {"mission_id": "local", "task_id": "test", "mode": "DELIVERY",
                              "objective": "Read one input", "current_predicate": "input_read"},
                  "context_summary": summary, "operations": ["read", "command"],
                  "read_scopes": ["input.txt"], "write_scopes": [],
                  "target_paths": ["input.txt"], "command_templates": [{
                      "argv": ["cat", "input.txt"], "effects": ["read"],
                      "targets": [{"operation": "read", "path": "input.txt", "argv_index": 1}]}],
                  "forbidden_effects": []}
        self.action.write_text(json.dumps(action))
        self.f.request_path.write_text(json.dumps(self.draft))
        result = stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assert_public_action_identity(result, action)
        self.assertNotIn("LOCAL_CONTEXT_SENTINEL_", json.dumps(result))
        self.assertEqual(json.loads((self.output / "preparation.json").read_text())["action"], action)
        capsule = json.loads((self.output / "capsule.json").read_text())
        contract = json.loads((self.output / "jspace.json").read_text())
        self.assertEqual(capsule["context_summary"], summary)
        self.assertEqual(contract["dcf_generation"], capsule["dcf_generation"])
        self.assertEqual(result["context_mode"], local_context.MODE)
        caller._verify_context(caller.load_request(self.output / "request.json"))

    def test_explicit_profile_preserved(self):
        self.draft.update(execution_profile="balanced", model="gpt-5.6-sol", reasoning_effort="xhigh",
                          service_tier="priority")
        with self.compiler():
            self.run_prepare()
        r = caller.load_request(self.output / "request.json")
        self.assertEqual((r.execution_profile, r.model, r.reasoning_effort, r.service_tier),
                         ("balanced", "gpt-5.6-sol", "xhigh", "priority"))

    def test_action_focused_verifiers_is_rejected_before_local_or_dcf_preparation(self):
        base_action = json.loads(self.action.read_text())
        for managed in (False, True):
            # A null states omission (test_null_blank_and_empty_fields_state_omission).
            for value in ([], [{"argv": ["/usr/bin/true"]}]):
                for with_supported in (False, True):
                    action = {**base_action, "focused_verifiers": value}
                    if with_supported:
                        action["verifier_commands"] = [{"argv": ["/usr/bin/true"]}]
                    self.action.write_text(json.dumps(action))
                    with self.subTest(managed=managed, value=value, with_supported=with_supported):
                        with (patch.object(local_context, "dcf_root",
                                           return_value=self.f.workspace if managed else None) as route,
                              patch.object(local_context, "compile_context") as local_compile,
                              patch.object(stack, "_compile_dcf") as dcf_compile,
                              patch.object(stack, "_compile_navigated_dcf") as navigated_compile,
                              self.compiler() as compiler,
                              patch.object(caller, "decode_request") as decode,
                              patch.object(caller, "preflight") as preflight,
                              patch.object(caller, "execute") as execute,
                              patch.object(caller, "_write_create_only") as publish):
                            with self.assertRaisesRegex(caller.EmbeddedNokiyError,
                                    r"action\.focused_verifiers is unsupported; use action\.verifier_commands") as error:
                                self.run_prepare("research_execution_engine" if managed else None)
                        self.assertEqual(error.exception.code, "NOKIY_FULL_STACK_PREPARATION_FAILED")
                        for downstream in (route, local_compile, dcf_compile, navigated_compile,
                                           compiler, decode, preflight, execute, publish):
                            downstream.assert_not_called()
                        self.assertFalse(self.output.exists())

    def test_dcf_requested_verifier_is_compiled_with_request_artifacts(self):
        action = json.loads(self.action.read_text())
        action["verifier_commands"] = [{"argv": ["/usr/bin/true"]}]
        self.compiled["contract"].update(
            verifier_commands=action["verifier_commands"],
            verifier_artifact_root=self.draft["artifact_root"],
            focused_verifiers=[{"declared": True, "result_status": "pass"}])
        for explicit_root in (False, True):
            with self.subTest(explicit_root=explicit_root):
                if explicit_root:
                    action["verifier_artifact_root"] = self.draft["artifact_root"]
                self.action.write_text(json.dumps(action))
                self.output = self.f.root / ("prepared-matching" if explicit_root
                                            else "prepared-omitted")
                with (self.compiler() as compiler,
                      patch.object(caller, "preflight", return_value={"status": "READY"}),
                      patch.object(caller, "execute") as execute):
                    result = self.run_prepare()
                execute.assert_not_called()
                self.assertEqual(compiler.call_count, 1)
                self.assertEqual(result["status"], "PREPARED")
                self.assertFalse(result["provider_execution_started"])
                self.assertTrue((self.output / "request.json").is_file())
                self.assertTrue((self.output / "preparation.json").is_file())
                submitted = json.loads(compiler.call_args_list[0].kwargs["input"])["action"]
                self.assertEqual(submitted["verifier_artifact_root"], self.draft["artifact_root"])
                self.assertEqual(submitted["verifier_commands"], action["verifier_commands"])
                contract = json.loads((self.output / "jspace.json").read_text())
                self.assertEqual(contract["verifier_artifact_root"], self.draft["artifact_root"])
                self.assertEqual(contract["verifier_commands"], action["verifier_commands"])
                self.assertEqual(contract["focused_verifiers"], self.compiled["contract"]["focused_verifiers"])

    def test_dcf_compiler_cannot_silently_drop_requested_verifier(self):
        action = json.loads(self.action.read_text())
        action["verifier_commands"] = [{"argv": ["/usr/bin/true"]}]
        self.action.write_text(json.dumps(action))
        with self.compiler(), patch.object(caller, "preflight") as preflight:
            with self.assertRaises(caller.EmbeddedNokiyError) as error:
                self.run_prepare()
        self.assertEqual(error.exception.code, "NOKIY_FULL_STACK_VERIFIER_UNBOUND")
        preflight.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_dcf_verifier_artifact_root_must_match_request(self):
        action = json.loads(self.action.read_text())
        action.update(verifier_commands=[{"argv": ["/usr/bin/true"]}],
                      verifier_artifact_root=str(self.f.root / "other-artifacts"))
        self.action.write_text(json.dumps(action))
        with (self.compiler() as compiler,
              patch.object(caller, "preflight") as preflight,
              patch.object(caller, "execute") as execute):
            with self.assertRaises(caller.EmbeddedNokiyError) as error:
                self.run_prepare()
        blocker = "NOKIY_FULL_STACK_VERIFIER_ARTIFACT_ROOT_MISMATCH"
        self.assertEqual(error.exception.code, blocker)
        self.assertEqual(error.exception.detail, "verifier artifact root differs from request")
        failure = caller._failure(error.exception)
        self.assertEqual(failure["first_typed_blocker"], blocker)
        self.assertEqual(failure["detail_sha256"],
                         hashlib.sha256(b"verifier artifact root differs from request").hexdigest())
        compiler.assert_not_called()
        preflight.assert_not_called()
        execute.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_exact_dcf_navigation_adds_locator_without_widening_action(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        generation = self.compiled["contract"]["dcf_generation"]["generation_id"]
        action = {**self.base_action,
                  "read_scopes": ["scripts/ops/dcf/jspace.py"],
                  "context_summary": "Inspect the admitted function.",
                  "navigation_targets": [target]}
        self.action.write_text(json.dumps(action))
        navigation = {"capability_id": "source-navigation", "target": target,
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": generation,
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": target, "payload": {
                              "path": "scripts/ops/dcf/jspace.py", "line": 1233}}]}}
        compiled = self.compiled_navigation(action, navigation)
        responses = [subprocess.CompletedProcess([], 0, json.dumps(compiled), "")]
        with patch.object(stack.subprocess, "run", side_effect=responses) as run:
            prepared = self.run_prepare()
        self.assertEqual(len(run.call_args_list), 1)
        self.assertTrue(run.call_args.args[0][-1].endswith("dcf_prepare.py"))
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["action"], action)
        compiled_action = json.loads((self.output / "preparation.json").read_text())["action"]
        self.assertNotIn("navigation_targets", compiled_action)
        self.assertEqual(compiled_action["read_scopes"], action["read_scopes"])
        self.assertIn("scripts/ops/dcf/jspace.py:1233", compiled_action["context_summary"])
        self.assertEqual(prepared["navigation_locators"][0]["generation_id"], generation)

    def navigation_pages(self, target="symbol:scripts.ops.dcf.jspace.render_task_context_capsule"):
        action = {**self.base_action,
                  "read_scopes": ["scripts/ops/dcf/jspace.py"], "write_scopes": [],
                  "context_summary": "Inspect the admitted function.", "navigation_targets": [target]}
        symbol = {"entity_id": target, "payload": {"path": "scripts/ops/dcf/jspace.py", "line": 1233}}
        envelope = {"capability_id": "source-navigation", "target": target,
                    "freshness_status": "current", "domain_verdict": "pass", "projection_status": "pass",
                    "generation_id": self.compiled["contract"]["dcf_generation"]["generation_id"]}
        initial = dict(envelope, result={
            "resolved": [symbol], "graph": {"nodes": ["unrelated graph preview"]},
            "_projection": {"complete": False, "full_result_sha256": "a" * 64,
                            "page_offset": 0, "returned_item_count": 1, "next_offset": 1}})
        selection = dict(envelope, result={
            "result_pointer": "/resolved", "items": [symbol], "item_count": 1,
            "_projection": {"complete": True, "full_result_sha256": "a" * 64,
                            "page_offset": 0, "returned_item_count": 1, "next_offset": None,
                            "omitted_item_count": 0, "deferred_item_count": 0}})
        return action, initial, selection

    def navigate_action(self, action):
        return stack._navigate_known_symbols(
            self.f.workspace, self.f.workspace / "scripts/ops/dcf.py",
            self.f.workspace / ".venv/bin/python", action)

    def test_paginated_dcf_navigation_prepares_only_same_locator_context(self):
        action, initial, selection = self.navigation_pages()
        original = json.loads(json.dumps(action))
        complete = json.loads(json.dumps(initial))
        complete["result"]["_projection"]["complete"] = True
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(complete), "")) as fast_run:
            fast_action, fast_locators = self.navigate_action(action)
        self.assertEqual(fast_run.call_count, 1)
        self.action.write_text(json.dumps(action))
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "")
                for reply in (initial, selection, self.compiled)]) as run:
            amended, locators = self.navigate_action(action)
            stack._compile_dcf(self.f.workspace, self.f.workspace / "scripts/ops/dcf.py",
                               self.f.workspace / ".venv/bin/python", "fixture", amended)
        self.assertEqual(run.call_count, 3)  # two navigation queries, one compile
        argv = [str(self.f.workspace / ".venv/bin/python"), "-B",
                str(self.f.workspace / "scripts/ops/dcf.py"), "query", "--capability", "source-navigation",
                "--target", initial["target"], "--depth", "1", "--json"]
        self.assertEqual(run.call_args_list[0].args[0], argv)
        self.assertEqual(run.call_args_list[1].args[0], argv + [
            "--result-pointer", "/resolved", "--expected-generation-id", initial["generation_id"],
            "--expected-full-result-sha256", "a" * 64])
        for call in run.call_args_list[:2]:
            self.assertEqual(call.kwargs, {"cwd": self.f.workspace, "capture_output": True,
                                           "text": True, "timeout": 15})
        compiled_action = json.loads(run.call_args_list[2].kwargs["input"])["action"]
        expected = dict(action)
        expected.pop("navigation_targets")
        expected["context_summary"] += (
            "\nDCF source-navigation locators (context only; no read/write grant):\n"
            + initial["target"] + " -> scripts/ops/dcf/jspace.py:1233")
        self.assertEqual(compiled_action, expected)
        self.assertEqual(compiled_action, fast_action)  # same grants and context bytes, no graph/binding injection
        self.assertEqual(locators, fast_locators)
        self.assertEqual(action, original)
        self.assertEqual(amended, expected)

    def test_paginated_navigation_does_not_need_resolved_in_graph_preview(self):
        for preview in ([], None):
            with self.subTest(preview=preview):
                action, initial, selection = self.navigation_pages()
                if preview is None:
                    initial["result"].pop("resolved")
                else:
                    initial["result"]["resolved"] = preview
                with patch.object(stack.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(reply), "")
                        for reply in (initial, selection)]) as run:
                    _, locators = self.navigate_action(action)
                self.assertEqual(run.call_count, 2)
                self.assertEqual(locators[0]["target"], initial["target"])

    def assert_navigation_mutations_rejected(self, page_index, mutations):
        for field, value in mutations:
            with self.subTest(page=page_index, field=field, value=value):
                action, initial, selection = self.navigation_pages()
                pages = [initial, selection][:page_index + 1]
                # Copy because the preview and selection share their symbol fixture.
                pages = json.loads(json.dumps(pages))
                node = pages[page_index]
                parts = field.split(".")
                for part in parts[:-1]:
                    node = node[int(part)] if isinstance(node, list) else node[part]
                if value is ...:
                    node.pop(parts[-1])
                else:
                    node[parts[-1]] = value
                with patch.object(stack.subprocess, "run", side_effect=[
                        subprocess.CompletedProcess([], 0, json.dumps(reply), "") for reply in pages]) as run:
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                        self.navigate_action(action)
                self.assertEqual(run.call_count, page_index + 1)

    def test_incomplete_navigation_rejects_invalid_binding_without_continuation(self):
        self.assert_navigation_mutations_rejected(0, [
            ("capability_id", "surface-map"), ("target", "symbol:other.item"),
            ("freshness_status", "stale"), ("domain_verdict", "fail"), ("projection_status", "fail"),
            ("generation_id", ...), ("generation_id", None), ("generation_id", ""),
            ("generation_id", 1), ("generation_id", " "), ("generation_id", "gen\n1"),
            ("generation_id", "gen\x00"), ("generation_id", "--option"), ("generation_id", "g" * 256),
            ("result", None), ("result._projection", []),
            ("result._projection.complete", ...), ("result._projection.complete", None),
            ("result._projection.complete", 0),
            ("result._projection.full_result_sha256", ...), ("result._projection.full_result_sha256", None),
            ("result._projection.full_result_sha256", ""), ("result._projection.full_result_sha256", "a" * 63),
            ("result._projection.full_result_sha256", "z" * 64),
        ])

    def test_navigation_selection_rejects_unbound_or_partial_locators(self):
        self.assert_navigation_mutations_rejected(1, [
            ("capability_id", "surface-map"), ("target", "symbol:other.item"), ("target", ...),
            ("freshness_status", "stale"), ("domain_verdict", "fail"), ("projection_status", "fail"),
            ("generation_id", "other-generation"), ("generation_id", ...), ("generation_id", None),
            ("result", []), ("result.result_pointer", ...), ("result.result_pointer", "/resolved/0"),
            ("result.result_pointer", "/graph"), ("result.items", ...), ("result.items", []),
            ("result.items", [{}, {}]), ("result.items", {}), ("result.items", [None]),
            ("result.item_count", ...), ("result.item_count", 0), ("result.item_count", 2),
            ("result.item_count", True), ("result.item_count", 1.0), ("result.item_count", "1"),
            ("result._projection", None), ("result._projection.complete", ...),
            ("result._projection.complete", False), ("result._projection.complete", 1),
            ("result._projection.full_result_sha256", ...),
            ("result._projection.full_result_sha256", "b" * 64),
            ("result._projection.page_offset", ...), ("result._projection.page_offset", 1),
            ("result._projection.page_offset", False), ("result._projection.page_offset", 0.0),
            ("result._projection.returned_item_count", ...), ("result._projection.returned_item_count", 0),
            ("result._projection.returned_item_count", 2), ("result._projection.returned_item_count", True),
            ("result._projection.next_offset", ...), ("result._projection.next_offset", 1),
            ("result._projection.deferred_item_refs", [{"pointer": "/resolved/0"}]),
            ("result._projection.deferred_value_refs", [{"pointer": "/resolved/0/payload"}]),
            ("result._projection.deferred_item_refs", None),
            ("result._projection.deferred_value_refs", {}),
            ("result._projection.omitted_item_count", 1), ("result._projection.deferred_item_count", 1),
            ("result._projection.omitted_item_count", None), ("result._projection.deferred_item_count", True),
            ("result.items.0.entity_id", "symbol:other.item"), ("result.items.0.payload", {}),
            ("result.items.0.payload.path", "/absolute.py"), ("result.items.0.payload.path", "../outside.py"),
            ("result.items.0.payload.path", "bad\npath.py"), ("result.items.0.payload.path", ""),
            ("result.items.0.payload.path", None), ("result.items.0.payload.line", ...),
            ("result.items.0.payload.line", 0), ("result.items.0.payload.line", -1),
            ("result.items.0.payload.line", True), ("result.items.0.payload.line", 1.0),
        ])

    def test_navigation_transport_failures_never_retry(self):
        failures = [subprocess.CompletedProcess([], 2, "{}", "failed"),
                    subprocess.CompletedProcess([], 0, "é" * 32769, ""),
                    subprocess.CompletedProcess([], 0, "not json", ""),
                    subprocess.CompletedProcess([], 0, "null", ""),
                    subprocess.CompletedProcess([], 0, "[]", ""),
                    OSError("unavailable"), subprocess.TimeoutExpired("dcf", 15)]
        for page_index in (0, 1):
            for failure in failures:
                with self.subTest(page=page_index, failure=failure):
                    action, initial, _ = self.navigation_pages()
                    responses = ([subprocess.CompletedProcess([], 0, json.dumps(initial), "")]
                                 if page_index else []) + [failure]
                    with patch.object(stack.subprocess, "run", side_effect=responses) as run:
                        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                            self.navigate_action(action)
                    self.assertEqual(run.call_count, page_index + 1)

    def test_paginated_navigation_preserves_cross_target_generation_check(self):
        action, first, _ = self.navigation_pages()
        first["result"]["_projection"]["complete"] = True
        _, second, _ = self.navigation_pages("symbol:other.item")
        second["generation_id"] = "other-generation"
        action["navigation_targets"].append(second["target"])
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "")
                for reply in (first, second)]) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.navigate_action(action)
        self.assertEqual(run.call_count, 2)  # drift blocks even the bound selection

    def test_paginated_navigation_preserves_compile_generation_check(self):
        action, initial, selection = self.navigation_pages()
        initial["generation_id"] = selection["generation_id"] = "old-generation"
        self.action.write_text(json.dumps(action))
        compiled = self.compiled_navigation(action, initial)
        with patch.object(stack.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 0, json.dumps(compiled), "")) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.output.exists())

    def surface_action(self, paths=None):
        paths = paths or ["src/one.py"]
        for name in paths:
            target = self.f.workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# source\n")
        action = {**self.base_action,
                  "read_scopes": [paths[0]], "context_summary": "Inspect bounded inputs.",
                  "surface_targets": paths}
        self.action.write_text(json.dumps(action))
        return action

    def surface_response(self, paths, matches, **changes):
        wire = {"schema_version": "dcf_query_response_v2", "capability_id": "surface-map",
                "target": None, "depth": 2, "freshness_status": "current",
                "domain_verdict": "pass", "projection_status": "pass",
                "generation_id": self.compiled["contract"]["dcf_generation"]["generation_id"],
                "safety": {"authority_effect": "none", "no_apply": True,
                           "protected_mutation_authorized": False,
                           "command_queue_authority": "proposal_only"},
                "result": {"surface_count": 3, "full_result_sha256": "a" * 64,
                           "paths": paths, "matches": matches, "complete": True}}
        wire.update(changes)
        return wire

    def test_surface_query_returns_candidates_without_selection_or_preparation(self):
        paths = ["src/one.py", "src/two.py"]
        self.surface_action(paths)
        for matches, common in (([["other", "fixture"], ["fixture", "other"]], ["fixture", "other"]),
                                ([[], ["fixture"]], []),
                                ([["fixture"], ["fixture"]], ["fixture"])):
            with (self.subTest(matches=matches),
                  patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                      [], 0, json.dumps(self.surface_response(paths, matches)), "")) as run,
                  patch.object(stack, "prepare_request") as prepare,
                  patch.object(caller, "execute") as execute):
                result = stack.query_surface_candidates(self.f.workspace, paths)
            self.assertEqual(run.call_count, 1)
            prepare.assert_not_called()
            execute.assert_not_called()
            self.assertEqual(result["status"], "SURFACE_CANDIDATES")
            self.assertEqual(result["common_surface_ids"], common)
            self.assertEqual(result["candidates"], [sorted(row) for row in matches])
            self.assertEqual(result["authority_effect"], "none")
            self.assertFalse(result["selection_grants_read_write"])
            self.assertFalse(result["provider_execution_started"])
            self.assertEqual(result["native_mutation_count"], 0)
            self.assertNotIn("selected_surface_id", result)
            self.assertNotIn("request", result)
            self.assertFalse(self.output.exists())

    def test_unmanaged_surface_query_reports_existing_route_without_execution(self):
        root = self.f.root / "unmanaged"
        root.mkdir()
        (root / "source.py").write_text("not exposed by metadata query\n")
        with (patch.object(stack.subprocess, "run") as run,
              patch.object(stack, "prepare_request") as prepare,
              patch.object(caller, "execute") as execute):
            result = stack.query_surface_candidates(root, ["source.py"])
        self.assertEqual(result["status"], "CONTEXT_ROUTE_AVAILABLE")
        self.assertEqual(result["context_mode"], local_context.MODE)
        self.assertEqual(result["dcf_state"], "not_configured")
        self.assertEqual(result["map_cache_state"], "not_applicable")
        self.assertFalse(result["surface_required"])
        self.assertFalse(result["selection_grants_read_write"])
        self.assertFalse(result["provider_execution_started"])
        self.assertEqual(result["native_mutation_count"], 0)
        self.assertEqual(result["authority_effect"], "none")
        self.assertNotIn("not exposed", json.dumps(result))
        run.assert_not_called()
        prepare.assert_not_called()
        execute.assert_not_called()

    def test_unmanaged_surface_query_still_validates_targets(self):
        root = self.f.root / "unmanaged"
        root.mkdir()
        source = root / "source.py"
        source.write_text("content\n")
        (root / "link.py").symlink_to(source)
        os.link(source, root / "hard.py")
        for targets in ([], ["missing.py"], ["../outside"], ["*"], ["link.py"],
                        ["source.py", "source.py"], ["source.py", "hard.py"], [None]):
            with self.subTest(targets=targets), patch.object(stack.subprocess, "run") as run:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                    stack.query_surface_candidates(root, targets)
                run.assert_not_called()

    def test_broken_dcf_marker_is_not_unmanaged(self):
        root = self.f.root / "broken"
        marker = root / "scripts/ops/dcf.py"
        marker.parent.mkdir(parents=True)
        marker.symlink_to(root / "missing.py")
        (root / "source.py").write_text("content\n")
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"):
                stack.query_surface_candidates(root, ["source.py"])
            run.assert_not_called()

    def test_surface_query_keeps_validation_and_no_local_downgrade(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        for changes in ({"freshness_status": "stale"}, {"domain_verdict": "blocked"},
                        {"result": {"complete": False}}):
            with (self.subTest(changes=changes), patch.object(stack.subprocess, "run", return_value=
                  subprocess.CompletedProcess([], 0, json.dumps(self.surface_response(paths, [["fixture"]],
                                                                            **changes)), ""))):
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                    stack.query_surface_candidates(self.f.workspace, paths)
        for targets in ([], ["../outside.py"], ["src/**"], paths * 2, ["src/missing.py"],
                        ["src/one.py"] * 5):
            with self.subTest(targets=targets), patch.object(stack.subprocess, "run") as run:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                    stack.query_surface_candidates(self.f.workspace, targets)
                run.assert_not_called()
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"):
                stack.query_surface_candidates(self.f.workspace / "src", ["one.py"])
            (self.f.workspace / ".venv/bin/python").unlink()
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "DCF_UNAVAILABLE"):
                stack.query_surface_candidates(self.f.workspace, paths)
            run.assert_not_called()

    def test_surface_query_rejects_link_and_target_replacement(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        (self.f.workspace / "src/link.py").symlink_to("one.py")
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                stack.query_surface_candidates(self.f.workspace, ["src/link.py"])
            run.assert_not_called()

        def replaced(*args, **kwargs):
            target = self.f.workspace / paths[0]
            target.rename(target.with_name("old.py"))
            target.write_text("# replacement\n")
            return subprocess.CompletedProcess([], 0, json.dumps(self.surface_response(paths, [["fixture"]])), "")

        with patch.object(stack.subprocess, "run", side_effect=replaced):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.query_surface_candidates(self.f.workspace, paths)

    def test_cli_surface_query_has_no_action_surface_id_or_worker(self):
        result = {"status": "SURFACE_CANDIDATES", "common_surface_ids": [],
                  "authority_effect": "none", "selection_grants_read_write": False}
        output = io.StringIO()
        with (patch.object(stack, "query_surface_candidates", return_value=result) as query,
              patch.object(stack, "prepare_request") as prepare,
              patch.object(caller, "execute") as execute, redirect_stdout(output)):
            code = caller.main(["query-surfaces", "--workspace", str(self.f.workspace),
                                "--target", "src/one.py", "--target", "src/two.py"])
        query.assert_called_once_with(self.f.workspace, ["src/one.py", "src/two.py"])
        prepare.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), result)

    def test_surface_query_uses_complete_legacy_or_advertised_full_api(self):
        for explicit_full in (False, True):
            with self.subTest(explicit_full=explicit_full):
                data = {"surfaces": [{"surface_id": "fixture", "display": "\u5de5\u4f5c\U0001f680"}]}
                wire = {"freshness_status": "current", "projection_status": "pass",
                        "domain_verdict": "pass", "result": data}
                response = SimpleNamespace(**wire, model_dump=lambda **kwargs: dict(wire))
                calls = []

                def legacy(runtime, *, capability_id):
                    calls.append({"capability_id": capability_id})
                    return response, 0

                def current(runtime, *, capability_id, full_output=False):
                    calls.append({"capability_id": capability_id, "full_output": full_output})
                    return response, 0

                modules = {name: ModuleType(name) for name in (
                    "scripts.ops.dcf.collectors", "scripts.ops.dcf.query", "scripts.ops.dcf.runtime",
                    "scripts.ops.dcf.storage")}
                modules["scripts.ops.dcf.storage"].GenerationUnavailable = type("GenerationUnavailable", (Exception,), {})
                modules["scripts.ops.dcf.collectors"].path_matches_surface = lambda path, row: True
                modules["scripts.ops.dcf.query"].query_capability = current if explicit_full else legacy
                modules["scripts.ops.dcf.runtime"].DcfRuntime = lambda root: root
                output = io.StringIO()
                with (patch.dict(sys.modules, modules), patch.object(sys, "stdin", io.StringIO('["src/one.py"]')),
                      redirect_stdout(output)):
                    exec(stack._SURFACE_QUERY, {})
                result = json.loads(output.getvalue())["result"]
                expected = {"capability_id": "surface-map"}
                if explicit_full:
                    expected["full_output"] = True
                self.assertEqual(calls, [expected])
                self.assertTrue(result["complete"])
                self.assertEqual(result["matches"], [["fixture"]])
                self.assertEqual(result["full_result_sha256"], hashlib.sha256(json.dumps(
                    data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_surface_query_reports_writable_membership_without_read_only_surfaces(self):
        data = {"surfaces": [{"surface_id": "projection", "read_only": True, "allowed_write_scopes": []},
                             {"surface_id": "runtime", "allowed_write_scopes": ["engine/**"]}]}
        wire = {"freshness_status": "current", "projection_status": "pass", "domain_verdict": "pass", "result": data}
        response = SimpleNamespace(**wire, model_dump=lambda **kwargs: dict(wire))
        modules = {name: ModuleType(name) for name in (
            "scripts.ops.dcf.collectors", "scripts.ops.dcf.query", "scripts.ops.dcf.runtime",
            "scripts.ops.dcf.storage")}
        modules["scripts.ops.dcf.storage"].GenerationUnavailable = type("GenerationUnavailable", (Exception,), {})
        modules["scripts.ops.dcf.collectors"].path_matches_surface = lambda path, row: True
        modules["scripts.ops.dcf.query"].query_capability = lambda runtime, *, capability_id: (response, 0)
        modules["scripts.ops.dcf.runtime"].DcfRuntime = lambda root: root
        output = io.StringIO()
        with (patch.dict(sys.modules, modules), patch.object(sys, "stdin", io.StringIO('["ui/app.py"]')),
              redirect_stdout(output)):
            exec(stack._SURFACE_QUERY, {})
        result = json.loads(output.getvalue())["result"]
        self.assertEqual(result["matches"], [["projection", "runtime"]])
        self.assertEqual(result["write_matches"], [[]])
        self.assertEqual(result["writable_matches"], [["runtime"]])

    def test_surface_generation_unavailable_is_typed_without_worker_or_fallback(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        modules = {name: ModuleType(name) for name in (
            "scripts.ops.dcf.collectors", "scripts.ops.dcf.query", "scripts.ops.dcf.runtime",
            "scripts.ops.dcf.storage")}
        missing = type("GenerationUnavailable", (Exception,), {})
        modules["scripts.ops.dcf.storage"].GenerationUnavailable = missing
        modules["scripts.ops.dcf.collectors"].path_matches_surface = lambda path, row: True
        modules["scripts.ops.dcf.runtime"].DcfRuntime = lambda root: root

        def unavailable(runtime, *, capability_id):
            raise missing("private-path-and-detail-must-not-be-returned")

        modules["scripts.ops.dcf.query"].query_capability = unavailable
        output = io.StringIO()
        with (patch.dict(sys.modules, modules), patch.object(sys, "stdin", io.StringIO(json.dumps(paths))),
              redirect_stdout(output), self.assertRaises(SystemExit) as exited):
            exec(stack._SURFACE_QUERY, {})
        self.assertEqual(exited.exception.code, 2)
        envelope = output.getvalue()
        output = io.StringIO()
        with (patch.object(stack.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 2, envelope, "")) as run,
              patch.object(stack, "prepare_request") as prepare,
              patch.object(caller, "execute") as execute, redirect_stdout(output)):
            self.assertEqual(caller.main(["query-surfaces", "--workspace", str(self.f.workspace),
                                          "--target", paths[0]]), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["first_typed_blocker"], "NOKIY_FULL_STACK_DCF_GENERATION_UNAVAILABLE")
        self.assertEqual(result["diagnostics"]["stage"], "dcf_surface_query")
        self.assertEqual(result["diagnostics"]["stdout_sha256"], hashlib.sha256(envelope.encode()).hexdigest())
        self.assertFalse(result["fallback_used"])
        self.assertNotIn("private-path", output.getvalue())
        self.assertEqual(run.call_count, 1)
        prepare.assert_not_called()
        execute.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_surface_missing_generation_requires_exact_typed_envelope(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        valid = {"schema_version": "nokiy_surface_query_error_v1",
                 "error_code": "DCF_GENERATION_UNAVAILABLE", "authority_effect": "none", "no_apply": True}
        for rc, stdout in ((1, json.dumps(valid)), (2, "GenerationUnavailable"),
                           (2, json.dumps({**valid, "no_apply": 1})),
                           (2, json.dumps({**valid, "authority_effect": "workspace"})),
                           (2, json.dumps({**valid, "extra": "value"})),
                           (2, json.dumps(valid)[:-1] + ', "no_apply": true}')):
            with (self.subTest(rc=rc, stdout=stdout), patch.object(stack.subprocess, "run",
                  return_value=subprocess.CompletedProcess([], rc, stdout, "private stderr"))):
                with self.assertRaises(caller.EmbeddedNokiyError) as failure:
                    stack.query_surface_candidates(self.f.workspace, paths)
            self.assertEqual(failure.exception.code, "NOKIY_FULL_STACK_SURFACE_UNVERIFIED")
            result = caller._failure(failure.exception)
            self.assertEqual(result["diagnostics"]["stderr_bytes"], len("private stderr"))
            self.assertNotIn("private stderr", json.dumps(result))

    def test_surface_unique_omitted_id_is_diagnostic_only(self):
        paths = ["src/one.py", "src/two.py"]
        action = self.surface_action(paths)
        wire = self.surface_response(paths, [["other", "fixture"], ["fixture"]])
        replies = [subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                   subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]
        self.f.request_path.write_text(json.dumps(self.draft))
        with (patch.object(stack.subprocess, "run", side_effect=replies) as run,
              patch.object(caller, "execute") as execute):
            result = stack.prepare_request(self.f.request_path, self.action, None, self.output)
        execute.assert_not_called()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][:3],
                         [str(self.f.workspace / ".venv/bin/python"), "-B", "-c"])
        self.assertIn("query_capability", run.call_args_list[0].args[0][3])
        self.assertIn("path_matches_surface", run.call_args_list[0].args[0][3])
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 30)
        self.assertEqual(run.call_args_list[0].kwargs["input"], json.dumps(paths))
        compile_envelope = json.loads(run.call_args_list[1].kwargs["input"])
        self.assertEqual(compile_envelope["surface_id"], "fixture")
        compiled_action = compile_envelope["action"]
        self.assertNotIn("surface_targets", compiled_action)
        self.assertEqual(compiled_action["read_scopes"], action["read_scopes"])
        self.assertEqual(compiled_action["operations"], ["read"])
        self.assertEqual(compiled_action["context_summary"], action["context_summary"])
        self.assertEqual(result["surface_resolution"]["common_surface_ids"],
                         ["fixture"])
        self.assertNotIn("surface_resolution", (self.output / "capsule.json").read_text())

    def test_surface_explicit_match_and_no_legacy_lookup(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["other", "fixture"]])
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]) as run:
            result = self.run_prepare("fixture")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(result["surface_resolution"]["selected_surface_id"],
                         "fixture")

    def test_surface_targets_do_not_modify_write_or_operation_declarations(self):
        paths = ["src/one.py"]
        action = self.surface_action(paths)
        action.update(write_scopes=["answer.txt"], declared_targets=["answer.txt"],
                      operations=["read", "modify"])
        wire = self.surface_response(paths, [["fixture"]])
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(wire), "")) as run:
            compiled, surface_id, evidence, _ = stack._resolve_surface_targets(
                self.f.workspace, self.f.workspace / ".venv/bin/python", action, "fixture")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(surface_id, "fixture")
        self.assertEqual(evidence["targets"], paths)
        self.assertEqual(compiled, {key: value for key, value in action.items()
                                    if key != "surface_targets"})

    def test_surface_lookup_and_symbol_navigation_share_compiled_generation(self):
        paths = ["src/one.py"]
        action = self.surface_action(paths)
        action["navigation_targets"] = ["symbol:package.item"]
        self.action.write_text(json.dumps(action))
        generation = self.compiled["contract"]["dcf_generation"]["generation_id"]
        navigation = {"capability_id": "source-navigation", "target": "symbol:package.item",
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": generation,
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": "symbol:package.item", "payload": {
                              "path": "src/one.py", "line": 1}}]}}
        replies = [self.surface_response(paths, [["fixture"]]), self.compiled_navigation(action, navigation)]
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "") for reply in replies]) as run:
            result = self.run_prepare(None)
        self.assertEqual(run.call_count, 2)
        compiled_action = json.loads((self.output / "preparation.json").read_text())["action"]
        self.assertNotIn("surface_targets", compiled_action)
        self.assertNotIn("navigation_targets", compiled_action)
        self.assertEqual(compiled_action["read_scopes"], action["read_scopes"])
        self.assertEqual(result["navigation_locators"][0]["generation_id"], generation)

    def test_surface_navigation_generation_disagreement_blocks_compilation(self):
        paths = ["src/one.py"]
        action = self.surface_action(paths)
        action["navigation_targets"] = ["symbol:package.item"]
        self.action.write_text(json.dumps(action))
        navigation = {"capability_id": "source-navigation", "target": "symbol:package.item",
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": "other-generation",
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": "symbol:package.item", "payload": {
                              "path": "src/one.py", "line": 1}}]}}
        replies = [self.surface_response(paths, [["fixture"]]), self.compiled_navigation(action, navigation)]
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(reply), "") for reply in replies]) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare(None)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_surface_resolution_failures_do_not_compile_or_send(self):
        paths = ["src/one.py", "src/two.py"]
        self.surface_action(paths)
        cases = [
            ([[], ["one"]], {}, "SURFACE_UNRESOLVED", None),
            ([["one"], ["two"]], {}, "SURFACE_UNRESOLVED", None),
            ([["one", "two"], ["one", "two"]], {}, "SURFACE_AMBIGUOUS", None),
            ([["one"], ["one"]], {}, "SURFACE_MISMATCH", "wrong"),
            ([["one"], ["one"]], {"freshness_status": "stale"}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"domain_verdict": "blocked"}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"generation_id": ""}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"result": {"complete": False}}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"result": {"complete": True, "paths": paths,
              "matches": [["one"], ["one"]], "surface_count": 3,
              "full_result_sha256": "bad"}}, "SURFACE_UNVERIFIED", None),
            ([["one"], ["one"]], {"result": {"complete": True, "paths": [paths[0]],
              "matches": [["one"], ["one"]], "surface_count": 3,
              "full_result_sha256": "a" * 64}}, "SURFACE_UNVERIFIED", None),
        ]
        for i, (matches, changes, error, explicit) in enumerate(cases):
            with self.subTest(case=i):
                wire = self.surface_response(paths, matches, **changes)
                self.f.request_path.write_text(json.dumps(self.draft))
                with (patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                        [], 0, json.dumps(wire), "")) as run,
                      patch.object(caller, "execute") as execute):
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, error):
                        stack.prepare_request(self.f.request_path, self.action, explicit,
                                              self.f.root / ("fail-" + str(i)))
                self.assertEqual(run.call_count, 1)
                execute.assert_not_called()

    def test_surface_query_failure_and_output_limit_are_unverified(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        self.f.request_path.write_text(json.dumps(self.draft))
        for i, process in enumerate((
                subprocess.CompletedProcess([], 2, "", "failure"),
                subprocess.CompletedProcess([], 0, "x" * 65537, ""),
                subprocess.CompletedProcess([], 0, "not-json", ""))):
            with self.subTest(case=i), patch.object(stack.subprocess, "run", return_value=process) as run:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                    stack.prepare_request(self.f.request_path, self.action, None,
                                          self.f.root / ("query-fail-" + str(i)))
            self.assertEqual(run.call_count, 1)
        with patch.object(stack.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 30)) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 1)

    def test_missing_dcf_surface_reports_a_typed_issue(self):
        self.f.request_path.write_text(json.dumps(self.draft))
        with self.assertRaises(caller.EmbeddedNokiyError) as caught:
            stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(caught.exception.detail, "DCF-managed workspace requires an exact surface")
        issue = [{"path": "surface_id", "reason": "surface_required"}]
        self.assertEqual(caught.exception.issues, issue)
        self.assertEqual(caller._failure(caught.exception)["issues"], issue)
        self.assertFalse(self.output.exists())

    def test_surface_generation_drift_blocks_after_compilation(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["fixture"]], generation_id="older")
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]) as run:
            self.f.request_path.write_text(json.dumps(self.draft))
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_surface_compiler_must_bind_selected_id(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["other"]])
        self.f.request_path.write_text(json.dumps(self.draft))
        with patch.object(stack.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, json.dumps(wire), ""),
                subprocess.CompletedProcess([], 0, json.dumps(self.compiled), "")]) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_surface_file_identity_drift_is_unverified(self):
        paths = ["src/one.py"]
        self.surface_action(paths)
        wire = self.surface_response(paths, [["fixture"]])
        target = self.f.workspace / paths[0]
        self.f.request_path.write_text(json.dumps(self.draft))
        def query_and_replace(*args, **kwargs):
            target.unlink()
            target.symlink_to(self.f.workspace / "scripts/ops/dcf.py")
            return subprocess.CompletedProcess([], 0, json.dumps(wire), "")
        with patch.object(stack.subprocess, "run", side_effect=query_and_replace) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_UNVERIFIED"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_invalid_surface_targets_and_local_mode_never_query(self):
        self.surface_action()
        workspace = self.f.workspace
        (workspace / "src/link.py").symlink_to(workspace / "src/one.py")
        (workspace / "src/alias").symlink_to(workspace / "src", target_is_directory=True)
        os.link(workspace / "src/one.py", workspace / "src/hardlink.py")
        bad = [[], ["src/one.py"] * 2, ["src/one.py"] * 5, ["."], ["src"],
               ["/tmp/x"], ["src/../src/one.py"], ["./src/one.py"], ["src//one.py"],
               ["src/*.py"], ["src/link.py"], ["src/alias/one.py"], [".git/config"],
               ["src/missing.py"], [12], ["src/one.py", "src/hardlink.py"]]
        self.f.request_path.write_text(json.dumps(self.draft))
        for i, paths in enumerate(bad):
            with self.subTest(paths=paths):
                self.action.write_text(json.dumps({**self.base_action, "surface_targets": paths}))
                with patch.object(stack.subprocess, "run") as run:
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                        stack.prepare_request(self.f.request_path, self.action, None,
                                              self.f.root / ("invalid-" + str(i)))
                run.assert_not_called()
        (workspace / "scripts/ops/dcf.py").unlink()
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                stack.prepare_request(self.f.request_path, self.action, None, self.output)
        run.assert_not_called()

    def excerpt_inputs(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        generation = self.compiled["contract"]["dcf_generation"]["generation_id"]
        contract = self.compiled["contract"]
        contract["authorization_semantic_sha256"] = contract["semantic_sha256"]
        contract["read_scopes"] = ["scripts/ops/dcf/jspace.py"]
        contract["write_scopes"] = []
        contract["allowed_operations"] = ["read"]
        contract["denied_operations"] = ["command", "create", "modify", "delete"]
        contract["command_templates"] = []
        contract["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "semantic_sha256"})
        self.draft.update(authority_effect="none", require_tool_call=False)
        self.action.write_text(json.dumps({
            "mission": {"mission_id": "fixture-mission", "task_id": "test", "mode": "DELIVERY",
                        "objective": "Inspect source", "current_predicate": "source_unknown"},
            "operations": ["read"],
            "read_scopes": ["scripts/ops/dcf/jspace.py"], "write_scopes": [],
            "context_summary": "Explain the exact definition.",
            "navigation_targets": [target], "include_source_excerpt": True,
        }))
        navigation = {"capability_id": "source-navigation", "target": target,
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": generation,
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": target, "payload": {
                              "path": "scripts/ops/dcf/jspace.py", "line": 1233}}]}}
        excerpt = {"path": "scripts/ops/dcf/jspace.py", "start_line": 1233,
                   "end_line": 1234, "source_sha256": "a" * 64,
                   "text": "def render_task_context_capsule():\n    pass\n"}
        return navigation, excerpt

    def excerpt_process(self, navigation, *, rebind=None):
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if argv[-1].endswith("dcf_prepare.py"):
                action = json.loads(kwargs["input"])["action"]
                compiled = (self.compiled_navigation(action, navigation)
                            if "navigation_targets" in action else self.compiled)
                return subprocess.CompletedProcess(argv, 0, json.dumps(compiled), "")
            if "source-navigation" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps(navigation), "")
            if "compile" in argv:
                return subprocess.CompletedProcess(argv, 0, json.dumps(self.compiled), "")
            self.assertEqual(argv[1:3], ["-B", "-c"])
            self.assertIn("compile_task_context_capsule", argv[3])
            self.assertIn("canonical_contract_bytes", argv[3])
            self.assertIn("render_task_context_capsule", argv[3])
            payload = json.loads(kwargs["input"])
            self.assertEqual(payload["contract"], self.compiled["contract"])
            if rebind is not None:
                return rebind(argv, payload)
            capsule = dict(self.compiled["task_context_capsule"])
            capsule.update(mission=payload["action"]["mission"],
                           context_summary=payload["action"]["context_summary"],
                           dcf_generation=payload["contract"]["dcf_generation"],
                           jspace_semantic_sha256=payload["contract"]["authorization_semantic_sha256"])
            capsule["semantic_sha256"] = caller._canonical_sha256(
                {key: value for key, value in capsule.items() if key != "semantic_sha256"})
            return subprocess.CompletedProcess(argv, 0, json.dumps(capsule), "")
        return run, calls

    def test_opt_in_excerpt_rebinds_canonical_capsule_once_without_provider(self):
        navigation, excerpt = self.excerpt_inputs()
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process) as run,
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt) as extract,
              patch("codex_collaboration_harness.source_excerpt.still_current", return_value=True),
              patch.object(caller, "preflight", return_value={"status": "READY"}),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        extract.assert_called_once()
        self.assertEqual(extract.call_args.kwargs, {})
        self.assertEqual(result["source_excerpt_decision"], "explicit")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(sum(argv[-1].endswith("dcf_prepare.py") for argv, _ in calls), 1)
        first_action = json.loads(calls[0][1]["input"])["action"]
        final_action = json.loads(calls[1][1]["input"])["action"]
        self.assertNotIn("include_source_excerpt", first_action)
        self.assertNotIn("Exact J-Space-authorized source excerpt", first_action["context_summary"])
        self.assertIn("Exact J-Space-authorized source excerpt", final_action["context_summary"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), self.compiled["contract"])
        self.assertEqual(json.loads((self.output / "capsule.json").read_text())["context_summary"],
                         final_action["context_summary"])
        self.assertEqual(result["source_excerpt"]["source_sha256"], "a" * 64)
        self.assertNotIn("def render_task_context_capsule", json.dumps(result))
        self.assertIn("context_summary_sha256", result["action"])
        self.assertFalse(result["provider_execution_started"])

    def test_opt_in_excerpt_rebinding_failure_or_drift_never_publishes(self):
        for failure in ("compiler", "malformed", "source", "generation", "identity"):
            with self.subTest(failure=failure):
                self.output = self.f.root / ("prepared-" + failure)
                navigation, excerpt = self.excerpt_inputs()
                def rebind(argv, payload):
                    if failure == "compiler":
                        return subprocess.CompletedProcess(argv, 2, "", "invalid contract")
                    if failure == "identity":
                        (self.f.workspace / "scripts/ops/dcf.py").write_text("changed compiler\n")
                    capsule = dict(self.compiled["task_context_capsule"])
                    capsule.update(mission=payload["action"]["mission"],
                                   context_summary=payload["action"]["context_summary"],
                                   dcf_generation=payload["contract"]["dcf_generation"],
                                   jspace_semantic_sha256=payload["contract"]["authorization_semantic_sha256"])
                    if failure == "malformed":
                        capsule["context_summary"] = "wrong excerpt"
                    if failure == "generation":
                        capsule["dcf_generation"] = {"generation_id": "stale"}
                    capsule["semantic_sha256"] = caller._canonical_sha256(
                        {key: value for key, value in capsule.items() if key != "semantic_sha256"})
                    return subprocess.CompletedProcess(argv, 0, json.dumps(capsule), "")
                run_process, calls = self.excerpt_process(navigation, rebind=rebind)
                current = iter([True, False] if failure == "source" else [True, True])
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt),
                      patch("codex_collaboration_harness.source_excerpt.still_current",
                            side_effect=lambda *_: next(current)),
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SOURCE_EXCERPT_UNVERIFIED"):
                        self.run_prepare()
                self.assertEqual(sum(argv[-1].endswith("dcf_prepare.py") for argv, _ in calls), 1)
                self.assertFalse(self.output.exists())

    def test_omitted_excerpt_flag_automatically_includes_support(self):
        navigation, excerpt = self.excerpt_inputs()
        action = json.loads(self.action.read_text())
        action.pop("include_source_excerpt")
        self.action.write_text(json.dumps(action))
        excerpt["supporting_spans"] = []
        run_process, _ = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt) as extract,
              patch("codex_collaboration_harness.source_excerpt.still_current", return_value=True),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(extract.call_args.kwargs,
                         {"include_dependencies": True, "allow_definitions_only": True})
        self.assertEqual(result["source_excerpt_decision"], "auto")
        self.assertIn('"supporting_spans": []',
                      json.loads((self.output / "capsule.json").read_text())["context_summary"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), self.compiled["contract"])

    def readonly_excerpt_inputs(self, target_source):
        navigation, _ = self.excerpt_inputs()
        payload = navigation["result"]["resolved"][0]["payload"]
        payload["line"] = 2
        path = self.f.workspace / payload["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(target_source + "\ndef helper():\n" + "    pass\n" * 130, encoding="utf-8")
        action = json.loads(self.action.read_text())
        action.pop("include_source_excerpt")
        self.action.write_text(json.dumps(action))
        return navigation, path

    def outline_inputs(self):
        navigation, path = self.readonly_excerpt_inputs("VALUE = 1\ndef target(): return VALUE\n")
        action = json.loads(self.action.read_text())
        action.pop("navigation_targets")
        action["operations"] = ["read", "command"]
        action["source_read"] = True
        second = path.with_name("second.py")
        second.write_text("def related(): return 'not context evidence'\n")
        action["read_scopes"].append(str(second.relative_to(self.f.workspace)))
        self.action.write_text(json.dumps(action))
        self.draft["require_tool_call"] = True
        self.compiled["contract"].update(source_read=True,
            allowed_operations=["read", "command"], denied_operations=["modify", "create", "delete"],
            read_scopes=action["read_scopes"])
        return navigation, path

    def test_exact_file_readonly_dcf_prepare_supplies_navigation_before_provider(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.outline_inputs()
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            capsule = json.loads((self.output / "capsule.json").read_text())
            source_excerpt.verify_context_excerpt(self.f.workspace, capsule, original)
            self.assertTrue(request.require_tool_call)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "auto_outline")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.compiled["contract"], original)
        self.assertEqual(result["navigation_locators"], [])
        capsule = json.loads((self.output / "capsule.json").read_text())
        outline = json.loads(capsule["context_summary"].split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(outline["files"][0]["definitions"][0], ["target", "f", 2, 2])
        self.assertEqual(outline["files"][0]["globals"], [[["VALUE"], 1, 1]])
        self.assertEqual(capsule["context_summary"].split(source_excerpt.CONTEXT_MARKER)[1],
                         source_excerpt.encode_context(outline))
        self.assertEqual(outline["files"][0]["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn("definitions", json.dumps(result))
        self.assertFalse(result["provider_execution_started"])

    def test_optional_outline_failure_preserves_ordinary_preparation(self):
        from codex_collaboration_harness import source_excerpt
        for name, error, expected in (
            ("capacity", source_excerpt.ExcerptBudgetExceeded(source_excerpt.CODE, "full"), "capacity_exceeded"),
            ("unavailable", caller.EmbeddedNokiyError(source_excerpt.CODE, "parse unavailable"), "outline_unavailable"),
        ):
            with self.subTest(name=name):
                navigation, _ = self.outline_inputs()
                self.output = self.f.root / ("outline-" + name)
                run_process, calls = self.excerpt_process(navigation)
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch.object(source_excerpt, "extract_outline", side_effect=error),
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    result = self.run_prepare()
                self.assertEqual(result["status"], "PREPARED")
                self.assertEqual(result["source_excerpt_decision"], expected)
                self.assertNotIn("source_excerpt", result)
                self.assertEqual(len(calls), 1)

    def test_outline_skips_explicit_disable_single_file_and_non_readonly_requests(self):
        from codex_collaboration_harness import source_excerpt
        for case in ("disabled", "single", "write"):
            with self.subTest(case=case):
                navigation, _ = self.outline_inputs()
                self.output = self.f.root / ("outline-" + case)
                action = json.loads(self.action.read_text())
                if case == "disabled":
                    action["include_source_excerpt"] = False
                elif case == "single":
                    self.compiled["contract"]["read_scopes"] = action["read_scopes"][:1]
                else:
                    self.compiled["contract"]["write_scopes"] = action["read_scopes"][:1]
                self.action.write_text(json.dumps(action))
                run_process, calls = self.excerpt_process(navigation)
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch.object(source_excerpt, "extract_outline") as extract,
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    result = self.run_prepare()
                self.assertEqual(result["status"], "PREPARED")
                self.assertNotIn("source_excerpt", result)
                self.assertEqual(len(calls), 1)
                extract.assert_not_called()

    def test_optional_outline_cannot_overflow_existing_context_budget(self):
        from codex_collaboration_harness import source_excerpt
        navigation, _ = self.outline_inputs()
        action = json.loads(self.action.read_text())
        action["context_summary"] = "x" * source_excerpt.MAX_ENVELOPE_BYTES
        self.action.write_text(json.dumps(action))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["status"], "PREPARED")
        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)

    def test_auto_readonly_support_overflow_rebinds_complete_target_once_without_provider(self):
        from codex_collaboration_harness import source_excerpt
        target = "@decorate\ndef render_task_context_capsule():\n    return helper()\n"
        navigation, path = self.readonly_excerpt_inputs(target)
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            source_excerpt.verify_context_excerpt(self.f.workspace,
                json.loads((self.output / "capsule.json").read_text()), original)
            self.assertFalse(request.require_tool_call)
            self.assertEqual(request.authority_effect, "none")
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertFalse(result["provider_execution_started"])
        self.assertEqual(result["source_excerpt_decision"], "auto_definitions_only")
        self.assertEqual(len(calls), 2)
        capsule = json.loads((self.output / "capsule.json").read_text())
        summary = capsule["context_summary"]
        self.assertEqual(summary.count(source_excerpt.CONTEXT_MARKER), 1)
        self.assertEqual(summary.count("def render_task_context_capsule("), 1)
        self.assertEqual(summary.count("@decorate"), 1)
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER, 1)[1])
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(excerpt["kind"], source_excerpt.READONLY_PARTIAL_KIND)
        self.assertEqual(excerpt["notice"], source_excerpt.PARTIAL_READONLY_NOTICE)
        self.assertEqual(excerpt["locator"], {"path": original["read_scopes"][0], "line": 2,
            "generation_id": original["dcf_generation"]["generation_id"]})
        self.assertEqual(excerpt["files"], [{"path": original["read_scopes"][0],
            "source_sha256": digest, "spans": [{"start_line": 1, "end_line": 3, "text": target}]}])
        self.assertEqual([row["name"] for row in excerpt["support_navigation"]["locators"]], ["helper"])
        self.assertNotIn("def helper(", json.dumps(excerpt["files"]))
        self.assertEqual(result["source_excerpt"], {"kind": source_excerpt.READONLY_PARTIAL_KIND,
            "files": [{"path": original["read_scopes"][0], "source_sha256": digest,
                       "spans": [{"start_line": 1, "end_line": 3}]}]})
        self.assertNotIn('"text"', json.dumps(result))
        self.assertNotIn("def render_task_context_capsule(", json.dumps(result))
        self.assert_public_action_identity(result, json.loads(calls[1][1]["input"])["action"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_auto_readonly_decorated_target_and_partial_envelope_capacity_use_read_fallback(self):
        from codex_collaboration_harness import source_excerpt
        targets = {
            "lines": "@decorate\ndef render_task_context_capsule():\n"
                     + "    pass\n" * 118 + "    return helper()\n",
            "bytes": "@decorate('" + "x" * source_excerpt.MAX_EXCERPT_BYTES
                     + "')\ndef render_task_context_capsule():\n    return helper()\n",
            "envelope": "@decorate\ndef render_task_context_capsule():\n    return helper('"
                        + "é" * 5000 + "')\n",
        }
        for capacity, target in targets.items():
            with self.subTest(capacity=capacity):
                self.output = self.f.root / ("readonly-capacity-" + capacity)
                navigation, _ = self.readonly_excerpt_inputs(target)
                original = json.loads(json.dumps(self.compiled["contract"]))
                run_process, calls = self.excerpt_process(navigation)
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch.object(source_excerpt, "extract", wraps=source_excerpt.extract) as extract,
                      patch.object(source_excerpt, "still_current") as current,
                      patch.object(caller, "preflight", return_value={"status": "READY"}),
                      patch.object(caller, "execute") as execute):
                    result = self.run_prepare()
                extract.assert_called_once()
                current.assert_not_called()
                execute.assert_not_called()
                self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
                self.assertNotIn("source_excerpt", result)
                self.assertFalse(result["provider_execution_started"])
                self.assertEqual(len(calls), 1)
                summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
                self.assertNotIn(source_excerpt.CONTEXT_MARKER, summary)
                self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_readonly_tool_required_preparation_remains_ineligible_for_auto_excerpt(self):
        from codex_collaboration_harness import source_excerpt
        navigation, _ = self.readonly_excerpt_inputs(
            "@decorate\ndef render_task_context_capsule():\n    return helper()\n")
        self.draft["require_tool_call"] = True
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(source_excerpt, "extract") as extract,
              patch.object(caller, "preflight", return_value={"status": "READY"}),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        extract.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "ineligible")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)
        self.assertIs(json.loads((self.output / "request.json").read_text())["require_tool_call"], True)

    def test_explicit_false_does_not_extract_or_rebind(self):
        navigation, _ = self.excerpt_inputs()
        action = json.loads(self.action.read_text())
        action["include_source_excerpt"] = False
        self.action.write_text(json.dumps(action))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract") as extract,
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        extract.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["source_excerpt_decision"], "disabled")
        self.assertNotIn("source_excerpt", result)

    def test_auto_excerpt_falls_back_only_on_capacity(self):
        from codex_collaboration_harness.source_excerpt import ExcerptBudgetExceeded
        for capacity in (True, False):
            self.output = self.f.root / f"auto-failure-{capacity}"
            navigation, _ = self.excerpt_inputs()
            action = json.loads(self.action.read_text())
            action.pop("include_source_excerpt")
            self.action.write_text(json.dumps(action))
            run_process, calls = self.excerpt_process(navigation)
            error = (ExcerptBudgetExceeded if capacity else caller.EmbeddedNokiyError)(
                "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "capacity or integrity")
            with (patch.object(stack.subprocess, "run", side_effect=run_process),
                  patch("codex_collaboration_harness.source_excerpt.extract", side_effect=error),
                  patch.object(caller, "preflight", return_value={"status": "READY"})):
                if capacity:
                    result = self.run_prepare()
                    self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
                    self.assertNotIn("source_excerpt", result)
                    self.assertEqual(json.loads((self.output / "jspace.json").read_text()),
                                     self.compiled["contract"])
                else:
                    with self.assertRaises(caller.EmbeddedNokiyError):
                        self.run_prepare()
                    self.assertFalse(self.output.exists())
            self.assertEqual(len(calls), 1)

    def test_auto_excerpt_eligibility_does_not_change_task_requirements(self):
        import copy
        self.excerpt_inputs()
        original = copy.deepcopy(self.compiled["contract"])
        locators = [{"path": original["read_scopes"][0]}]
        self.assertTrue(stack._auto_excerpt_eligible(self.draft, original, locators))
        for update in ({"require_tool_call": True}, {"require_tool_call": None},
                       {"authority_effect": "workspace"}):
            self.assertFalse(stack._auto_excerpt_eligible({**self.draft, **update}, original, locators))
        for update in ({"read_scopes": ["scripts/**"]}, {"write_scopes": ["src/a.py"]},
                       {"allowed_operations": ["read", "modify"]},
                       {"allowed_operations": ["read", "command"]},
                       {"denied_operations": ["read"]}, {"command_templates": ["pytest"]},
                       {"verifier_commands": [{"id": "verify"}]}):
            self.assertFalse(stack._auto_excerpt_eligible(self.draft, {**original, **update}, locators))
        self.assertFalse(stack._auto_excerpt_eligible(self.draft, original, []))
        self.assertFalse(stack._auto_excerpt_eligible(self.draft, original, locators * 2))
        admitted = {**original, "allowed_operations": ["read", "command"], "source_read": True}
        self.assertTrue(stack._auto_excerpt_eligible(self.draft, admitted, locators))
        self.assertTrue(stack._auto_excerpt_eligible(self.draft,
            {**admitted, "focused_verifiers": [{"declared": True, "result_status": "pass"}]}, locators))
        self.assertEqual(original, self.compiled["contract"])

    def test_auto_excerpt_json_expansion_falls_back_without_truncation(self):
        navigation, excerpt = self.excerpt_inputs()
        action = json.loads(self.action.read_text())
        action.pop("include_source_excerpt")
        self.action.write_text(json.dumps(action))
        excerpt["text"] = "\u00e9" * 6000
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)

    def test_opt_in_excerpt_final_preflight_failure_never_publishes_request(self):
        navigation, excerpt = self.excerpt_inputs()
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch("codex_collaboration_harness.source_excerpt.extract", return_value=excerpt),
              patch("codex_collaboration_harness.source_excerpt.still_current", return_value=True),
              patch.object(caller, "preflight", side_effect=caller.EmbeddedNokiyError(
                  "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "source drift during preflight"))):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SOURCE_EXCERPT_UNVERIFIED"):
                self.run_prepare()
        self.assertEqual(sum(argv[-1].endswith("dcf_prepare.py") for argv, _ in calls), 1)
        self.assertFalse((self.output / "request.json").exists())

    def edit_excerpt_inputs(self):
        authority_effect = self.draft.get("authority_effect")
        navigation, _ = self.excerpt_inputs()
        name = "scripts/ops/dcf/jspace.py"
        source = ("LIMIT = 20\n"
                  "def normalize(value):\n    return min(int(value), LIMIT)\n\n"
                  "def render(items, limit):\n    return items[:normalize(limit)]\n")
        path = self.f.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        contract = self.compiled["contract"]
        contract.update(allowed_operations=["read", "modify", "command"],
                        denied_operations=["create", "delete"], source_read=True,
                        write_scopes=[name])
        contract["semantic_sha256"] = caller._canonical_sha256(
            {key: value for key, value in contract.items() if key != "semantic_sha256"})
        self.draft.update(authority_effect=authority_effect, require_tool_call=True)
        navigation_rows = []
        for symbol, line in (("render", 5), ("normalize", 2)):
            row = json.loads(json.dumps(navigation))
            row["target"] = "symbol:scripts.ops.dcf.jspace." + symbol
            row["result"]["resolved"][0].update(entity_id=row["target"], payload={"path": name, "line": line})
            navigation_rows.append(row)
        action = json.loads(self.action.read_text())
        action.pop("include_source_excerpt")
        action.update(operations=["read", "modify", "command"], write_scopes=[name],
                      context_summary="Edit normalize and render, then read back the changed source.",
                      navigation_targets=[row["target"] for row in navigation_rows])
        self.action.write_text(json.dumps(action))
        return navigation_rows, path

    def test_auto_edit_context_rebinds_once_without_changing_authority_or_tools(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        original = json.loads(json.dumps(self.compiled["contract"]))
        authority_effect = self.draft["authority_effect"]
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            capsule = json.loads((self.output / "capsule.json").read_text())
            source_excerpt.verify_context_excerpt(self.f.workspace, capsule, original)
            self.assertTrue(request.require_tool_call)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(request["authority_effect"], authority_effect)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(len(excerpt["files"]), 1)
        text = "".join(span["text"] for span in excerpt["files"][0]["spans"])
        self.assertEqual(text.count("def normalize("), 1)
        self.assertEqual(text.count("def render("), 1)
        self.assertEqual(excerpt["files"][0]["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotIn("def normalize", json.dumps(result))
        self.assertIn("fresh readback", excerpt["notice"])

    def test_auto_edit_changed_source_during_rebind_is_rejected(self):
        navigation, path = self.edit_excerpt_inputs()
        run_process, _ = self.excerpt_process(navigation)
        def change_after_rebind(argv, **kwargs):
            response = run_process(argv, **kwargs)
            if argv[1:3] == ["-B", "-c"]:
                path.write_text(path.read_text().replace("LIMIT = 20", "LIMIT = 30"))
            return response
        with (patch.object(stack.subprocess, "run", side_effect=change_after_rebind),
              patch.object(caller, "preflight") as preflight):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SOURCE_EXCERPT_UNVERIFIED"):
                self.run_prepare()
        preflight.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_auto_edit_preflight_hook_rejects_stale_source(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        run_process, _ = self.excerpt_process(navigation)
        def changed_at_preflight(request):
            path.write_text(path.read_text() + "\nCHANGED = True\n")
            source_excerpt.verify_context_excerpt(
                self.f.workspace, json.loads((self.output / "capsule.json").read_text()),
                self.compiled["contract"])
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=changed_at_preflight)):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "preimage changed"):
                self.run_prepare()
        self.assertFalse((self.output / "request.json").exists())

    def test_auto_edit_large_render_preloads_only_complete_normalize(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text() + "    pass\n" * 120)
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(excerpt["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(len(excerpt["deferred_locators"]), 1)
        deferred = json.dumps(excerpt["deferred_locators"])
        self.assertIn("scripts/ops/dcf/jspace.py", deferred)
        self.assertTrue('"line": 5' in deferred or ':5' in deferred, deferred)
        text = "".join(span["text"] for file in excerpt["files"] for span in file["spans"])
        self.assertIn("def normalize(value):\n    return min(int(value), LIMIT)", text)
        self.assertNotIn("def render(", text)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_auto_partial_edit_binds_support_navigation_without_changing_grants(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace(
            "return min(int(value), LIMIT)", "return helper(value)")
            + "    pass\n" * 120 + "\ndef helper(value):\n" + "    pass\n" * 125)
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, _ = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        capsule = json.loads((self.output / "capsule.json").read_text())
        excerpt = json.loads(capsule["context_summary"].split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual([row["name"] for row in excerpt["support_navigation"]["locators"]], ["helper"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)
        source_excerpt.verify_context_excerpt(self.f.workspace, capsule, original)

    def test_auto_edit_all_definitions_oversized_uses_ordinary_read_fallback(self):
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace(
            "    return min(int(value), LIMIT)",
            "    pass\n" * 121 + "    return min(int(value), LIMIT)") + "    pass\n" * 120)
        navigation[0]["result"]["resolved"][0]["payload"]["line"] += 121
        original = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_auto_edit_support_overflow_rebinds_definitions_only(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace("return items[:normalize(limit)]",
                                                "return helper(items[:normalize(limit)])") +
                        "def helper(items):\n" + "    pass\n" * 241 + "    return items\n")
        contract = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            source_excerpt.verify_context_excerpt(self.f.workspace,
                json.loads((self.output / "capsule.json").read_text()), contract)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight)):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(excerpt["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(excerpt["deferred_locators"], [])
        self.assertNotIn("def helper", json.dumps(excerpt["files"]))
        self.assertIs(json.loads((self.output / "jspace.json").read_text())["source_read"], True)

    def test_auto_edit_preloads_multiple_complete_targets_under_fixed_byte_cap(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        path.write_text(path.read_text().replace("    return min(int(value), LIMIT)",
                                                "    pass\n" * 79 + "    return min(int(value), LIMIT)") +
                        "    pass\n" * 79)
        navigation[0]["result"]["resolved"][0]["payload"]["line"] += 79
        contract = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        def preflight(request):
            source_excerpt.verify_context_excerpt(self.f.workspace,
                json.loads((self.output / "capsule.json").read_text()), contract)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        excerpt = json.loads(summary.split(source_excerpt.CONTEXT_MARKER)[1])
        self.assertEqual(excerpt["line_budget"], 240)
        self.assertNotIn("deferred_locators", excerpt)
        text = "".join(span["text"] for row in excerpt["files"] for span in row["spans"])
        self.assertEqual(text.count("def normalize("), 1)
        self.assertEqual(text.count("def render("), 1)
        self.assertLessEqual(len(text.encode()), source_excerpt.MAX_EXCERPT_BYTES)
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), contract)
        self.assertNotIn("def normalize", json.dumps(result))

    def test_auto_edit_envelope_allocation_fits_four_complete_targets_above_raw_cap(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        text = "LIMIT = 20\n"
        rows = []
        definitions = []
        for symbol in ("normalize", "render", "select", "summarize"):
            row = json.loads(json.dumps(navigation[0]))
            row["target"] = "symbol:scripts.ops.dcf.jspace." + symbol
            row["result"]["resolved"][0].update(entity_id=row["target"],
                payload={"path": "scripts/ops/dcf/jspace.py", "line": len(text.splitlines()) + 1})
            rows.append(row)
            definition = (f"def {symbol}():\n    value = '" + "x" * 2700 + "'\n"
                          + "    pass\n" * 76 + "    return value, LIMIT\n")
            definitions.append(definition)
            text += definition + "\n"
        path.write_text(text, encoding="utf-8")
        action = json.loads(self.action.read_text())
        action["navigation_targets"] = [row["target"] for row in rows]
        self.action.write_text(json.dumps(action))
        contract = json.loads(json.dumps(self.compiled["contract"]))
        authority_effect = self.draft["authority_effect"]
        run_process, calls = self.excerpt_process(rows)
        def preflight(request):
            source_excerpt.verify_context_excerpt(self.f.workspace,
                json.loads((self.output / "capsule.json").read_text()), contract)
            return {"status": "READY"}
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", side_effect=preflight),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(len(calls), 2)
        summary = json.loads((self.output / "capsule.json").read_text())["context_summary"]
        raw = summary.split(source_excerpt.CONTEXT_MARKER)[1]
        excerpt = json.loads(raw)
        self.assertEqual(excerpt["allocation_mode"], "envelope_bounded")
        self.assertEqual(excerpt["byte_budget"], 24576)
        self.assertEqual(excerpt["line_budget"], 480)
        self.assertNotIn("deferred_locators", excerpt)
        selected = "".join(span["text"] for file in excerpt["files"] for span in file["spans"])
        self.assertGreater(len(selected.encode()), source_excerpt.MAX_EXCERPT_BYTES)
        self.assertLessEqual(len(raw.encode()), 24576)
        for definition in definitions:
            self.assertEqual(selected.count(definition), 1)
        self.assertEqual(selected.count("LIMIT = 20"), 1)
        self.assertEqual(excerpt["files"][0]["source_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), contract)
        request = json.loads((self.output / "request.json").read_text())
        self.assertIs(request["require_tool_call"], True)
        self.assertEqual(request["authority_effect"], authority_effect)
        self.assertNotIn("def normalize", json.dumps(result))

    def test_auto_edit_serialized_overflow_uses_ordinary_read_fallback(self):
        from codex_collaboration_harness import source_excerpt
        navigation, path = self.edit_excerpt_inputs()
        text = "def normalize():\n    return '" + "é" * 4100 + "'\n\n"
        text += "def render():\n    return '" + "é" * 4100 + "'\n"
        path.write_text(text, encoding="utf-8")
        navigation[1]["result"]["resolved"][0]["payload"]["line"] = 1
        navigation[0]["result"]["resolved"][0]["payload"]["line"] = 4
        contract = json.loads(json.dumps(self.compiled["contract"]))
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"}),
              patch.object(caller, "execute") as execute):
            result = self.run_prepare()
        execute.assert_not_called()
        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)
        capsule = json.loads((self.output / "capsule.json").read_text())
        self.assertNotIn(source_excerpt.CONTEXT_MARKER, capsule["context_summary"])
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), contract)
        self.assertIs(json.loads((self.output / "request.json").read_text())["require_tool_call"], True)

    def test_auto_and_explicit_readonly_preparation_keep_legacy_raw_cap(self):
        from codex_collaboration_harness import source_excerpt
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                self.output = self.f.root / f"readonly-cap-{explicit}"
                navigation, _ = self.excerpt_inputs()
                name = "scripts/ops/dcf/jspace.py"
                path = self.f.workspace / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("def render_task_context_capsule():\n    return '" + "x" * 13000 + "'\n")
                navigation["result"]["resolved"][0]["payload"]["line"] = 1
                if not explicit:
                    action = json.loads(self.action.read_text())
                    action.pop("include_source_excerpt")
                    self.action.write_text(json.dumps(action))
                run_process, calls = self.excerpt_process(navigation)
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch.object(source_excerpt, "extract_edits") as edits,
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    if explicit:
                        with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                            self.run_prepare()
                        self.assertFalse((self.output / "request.json").exists())
                    else:
                        result = self.run_prepare()
                        self.assertEqual(result["source_excerpt_decision"], "capacity_exceeded")
                        self.assertNotIn("source_excerpt", result)
                    edits.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_auto_edit_ineligible_broad_scope_keeps_ordinary_read_path(self):
        navigation, _ = self.edit_excerpt_inputs()
        self.compiled["contract"]["write_scopes"] = ["scripts/**"]
        run_process, calls = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "ineligible")
        self.assertNotIn("source_excerpt", result)
        self.assertEqual(len(calls), 1)

    def test_auto_edit_with_additional_task_and_test_reads_keeps_same_grants(self):
        navigation, _ = self.edit_excerpt_inputs()
        contract = self.compiled["contract"]
        contract["read_scopes"] += ["TASK.md", "tests/test_example.py"]
        original = json.loads(json.dumps(contract))
        run_process, _ = self.excerpt_process(navigation)
        with (patch.object(stack.subprocess, "run", side_effect=run_process),
              patch.object(caller, "preflight", return_value={"status": "READY"})):
            result = self.run_prepare()
        self.assertEqual(result["source_excerpt_decision"], "auto_edit")
        self.assertEqual(json.loads((self.output / "jspace.json").read_text()), original)

    def test_edit_explicit_flags_and_no_tool_requirement_keep_old_behavior(self):
        from codex_collaboration_harness import source_excerpt
        for flag in (False, True, "no-tool"):
            with self.subTest(flag=flag):
                self.output = self.f.root / f"edit-flag-{flag}"
                navigation, _ = self.edit_excerpt_inputs()
                action = json.loads(self.action.read_text())
                if flag == "no-tool":
                    self.draft["require_tool_call"] = False
                else:
                    action["include_source_excerpt"] = flag
                self.action.write_text(json.dumps(action))
                run_process, calls = self.excerpt_process(navigation)
                with (patch.object(stack.subprocess, "run", side_effect=run_process),
                      patch.object(source_excerpt, "extract_edits") as extract,
                      patch.object(caller, "preflight", return_value={"status": "READY"})):
                    if flag is True:
                        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "read-only, no-tool"):
                            self.run_prepare()
                    else:
                        result = self.run_prepare()
                        self.assertNotIn("source_excerpt", result)
                        self.assertEqual(result["source_excerpt_decision"],
                                         "disabled" if flag is False else "ineligible")
                    extract.assert_not_called()
                self.assertEqual(len(calls), 1)

    def test_navigation_generation_drift_blocks_before_request(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        self.action.write_text(json.dumps({**self.base_action, "context_summary": "Inspect it.",
                                           "navigation_targets": [target]}))
        navigation = {"capability_id": "source-navigation", "target": target,
                      "freshness_status": "current", "domain_verdict": "pass",
                      "projection_status": "pass", "generation_id": "old-generation",
                      "result": {"_projection": {"complete": True}, "resolved": [{
                          "entity_id": target, "payload": {
                              "path": "scripts/ops/dcf/jspace.py", "line": 1233}}]}}
        compiled = self.compiled_navigation(json.loads(self.action.read_text()), navigation)
        responses = [subprocess.CompletedProcess([], 0, json.dumps(compiled), "")]
        with patch.object(stack.subprocess, "run", side_effect=responses):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_partial_dcf_navigation_blocks_before_compilation(self):
        target = "symbol:scripts.ops.dcf.jspace.render_task_context_capsule"
        self.action.write_text(json.dumps({**self.base_action, "context_summary": "Inspect it.",
                                           "navigation_targets": [target]}))
        partial = {"capability_id": "source-navigation", "target": target,
                   "freshness_status": "current", "domain_verdict": "pass",
                   "projection_status": "pass", "generation_id": "generation-1",
                   "result": {"_projection": {"complete": False}, "resolved": []}}
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(partial), "")) as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "NAVIGATION_UNVERIFIED"):
                self.run_prepare()
        self.assertEqual(run.call_count, 1)
        self.assertFalse(self.output.exists())

    def test_navigation_never_falls_back_to_non_dcf_context(self):
        self.action.write_text(json.dumps({**self.base_action, "context_summary": "Inspect it.",
                                           "navigation_targets": ["symbol:package.item"]}))
        (self.f.workspace / "scripts/ops/dcf.py").unlink()
        with patch.object(stack.subprocess, "run") as run:
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "only in a DCF workspace"):
                self.run_prepare()
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_compiler_failure_no_fallback_or_request(self):
        with patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "{}", "failed")):
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
        self.assertFalse(self.output.exists())

    def test_typed_compiler_failure_preserves_safe_cause_and_stream_identities(self):
        secret = "NOKIY_DCF_FAILURE_SECRET_SENTINEL"
        stdout = json.dumps({"schema_version": "jspace_error_v1", "status": "blocked",
                             "error_code": "JSPACE_SOURCE_READ_FILE_INVALID", "error": secret,
                             "authority_effect": "none", "no_apply": True})
        with (patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout, secret)) as run,
              patch.object(caller, "execute") as execute):
            with self.assertRaises(caller.EmbeddedNokiyError) as error:
                self.run_prepare()
        failure = caller._failure(error.exception)
        self.assertEqual(failure["first_typed_blocker"], "NOKIY_FULL_STACK_PREPARATION_FAILED")
        self.assertEqual(failure["diagnostics"], {
            "stage": "dcf_compile", "returncode": 2,
            "stdout_bytes": len(stdout.encode()), "stderr_bytes": len(secret.encode()),
            "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(secret.encode()).hexdigest(),
            "child_error_code": "JSPACE_SOURCE_READ_FILE_INVALID",
        })
        self.assertEqual(failure["issues"], [{"path": "action.read_scopes", "reason": "exact_file_unavailable"}])
        self.assertNotIn(secret, json.dumps(failure))
        run.assert_called_once()
        execute.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_out_of_namespace_or_malformed_compiler_error_never_becomes_a_typed_cause(self):
        payload = {"schema_version": "jspace_error_v1", "status": "blocked",
                   "error_code": "JSPACE_SOURCE_READ_FILE_INVALID",
                   "authority_effect": "none", "no_apply": True}
        outputs = ["not json", "null", "[]", "{}", json.dumps({**payload, "error_code": "UNKNOWN_FAILURE"}),
                   json.dumps({**payload, "error_code": "JSPACE_failure"}),
                   json.dumps({**payload, "error_code": "JSPACE_FAILURE\n"}),
                   json.dumps({**payload, "error_code": "JSPACE_" + "A" * 97}),
                   json.dumps({**payload, "authority_effect": "workspace"}),
                   json.dumps({**payload, "no_apply": False}),
                   json.dumps({**payload, "status": "ok"}),
                   json.dumps({**payload, "schema_version": "unknown"}),
                   json.dumps({**payload, "extra": "secret"}),
                   json.dumps(payload)[:-1] + ',"error_code":"JSPACE_READ_SCOPE_MISSING"}',
                   json.dumps(payload)[:-1] + ',"error":NaN}',
                   "x" * (caller.MAX_REQUEST_BYTES + 1)]
        for stdout in outputs:
            with self.subTest(output_prefix=stdout[:40]), patch.object(
                    stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout, "secret")) as run:
                with self.assertRaises(caller.EmbeddedNokiyError) as error:
                    self.run_prepare()
            failure = caller._failure(error.exception)
            self.assertNotIn("child_error_code", failure["diagnostics"])
            self.assertNotIn("issues", failure)
            self.assertEqual(failure["diagnostics"]["stdout_bytes"], len(stdout.encode()))
            run.assert_called_once()
            self.assertFalse(self.output.exists())

    def test_canonical_compiler_new_codes_remain_blocked_without_fallback(self):
        for code in ("JSPACE_COMMAND_EFFECT_UNVERIFIED", "JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT",
                     "TASK_CONTEXT_FIELDS_INVALID", "JSPACE_FUTURE_DIAGNOSTIC"):
            stdout = json.dumps({"schema_version": "jspace_error_v1", "status": "blocked",
                                 "error_code": code, "error": "private detail",
                                 "authority_effect": "none", "no_apply": True})
            with (self.subTest(code=code),
                  patch.object(stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout, "")) as run,
                  patch.object(caller, "execute") as execute):
                with self.assertRaises(caller.EmbeddedNokiyError) as error:
                    self.run_prepare()
                failure = caller._failure(error.exception)
                self.assertEqual(failure["first_typed_blocker"], "NOKIY_FULL_STACK_PREPARATION_FAILED")
                self.assertEqual(failure["diagnostics"]["child_error_code"], code)
                self.assertNotIn("private detail", json.dumps(failure))
                self.assertNotIn("issues", failure)
                run.assert_called_once()
                execute.assert_not_called()
                self.assertFalse(self.output.exists())

    def test_corrupt_contract_never_publishes_executable_request(self):
        self.compiled["contract"]["semantic_sha256"] = "0" * 64
        with self.compiler():
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
        self.assertFalse((self.output / "request.json").exists())

    def test_reuse_rejected_before_compiler(self):
        with self.compiler():
            self.run_prepare()
        before = (self.output / "request.json").read_bytes()
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
            compiler.assert_not_called()
        self.assertEqual((self.output / "request.json").read_bytes(), before)

    def test_submitted_request_not_recompiled(self):
        self.draft["context_capsule"] = {"path": "old", "sha256": "0" * 64}
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
            compiler.assert_not_called()

    def test_wrong_thread_rejected_before_compilation(self):
        self.draft["native_thread_id"] = "019fd83e-861a-7b62-a628-0e0ad2f88a27"
        with self.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.run_prepare()
            compiler.assert_not_called()

    def test_cli_prepare_is_available(self):
        args = caller.build_parser().parse_args(["prepare", "--request", "draft.json", "--action", "action.json",
                                                "--surface-id", "s", "--output-dir", "/tmp/new"])
        self.assertEqual(args.command, "prepare")

    def aged_v2_request(self):
        f = embedded_fixtures.EmbeddedNokiyFixture()
        f.setUp()
        self.addCleanup(f.tearDown)
        f._canonical_v2_context()
        context = json.loads(f.context.read_text())
        contract = json.loads(f.jspace.read_text())
        generation = contract['dcf_generation']
        generation['generated_at'] = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        generation['action_freshness'] = {'source_fingerprints': {'surface': 'test'}}
        contract['dcf_generation'] = generation
        context['dcf_generation'] = generation
        contract['content_sha256'] = embedded_fixtures.canonical_sha256(
            {k:v for k,v in contract.items() if k != 'content_sha256'})
        context['semantic_sha256'] = embedded_fixtures.canonical_sha256(
            {k:v for k,v in context.items() if k != 'semantic_sha256'})
        f.context.write_text(json.dumps(context))
        f.jspace.write_text(json.dumps(contract))
        return caller.load_request(f._write_request())

    def test_old_generation_requires_live_fingerprint_verifier(self):
        request = self.aged_v2_request()
        with patch.object(stack, 'verify_action_freshness') as verify:
            caller._verify_context(request)
            verify.assert_called_once()
        with patch.object(stack, 'verify_action_freshness', side_effect=caller.EmbeddedNokiyError('STALE', 'changed')):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'STALE'):
                caller._verify_context(request)

    def test_action_scoped_preflight_rechecks_embedded_source(self):
        request = self.aged_v2_request()
        with (patch.object(stack, 'verify_action_freshness'),
              patch('codex_collaboration_harness.source_excerpt.verify_context_excerpt',
                    side_effect=caller.EmbeddedNokiyError('NOKIY_SOURCE_EXCERPT_UNVERIFIED', 'changed')) as verify):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'NOKIY_SOURCE_EXCERPT_UNVERIFIED'):
                caller._verify_context(request)
        verify.assert_called_once()

    def test_freshness_verifier_failure_is_not_ignored(self):
        with patch.object(stack.subprocess, 'run', return_value=subprocess.CompletedProcess([], 2, '', 'stale')):
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, 'FRESHNESS_UNVERIFIED'):
                stack.verify_action_freshness(self.f.workspace, {})


class ReadOnlyRouteTests(unittest.TestCase):
    """prepare_request reprepares a refused read-only action once as inspect."""

    def setUp(self):
        self.fixture = FullStackTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.exact_only.stop()
        self.f = self.fixture.f
        self.f.request_path.write_text(json.dumps(self.fixture.draft))

    def test_refused_read_only_action_is_prepared_once_as_inspect(self):
        action = {**self.fixture.surface_action(), "surface_targets": ["src/missing.py"]}
        self.fixture.action.write_text(json.dumps(action))
        with patch.object(stack.subprocess, "run") as run:
            result = stack.prepare_request(self.f.request_path, self.fixture.action, None, self.fixture.output)
        run.assert_not_called()  # the invalid target never reaches a DCF query
        self.assertEqual((result["status"], result["context_mode"]), ("PREPARED", "inspect_workspace_jspace"))
        routes = [row for row in result["input_adaptations"] if row["rule"] == stack.READ_ONLY_ROUTE_RULE]
        self.assertEqual(len(routes), 1)
        self.assertTrue(routes[0]["refused"].startswith("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID"))
        request = json.loads((self.fixture.output / "request.json").read_text())
        contract = json.loads(Path(request["jspace_contract"]["path"]).read_text())
        self.assertEqual((contract["write_scopes"], contract["allowed_operations"]), ([], ["command", "read"]))
        summary = json.loads(Path(request["context_capsule"]["path"]).read_text())["context_summary"]
        self.assertIn("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID", summary)
        self.assertIn("Requested surface targets: src/missing.py", summary)

    def test_refusals_that_cannot_be_routed_keep_their_exact_cause(self):
        surface = self.fixture.surface_action()
        cases = {
            "may_write": {**surface, "operations": ["read", "modify"], "write_scopes": ["src/one.py"],
                          "target_paths": ["src/one.py"], "surface_targets": ["src/missing.py"]},
            "explicit_excerpt": {**surface, "surface_targets": ["src/missing.py"],
                                 "include_source_excerpt": True},
            "incomplete_mission": {"mission": {"task_id": "test"}, "operations": ["read"],
                                   "surface_targets": ["src/missing.py"]},
        }
        for name, action in cases.items():
            output = self.f.root / ("unrouted-" + name)
            self.fixture.action.write_text(json.dumps(action))
            with self.subTest(case=name), patch.object(stack.subprocess, "run") as run:
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "SURFACE_TARGET_INVALID"):
                    stack.prepare_request(self.f.request_path, self.fixture.action, None, output)
                run.assert_not_called()
                self.assertFalse(output.exists())

    def test_compiler_refusal_routes_unless_it_shows_the_action_may_write(self):
        # New refusal kinds route without enumeration; only a not-read-only cause keeps it.
        for code, routed in (("JSPACE_FUTURE_DIAGNOSTIC", True), ("JSPACE_COMMAND_EFFECT_UNDERDECLARED", False)):
            stdout = json.dumps({"schema_version": "jspace_error_v1", "status": "blocked",
                                 "error_code": code, "error": "private detail",
                                 "authority_effect": "none", "no_apply": True})
            self.fixture.output = self.f.root / ("compiler-" + code)
            with (self.subTest(code=code), patch.object(
                    stack.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, stdout, "")) as run):
                if routed:
                    result = self.fixture.run_prepare()
                    self.assertEqual(result["context_mode"], "inspect_workspace_jspace")
                    routes = [row for row in result["input_adaptations"]
                              if row["rule"] == stack.READ_ONLY_ROUTE_RULE]
                    self.assertEqual(len(routes), 1)
                    self.assertIn(code, routes[0]["refused"])
                    self.assertNotIn("private detail", json.dumps(result))
                else:
                    with self.assertRaises(caller.EmbeddedNokiyError) as error:
                        self.fixture.run_prepare()
                    self.assertEqual(caller._failure(error.exception)["diagnostics"]["child_error_code"], code)
                    self.assertFalse(self.fixture.output.exists())
                run.assert_called_once()
