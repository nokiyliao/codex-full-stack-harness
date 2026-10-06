# SPDX-License-Identifier: MIT
"""Opt-in real Router/DB acceptance through the complete caller, without a model."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_full_core as fixtures
from test_postimage_context import dcf_generation
from codex_collaboration_harness import embedded_nokiy as caller, full_core, local_context


class FullCoreDcfPostimageBindingTests(unittest.TestCase):
    def test_only_caller_validated_action_scoped_dcf_generation_is_forwarded(self):
        cases = (("dcf", "dcf_jspace_required", "jspace_contract_v2", True),
                 ("local", "local_workspace_jspace", "jspace_contract_v2", True),
                 ("legacy", "dcf_jspace_required", "jspace_contract_v1", True),
                 ("unscoped", "dcf_jspace_required", "jspace_contract_v2", False))
        for name, mode, schema, action_scoped in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                workspace = Path(temporary).resolve()
                artifacts = workspace / "artifacts"
                artifacts.mkdir()
                generation = dcf_generation(workspace)
                if not action_scoped:
                    del generation["action_freshness"]
                contract = {
                    "schema_version": schema, "authorization_semantic_sha256": "a" * 64,
                    "repo_root": str(workspace), "dcf_generation": generation,
                    "source_read": True, "read_scopes": ["core.py"], "write_scopes": ["core.py"],
                    "allowed_operations": ["command", "modify", "read"],
                    "denied_operations": ["delete", "install", "network", "system_mutation"],
                    "verifier_commands": [{"argv": ["/python", "/verify.py"], "timeout_seconds": 7}],
                }
                original = copy.deepcopy(contract)
                capsule = {"dcf_generation": copy.deepcopy(generation)}
                path = workspace / "contract.json"
                path.write_text(json.dumps(contract))
                request = SimpleNamespace(
                    artifact_root=artifacts, request_id="run", workspace=workspace,
                    jspace_contract=caller.FileIdentity(path, caller._file_sha256(path)),
                    prompt="Capture at the validated pre-worker boundary.", timeout_seconds=None,
                    terminal_delivery="assistant_reply",
                    to_wire=Mock(return_value={}))
                router = object()
                runtime = SimpleNamespace(artifacts={"tura_router": router})
                ready = {"status": "READY", "context": {"context_mode": mode}}
                with patch.object(caller, "_verify_native_thread_binding"), \
                        patch.object(full_core, "prepare", return_value=(ready, runtime, capsule, contract)), \
                        patch.object(caller, "_write_create_only"), \
                        patch.object(full_core, "_provider_prompt", return_value=request.prompt), \
                        patch("codex_collaboration_harness.verifier_parent.ParentVerifier",
                              side_effect=RuntimeError("capture boundary")) as constructor:
                    # Stop before any channel/process/provider execution.
                    with self.assertRaisesRegex(RuntimeError, "capture boundary"):
                        full_core.execute_full_core(request)
                constructor.assert_called_once_with(workspace, contract, router, None,
                    validated_dcf_generation=capsule["dcf_generation"] if name == "dcf" else None)
                self.assertEqual(contract, original)
                self.assertNotIn("source_snapshot", contract["dcf_generation"])


class VerifierPromptTests(unittest.TestCase):
    _PROMPT_HEADER = "\n\nVerified execution capability (caller projection):\n"

    def setUp(self):
        self.jspace = {
            "allowed_operations": ["read", "command", "modify"],
            "denied_operations": ["create", "delete"],
            "source_read": True,
            "read_scopes": ["answer.txt"],
            "write_scopes": ["answer.txt"],
            "verifier_commands": [{"argv": ["/usr/bin/python3", "verify.py"]}],
        }

    def _prompt_parts(self, task, text):
        handoff = "\n" + full_core.CAPABILITY_GAP_GUIDANCE
        self.assertTrue(text.endswith(handoff))
        self.assertEqual(text.count(full_core.CAPABILITY_GAP_GUIDANCE), 1)
        text = text.removesuffix(handoff)
        prefix = task + self._PROMPT_HEADER
        self.assertEqual(text[:len(prefix)].encode("utf-8"), prefix.encode("utf-8"))
        projection, guidance = text[len(prefix):].split("\n", 1)
        return json.loads(projection), guidance

    def test_verifier_only_applies_to_its_step_group(self):
        task = "Run admitted verification."
        projection, guidance = self._prompt_parts(task, full_core._provider_prompt(task, self.jspace))
        self.assertIn("admitted verifier_index only; caller-bound, no new authority.", guidance)
        self.assertIn("Verifier-only step group; whole-array task limits prevail.", guidance)
        self.assertIn("No task rewrites/invented capabilities.", guidance)
        self.assertEqual(projection["focused_verifier"], {
            "verifier_indices": [0],
            "example": {"commands": [{"command_type": "focused_verifier",
                                      "command_line": '{"verifier_index":0}', "step": 1}]},
        })
        self.assertEqual(projection["allowed_operations"], self.jspace["allowed_operations"])
        self.assertEqual(projection["denied_operations"], self.jspace["denied_operations"])

    def test_known_granted_patches_require_a_later_positive_verifier_step(self):
        task = "Patch and verify admitted source."
        _, guidance = self._prompt_parts(task, full_core._provider_prompt(task, self.jspace))
        self.assertIn("Task-permitted mixed responses: Once final patches are fully known, "
                      "exact-write patches precede the admitted focused_verifier "
                      "at a strictly later positive step in the same command_run response.", guidance)
        self.assertIn("No dependent same-step verification or missing-evidence edits.", guidance)

    def test_explicit_whole_array_verifier_restriction_takes_precedence(self):
        task = ("The WHOLE command_run commands array must contain only focused_verifier "
                "commands. Do not patch in this response.")
        _, guidance = self._prompt_parts(task, full_core._provider_prompt(task, self.jspace))
        self.assertIn("Verifier-only step group; whole-array task limits prevail.", guidance)
        self.assertIn("No task rewrites/invented capabilities.", guidance)
        self.assertIn("Task-permitted mixed responses: Once final patches are fully known,", guidance)

    def test_ordered_patch_guidance_preserves_existing_gates(self):
        cases = (
            ("command not granted", {"allowed_operations": ["read", "modify"]}),
            ("modify not granted", {"allowed_operations": ["read", "command"]}),
            ("command denied", {"denied_operations": ["command"]}),
            ("modify denied", {"denied_operations": ["modify"]}),
            ("invalid denied operations", {"denied_operations": None}),
            ("missing write scopes", {"write_scopes": None}),
            ("empty write scopes", {"write_scopes": []}),
            ("non-list write scopes", {"write_scopes": "answer.txt"}),
            ("non-string write scope", {"write_scopes": [7]}),
            ("empty write scope", {"write_scopes": [""]}),
        )
        for label, overrides in cases:
            with self.subTest(gate=label):
                text = full_core._provider_prompt("Task", {**self.jspace, **overrides})
                self.assertNotIn("Task-permitted mixed responses:", text)
                self.assertNotIn("Once final patches are fully known", text)

    def _source_read_cases(self):
        return (
            ("read-only", {**self.jspace, "allowed_operations": ["read", "command"],
                           "write_scopes": [], "verifier_commands": []}),
            ("edit+verifier", self.jspace),
        )

    def test_source_read_first_exact_file_read_without_borrowed_sha(self):
        task = "Read b.py. Only a.py's SHA is known: " + "a" * 64
        for label, contract in self._source_read_cases():
            with self.subTest(case=label):
                _, guidance = self._prompt_parts(task, full_core._provider_prompt(
                    task, {**contract, "read_scopes": ["a.py", "b.py"]}))
                if label == "read-only":
                    self.assertIn("First read: omit expected_sha256 only if no current exact-path SHA "
                                  "is known.", guidance)
                    self.assertIn("Never borrow another file's SHA; keep task-bound digests.", guidance)
                else:
                    self.assertIn("expected_sha256: this path's current verbatim 64-hex SHA; "
                                  "omit iff unknown.", guidance)
                    self.assertIn("Never borrow another file's SHA.", guidance)

    def test_source_read_preserves_task_and_explicit_digest(self):
        tasks = (
            "Read answer.txt with expected_sha256=" + "b" * 64 + ".\nKeep résumé output.",
            "The WHOLE command_run commands array must contain only focused_verifier "
            "commands. Do not patch in this response.",
            "original\n\t\u00e9\x00",
        )
        for label, contract in self._source_read_cases():
            for task in tasks:
                with self.subTest(case=label, task=task):
                    _, guidance = self._prompt_parts(task, full_core._provider_prompt(task, contract))
                    if label == "read-only":
                        self.assertIn("Never borrow another file's SHA; keep task-bound digests.", guidance)
                        self.assertIn("SHA-256 must be exactly 64 hexadecimal characters copied verbatim, "
                                      "not shortened or reconstructed.", guidance)
                    else:
                        self.assertIn("expected_sha256: this path's current verbatim 64-hex SHA; "
                                      "omit iff unknown.", guidance)
                        self.assertIn("Never borrow another file's SHA.", guidance)

    def test_source_read_continuation_binds_same_file_sha_and_cursor(self):
        for label, contract in self._source_read_cases():
            with self.subTest(case=label):
                task = "Continue answer.txt from its receipt."
                _, guidance = self._prompt_parts(task, full_core._provider_prompt(task, contract))
                if label == "read-only":
                    self.assertIn("Page same file: expected_sha256=returned SHA, "
                                  "start_line=next_line.", guidance)
                else:
                    self.assertIn("expected_sha256: this path's current verbatim 64-hex SHA; "
                                  "omit iff unknown.", guidance)
                    self.assertIn("Page: returned SHA/next_line.", guidance)

    def test_source_read_after_mutation_requires_fresh_scoped_readback(self):
        for label, contract in self._source_read_cases():
            with self.subTest(case=label):
                text = full_core._provider_prompt("Read answer.txt after mutation.", contract)
                self.assertIn("After mutation: fresh scoped readback, not preimage SHA.", text)

    def test_source_read_projection_preserves_authority(self):
        for label, base in self._source_read_cases():
            for binding in ("authorization", "legacy"):
                with self.subTest(case=label, binding=binding):
                    contract = {**base, "read_scopes": ["a.py", "b.py"],
                                "semantic_sha256": "b" * 64}
                    if binding == "authorization":
                        contract["authorization_semantic_sha256"] = "a" * 64
                    before = json.loads(json.dumps(contract))
                    task = "Inspect exact granted files."
                    projection, _ = self._prompt_parts(task, full_core._provider_prompt(task, contract))
                    expected = {
                        "jspace_semantic_sha256": "a" * 64 if binding == "authorization" else "b" * 64,
                        "allowed_operations": before["allowed_operations"],
                        "denied_operations": before["denied_operations"],
                        "schema_version": "nokiy_source_read_presentation_v1",
                        "source_read": True,
                        "read_scopes": before["read_scopes"],
                        "source_read_example": {
                            "commands": [{"command_type": "source_read",
                                          "command_line": '{"end_line":80,"path":"a.py","start_line":1}',
                                          "step": 1}],
                        },
                    }
                    if before["verifier_commands"]:
                        expected["focused_verifier"] = {
                            "verifier_indices": [0],
                            "example": {"commands": [{"command_type": "focused_verifier",
                                                      "command_line": '{"verifier_index":0}',
                                                      "step": 1}]},
                        }
                    self.assertEqual(projection, expected)
                    self.assertEqual(contract, before)

    def test_source_read_activation_gates_are_unchanged(self):
        base = self._source_read_cases()[0][1]
        cases = (
            ("command absent", {"allowed_operations": ["read"]}),
            ("read absent", {"allowed_operations": ["command"]}),
            ("operations not a list", {"allowed_operations": ("read", "command")}),
            ("operations unset", {"allowed_operations": None}),
            ("opt-in unset", {"source_read": None}),
            ("opt-in false", {"source_read": False}),
            ("truthy opt-in not true", {"source_read": 1}),
        )
        task = "A first read with an unknown digest must not add a tool grant."
        for label, overrides in cases:
            with self.subTest(gate=label):
                text = full_core._provider_prompt(task, {**base, **overrides})
                self.assertEqual(full_core._capability_gap_prompt(task), text)
                self.assertNotIn(self._PROMPT_HEADER, text)

    def test_source_read_preserves_verifier_fences_and_patch_order(self):
        task = "Patch and verify answer.txt."
        for label, contract in (
            ("mixed", self.jspace),
            ("verifier-only", {**self.jspace, "source_read": False}),
        ):
            with self.subTest(case=label):
                _, guidance = self._prompt_parts(task, full_core._provider_prompt(task, contract))
                for fence in (
                    "admitted verifier_index only; caller-bound, no new authority.",
                    "Verifier-only step group; whole-array task limits prevail.",
                    "No task rewrites/invented capabilities.",
                    "Reuse caller-verified current pre-edit baseline; reproduce if required; "
                    "run required focused post-edit verification.",
                    "Verifier success + semantically reviewed fresh scoped postimage readback "
                    "covers only the exact unchanged state: no redundant tests/reads/status polls.",
                    "Reverify after relevant source/test/config changes, failure, "
                    "unknown/mismatched evidence or explicit task requirements.",
                    "Reuse evidence, never ownership/permission clearance.",
                    "Mandatory status updates and terminal fences.",
                    "No argv/cwd/timeout overrides or blocked/cancel bypass.",
                    "Task-permitted mixed responses: Once final patches are fully known, "
                    "exact-write patches precede the admitted focused_verifier "
                    "at a strictly later positive step in the same command_run response.",
                    "No dependent same-step verification or missing-evidence edits.",
                ):
                    self.assertIn(fence, guidance)

    def test_source_postimages_context_is_review_evidence_not_authority_or_auto_done(self):
        task = "Review optional post-edit verifier context."
        before = json.loads(json.dumps(self.jspace))
        projection, guidance = self._prompt_parts(task, full_core._provider_prompt(task, self.jspace))
        self.assertEqual(set(projection), {
            "schema_version", "source_read", "read_scopes", "jspace_semantic_sha256",
            "allowed_operations", "denied_operations", "focused_verifier", "source_read_example",
        })
        for key in ("allowed_operations", "denied_operations", "read_scopes"):
            self.assertEqual(projection[key], before[key])
        self.assertNotIn("source_postimages", projection)
        self.assertEqual(self.jspace, before)
        for fence in (
            "Optional source_postimages: known exit0/reaped/group-empty success only.",
            "Semantically review complete definitions/bindings + support, or all-text delta "
            "on definition-size overflow (not complete definitions).",
            "No dependency/file proof; exact paths/postimage SHA/lines only.",
            "Missing/overflow/stale/uncovered evidence: ordinary granted source_read.",
            "Preserve raw stdout/stderr, verifier/receipt/cleanup facts; "
            "no new authority, acceptance or automatic done.",
            "If post-edit evidence is present, review:",
            "Mechanical missing readbacks only; mandatory task_status done "
            "at a strictly later positive step in the same existing command_run.",
            "Else propose fully-known mechanical checks/readbacks "
            "in strict later steps per guidance.",
            "No output-dependent decisions; safe exact read+write paths only.",
            "Result-dependent review: later model round.",
            "Keep publication fences; no failed/unknown-effect closure or expanded grants.",
            "Mandatory status updates and terminal fences.",
        ):
            with self.subTest(fence=fence):
                self.assertIn(fence, guidance)
        for label, contract in (
            self._source_read_cases()[0],
            ("verifier-only", {**self.jspace, "source_read": False}),
        ):
            with self.subTest(case=label):
                _, guidance = self._prompt_parts(task, full_core._provider_prompt(task, contract))
                self.assertNotIn("Optional source_postimages:", guidance)

    def test_composed_completion_allows_pending_checks_without_unseen_review(self):
        task = "Complete fully-known admitted final effects."
        for mode in ("assistant_reply", "evidence_only"):
            with self.subTest(mode=mode):
                prefix = task
                if mode == "evidence_only":
                    prefix += full_core._EVIDENCE_ONLY_COMPLETION_GUIDANCE
                text = full_core._provider_prompt(task, self.jspace, terminal_delivery=mode)
                _, guidance = self._prompt_parts(prefix, text)
                condition = "If post-edit evidence is present, review:"
                readbacks = "Mechanical missing readbacks only; mandatory task_status done"
                pending = ("Else propose fully-known mechanical checks/readbacks "
                           "in strict later steps per guidance.")
                self.assertLess(guidance.index(condition), guidance.index(readbacks))
                self.assertLess(guidance.index(readbacks), guidance.index(pending))
                self.assertNotIn("required verification must have passed", guidance)
                for fence in (
                    "Task-permitted mixed responses: Once final patches are fully known, "
                    "exact-write patches precede the admitted focused_verifier "
                    "at a strictly later positive step in the same command_run response.",
                    "No dependent same-step verification or missing-evidence edits.",
                    "at a strictly later positive step in the same existing command_run.",
                    "No output-dependent decisions; safe exact read+write paths only.",
                    "Result-dependent review: later model round.",
                    "Keep publication fences; no failed/unknown-effect closure or expanded grants.",
                    "Mandatory status updates and terminal fences.",
                ):
                    self.assertIn(fence, guidance)
                if mode == "evidence_only":
                    self.assertIn("Checks must pass before done executes, not before proposing.", text)
                    self.assertIn("Parent acceptance remains required.", text)
                    self.assertLess(text.index("not before proposing."), text.index(condition))

    def test_composed_completion_preserves_modes_and_absent_capability_routes(self):
        task = "Original task restrictions.\nKeep résumé output."
        cases = (
            ("mixed", self.jspace, True),
            ("no read", {**self.jspace, "source_read": False}, False),
            ("read not granted", {**self.jspace, "allowed_operations": ["command", "modify"]}, False),
            ("no modify", {**self.jspace, "allowed_operations": ["read", "command"]}, False),
            ("no writes", {**self.jspace, "write_scopes": []}, False),
            ("no verifier", {**self.jspace, "verifier_commands": []}, False),
            ("no command", {**self.jspace, "allowed_operations": ["read", "modify"]}, False),
            ("no capabilities", {}, False),
        )
        for label, contract, has_finish in cases:
            with self.subTest(route=label):
                before = copy.deepcopy(contract)
                default = full_core._provider_prompt(task, contract)
                explicit = full_core._provider_prompt(task, contract, terminal_delivery="assistant_reply")
                evidence = full_core._provider_prompt(task, contract, terminal_delivery="evidence_only")
                self.assertEqual(default.encode("utf-8"), explicit.encode("utf-8"))
                self.assertEqual(evidence.encode("utf-8"), full_core._provider_prompt(
                    task + full_core._EVIDENCE_ONLY_COMPLETION_GUIDANCE, contract,
                    terminal_delivery="assistant_reply").encode("utf-8"))
                for mode, text in (("assistant_reply", explicit), ("evidence_only", evidence)):
                    with self.subTest(mode=mode):
                        for rule in (
                            "If post-edit evidence is present, review:",
                            "Mechanical missing readbacks only; mandatory task_status done",
                            "Else propose fully-known mechanical checks/readbacks",
                        ):
                            self.assertEqual(rule in text, has_finish)
                        self.assertEqual(full_core._EVIDENCE_ONLY_COMPLETION_GUIDANCE in text,
                                         mode == "evidence_only")
                        if self._PROMPT_HEADER in text:
                            prefix = task + (full_core._EVIDENCE_ONLY_COMPLETION_GUIDANCE
                                             if mode == "evidence_only" else "")
                            projection, _ = self._prompt_parts(prefix, text)
                            self.assertEqual(projection["allowed_operations"], contract["allowed_operations"])
                            self.assertEqual(projection["denied_operations"], contract["denied_operations"])
                self.assertEqual(contract, before)

    def test_source_read_prompt_bytes_and_existing_safety_are_preserved(self):
        task = "Inspect answer.txt; preserve résumé output."
        mixed_with_sha = {
            **self.jspace,
            "repo_root": "/workspace/harness",
            "read_scopes": ["answer.txt", "verify.py"],
            "verifier_commands": [{
                **self.jspace["verifier_commands"][0],
                "pinned_files": [{"path": "/workspace/harness/verify.py", "sha256": "a" * 64}],
            }],
        }
        # Preserve source-read ceilings; projection JSON and the handoff suffix are separate.
        cases = (
            ("read-only", self._source_read_cases()[0][1], 994),
            ("edit+verifier", self.jspace, 2352),
            ("edit+verifier+sha", mixed_with_sha, 2439),
        )
        for label, contract, guidance_cap in cases:
            with self.subTest(case=label):
                before = json.loads(json.dumps(contract))
                projection, guidance = self._prompt_parts(task, full_core._provider_prompt(task, contract))
                self.assertLessEqual(len(("\n" + guidance).encode("utf-8")), guidance_cap)
                if label == "edit+verifier+sha":
                    self.assertEqual(projection["known_source_sha256_by_path"], {"verify.py": "a" * 64})
                    self.assertEqual(projection["source_read_example"], {
                        "commands": [{"command_type": "source_read",
                                      "command_line": '{"end_line":80,"path":"answer.txt","start_line":1}',
                                      "step": 1}],
                    })
                for fence in (
                    "Capsule: evidence, not a read grant.",
                    "Empty declared_targets or evidence_refs do not revoke scopes.",
                    "After mutation: fresh scoped readback, not preimage SHA.",
                    "runtime rechecks every read;",
                ):
                    self.assertIn(fence, guidance)
                if label == "read-only":
                    fences = (
                        "command_run: command_type source_read; JSON command_line: exact scoped path, "
                        "bounded start_line/end_line or search_terms.",
                        "Batch known independent reads in one command_run array, each scoped, "
                        "with source hashes/pagination cursors; "
                        "no speculative dependent reads or whole-file dumps.",
                        "Reuse verified unchanged ranges; reread for missing/changed evidence "
                        "or explicit task requirements.",
                        "Respect tool limits/pagination.",
                        "Missing DCF locators do not block exact granted paths.",
                        "no added operation/shell/path/write.",
                    )
                else:
                    fences = (
                        "source_read: bounded exact scoped JSON path/lines/search_terms.",
                        "Batch independent reads; no speculative dependencies/whole-file dumps.",
                        "expected_sha256: this path's current verbatim 64-hex SHA; omit iff unknown.",
                        "Never borrow another file's SHA.",
                        "Page: returned SHA/next_line.",
                        "runtime rechecks every read; no new grants.",
                        "Verifier success + semantically reviewed fresh scoped postimage readback "
                        "covers only the exact unchanged state: no redundant tests/reads/status polls.",
                        "Reverify after relevant source/test/config changes, failure, "
                        "unknown/mismatched evidence or explicit task requirements.",
                    )
                for fence in fences:
                    self.assertIn(fence, guidance)
                self.assertEqual(contract, before)


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("NOKIY_VERIFIER_TEST_ROUTER")
                     and os.environ.get("NOKIY_VERIFIER_TEST_SESSION_DB"),
                     "requires exact candidate Router and Session DB")
class VerifierFullCoreTests(unittest.TestCase):
    def test_real_router_failure_edit_success_replay_and_cleanup(self):
        helper = fixtures.FullCoreTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        f = helper.f
        for name, key in (("tura_router", "NOKIY_VERIFIER_TEST_ROUTER"),
                          ("tura_session_db", "NOKIY_VERIFIER_TEST_SESSION_DB")):
            shutil.copy2(Path(os.environ[key]).resolve(), f.runtime / name)
        solution = f.workspace / "answer.txt"
        solution.write_text("broken")
        script = f.workspace / "verify.py"
        scratch = f.artifacts / "verifier"
        scratch.mkdir()
        script.write_text("from pathlib import Path\nimport sys\n"
                          f"counter=Path({str(scratch / 'runs')!r})\n"
                          "counter.write_text(counter.read_text()+'x' if counter.exists() else 'x')\n"
                          "print('public-test', flush=True)\n"
                          "sys.exit(0 if Path('answer.txt').read_text() == 'fixed' else 1)\n")
        python = Path(sys.executable).resolve()
        action = {
            "mission":{"mission_id":"full-verifier-test","task_id":"exact-fixture",
                       "mode":"DELIVERY","objective":"Verify the full-core feedback loop",
                       "current_predicate":"real Router feedback not yet proven"},
            "context_summary":"Disposable fixture. No model, network, or production effects.",
            "operations":["read","command","modify"],
            "read_scopes":["verify.py","answer.txt"],"write_scopes":["answer.txt"],
            "target_paths":["answer.txt"],"command_templates":[],"source_read":True,
            "forbidden_effects":["network","delete"],
            "verifier_commands":[{"argv":[str(python),str(script)],
                "executable_sha256":caller._file_sha256(python),
                "pinned_files":[{"path":str(script),"sha256":caller._file_sha256(script)}],
                "timeout_seconds":5,"scratch_root":str(scratch),"network":False}],
        }
        capsule, contract = local_context.compile_context(f.workspace, action, artifact_root=f.artifacts)
        f.context.write_text(json.dumps(capsule))
        f.jspace.write_text(json.dumps(contract))
        f._write_executable(f.runtime / "tura_exec", f"#!{sys.executable}\n" + r'''
import json,os,pathlib,socket,sys
args=sys.argv[1:]
def value(key): return args[args.index(key)+1]
assert 'NOKIY_VERIFIER_FD' not in os.environ
contract=json.loads(pathlib.Path(value('--jspace-contract')).read_text())
workspace=pathlib.Path(value('-C'))
host,port=value('--router-address').rsplit(':',1)
def call(execution_id, request_id):
    payload={'session_id':value('--session-id'),'runtime_id':'synthetic-runtime',
        'session_directory':str(workspace),'arguments':{'execution_id':execution_id,
        'commands':[{'command':'focused_verifier','command_line':'{"verifier_index":0}',
                     'command_id':execution_id,'command_run_id':execution_id,
                     'provider_tool_call_id':'fixture-tool','command_index':0}]},
        'allowed_commands':['focused_verifier'],'jspace_contract':contract,'sandbox':True}
    request={'request_id':request_id,'kind':'call','method':'execution.command_run','payload':payload}
    with socket.create_connection((host,int(port)),timeout=20) as connection:
        connection.sendall((json.dumps(request)+'\n').encode())
        with connection.makefile('rb') as stream: reply=json.loads(stream.readline(1048576))
    assert reply['ok'],reply
    return reply['payload']['result']['results'][0]
results=[]
for batch,expected in [('public-first',1),('public-second',0)]:
    result=call(batch,batch)
    output=result['output'];receipt=output['terminal_receipt']
    assert output['exit_code']==expected,result
    assert receipt['outcome']=='known' and receipt['process_reaped'] and receipt['process_group_empty'],result
    results.append(result)
    print(json.dumps({'type':'item.completed','item':{'type':'command_execution',
        'status':'completed','command':'focused_verifier','aggregated_output':json.dumps(output)}}),flush=True)
    if expected: (workspace/'answer.txt').write_text('fixed')
replay=call('public-second','replay-second')
assert not replay['success'] and 'COMMAND_EXECUTION_ALREADY_CLAIMED' in json.dumps(replay),replay
state=pathlib.Path(os.environ['TURA_HOME'])
(state/'verifier-readback.json').write_text(json.dumps({'results':results,'replay':replay}))
print(json.dumps({'type':'item.completed','item':{'type':'assistant_message','text':'verified public feedback loop'}}))
print(json.dumps({'type':'turn.completed','status':'completed','usage':None,
    'model':value('-m'),'agent':value('-a'),'session_id':value('--session-id'),'cwd':value('-C'),
    'reasoning_effort':value('--model-reasoning-effort'),'service_tier':value('--service-tier')}))
''')
        helper._image()
        helper._request()
        value = json.loads(f.request_path.read_text())
        value["timeout_seconds"] = 40
        f.request_path.write_text(json.dumps(value))
        request = caller.load_request(f.request_path)
        terminal = caller.execute(request)
        self.assertEqual(terminal["status"], "RESULT_AVAILABLE", terminal)
        self.assertTrue(terminal["cleanup_pass"], terminal)
        self.assertEqual(terminal["tool_loop"], {"completed_count":2,"successful_count":1})
        self.assertEqual(solution.read_text(), "fixed")
        self.assertEqual((scratch / "runs").read_text(), "xx")
        run = request.artifact_root / request.request_id
        readback = json.loads((run / "execution-state/verifier-readback.json").read_text())
        ids = [item["call_id"] for item in readback["results"]]
        self.assertEqual(len(set(ids)), 2)
        self.assertFalse((scratch / ".tura").exists())
        self.assertEqual(caller.execute(request), terminal)
        self.assertEqual((scratch / "runs").read_text(), "xx")
