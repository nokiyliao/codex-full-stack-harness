import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core as core
from codex_collaboration_harness import source_excerpt as source


class CapsuleSourceDigestTests(unittest.TestCase):
    def setUp(self):
        self.contract = {
            "schema_version": "jspace_contract_v2", "repo_root": "/workspace",
            "authorization_semantic_sha256": "c" * 64,
            "dcf_generation": {"generation_id": "generation-1"},
            "source_read": True, "allowed_operations": ["read", "command"],
            "denied_operations": ["modify", "network"],
            "read_scopes": ["src/a.py", "src/b.py"], "write_scopes": [],
        }
        self.outline = {"kind": source.OUTLINE_KIND, "notice": source.OUTLINE_NOTICE,
                        "files": [{"path": path, "source_sha256": sha * 64, "definitions": []}
                                  for path, sha in [("src/a.py", "a"), ("src/b.py", "b")]]}
        self.capsule = self.capsule_for(self.outline)

    def capsule_for(self, outline):
        capsule = {"schema_version": "task_context_capsule_v1",
                   "context_summary": "Inspect scoped source." + source.CONTEXT_MARKER
                                      + source.encode_context(outline),
                   "jspace_semantic_sha256": self.contract["authorization_semantic_sha256"]}
        capsule["semantic_sha256"] = caller._canonical_sha256(capsule)
        return capsule

    def projection(self, *, enabled=True, capsule=None, contract=None):
        text = core._provider_prompt("Original task with explicit requirements.",
            self.contract if contract is None else contract,
            capsule=self.capsule if capsule is None else capsule,
            capsule_digest_reference=enabled)
        return text, json.loads(text.split("Verified execution capability (caller projection):\n", 1)[1].splitlines()[0])

    def test_bound_exact_readonly_pins_are_presentation_not_new_grants(self):
        before = copy.deepcopy((self.contract, self.capsule))
        self.assertEqual(source.readonly_outline_pins(self.capsule, self.contract),
                         {"src/a.py": "a" * 64, "src/b.py": "b" * 64})
        text, projection = self.projection()
        self.assertEqual(projection["admitted_source_digest_paths"], self.contract["read_scopes"])
        example = json.loads(projection["source_read_example"]["commands"][0]["command_line"])
        self.assertEqual(example, {"path": "src/a.py", "start_line": 1, "end_line": 80,
                                   "expected_sha256_ref": "admitted"})
        self.assertIn("explicit wrong hashes still reject", text)
        self.assertNotIn("known_source_sha256_by_path", projection)
        self.assertEqual((self.contract, self.capsule), before)

    def test_old_runtime_presentation_does_not_advertise_capsule_reference(self):
        _, projection = self.projection(enabled=False)
        self.assertNotIn("admitted_source_digest_paths", projection)
        self.assertEqual(json.loads(projection["source_read_example"]["commands"][0]["command_line"]),
                         ["src/a.py", 1, 80])

    def test_existing_outline_globals_do_not_disable_bound_pins(self):
        outline = copy.deepcopy(self.outline)
        outline["files"][0]["globals"] = [[["CONFIG"], 1, 3]]
        capsule = self.capsule_for(outline)
        _, projection = self.projection(capsule=capsule)
        self.assertEqual(projection["admitted_source_digest_paths"], self.contract["read_scopes"])

    def test_changed_or_foreign_binding_is_not_presented(self):
        for key, value in [("semantic_sha256", "0" * 64),
                           ("jspace_semantic_sha256", "0" * 64),
                           ("schema_version", "other")]:
            capsule = copy.deepcopy(self.capsule)
            capsule[key] = value
            if key != "semantic_sha256":
                capsule["semantic_sha256"] = caller._canonical_sha256(
                    {k: v for k, v in capsule.items() if k != "semantic_sha256"})
            with self.subTest(key=key):
                self.assertEqual(core._capsule_digest_paths(capsule, self.contract), [])

    def test_missing_authorization_or_foreign_root_and_generation_is_not_presented(self):
        contract = copy.deepcopy(self.contract)
        contract.pop("authorization_semantic_sha256")
        capsule = self.capsule_for(self.outline)
        capsule["jspace_semantic_sha256"] = None
        capsule["semantic_sha256"] = caller._canonical_sha256(
            {k: v for k, v in capsule.items() if k != "semantic_sha256"})
        self.assertEqual(core._capsule_digest_paths(capsule, contract), [])
        for field, metadata in (("surface", {"repo_root": "/foreign"}),
                                ("dcf_generation", {"repo_root": "/foreign"}),
                                ("dcf_generation", {"generation_id": "other"})):
            capsule = {**self.capsule, field: metadata}
            capsule["semantic_sha256"] = caller._canonical_sha256(
                {k: v for k, v in capsule.items() if k != "semantic_sha256"})
            with self.subTest(field=field, metadata=metadata):
                self.assertEqual(core._capsule_digest_paths(capsule, self.contract), [])

    def test_invalid_missing_duplicate_or_extra_pins_are_not_advertised(self):
        for shape in ("short_hash", "uppercase", "duplicate", "missing", "outside", "unknown_field"):
            outline = copy.deepcopy(self.outline)
            if shape == "short_hash":
                outline["files"][0]["source_sha256"] = "a" * 67
            elif shape == "uppercase":
                outline["files"][0]["source_sha256"] = "A" * 64
            elif shape == "duplicate":
                outline["files"].append(copy.deepcopy(outline["files"][0]))
            elif shape == "missing":
                outline["files"].pop()
            elif shape == "outside":
                outline["files"][0]["path"] = "outside.py"
            else:
                outline["files"][0]["new_grant"] = True
            with self.subTest(shape=shape):
                self.assertEqual(core._capsule_digest_paths(self.capsule_for(outline), self.contract), [])

    def test_writable_denied_mapped_or_nonexact_scopes_stay_on_existing_path(self):
        for delta in ({"write_scopes": ["src/a.py"]}, {"denied_operations": ["read"]},
                      {"mapped_source_reads": [{"logical_path": "src/a.py"}]},
                      {"read_scopes": ["src/**"]}, {"source_read": False},
                      {"allowed_operations": ["read"]}):
            contract = {**copy.deepcopy(self.contract), **delta}
            with self.subTest(delta=delta):
                self.assertEqual(core._capsule_digest_paths(self.capsule, contract), [])

    def test_ambiguous_or_invalid_structured_json_never_becomes_a_pin(self):
        for summary in ("Freeform sha256: " + "a" * 64,
                        self.capsule["context_summary"] + source.CONTEXT_MARKER + "{}",
                        "Task" + source.CONTEXT_MARKER + '{"kind":0,"kind":1}',
                        "Task" + source.CONTEXT_MARKER + "null"):
            capsule = {**self.capsule, "context_summary": summary}
            capsule["semantic_sha256"] = caller._canonical_sha256(
                {k: v for k, v in capsule.items() if k != "semantic_sha256"})
            self.assertEqual(core._capsule_digest_paths(capsule, self.contract), [])

    def test_capability_probe_requires_exact_supported_router(self):
        runtime = SimpleNamespace(artifacts={"tura_router": SimpleNamespace(path=Path("/router"))})
        for stdout, returncode, expected in [
            (core.CAPSULE_DIGEST_CAPABILITY + b"\n", 0, True),
            (core.SOURCE_DIGEST_CAPABILITY + b"\n", 0, False),
            (core.CAPSULE_DIGEST_CAPABILITY + b"\n", 1, False),
        ]:
            with patch.object(core.subprocess, "run", return_value=SimpleNamespace(
                    stdout=stdout, returncode=returncode)) as run:
                self.assertIs(core._supports_capsule_digest(runtime, self.capsule, self.contract, {}), expected)
                self.assertEqual(run.call_args.args[0], ["/router", "capsule-source-read-capabilities"])
        with patch.object(core.subprocess, "run") as run:
            self.assertFalse(core._supports_capsule_digest(runtime, None, self.contract, {}))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
