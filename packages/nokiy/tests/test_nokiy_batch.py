# SPDX-License-Identifier: MIT
"""Offline batch coordination contract: never call a provider in these tests."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_collaboration_harness import batch
from codex_collaboration_harness.embedded_nokiy import EmbeddedNokiyError


THREAD = "019fd4f0-1079-77e2-8be7-cbf75ca28df5"


class BatchFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()  # macOS /var is a /private/var alias.
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.members = []
        self.requests = []
        for index in range(getattr(self, "member_count", 2)):
            request_path = root / f"request-{index}.json"
            request_path.write_text(f"{{\"index\":{index}}}\n")
            artifact_root = root / f"artifacts-{index}"
            artifact_root.mkdir()
            request_id = "tura_embedded_" + f"{index + 1:064x}"
            contract = root / f"jspace-{index}.json"
            self.write_scopes(contract, [f"input-{index}.txt"], [f"output-{index}.txt"])
            self.members.append({"request_path": str(request_path),
                                 "request_file_sha256": hashlib.sha256(request_path.read_bytes()).hexdigest(),
                                 "request_id": request_id, "artifact_root": str(artifact_root)})
            self.requests.append(SimpleNamespace(
                request_id=request_id, artifact_root=artifact_root, workspace=self.workspace,
                jspace_contract=SimpleNamespace(path=contract, sha256=hashlib.sha256(contract.read_bytes()).hexdigest()),
                schema_version="tura_embedded_request_v3", execution_profile="direct",
                native_thread_id=THREAD, model=f"model-{index}", reasoning_effort="max",
                service_tier="priority", model_selection=None))
        self.plan = root / "plan.json"
        self.save_plan()

    def write_scopes(self, path, reads, writes):
        path.write_text(json.dumps({
            "schema_version": "jspace_contract_v2", "repo_root": str(self.workspace), "read_scopes": reads,
            "write_scopes": writes, "declared_targets": writes,
            "allowed_operations": ["read", "create", "modify", "command"],
            "source_read": True, "command_templates": [],
            "command_effect_policy": "trusted_argv_effects_v1",
            "expansion": {"mode": "exact_target_only", "error_code": "JSPACE_EXPANSION_REQUIRED",
                          "mutation_on_expansion": False}}))

    def bind_contract(self, index, contract):
        identity = self.requests[index].jspace_contract
        identity.path.write_text(json.dumps(contract))
        identity.sha256 = hashlib.sha256(identity.path.read_bytes()).hexdigest()

    def focused_verifiers(self):
        # A pinned Mach-O header fixture, never an executable test/provider call.
        executable = self.plan.parent / "verifier-fixture"
        executable.write_bytes(bytes.fromhex("cffaedfe") + b"offline fixture\n")
        executable.chmod(0o755)
        contracts = []
        for index, request in enumerate(self.requests):
            script = self.workspace / f"verify-{index}.py"
            script.write_text("# offline verifier fixture\n")
            scratch = request.artifact_root / "verifier-scratch"
            scratch.mkdir()
            contract = json.loads(request.jspace_contract.path.read_text())
            contract["read_scopes"].append(script.name)
            contract["denied_operations"] = ["delete", "network"]
            contract["verifier_artifact_root"] = str(request.artifact_root)
            contract["verifier_commands"] = [{
                "argv": [str(executable), str(script)],
                "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "pinned_files": [{"path": str(script),
                                  "sha256": hashlib.sha256(script.read_bytes()).hexdigest()}],
                "timeout_seconds": 7, "scratch_root": str(scratch), "network": False}]
            self.bind_contract(index, contract)
            contracts.append(contract)
        return contracts

    def save_plan(self):
        self.plan.write_text(json.dumps({"schema_version": batch.SCHEMA, "members": self.members}))
        self.digest = hashlib.sha256(self.plan.read_bytes()).hexdigest()

    def mocks(self, *, preflight=None, execute=None, project=True):
        self.enterContext(patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD}))
        self.enterContext(patch.object(batch.caller, "decode_request",
                                       side_effect=lambda data: self.requests[data["index"]]))
        pre = self.enterContext(patch.object(batch.caller, "preflight", side_effect=preflight or (lambda r: {"status": "READY"})))
        exe = self.enterContext(patch.object(batch.caller, "execute", side_effect=execute or (lambda r: self.terminal(r))))
        if project:
            self.enterContext(patch.object(batch, "_project", side_effect=lambda m, terminal: {
                **terminal, "result_inspection": {"status": "EVIDENCE_VERIFIED", "first_blocker": None}}))
        return pre, exe

    @staticmethod
    def terminal(request, status="RESULT_AVAILABLE", cleanup=True):
        return {"status": status, "request_id": request.request_id,
                "native_thread_id": request.native_thread_id,
                "first_typed_blocker": None if status == "RESULT_AVAILABLE" else "MEMBER_FAILED",
                "cleanup_pass": cleanup, "model": request.model,
                "reasoning_effort": request.reasoning_effort,
                "requested_service_tier": request.service_tier}

    def test_plan_identity_and_overlap_rejected_before_preflight(self):
        pre, exe = self.mocks()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, "0" * 64)
        self.members[1]["request_id"] = self.members[0]["request_id"]
        self.save_plan()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.members[1]["request_id"] = self.requests[1].request_id
        self.members[1]["artifact_root"] = self.members[0]["artifact_root"]
        self.save_plan()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.members.append(dict(self.members[0], request_id="tura_embedded_" + "f" * 64))
        self.save_plan()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.members.pop()
        pre.assert_not_called()
        exe.assert_not_called()

    def test_all_preflight_before_any_execute(self):
        pre, exe = self.mocks(preflight=lambda r: {"status": "BLOCKED"} if r == self.requests[1] else {"status": "READY"})
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 2)
        exe.assert_not_called()

    def test_request_bytes_and_actual_thread_bound_before_execution(self):
        pre, exe = self.mocks()
        self.members[1]["request_file_sha256"] = "0" * 64
        self.save_plan()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.members[1]["request_file_sha256"] = hashlib.sha256(
            Path(self.members[1]["request_path"]).read_bytes()).hexdigest()
        self.requests[1].native_thread_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        self.save_plan()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 2)
        exe.assert_not_called()

    def test_snapshot_rejects_duplicate_nan_oversize_and_symlink(self):
        path = Path(self.members[0]["request_path"])
        for raw in (b'{"index":0,"index":1}', b'{"index":NaN}',
                    b" " * (batch.caller.MAX_REQUEST_BYTES + 1)):
            path.write_bytes(raw)
            with self.assertRaises(EmbeddedNokiyError):
                batch.load_prepared_request(path)
        target = path.with_name("target.json")
        target.write_text('{"index":0}')
        path.unlink()
        path.symlink_to(target)
        with self.assertRaises(EmbeddedNokiyError):
            batch.load_prepared_request(path)

    def test_plan_snapshot_rejects_duplicate_and_non_object(self):
        for raw in (b'{"schema_version":0,"schema_version":1}', b'NaN', b'[]'):
            self.plan.write_bytes(raw)
            with self.assertRaises(EmbeddedNokiyError):
                batch.load_plan(self.plan, hashlib.sha256(raw).hexdigest())

    def test_batch_import_does_not_require_model_topology(self):
        with patch.dict(sys.modules, {"codex_collaboration_harness.model_topology": None}):
            importlib.reload(batch)

    def test_run_uses_decoded_snapshot_not_second_read(self):
        path = Path(self.members[0]["request_path"])
        expected = self.members[0]["request_file_sha256"]
        _, execute = self.mocks()
        original_decode = batch.caller.decode_request

        def mutate_after_decode(document):
            if document["index"] == 0:
                path.write_bytes(b'{"index":1}')
            return original_decode(document)

        with patch.object(batch.caller, "decode_request", side_effect=mutate_after_decode):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertNotEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
        self.assertEqual(execute.call_count, 2)

    def test_source_only_selection_rejected_before_preflight(self):
        pre, exe = self.mocks()
        path = Path(self.members[0]["request_path"])
        for extra in ({"model_selection_sha256": "0" * 64},
                      {"preparation": {"model_selection": {"marker": "source-only"}}}):
            path.write_text(json.dumps({"index": 0, **extra}))
            self.members[0]["request_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            self.save_plan()
            with self.assertRaisesRegex(EmbeddedNokiyError, "model selection"):
                batch.run_batch(self.plan, self.digest)
        pre.assert_not_called()
        exe.assert_not_called()

    def test_scope_overlap_and_unsupported_commands_fail_closed(self):
        _, exe = self.mocks()
        self.write_scopes(self.requests[1].jspace_contract.path, ["output-0.txt"], ["output-1.txt"])
        self.requests[1].jspace_contract.sha256 = hashlib.sha256(self.requests[1].jspace_contract.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EmbeddedNokiyError, "overlap"):
            batch.run_batch(self.plan, self.digest)
        self.write_scopes(self.requests[1].jspace_contract.path, ["input-1.txt"], ["output-0.txt"])
        self.requests[1].jspace_contract.sha256 = hashlib.sha256(self.requests[1].jspace_contract.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EmbeddedNokiyError, "overlap"):
            batch.run_batch(self.plan, self.digest)
        self.write_scopes(self.requests[1].jspace_contract.path, ["input-1/**"], ["output-1.txt"])
        self.requests[1].jspace_contract.sha256 = hashlib.sha256(self.requests[1].jspace_contract.path.read_bytes()).hexdigest()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.write_scopes(self.requests[1].jspace_contract.path, ["input-1.txt"], ["output-1.txt"])
        contract = json.loads(self.requests[1].jspace_contract.path.read_text())
        contract["verifier_commands"] = [{"argv": ["pytest"], "effects": ["read"]}]
        self.requests[1].jspace_contract.path.write_text(json.dumps(contract))
        self.requests[1].jspace_contract.sha256 = hashlib.sha256(self.requests[1].jspace_contract.path.read_bytes()).hexdigest()
        with self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        exe.assert_not_called()

    def test_cross_member_hardlink_aliases_fail_before_execution(self):
        pre, exe = self.mocks()
        original = self.workspace / "output-0.txt"
        original.write_text("existing output")
        alias = self.workspace / "alias-1.txt"
        os.link(original, alias)
        for reads, writes in ((["alias-1.txt"], ["output-1.txt"]),
                              (["input-1.txt"], ["alias-1.txt"])):
            self.write_scopes(self.requests[1].jspace_contract.path, reads, writes)
            self.requests[1].jspace_contract.sha256 = hashlib.sha256(
                self.requests[1].jspace_contract.path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(EmbeddedNokiyError, "overlap"):
                batch.run_batch(self.plan, self.digest)
        original.unlink()
        source = self.workspace / "input-0.txt"
        source.write_text("existing input")
        alias.unlink()
        os.link(source, alias)
        self.write_scopes(self.requests[1].jspace_contract.path, ["input-1.txt"], ["alias-1.txt"])
        self.requests[1].jspace_contract.sha256 = hashlib.sha256(
            self.requests[1].jspace_contract.path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(EmbeddedNokiyError, "overlap"):
            batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 6)
        exe.assert_not_called()

    def test_focused_verifiers_allow_two_verified_workers_without_new_source_grants(self):
        contracts = self.focused_verifiers()
        selection = SimpleNamespace(fixture="efficient-selection")
        self.requests[1].model_selection = selection
        for contract in contracts:
            scratch = Path(contract["verifier_commands"][0]["scratch_root"])
            (scratch / "existing-output.txt").write_text("ordinary scratch fixture")

        def execute(request):
            self.assertEqual(pre.call_count, 2)
            self.assertTrue(any(request is original for original in self.requests))
            barrier.wait()
            return self.terminal(request)

        pre, exe = self.mocks(execute=execute, project=False)
        for read_only in (False, True):
            with self.subTest(read_only=read_only):
                bound_contracts = deepcopy(contracts)
                if read_only:
                    # Scratch effects do not imply create/modify source authority.
                    bound_contracts[1].update(write_scopes=[], declared_targets=[],
                                              allowed_operations=["read", "command"],
                                              denied_operations=["create", "modify", "delete", "network"])
                self.bind_contract(1, bound_contracts[1])
                barrier = threading.Barrier(2, timeout=2)
                pre.reset_mock()
                exe.reset_mock()
                # Selection binding is independently tested; this fixture's
                # synthetic marker only checks preservation after decoding.
                with patch.object(batch, "load_prepared_request", side_effect=lambda path: (
                        next(request for request, member in zip(self.requests, self.members)
                             if Path(member["request_path"]) == path), path.read_bytes())), \
                        patch.object(batch, "worker_capacity", return_value=2), \
                        patch("codex_collaboration_harness.result_inspection.inspect", return_value={
                            "status": "EVIDENCE_VERIFIED", "first_blocker": None, "commands": [],
                            "pagination": {"offset": 0, "limit": 24, "next_offset": None}}) as inspector:
                    result = batch.run_batch(self.plan, self.digest)
                self.assertEqual(result["status"], "RESULT_AVAILABLE")
                self.assertEqual(pre.call_count, 2)
                self.assertEqual(exe.call_count, 2)
                self.assertEqual(inspector.call_count, 2)
                self.assertIs(self.requests[1].model_selection, selection)
                self.assertEqual([row["request_id"] for row in result["members"]],
                                 [request.request_id for request in self.requests])
                self.assertEqual([row["terminal"]["model"] for row in result["members"]],
                                 [request.model for request in self.requests])
                self.assertTrue(all(row["cleanup_pass"] and
                                    row["terminal"]["result_inspection"]["status"] == "EVIDENCE_VERIFIED"
                                    for row in result["members"]))
                for request, contract in zip(self.requests, bound_contracts):
                    self.assertEqual(json.loads(request.jspace_contract.path.read_text()), contract)

    def test_focused_verifiers_still_complete_all_preflights_before_execution(self):
        self.focused_verifiers()
        pre, exe = self.mocks(preflight=lambda r: {"status": "BLOCKED"} if r is self.requests[1]
                              else {"status": "READY"})
        with patch.object(batch, "worker_capacity", return_value=2), \
                self.assertRaises(EmbeddedNokiyError):
            batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 2)
        exe.assert_not_called()

    def test_invalid_focused_verifier_grants_fail_before_execution(self):
        contracts = self.focused_verifiers()
        pre, exe = self.mocks()
        foreign_script = contracts[0]["verifier_commands"][0]["pinned_files"][0]
        cases = (
            ("missing root", lambda c: c.pop("verifier_artifact_root")),
            ("root only", lambda c: c.pop("verifier_commands")),
            ("foreign root", lambda c: c.update(verifier_artifact_root=str(self.requests[0].artifact_root))),
            ("no command", lambda c: c.update(allowed_operations=["read", "create", "modify"])),
            ("no read", lambda c: c.update(allowed_operations=["create", "modify", "command"])),
            ("denied command", lambda c: c.update(denied_operations=["command"])),
            ("denied read", lambda c: c.update(denied_operations=["read"])),
            ("denied modify", lambda c: c.update(denied_operations=["modify"])),
            ("denied write", lambda c: c.update(denied_operations=["write"])),
            ("invalid denials", lambda c: c.update(denied_operations="command")),
            ("empty commands", lambda c: c.update(verifier_commands=[])),
            ("arbitrary command", lambda c: c["verifier_commands"][0].update(argv=["pytest"])),
            ("shell", lambda c: c["verifier_commands"][0].update(argv=["/bin/sh", "-c", "true"])),
            ("extra effects", lambda c: c["verifier_commands"][0].update(effects=["read"])),
            ("executable pin", lambda c: c["verifier_commands"][0].update(executable_sha256="0" * 64)),
            ("input pin", lambda c: c["verifier_commands"][0]["pinned_files"][0].update(sha256="0" * 64)),
            ("no pins", lambda c: c["verifier_commands"][0].update(pinned_files=[])),
            ("foreign pin", lambda c: c["verifier_commands"][0].update(pinned_files=[foreign_script])),
            ("unbound pin", lambda c: c["verifier_commands"][0]["argv"].pop()),
            ("network", lambda c: c["verifier_commands"][0].update(network=True)),
            ("timeout", lambda c: c["verifier_commands"][0].update(timeout_seconds=True)),
            ("artifact root scratch", lambda c: c["verifier_commands"][0].update(
                scratch_root=str(self.requests[1].artifact_root))),
            ("foreign scratch", lambda c: c["verifier_commands"][0].update(
                scratch_root=contracts[0]["verifier_commands"][0]["scratch_root"])),
            ("workspace scratch", lambda c: c["verifier_commands"][0].update(scratch_root=str(self.workspace))),
            ("unallocated scratch", lambda c: c["verifier_commands"][0].update(
                scratch_root=str(self.requests[1].artifact_root / "unallocated"))),
            ("glob source", lambda c: c["read_scopes"].append("**")),
            ("directory source", lambda c: c["read_scopes"].append("discovery")),
            ("source search", lambda c: c.update(read_search_scopes=["verify-1.py"])),
            ("read commands", lambda c: c.update(read_commands={})),
            ("command templates", lambda c: c.update(command_templates=[{"argv": ["pwd"]}])),
        )
        (self.workspace / "discovery").mkdir()
        with patch.object(batch, "worker_capacity", return_value=2):
            for label, mutate in cases:
                with self.subTest(label=label):
                    contract = deepcopy(contracts[1])
                    mutate(contract)
                    self.bind_contract(1, contract)
                    pre.reset_mock()
                    with self.assertRaises(EmbeddedNokiyError):
                        batch.run_batch(self.plan, self.digest)
                    self.assertEqual(pre.call_count, 1)
                    exe.assert_not_called()

    def test_focused_verifier_pin_drift_fails_before_execution(self):
        contracts = self.focused_verifiers()
        pre, exe = self.mocks()
        for key, expected_preflights in (("pinned_files", 1), ("argv", 0)):
            pre.reset_mock()
            command = contracts[1]["verifier_commands"][0]
            path = Path(command[key][0]["path"] if key == "pinned_files" else command[key][0])
            original = path.read_bytes()
            path.write_bytes(original + b"changed\n")
            with patch.object(batch, "worker_capacity", return_value=2), \
                    self.assertRaisesRegex(EmbeddedNokiyError, "SHA-256 changed"):
                batch.run_batch(self.plan, self.digest)
            path.write_bytes(original)
            self.assertEqual(pre.call_count, expected_preflights)
        exe.assert_not_called()

    def test_focused_verifier_pinned_reads_conflict_with_foreign_source_writes(self):
        contracts = self.focused_verifiers()
        pre, exe = self.mocks()
        script = Path(contracts[0]["verifier_commands"][0]["pinned_files"][0]["path"])
        alias = self.workspace / "pinned-alias.txt"
        os.link(script, alias)
        for target in (script, alias):
            contract = deepcopy(contracts[1])
            contract["write_scopes"].append(target.name)
            contract["declared_targets"].append(target.name)
            self.bind_contract(1, contract)
            with patch.object(batch, "worker_capacity", return_value=2), \
                    self.assertRaisesRegex(EmbeddedNokiyError, "cross-member.*overlap"):
                batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 4)
        exe.assert_not_called()

    def test_focused_verifier_executable_hardlink_conflicts_with_foreign_write(self):
        contracts = self.focused_verifiers()
        executable = Path(contracts[0]["verifier_commands"][0]["argv"][0])
        other_executable = self.plan.parent / "other-verifier-fixture"
        other_executable.write_bytes(executable.read_bytes())
        other_executable.chmod(0o755)
        contracts[1]["verifier_commands"][0]["argv"][0] = str(other_executable)
        alias = self.workspace / "executable-alias.txt"
        os.link(executable, alias)
        contracts[1]["write_scopes"].append(alias.name)
        contracts[1]["declared_targets"].append(alias.name)
        self.bind_contract(1, contracts[1])
        pre, exe = self.mocks()
        with patch.object(batch, "worker_capacity", return_value=2), \
                self.assertRaisesRegex(EmbeddedNokiyError, "cross-member.*overlap"):
            batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 2)
        exe.assert_not_called()

    def test_focused_verifier_scratch_writes_conflict_with_foreign_executable_reads(self):
        contracts = self.focused_verifiers()
        executable = Path(contracts[0]["verifier_commands"][0]["argv"][0])
        pre, exe = self.mocks()
        for owner in (0, 1):
            bound_contracts = deepcopy(contracts)
            scratch = Path(bound_contracts[owner]["verifier_commands"][0]["scratch_root"])
            owned_executable = scratch / "owned-verifier-fixture"
            owned_executable.write_bytes(executable.read_bytes())
            owned_executable.chmod(0o755)
            bound_contracts[1 - owner]["verifier_commands"][0]["argv"][0] = str(owned_executable)
            for index, contract in enumerate(bound_contracts):
                self.bind_contract(index, contract)
            pre.reset_mock()
            with patch.object(batch, "worker_capacity", return_value=2), \
                    self.assertRaisesRegex(EmbeddedNokiyError, "cross-member.*overlap"):
                batch.run_batch(self.plan, self.digest)
            self.assertEqual(pre.call_count, 2)
            exe.assert_not_called()

    def test_focused_verifier_dependencies_cannot_be_member_write_targets(self):
        contracts = self.focused_verifiers()
        contract = contracts[0]
        script = Path(contract["verifier_commands"][0]["pinned_files"][0]["path"])
        contract["write_scopes"].append(script.name)
        contract["declared_targets"].append(script.name)
        self.bind_contract(0, contract)
        pre, exe = self.mocks()
        with patch.object(batch, "worker_capacity", return_value=2), \
                self.assertRaisesRegex(EmbeddedNokiyError, "immutable verifier dependencies"):
            batch.run_batch(self.plan, self.digest)
        pre.assert_not_called()
        exe.assert_not_called()

    def test_focused_verifier_duplicate_or_nested_scratch_scopes_fail(self):
        contracts = self.focused_verifiers()
        pre, exe = self.mocks()
        scratch = Path(contracts[1]["verifier_commands"][0]["scratch_root"])
        nested = scratch / "nested"
        nested.mkdir()
        for roots in ((scratch, scratch), (scratch, nested), (nested, scratch)):
            contract = deepcopy(contracts[1])
            first = contract["verifier_commands"][0]
            first["scratch_root"] = str(roots[0])
            second = deepcopy(first)
            second["argv"].append("--fixture-second")
            second["scratch_root"] = str(roots[1])
            contract["verifier_commands"].append(second)
            self.bind_contract(1, contract)
            with patch.object(batch, "worker_capacity", return_value=2), \
                    self.assertRaisesRegex(EmbeddedNokiyError, "scratch"):
                batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 3)
        exe.assert_not_called()

    def test_focused_verifier_symlink_grants_fail_before_execution(self):
        contracts = self.focused_verifiers()
        pre, exe = self.mocks()
        for kind in ("executable", "pin", "scratch", "scratch parent"):
            with self.subTest(kind=kind):
                contract = deepcopy(contracts[1])
                command = contract["verifier_commands"][0]
                if kind == "executable":
                    alias = self.plan.parent / "executable-symlink"
                    alias.symlink_to(command["argv"][0])
                    command["argv"][0] = str(alias)
                elif kind == "pin":
                    alias = self.workspace / "pin-symlink.py"
                    alias.symlink_to(command["pinned_files"][0]["path"])
                    command["argv"][1] = str(alias)
                    command["pinned_files"][0]["path"] = str(alias)
                    contract["read_scopes"].append(alias.name)
                else:
                    alias = self.requests[1].artifact_root / kind.replace(" ", "-")
                    alias.symlink_to(contracts[0]["verifier_commands"][0]["scratch_root"],
                                     target_is_directory=True)
                    if kind == "scratch parent":
                        (Path(contracts[0]["verifier_commands"][0]["scratch_root"]) / "nested").mkdir()
                        alias = alias / "nested"
                    command["scratch_root"] = str(alias)
                self.bind_contract(1, contract)
                with patch.object(batch, "worker_capacity", return_value=2), \
                        self.assertRaisesRegex(EmbeddedNokiyError, "symlink"):
                    batch.run_batch(self.plan, self.digest)
        self.assertEqual(pre.call_count, 4)
        exe.assert_not_called()

    def test_focused_verifier_scratch_descendant_aliases_fail_before_execution(self):
        contracts = self.focused_verifiers()
        pre, exe = self.mocks()
        scratch = Path(contracts[0]["verifier_commands"][0]["scratch_root"])
        workspace_file = self.workspace / "input-1.txt"
        workspace_file.write_text("workspace fixture")
        other_scratch = Path(contracts[1]["verifier_commands"][0]["scratch_root"])
        other_file = other_scratch / "owned.txt"
        other_file.write_text("foreign scratch fixture")
        for symlink, target in ((True, self.workspace), (True, other_scratch),
                                (False, workspace_file), (False, other_file)):
            with self.subTest(symlink=symlink, target=target):
                alias = scratch / "alias"
                if symlink:
                    alias.symlink_to(target, target_is_directory=True)
                else:
                    os.link(target, alias)
                try:
                    with patch.object(batch, "worker_capacity", return_value=2), \
                            self.assertRaisesRegex(EmbeddedNokiyError, "scratch contains"):
                        batch.run_batch(self.plan, self.digest)
                finally:
                    alias.unlink()
        pre.assert_not_called()
        exe.assert_not_called()

    def test_focused_verifier_directory_identity_aliases_fail_before_execution(self):
        contracts = self.focused_verifiers()
        scratch = Path(contracts[0]["verifier_commands"][0]["scratch_root"])
        pre, exe = self.mocks()
        original_stat = Path.stat
        # Directory hardlinks/bind aliases are not portable fixture operations;
        # model their inode identity while retaining real typed files and paths.
        cases = (
            (self.requests[1].artifact_root, self.requests[0].artifact_root,
             "overlapping artifact roots", 0),
            (scratch, self.workspace, "scratch and workspace overlap", 0),
            (scratch, self.requests[1].artifact_root, "foreign member artifact ownership", 2),
        )
        for alias, target, blocker, expected_preflights in cases:
            with self.subTest(blocker=blocker):
                target_identity = original_stat(target)

                def aliased_stat(path, *args, **kwargs):
                    return target_identity if path == alias else original_stat(path, *args, **kwargs)

                pre.reset_mock()
                with patch.object(Path, "stat", aliased_stat), \
                        patch.object(batch, "worker_capacity", return_value=2), \
                        self.assertRaisesRegex(EmbeddedNokiyError, blocker):
                    batch.run_batch(self.plan, self.digest)
                self.assertEqual(pre.call_count, expected_preflights)
                exe.assert_not_called()

    def test_focused_verifier_scratch_inspection_is_bounded(self):
        contracts = self.focused_verifiers()
        scratch = Path(contracts[0]["verifier_commands"][0]["scratch_root"])
        (scratch / "one.txt").write_text("scratch fixture")
        pre, exe = self.mocks()
        with patch.object(batch, "worker_capacity", return_value=2), \
                patch.object(batch, "MAX_SCRATCH_ENTRIES", 1), \
                self.assertRaisesRegex(EmbeddedNokiyError, "bounded entry limit"):
            batch.run_batch(self.plan, self.digest)
        pre.assert_not_called()
        exe.assert_not_called()

    def test_two_simultaneous_workers_preserve_original_request_identity(self):
        lock = threading.Lock()
        release = threading.Event()
        count = 0
        maximum = 0

        def execute(request):
            nonlocal count, maximum
            with lock:
                count += 1
                maximum = max(maximum, count)
                if count == 2:
                    release.set()
            if not release.wait(timeout=2):
                raise AssertionError("second independent worker did not start")
            with lock:
                count -= 1
            return self.terminal(request)

        pre, exe = self.mocks(execute=execute)
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertEqual(maximum, 2)
        self.assertEqual(pre.call_count, 2)
        self.assertEqual(exe.call_count, 2)
        self.assertEqual([r["request_id"] for r in result["members"]], [r.request_id for r in self.requests])
        self.assertEqual([r["terminal"]["model"] for r in result["members"]], [r.model for r in self.requests])

    def test_partial_failure_and_cleanup_uncertainty_are_not_all_success(self):
        self.mocks(execute=lambda r: self.terminal(r, "BLOCKED", False) if r == self.requests[1] else self.terminal(r))
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "MEMBER_FAILED")
        self.assertTrue(result["members"][0]["cleanup_pass"])
        self.assertFalse(result["members"][1]["cleanup_pass"])

    def test_cleanup_only_uncertainty(self):
        self.mocks(execute=lambda r: self.terminal(r, cleanup=False) if r == self.requests[1] else self.terminal(r))
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "NOKIY_BATCH_MEMBER_INCOMPLETE")

    def test_missing_projection_cannot_make_batch_successful(self):
        self.mocks()
        with patch.object(batch, "_project", side_effect=lambda m, terminal: terminal):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "NOKIY_BATCH_MEMBER_INCOMPLETE")

    def test_missing_receipt_proof_preserves_inspection_blocker(self):
        self.mocks(project=False)
        with patch("codex_collaboration_harness.result_inspection.inspect", return_value={
                    "status": "INCOMPLETE_EVIDENCE", "first_blocker": "EXECUTION_PROOF_UNAVAILABLE",
                    "commands": [], "pagination": {"offset": 0, "limit": 24, "next_offset": None},
                }) as inspector:
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(inspector.call_count, 2)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "EXECUTION_PROOF_UNAVAILABLE")
        self.assertEqual(result["members"][0]["terminal"]["result_inspection"]["status"],
                         "INCOMPLETE_EVIDENCE")

    def test_inspection_blocker_precedes_terminal_blocker(self):
        self.mocks(execute=lambda r: self.terminal(r, "BLOCKED"), project=False)
        with patch("codex_collaboration_harness.result_inspection.inspect", return_value={
                    "status": "INCOMPLETE_EVIDENCE", "first_blocker": "EXECUTION_PROOF_UNAVAILABLE",
                    "commands": [], "pagination": {"offset": 0, "limit": 24, "next_offset": None},
                }):
            result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "EXECUTION_PROOF_UNAVAILABLE")

    def test_projection_uses_actual_caller_thread_not_receipt_identity(self):
        self.mocks(project=False)
        with patch.dict(os.environ, {"CODEX_THREAD_ID": "ffffffff-ffff-ffff-ffff-ffffffffffff"}), \
                patch("codex_collaboration_harness.result_inspection.inspect",
                      side_effect=AssertionError("must not inspect mismatched caller")):
            for member in self.members:
                (Path(member["artifact_root"]) / member["request_id"]).mkdir()
            with patch.object(batch.caller, "read_terminal", side_effect=lambda root, rid: self.terminal(
                    self.requests[0] if rid == self.requests[0].request_id else self.requests[1])):
                result = batch.read_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["first_typed_blocker"], "CALLER_THREAD_ID_MISMATCH")

    def test_worker_exception_does_not_hide_other_result(self):
        def execute(request):
            if request == self.requests[1]:
                raise RuntimeError("fixture crash")
            return self.terminal(request)

        self.mocks(execute=execute)
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["members"][0]["status"], "RESULT_AVAILABLE")
        self.assertEqual(result["members"][1]["first_typed_blocker"], "NOKIY_BATCH_EXECUTION_UNCERTAIN")

    def test_reentry_never_starts_missing_member_and_read_survives_expiry(self):
        original = self.requests[0]
        run = original.artifact_root / original.request_id
        run.mkdir()
        # Simulate a lost terminal after create-only claim; request files can expire.
        self.members[0]["request_path"] = str(self.plan.parent / "expired.json")
        Path(self.members[1]["request_path"]).unlink()
        self.save_plan()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": THREAD}), \
                patch.object(batch, "load_prepared_request", side_effect=AssertionError("must not load")), \
                patch.object(batch.caller, "preflight", side_effect=AssertionError("must not preflight")), \
                patch.object(batch.caller, "execute", side_effect=AssertionError("must not execute")):
            result = batch.run_batch(self.plan, self.digest)
            read = batch.read_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(read["members"][0]["first_typed_blocker"], "NOKIY_EMBEDDED_RECEIPT_INVALID")
        self.assertEqual(read["members"][1]["first_typed_blocker"], "NOKIY_BATCH_NOT_STARTED")

    def test_original_id_readback_and_projection_without_request(self):
        self.mocks(project=False)
        for member in self.members:
            Path(member["request_path"]).unlink()
        with patch.object(batch.caller, "read_terminal", side_effect=lambda root, rid: self.terminal(
                self.requests[0] if rid == self.requests[0].request_id else self.requests[1])) as reader, \
                patch("codex_collaboration_harness.result_inspection.inspect", return_value={
                    "status": "EVIDENCE_VERIFIED", "first_blocker": None, "commands": [],
                    "pagination": {"offset": 0, "limit": 24, "next_offset": None},
                }) as inspector:
            for member in self.members:
                (Path(member["artifact_root"]) / member["request_id"]).mkdir()
            result = batch.read_batch(self.plan, self.digest)
        self.assertEqual(result["status"], "RESULT_AVAILABLE")
        self.assertEqual(reader.call_count, 2)
        self.assertEqual(inspector.call_count, 2)
        self.assertEqual([row["request_id"] for row in result["members"]],
                         [request.request_id for request in self.requests])

    def test_recovery_explicitly_omits_snapshot_without_publication(self):
        self.mocks(project=False)
        for member in self.members:
            (Path(member["artifact_root"]) / member["request_id"]).mkdir()
        with patch.object(batch.caller, "read_terminal", side_effect=lambda root, rid: self.terminal(
                self.requests[0] if rid == self.requests[0].request_id else self.requests[1])), \
                patch("codex_collaboration_harness.result_inspection.inspect", return_value={
                    "status": "EVIDENCE_VERIFIED", "first_blocker": None, "commands": [],
                    "pagination": {"offset": 0, "limit": 24, "next_offset": None}}), \
                patch.object(batch, "retain_inspection_summary", side_effect=AssertionError("read-only")):
            result = batch.read_batch(self.plan, self.digest)
        compact = batch.expand_summary(batch.summarize(result))
        for row in compact["members"]:
            self.assertIsNone(row["inspection_summary"])
            self.assertEqual(row["inspection_summary_omission"], "READ_ONLY_RECOVERY")


if __name__ == "__main__":
    unittest.main()
