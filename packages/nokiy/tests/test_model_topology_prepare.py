# SPDX-License-Identifier: MIT
"""Real full-stack preparation/preflight; synthetic image and DCF, no provider.

Reuse existing fixture boundaries. A passing test is not live host admission.
"""
import hashlib
import json
import shutil
import unittest
from unittest.mock import patch

import test_full_stack as fixtures
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack as stack
from codex_collaboration_harness import model_topology as topology
from codex_collaboration_harness import batch


class ModelTopologyPrepareTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullStackTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def prepare(self, mode="nokiy-direct", family="sol", suffix=""):
        f = self.fixture
        output = f.output.with_name(f.output.name + suffix)
        f.f.request_path.write_text(json.dumps(f.draft))
        with f.compiler(), patch.object(caller, "execute") as execute:
            result = stack.prepare_request(f.f.request_path, f.action, "research_execution_engine",
                                           output, topology_model=mode, worker_family=family)
        execute.assert_not_called()
        return output, result

    def test_six_combinations_compile_into_actual_caller_requests(self):
        for i, mode in enumerate(topology.MODES):
            for j, family in enumerate(topology.FAMILIES):
                with self.subTest(mode=mode, family=family):
                    output, result = self.prepare(mode, family, str(i * 3 + j))
                    request = caller.load_request(output / "request.json")
                    self.assertEqual((request.model, request.reasoning_effort, request.execution_profile),
                                     ("gpt-6.1-sol" if family == "sol" else "gpt-6-" + family, "max", "direct"))
                    self.assertEqual(request.model_selection, topology.request_marker(topology.resolve(mode, family)))
                    self.assertEqual(request.native_thread_id, self.fixture.f.native_thread_id)
                    self.assertEqual(result["status"], "PREPARED")
                    self.assertFalse(result["root_topology_verified"])
                    selection = topology.verify_prepared_selection(output / "request.json")
                    self.assertEqual(selection, topology.resolve(mode, family))
                    self.assertEqual(result["model_selection"]["sha256"],
                                     hashlib.sha256((output / "model-selection.json").read_bytes()).hexdigest())

    def test_publish_request_after_selection_and_preparation(self):
        with patch.object(caller, "_write_create_only", wraps=caller._write_create_only) as writes:
            self.prepare()
        names = [call.args[0].name for call in writes.call_args_list]
        self.assertEqual(names[-3:], ["model-selection.json", "preparation.json", "request.json"])

    def test_batch_accepts_same_verified_selection_snapshot(self):
        output, _ = self.prepare()
        path = output / "request.json"
        request, raw = batch.load_prepared_request(path)
        self.assertEqual(raw, path.read_bytes())
        self.assertEqual((request.model, request.reasoning_effort), ("gpt-6.1-sol", "max"))
        self.assertEqual(request.model_selection, topology.request_marker(topology.resolve("nokiy-direct", "sol")))

    def test_batch_rejects_missing_companion_before_preflight_or_model(self):
        output, _ = self.prepare()
        (output / "model-selection.json").unlink()
        with patch.object(caller, "preflight") as preflight, patch.object(caller, "execute") as execute:
            with self.assertRaises(caller.EmbeddedNokiyError):
                batch.load_prepared_request(output / "request.json")
        preflight.assert_not_called()
        execute.assert_not_called()

    def test_batch_rejects_tampered_companion(self):
        output, _ = self.prepare()
        path = output / "model-selection.json"
        document = json.loads(path.read_text())
        document["selection"]["worker"]["model"] = "gpt-6-sol"
        path.write_text(json.dumps(document))
        with self.assertRaises(caller.EmbeddedNokiyError):
            batch.load_prepared_request(output / "request.json")

    def test_conflicting_draft_fails_before_compiler_or_output(self):
        self.fixture.draft["model"] = "gpt-6-luna"
        with self.fixture.compiler() as compiler:
            with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                self.prepare("nokiy-direct", "sol")
        self.assertEqual(raised.exception.code, "NOKIY_TOPOLOGY_REQUEST_CONFLICT")
        compiler.assert_not_called()
        self.assertFalse(self.fixture.output.exists())

    def test_selector_requires_explicit_topology(self):
        f = self.fixture
        f.f.request_path.write_text(json.dumps(f.draft))
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            stack.prepare_request(f.f.request_path, f.action, None, f.output, worker_family="sol")
        self.assertEqual(raised.exception.code, "NOKIY_TOPOLOGY_MODE_REQUIRED")

    def test_fast_is_worker_tier_not_family_or_commander_effort(self):
        self.fixture.draft["service_tier"] = "priority"
        output, _ = self.prepare("nokiy-direct", "luna")
        request = caller.load_request(output / "request.json")
        self.assertEqual(request.service_tier, "priority")
        selection = topology.verify_prepared_selection(output / "request.json")
        self.assertEqual(selection["commander"], {"model": "gpt-6-astra", "reasoning_effort": "ultra"})
        self.assertEqual(selection["worker"]["reasoning_effort"], "max")

    def test_request_tampering_blocked_before_provider_send(self):
        output, _ = self.prepare()
        path = output / "request.json"
        value = json.loads(path.read_text())
        value["model"] = "gpt-6-luna"
        path.write_text(json.dumps(value))
        with patch.object(caller, "execute") as execute, patch("builtins.print"):
            self.assertEqual(caller.main(["run", "--request", str(path)]), 2)
        execute.assert_not_called()

    def test_selection_tampering_and_symlink_blocked(self):
        output, _ = self.prepare()
        binding = output / "model-selection.json"
        original = binding.read_bytes()
        value = json.loads(original)
        value["selection"]["worker_family"] = "luna"
        binding.write_text(json.dumps(value))
        with self.assertRaises(caller.EmbeddedNokiyError):
            topology.verify_prepared_selection(output / "request.json")
        target = output / "other.json"
        target.write_bytes(original)
        binding.unlink()
        binding.symlink_to(target)
        with self.assertRaises(caller.EmbeddedNokiyError):
            topology.verify_prepared_selection(output / "request.json")

    def test_missing_declared_selection_does_not_become_legacy(self):
        output, _ = self.prepare()
        (output / "model-selection.json").unlink()
        with patch.object(caller, "execute") as execute, patch("builtins.print"):
            self.assertEqual(caller.main(["run", "--request", str(output / "request.json")]), 2)
        execute.assert_not_called()

    def test_removing_declaration_or_whole_preparation_fails_before_execute(self):
        for index, remove_whole in enumerate((False, True)):
            with self.subTest(remove_whole=remove_whole):
                output, _ = self.prepare(suffix=str(index))
                preparation_path = output / "preparation.json"
                if remove_whole:
                    preparation_path.unlink()
                else:
                    preparation = json.loads(preparation_path.read_text())
                    preparation.pop("model_selection")
                    preparation_path.write_text(json.dumps(preparation))
                with patch.object(caller, "execute") as execute, patch("builtins.print"):
                    self.assertEqual(caller.main(["run", "--request", str(output / "request.json")]), 2)
                execute.assert_not_called()

    def test_missing_both_companion_and_declaration_and_copied_request_fail(self):
        output, _ = self.prepare()
        (output / "model-selection.json").unlink()
        preparation_path = output / "preparation.json"
        preparation = json.loads(preparation_path.read_text())
        preparation.pop("model_selection")
        preparation_path.write_text(json.dumps(preparation))
        copied = self.fixture.f.root / "isolated-request.json"
        shutil.copyfile(output / "request.json", copied)
        for path in (output / "request.json", copied):
            with self.subTest(path=path), patch.object(caller, "execute") as execute, patch("builtins.print"):
                self.assertEqual(caller.main(["run", "--request", str(path)]), 2)
            execute.assert_not_called()

    def test_malformed_request_markers_are_not_legacy(self):
        output, _ = self.prepare()
        original = json.loads((output / "request.json").read_text())
        markers = (None, {}, {**original["model_selection"], "worker_family": "invalid"},
                   {**original["model_selection"], "schema_version": "v2"},
                   {**original["model_selection"], "extra": True},
                   {**original["model_selection"], "worker_family": "luna"})
        for marker in markers:
            with self.subTest(marker=marker):
                value = {**original, "model_selection": marker}
                (output / "request.json").write_text(json.dumps(value))
                with patch.object(caller, "execute") as execute, patch("builtins.print"):
                    self.assertEqual(caller.main(["run", "--request", str(output / "request.json")]), 2)
                execute.assert_not_called()

    def test_removed_request_marker_with_companion_does_not_run(self):
        output, _ = self.prepare()
        path = output / "request.json"
        value = json.loads(path.read_text())
        value.pop("model_selection")
        path.write_text(json.dumps(value))
        with patch.object(caller, "execute") as execute, patch("builtins.print"):
            self.assertEqual(caller.main(["run", "--request", str(path)]), 2)
        execute.assert_not_called()

    def test_replacement_after_request_snapshot_executes_original_selection(self):
        output, _ = self.prepare(suffix="-sol")
        other, _ = self.prepare(family="luna", suffix="-luna")
        path = output / "request.json"
        replacement = (other / "request.json").read_bytes()
        load_snapshot = caller._load_json_snapshot
        snapshots = []

        def swap_after_snapshot(candidate, *, limit, code):
            value, raw = load_snapshot(candidate, limit=limit, code=code)
            snapshots.append(candidate)
            if candidate == path:
                path.write_bytes(replacement)
            return value, raw

        with patch.object(caller, "_load_json_snapshot", side_effect=swap_after_snapshot), \
                patch.object(caller, "execute", return_value={"status": "RESULT_AVAILABLE"}) as execute, \
                patch("builtins.print"):
            self.assertEqual(caller.main(["run", "--request", str(path)]), 0)
        self.assertEqual(snapshots.count(path), 1)
        self.assertEqual(execute.call_args.args[0].model, "gpt-6.1-sol")
        self.assertEqual(caller.load_request(path).model, "gpt-6-luna")

    def test_companion_digest_and_decode_share_one_snapshot(self):
        output, _ = self.prepare()
        binding_path = output / "model-selection.json"
        snapshot = caller._load_json_snapshot
        opened = []

        def replace_after_snapshot(path, *, limit, code):
            value, raw = snapshot(path, limit=limit, code=code)
            if path == binding_path:
                opened.append(path)
                binding_path.write_text("{}")
            return value, raw

        with patch.object(caller, "_load_json_snapshot", side_effect=replace_after_snapshot):
            request, selection = topology.load_prepared_request(output / "request.json")
        self.assertEqual(opened, [binding_path])
        self.assertEqual(request.model, "gpt-6.1-sol")
        self.assertEqual(selection, topology.resolve("nokiy-direct", "sol"))

    def test_binding_does_not_claim_root_or_provider_observation(self):
        output, _ = self.prepare()
        binding = json.loads((output / "model-selection.json").read_text())
        self.assertFalse(binding["root_topology_verified"])
        self.assertIsNone(binding["provider_observed_model"])
        binding["root_topology_verified"] = True
        (output / "model-selection.json").write_text(json.dumps(binding))
        with self.assertRaises(caller.EmbeddedNokiyError):
            topology.verify_prepared_selection(output / "request.json")

    def test_recovery_reads_original_id_without_execute_or_new_selection(self):
        f = self.fixture
        output, _ = self.prepare()
        request_id = "tura_embedded_" + "a" * 64
        for companion_present in (True, False):
            with self.subTest(companion_present=companion_present):
                if not companion_present:
                    (output / "model-selection.json").unlink()
                with patch.object(caller, "read_terminal", return_value={"status": "RESULT_AVAILABLE"}) as read, \
                        patch.object(caller, "execute") as execute, \
                        patch.object(topology, "resolve") as resolve, patch("builtins.print"):
                    self.assertEqual(caller.main(["read-result", "--artifact-root", str(f.f.artifacts),
                                                  "--request-id", request_id]), 0)
                read.assert_called_once_with(f.f.artifacts, request_id)
                execute.assert_not_called()
                resolve.assert_not_called()

    def test_legacy_requests_do_not_acquire_selector_defaults(self):
        f = self.fixture
        f.f.request_path.write_text(json.dumps(f.draft))
        with f.compiler():
            result = stack.prepare_request(f.f.request_path, f.action, "research_execution_engine", f.output)
        self.assertNotIn("model_selection", result)
        self.assertIsNone(topology.verify_prepared_selection(f.output / "request.json"))
        with patch.object(caller, "execute", return_value={"status": "RESULT_AVAILABLE"}) as execute, \
                patch("builtins.print"):
            self.assertEqual(caller.main(["run", "--request", str(f.output / "request.json")]), 0)
        execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
