"""Postimage completeness, identity, capacity and disclosure boundary tests."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import difflib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import postimage_context as context


def dcf_generation(workspace):
    """Action-scoped DCF fixture shape, deliberately without source_snapshot."""
    domains = ["authority", "dcf_contract", "evidence", "surface"]
    return {
        "action_freshness": {"required_domains": domains,
                             "source_fingerprints": {domain: "b" * 64 for domain in domains}},
        "generated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "generation_id": "test-dcf-generation", "input_fingerprint": "c" * 64,
        "repo_head": "d" * 40, "repo_root": str(workspace),
        "required_capabilities": ["surface-map", "verifier-gates"],
        "required_domain_bindings": {}, "worktree_fingerprint": "e" * 64,
    }


class PostimageContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.success = dict(success=True, exit_code=0, outcome="known",
                            process_reaped=True, process_group_empty=True,
                            stdout="raw verifier output\n", stderr="raw warning\n")
        self.emitted = set()

    def write(self, name, text):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))

    def fixture(self, sources=None, *, dcf=False):
        self.sources = sources or {"work.py": "def answer():\n    return 1\n"}
        for name, text in self.sources.items():
            self.write(name, text)
        self.contract = {
            "repo_root": str(self.workspace), "authorization_semantic_sha256": "a" * 64,
            "source_read": True, "allowed_operations": ["command", "read", "modify"],
            "denied_operations": (["delete", "install", "network", "system_mutation"]
                                  if dcf else ["create", "delete"]),
            "read_scopes": sorted(self.sources), "write_scopes": sorted(self.sources),
            "dcf_generation": dcf_generation(self.workspace) if dcf else {"source_snapshot": {
                name: {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
                for name, text in self.sources.items()}},
        }
        if dcf:
            self.contract["schema_version"] = "jspace_contract_v2"
        preimages = context.capture_preimages(self.workspace, self.contract,
            validated_dcf_generation=self.contract["dcf_generation"] if dcf else None)
        self.assertIsNotNone(preimages)
        return preimages

    def project(self, preimages, observation=None):
        return context.project(self.workspace, self.contract, preimages,
                               self.success if observation is None else observation, self.emitted)

    def assert_wire(self, payload):
        self.assertIsNotNone(payload)
        self.assertEqual(set(payload), {"schema_version", "kind", "jspace_semantic_sha256",
                                       "notice", "files"})
        self.assertEqual(payload["schema_version"], "nokiy_source_postimages_v1")
        self.assertEqual(payload["kind"], "verifier_postimage_context")
        self.assertEqual(payload["jspace_semantic_sha256"], "a" * 64)
        self.assertIn("not dependency resolution or complete-file proof", payload["notice"])
        self.assertIn("not verifier facts, acceptance or a new grant", payload["notice"])
        self.assertLessEqual(len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("ascii")),
                             context.MAX_JSON_BYTES)
        for row in payload["files"]:
            self.assertEqual(set(row), {"path", "preimage_sha256", "postimage_sha256",
                                        "locators", "spans"})
            data = (self.workspace / row["path"]).read_bytes()
            self.assertEqual(row["postimage_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(row["preimage_sha256"], hashlib.sha256(
                self.sources[row["path"]].encode("utf-8")).hexdigest())
            # Fixture newlines are physical LF or CRLF; do not normalize wire text.
            lines = data.decode("utf-8").splitlines(keepends=True)
            previous = 0
            for span in row["spans"]:
                self.assertEqual(set(span), {"start_line", "end_line", "text"})
                self.assertGreater(span["start_line"], previous)
                self.assertLessEqual(span["end_line"], len(lines))
                self.assertEqual(span["text"], "".join(lines[span["start_line"]-1:span["end_line"]]))
                previous = span["end_line"]
            for locator in row["locators"]:
                self.assertEqual(set(locator), {"qualified_name", "kind", "line", "start_line",
                                               "end_line", "complete"})
                self.assertIs(locator["complete"], True)
                self.assertLessEqual(locator["start_line"], locator["line"])
                self.assertLessEqual(locator["line"], locator["end_line"])
                self.assertTrue(any(span["start_line"] <= locator["start_line"]
                                    and locator["end_line"] <= span["end_line"]
                                    for span in row["spans"]))
        return payload["files"]

    def assert_delta_file(self, row, *, navigation=False, coordinates=None):
        keys = {"path", "preimage_sha256", "postimage_sha256", "coverage", "hunks"}
        self.assertEqual(set(row), keys | ({"locators"} if navigation else set()))
        self.assertEqual(row["coverage"], "all_text_changes")
        before = self.sources[row["path"]]
        data = (self.workspace / row["path"]).read_bytes()
        after = data.decode("utf-8")
        self.assertEqual(row["preimage_sha256"], hashlib.sha256(before.encode("utf-8")).hexdigest())
        self.assertEqual(row["postimage_sha256"], hashlib.sha256(data).hexdigest())
        old_lines = io.StringIO(before, newline="").readlines()
        new_lines = io.StringIO(after, newline="").readlines()
        fields = ("before_start_line", "before_line_count", "after_start_line", "after_line_count")
        if coordinates is None:
            groups = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False).get_grouped_opcodes(3)
            coordinates = [(group[0][1] + 1, group[-1][2] - group[0][1],
                            group[0][3] + 1, group[-1][4] - group[0][3]) for group in groups]
        self.assertEqual([tuple(hunk[field] for field in fields) for hunk in row["hunks"]],
                         coordinates)
        old_cursor = new_cursor = 0
        rebuilt_before, rebuilt_after = [], []
        for hunk in row["hunks"]:
            self.assertEqual(set(hunk), set(fields) | {"before_text", "after_text"})
            for field in fields:
                self.assertIs(type(hunk[field]), int)
                self.assertGreaterEqual(hunk[field], 1 if field.endswith("start_line") else 0)
            i1, j1 = hunk["before_start_line"] - 1, hunk["after_start_line"] - 1
            i2, j2 = i1 + hunk["before_line_count"], j1 + hunk["after_line_count"]
            self.assertGreaterEqual(i1, old_cursor)
            self.assertGreaterEqual(j1, new_cursor)
            self.assertLessEqual(i2, len(old_lines))
            self.assertLessEqual(j2, len(new_lines))
            self.assertEqual(old_lines[old_cursor:i1], new_lines[new_cursor:j1])
            self.assertEqual(hunk["before_text"], "".join(old_lines[i1:i2]))
            self.assertEqual(hunk["after_text"], "".join(new_lines[j1:j2]))
            self.assertEqual(len(io.StringIO(hunk["before_text"], newline="").readlines()),
                             hunk["before_line_count"])
            self.assertEqual(len(io.StringIO(hunk["after_text"], newline="").readlines()),
                             hunk["after_line_count"])
            rebuilt_after.extend(old_lines[old_cursor:i1])
            rebuilt_after.append(hunk["after_text"])
            rebuilt_before.extend(new_lines[new_cursor:j1])
            rebuilt_before.append(hunk["before_text"])
            old_cursor, new_cursor = i2, j2
        self.assertEqual(old_lines[old_cursor:], new_lines[new_cursor:])
        rebuilt_after.extend(old_lines[old_cursor:])
        rebuilt_before.extend(new_lines[new_cursor:])
        self.assertEqual("".join(rebuilt_after).encode("utf-8"), data)
        self.assertEqual("".join(rebuilt_before).encode("utf-8"), before.encode("utf-8"))
        if navigation:
            self.assertIs(type(row["locators"]), list)
            names = []
            for locator in row["locators"]:
                self.assertEqual(set(locator), {"qualified_name", "kind", "line", "start_line", "end_line"})
                name = locator["qualified_name"]
                self.assertIsInstance(name, str)
                self.assertTrue(all(name.split(".")))
                self.assertFalse(any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name))
                self.assertLessEqual(len(name.encode("utf-8")), context.MAX_DELTA_NAME_BYTES)
                self.assertIn(locator["kind"], ("function", "async_function", "class", "binding"))
                for field in ("line", "start_line", "end_line"):
                    self.assertIs(type(locator[field]), int)
                    self.assertGreater(locator[field], 0)
                self.assertLessEqual(locator["start_line"], locator["line"])
                self.assertLessEqual(locator["line"], locator["end_line"])
                names.append(name)
            self.assertEqual(len(names), len(set(names)))
            self.assertEqual(row["locators"], sorted(row["locators"], key=lambda locator: (
                locator["start_line"], locator["end_line"], locator["qualified_name"])))

    def assert_delta_wire(self, payload, *, schema_version=None, coordinates=None):
        self.assertIsNotNone(payload)
        self.assertEqual(set(payload), {"schema_version", "kind", "jspace_semantic_sha256", "notice", "files"})
        self.assertIn(payload["schema_version"], ("nokiy_source_postimage_delta_v1", "nokiy_source_postimage_delta_v2"))
        if schema_version is not None:
            self.assertEqual(payload["schema_version"], schema_version)
        navigation = payload["schema_version"] == "nokiy_source_postimage_delta_v2"
        self.assertEqual(payload["kind"], "verifier_postimage_delta")
        self.assertEqual(payload["jspace_semantic_sha256"], "a" * 64)
        self.assertEqual(payload["notice"], context.DELTA_NAVIGATION_NOTICE if navigation else context.DELTA_NOTICE)
        self.assertIn("All textual changes between each pinned preimage and fresh postimage", payload["notice"])
        self.assertIn("not complete definitions, dependencies or files", payload["notice"])
        self.assertIn("not verifier facts, acceptance or a new grant", payload["notice"])
        if navigation:
            self.assertIn("AST locators identify qualified units in the pinned postimage", payload["notice"])
            self.assertIn("they do not supply complete source text or prove dependencies", payload["notice"])
            self.assertLessEqual(sum(len(row["locators"]) for row in payload["files"]), context.MAX_DELTA_LOCATORS)
        self.assertLessEqual(len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("ascii")),
                             context.MAX_JSON_BYTES)
        self.assertLessEqual(sum(hunk["before_line_count"] + hunk["after_line_count"]
                                 for row in payload["files"] for hunk in row["hunks"]), context.MAX_LINES)
        self.assertIs(type(payload["files"]), list)
        self.assertTrue(payload["files"])
        for row in payload["files"]:
            self.assertTrue(row["hunks"])
            self.assert_delta_file(row, navigation=navigation,
                                   coordinates=None if coordinates is None else coordinates[row["path"]])
        return payload["files"]

    def delta_v1_and_navigation(self, preimages):
        with patch.object(context, "_with_delta_navigation",
                          side_effect=lambda envelope, navigation: envelope) as upgrade:
            payload = self.project(preimages)
        self.assert_delta_wire(payload, schema_version="nokiy_source_postimage_delta_v1")
        upgrade.assert_called_once()
        self.assertIs(upgrade.call_args.args[0], payload)
        return payload, upgrade.call_args.args[1]

    def test_admitted_text_extensions_use_only_complete_v1_deltas_without_parsing(self):
        sources = {
            ".js": "function answer() {\n  return 1;\n}\n",
            ".mjs": "export const answer = () => 1;\n",
            ".cjs": "module.exports = () => 1;\n",
            ".jsx": "export const Answer = () => <span>1</span>;\n",
            ".ts": "export const answer: number = 1;\n",
            ".tsx": "export const Answer = (): JSX.Element => <span>1</span>;\n",
            ".rs": "pub fn answer() -> u32 {\n    1\n}\n",
        }
        for suffix, before in sources.items():
            with self.subTest(suffix=suffix), \
                    patch.object(context.ast, "parse", side_effect=AssertionError("no text parser")), \
                    patch.object(context.source_excerpt, "still_current",
                                 side_effect=AssertionError("Python-only freshness helper")):
                name = "src/work" + suffix
                preimages = self.fixture({name: before})
                self.assertIsNone(preimages.sources[name][1])
                self.assertIsNone(self.project(preimages))
                self.write(name, before.replace("1", "2"))
                original = copy.deepcopy((self.contract, self.success, self.emitted))
                row, = self.assert_delta_wire(self.project(preimages),
                                              schema_version=context.DELTA_SCHEMA_VERSION)
                self.assertEqual(row["path"], name)
                self.assertEqual((self.contract, self.success, self.emitted), original)

    def test_text_insert_delete_and_multiple_hunks_preserve_exact_physical_text(self):
        before = "".join(f"// line {index}\n" for index in range(20))
        cases = (
            ("", "export const snow = '\u96ea';"),
            ("export const snow = '\u96ea';\n", ""),
            ("\ufeff// caf\u00e9\r\nconst snow = '\u96ea';\rconst keep = 1;\n// end",
             "\ufeff// caf\u00e9\r\nconst added = '\U0001f30d';\rconst keep = 1;\n// new end"),
            (before, before.replace("// line 2\n", "// inserted\n// line 2\n")
                           .replace("// line 15\n", "")),
        )
        for old, new in cases:
            with self.subTest(old=old, new=new):
                preimages = self.fixture({"work.mjs": old})
                self.write("work.mjs", new)
                row, = self.assert_delta_wire(self.project(preimages),
                                              schema_version=context.DELTA_SCHEMA_VERSION)
                if not old or not new:
                    hunk, = row["hunks"]
                    self.assertEqual((hunk["before_start_line"], hunk["after_start_line"]), (1, 1))
                    self.assertEqual(hunk["before_line_count"] if not old else hunk["after_line_count"], 0)
                if old == before:
                    self.assertEqual(len(row["hunks"]), 2)

    def test_mixed_python_and_text_changes_share_v1_and_one_diff_per_file(self):
        for padding in (0, context.MAX_LINES):
            with self.subTest(padding=padding):
                python = "def answer():\n" + "    pass\n" * padding + "    return 1\n"
                preimages = self.fixture({"a.py": python, "b.mjs": "export const answer = 1;\n",
                                          "c.rs": "pub const ANSWER: u32 = 1;\n"})
                for name, before in self.sources.items():
                    self.write(name, before.replace("1", "2"))
                with patch.object(context, "_line_changes", wraps=context._line_changes) as changes, \
                        patch.object(context, "_project_file", wraps=context._project_file) as definitions, \
                        patch.object(context, "_with_delta_navigation") as navigation:
                    rows = self.assert_delta_wire(self.project(preimages),
                                                   schema_version=context.DELTA_SCHEMA_VERSION)
                self.assertEqual([row["path"] for row in rows], ["a.py", "b.mjs", "c.rs"])
                self.assertEqual(changes.call_count, 3)
                definitions.assert_called_once()
                navigation.assert_not_called()

    def test_unchanged_text_preserves_python_definition_and_navigation_behavior(self):
        for padding in (0, context.MAX_LINES):
            with self.subTest(padding=padding):
                python = "def answer():\n" + "    pass\n" * padding + "    return 1\n"
                preimages = self.fixture({"work.py": python, "work.ts": "export const answer = 1;\n"})
                self.write("work.py", python.replace("return 1", "return 2"))
                payload = self.project(preimages)
                rows = (self.assert_delta_wire(payload, schema_version=context.DELTA_NAVIGATION_SCHEMA_VERSION)
                        if padding else self.assert_wire(payload))
                self.assertEqual([row["path"] for row in rows], ["work.py"])

    def test_text_never_turns_invalid_or_incomplete_python_into_a_delta(self):
        python = "def answer():\n    return 1\n\ndef keep():\n    return 2\n"
        for text_name, python_name in (("a.mjs", "z.py"), ("z.mjs", "a.py")):
            for after in ("def answer(\n", "def keep():\n    return 2\n",
                          "# changed outside any unit\n" + python,
                          python + "def answer():\n    return 3\n"):
                with self.subTest(text_name=text_name, after=after):
                    preimages = self.fixture({python_name: python, text_name: "export const answer = 1;\n"})
                    self.write(text_name, "export const answer = 2;\n")
                    self.write(python_name, after)
                    with patch.object(context, "_project_delta_file") as delta:
                        self.assertIsNone(self.project(preimages))
                        delta.assert_not_called()
                    self.assertEqual(self.emitted, set())
        preimages = self.fixture({"work.py": python, "work.js": "const answer = 1;\n"})
        for invalid in ("def broken(\n", python + "def answer():\n    return 3\n"):
            self.write("work.py", invalid)
            self.contract["dcf_generation"]["source_snapshot"]["work.py"]["sha256"] = hashlib.sha256(
                invalid.encode()).hexdigest()
            self.assertIsNone(context.capture_preimages(self.workspace, self.contract))

    def test_text_exact_read_write_intersection_never_reads_other_sources(self):
        self.fixture({name: "const answer = 1;\n" for name in
                      ("public.js", "private.ts", "read_only.rs", "unsupported.txt")}, dcf=True)
        self.contract["read_scopes"] = ["public.js", "read_only.rs", "*.ts", ".", "unsupported.txt"]
        self.contract["write_scopes"] = [str(self.workspace / "public.js"), "private.ts", "*.rs",
                                         "unsupported.txt"]
        reader = context.source_excerpt._read_exact
        with patch.object(context.source_excerpt, "_read_exact", wraps=reader) as reads, \
                patch.object(Path, "rglob", side_effect=AssertionError("no discovery")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("no discovery")):
            preimages = context.capture_preimages(self.workspace, self.contract,
                validated_dcf_generation=self.contract["dcf_generation"])
            self.assertIsNotNone(preimages)
            self.assertEqual(set(preimages.pins), {"public.js"})
            for name in self.sources:
                self.write(name, "const answer = 2;\n")
            row, = self.assert_delta_wire(self.project(preimages), schema_version=context.DELTA_SCHEMA_VERSION)
            self.assertEqual(row["path"], "public.js")
        self.assertTrue(all(call.args[1] == "public.js" and call.kwargs == {"python_only": False}
                            for call in reads.call_args_list))

    def test_text_denied_missing_or_nonexact_grants_cannot_capture_or_project(self):
        preimages = self.fixture({"work.ts": "export const answer = 1;\n"})
        original = copy.deepcopy(self.contract)
        self.write("work.ts", "export const answer = 2;\n")
        updates = (
            {"source_read": False}, {"source_read": 1}, {"repo_root": "/other"},
            {"allowed_operations": ["command", "read"]}, {"allowed_operations": ["read", "modify"]},
            {"denied_operations": ["read"]}, {"denied_operations": ["modify"]},
            {"denied_operations": ["command"]}, {"authorization_semantic_sha256": "b" * 64},
            {"read_scopes": []}, {"write_scopes": []}, {"read_scopes": ["*.ts"]},
            {"write_scopes": ["*.ts"]}, {"read_scopes": ["."]}, {"write_scopes": [str(self.workspace)]},
            {"read_scopes": [str(self.workspace / "work.ts")]}, {"write_scopes": ["../work.ts"]},
        )
        for update in updates:
            with self.subTest(update=update), patch.object(context.source_excerpt, "_read_exact") as reads:
                self.contract = dict(original, **update)
                self.assertIsNone(self.project(preimages))
                # A different valid binding can capture its own bytes, but stale pins cannot.
                if "authorization_semantic_sha256" not in update:
                    self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
                reads.assert_not_called()

    def test_unsupported_text_types_and_unsafe_paths_are_not_read(self):
        preimages = self.fixture({"work.js": "const answer = 1;\n"})
        original = copy.deepcopy(self.contract)
        for name in ("work.txt", "work.json", "work.pyc", "work.wasm", "work.JS", "work.js.map",
                     "work.vue", "work", "*.js", "./work.js", "../work.js", ".git/work.js",
                     "work.js/", "src\\work.js", "bad\x00.js", "bad\n.js"):
            with self.subTest(name=name), patch.object(context.source_excerpt, "_read_exact") as reads:
                self.assertFalse(context._safe_path(name))
                self.contract = dict(original, read_scopes=[name], write_scopes=[name])
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
                self.assertIsNone(self.project(preimages))
                reads.assert_not_called()

    def test_text_snapshot_pins_and_validated_dcf_identity_remain_mandatory(self):
        for dcf in (False, True):
            with self.subTest(dcf=dcf):
                preimages = self.fixture({"work.mjs": "export const answer = 1;\n"}, dcf=dcf)
                generation = copy.deepcopy(self.contract["dcf_generation"])
                if dcf:
                    self.assertIsNot(preimages.dcf_generation, self.contract["dcf_generation"])
                    with patch.object(context.source_excerpt, "_read_exact") as reads:
                        self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
                        reads.assert_not_called()
                    self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                        validated_dcf_generation=dict(generation, generation_id="other")))
                for snapshot in ({"work.mjs": {"sha256": "b" * 64}}, {"work.mjs": {}}, None):
                    self.contract["dcf_generation"] = dict(generation, source_snapshot=snapshot)
                    self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                        validated_dcf_generation=generation if dcf else None))
                    self.assertIsNone(self.project(preimages))
                self.contract["dcf_generation"] = copy.deepcopy(generation)
                self.write("work.mjs", "export const answer = 2;\n")
                self.assert_delta_wire(self.project(preimages), schema_version=context.DELTA_SCHEMA_VERSION)

    def test_text_success_gate_does_not_read_for_unknown_or_incomplete_observations(self):
        preimages = self.fixture({"work.cjs": "module.exports = 1;\n"})
        self.write("work.cjs", "module.exports = 2;\n")
        observations = [None, [], {}, dict(self.success, success=False), dict(self.success, success=1),
                        dict(self.success, exit_code=1), dict(self.success, exit_code=True),
                        dict(self.success, outcome="unknown"), dict(self.success, process_reaped=False),
                        dict(self.success, process_group_empty=False)]
        for key in ("success", "exit_code", "outcome", "process_reaped", "process_group_empty"):
            missing = dict(self.success)
            del missing[key]
            observations.append(missing)
        for observation in observations:
            with self.subTest(observation=observation), \
                    patch.object(context.source_excerpt, "_read_exact") as reads:
                self.assertIsNone(context.project(self.workspace, self.contract, preimages,
                                                  observation, self.emitted))
                reads.assert_not_called()
        self.assertEqual(self.emitted, set())

    def test_text_capture_and_projection_fence_source_grant_binding_and_dcf_drift(self):
        before = "export const answer = 1;\n"
        reader = context.source_excerpt._read_exact
        for dcf in (False, True):
            preimages = self.fixture({"work.mjs": before}, dcf=dcf)
            original = copy.deepcopy(self.contract)
            for phase in ("capture", "project"):
                for drift in ("early_source", "final_source", "binding", "read_scope", "write_scope", "generation"):
                    with self.subTest(dcf=dcf, phase=phase, drift=drift):
                        self.contract = copy.deepcopy(original)
                        self.write("work.mjs", before if phase == "capture" else before.replace("1", "2"))
                        calls = 0
                        def changed(workspace, name, **kwargs):
                            nonlocal calls
                            calls += 1
                            if calls == 2 and drift == "final_source":
                                self.write(name, before.replace("1", "3"))
                            data = reader(workspace, name, **kwargs)
                            if calls == 1 and drift == "early_source":
                                self.write(name, before.replace("1", "3"))
                            if calls == 2:
                                if drift == "binding":
                                    self.contract["authorization_semantic_sha256"] = "b" * 64
                                elif drift == "read_scope":
                                    self.contract["read_scopes"] = []
                                elif drift == "write_scope":
                                    self.contract["write_scopes"] = []
                                elif drift == "generation":
                                    if dcf:
                                        self.contract["dcf_generation"]["action_freshness"]["source_fingerprints"]["surface"] = "changed"
                                    else:
                                        self.contract["dcf_generation"]["source_snapshot"][name]["sha256"] = "b" * 64
                            return data
                        with patch.object(context.source_excerpt, "_read_exact", side_effect=changed):
                            result = (context.capture_preimages(self.workspace, self.contract,
                                      validated_dcf_generation=original["dcf_generation"] if dcf else None)
                                      if phase == "capture" else self.project(preimages))
                        self.assertIsNone(result)
        self.assertEqual(self.emitted, set())

    def test_text_symlinks_missing_nonregular_and_denied_reads_omit_projection(self):
        for kind in ("file", "parent", "directory", "missing"):
            with self.subTest(kind=kind):
                name = kind + "/work.ts"
                preimages = self.fixture({name: "export const answer = 1;\n"})
                path = self.workspace / name
                if kind == "file":
                    target = path.with_name("real.ts")
                    path.rename(target)
                    path.symlink_to(target)
                elif kind == "parent":
                    target = self.workspace / "real_parent"
                    path.parent.rename(target)
                    (self.workspace / kind).symlink_to(target, target_is_directory=True)
                else:
                    path.unlink()
                    if kind == "directory":
                        path.mkdir()
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
                self.assertIsNone(self.project(preimages))
        preimages = self.fixture({"work.ts": "export const answer = 1;\n"})
        link = self.workspace / "workspace_link"
        link.symlink_to(self.workspace, target_is_directory=True)
        self.assertIsNone(context.capture_preimages(link, dict(self.contract, repo_root=str(link))))
        with patch.object(context.source_excerpt, "_read_exact", side_effect=PermissionError("denied")):
            self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
            self.assertIsNone(self.project(preimages))

    def test_text_binary_controls_nul_and_invalid_utf8_omit_the_entire_projection(self):
        for invalid in (b"\x00", b"const x = 1;\n\x00", b"\xff", b"\xc3\x28", b"\xef\xbb\xbf\x00",
                        b"const x = '\x01';\n", b"const x = '\x7f';\n", "const x = '\u0085';\n".encode()):
            for phase in ("capture", "project"):
                with self.subTest(invalid=invalid, phase=phase):
                    preimages = self.fixture({"a.py": "def answer():\n    return 1\n",
                                              "b.js": "const answer = 1;\n"})
                    if phase == "project":
                        self.write("a.py", "def answer():\n    return 2\n")
                    (self.workspace / "b.js").write_bytes(invalid)
                    if phase == "capture":
                        self.contract["dcf_generation"]["source_snapshot"]["b.js"]["sha256"] = hashlib.sha256(
                            invalid).hexdigest()
                    original = copy.deepcopy(self.success)
                    result = (context.capture_preimages(self.workspace, self.contract)
                              if phase == "capture" else self.project(preimages))
                    self.assertIsNone(result)
                    self.assertEqual(self.success, original)
                    self.assertEqual(self.emitted, set())

    def test_text_global_file_line_json_and_capture_budgets_are_exact_and_all_or_none(self):
        preimages = self.fixture({"a.ts": "export const answer = '\u00e9';\n", "b.rs": "pub const ANSWER: u32 = 1;\n"})
        for name, before in self.sources.items():
            self.write(name, before.replace("\u00e9", "\u96ea").replace("1", "2"))
        payload = self.project(preimages)
        rows = self.assert_delta_wire(payload, schema_version=context.DELTA_SCHEMA_VERSION)
        lines = sum(hunk["before_line_count"] + hunk["after_line_count"]
                    for row in rows for hunk in row["hunks"])
        size = len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("ascii"))
        current_size = sum((self.workspace / name).stat().st_size for name in self.sources)
        for constant, limit in (("MAX_FILES", 1), ("MAX_LINES", lines - 1),
                                ("MAX_JSON_BYTES", size - 1), ("MAX_CAPTURE_BYTES", current_size - 1)):
            with self.subTest(constant=constant), patch.object(context, constant, limit):
                self.assertIsNone(self.project(preimages))
                self.assertEqual(self.emitted, set())
        with patch.object(context, "MAX_FILES", 2), patch.object(context, "MAX_LINES", lines), \
                patch.object(context, "MAX_JSON_BYTES", size), patch.object(context, "MAX_CAPTURE_BYTES", current_size):
            self.assertEqual(self.project(preimages), payload)
        self.emitted.add(("a" * 64, rows[0]["path"], rows[0]["postimage_sha256"]))
        with patch.object(context, "MAX_FILES", 1):
            self.assertIsNone(self.project(preimages))  # Dedup does not reduce the changed-file budget.
        for name, before in self.sources.items():
            self.write(name, before)
        capture_size = sum(len(before.encode()) for before in self.sources.values())
        with patch.object(context, "MAX_CAPTURE_FILES", 2), patch.object(context, "MAX_CAPTURE_BYTES", capture_size):
            captured = context.capture_preimages(self.workspace, self.contract)
            self.assertIsNotNone(captured)
            self.assertEqual(captured.pins, preimages.pins)
        for constant, limit in (("MAX_CAPTURE_FILES", 1), ("MAX_CAPTURE_BYTES", capture_size - 1)):
            with self.subTest(constant=constant), patch.object(context, constant, limit):
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract))

    def test_text_exact_source_byte_boundary_and_one_byte_overflow(self):
        before = "export const answer = 1;\n"
        with patch.object(context.source_excerpt, "MAX_SOURCE_BYTES", len(before.encode())):
            preimages = self.fixture({"work.mjs": before})
            self.write("work.mjs", before.replace("1", "2"))
            self.assert_delta_wire(self.project(preimages), schema_version=context.DELTA_SCHEMA_VERSION)
            oversize = before + "\n"
            self.write("work.mjs", oversize)
            self.assertIsNone(self.project(preimages))
            self.contract["dcf_generation"]["source_snapshot"]["work.mjs"]["sha256"] = hashlib.sha256(
                oversize.encode()).hexdigest()
            self.assertIsNone(context.capture_preimages(self.workspace, self.contract))

    def test_text_adversarial_middle_work_overflow_is_optional_context_omission(self):
        repeated = "// repeated\n" * 2500
        before = "// old start\n" + repeated + "// old end\n"
        preimages = self.fixture({"work.js": before})
        self.write("work.js", "// new start\n" + repeated + "// new end\n")
        original = copy.deepcopy(self.success)
        with patch.object(context, "_project_delta_file") as delta:
            self.assertIsNone(self.project(preimages))
            delta.assert_not_called()
        self.assertEqual(self.success, original)
        self.assertEqual(self.emitted, set())

    def test_text_dedup_is_explicit_and_rechecks_skipped_source_identities(self):
        preimages = self.fixture({"a.mjs": "export const answer = 1;\n", "b.rs": "pub const ANSWER: u32 = 1;\n"})
        self.write("a.mjs", self.sources["a.mjs"].replace("1", "2"))
        payload = self.project(preimages)
        row, = self.assert_delta_wire(payload, schema_version=context.DELTA_SCHEMA_VERSION)
        self.assertEqual(self.project(preimages), payload)
        self.assertEqual(self.emitted, set())
        self.emitted.add(("a" * 64, row["path"], row["postimage_sha256"]))
        self.assertIsNone(self.project(preimages))
        self.write("b.rs", self.sources["b.rs"].replace("1", "2"))
        row, = self.assert_delta_wire(self.project(preimages), schema_version=context.DELTA_SCHEMA_VERSION)
        self.assertEqual(row["path"], "b.rs")
        self.emitted.add(("a" * 64, row["path"], row["postimage_sha256"]))
        self.write("a.mjs", self.sources["a.mjs"].replace("1", "3"))
        row, = self.assert_delta_wire(self.project(preimages), schema_version=context.DELTA_SCHEMA_VERSION)
        self.assertEqual(row["path"], "a.mjs")
        self.emitted.add(("a" * 64, row["path"], row["postimage_sha256"]))
        self.write("b.rs", self.sources["b.rs"].replace("1", "3"))
        reader = context.source_excerpt._read_exact
        def changed(workspace, name, **kwargs):
            data = reader(workspace, name, **kwargs)
            if name == "a.mjs":
                self.write(name, self.sources[name].replace("1", "4"))
            return data
        with patch.object(context.source_excerpt, "_read_exact", side_effect=changed):
            self.assertIsNone(self.project(preimages))
        (self.workspace / "a.mjs").unlink()
        self.assertIsNone(self.project(preimages))
        self.assertEqual(len(self.emitted), 3)

    def test_emitted_text_does_not_force_remaining_python_definitions_to_delta(self):
        preimages = self.fixture({"a.js": "const answer = 1;\n", "b.py": "def answer():\n    return 1\n"})
        for name, before in self.sources.items():
            self.write(name, before.replace("1", "2"))
        rows = self.assert_delta_wire(self.project(preimages), schema_version=context.DELTA_SCHEMA_VERSION)
        self.emitted.add(("a" * 64, rows[0]["path"], rows[0]["postimage_sha256"]))
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["path"], "b.py")

    def test_line_changes_match_only_the_exact_trimmed_middle(self):
        prefix = "header\r\n" + "    pass\r\n" * 1000
        suffix = "    pass\r\n" * 1000 + "unterminated tail"
        before, after = prefix + "old\r\n" + suffix, prefix + "new\r\n" + suffix
        with patch.object(context, "_BoundedMatcher", wraps=context._BoundedMatcher) as matcher:
            changes = context._line_changes(before, after)
        matcher.assert_called_once_with(a=["old\r\n"], b=["new\r\n"])
        self.assertEqual(changes.opcodes, [("equal", 0, 1001, 0, 1001),
                                          ("replace", 1001, 1002, 1001, 1002),
                                          ("equal", 1002, 2003, 1002, 2003)])
        self.assertEqual("".join(changes.old_lines), before)
        self.assertEqual("".join(changes.new_lines), after)

    def test_delta_fallback_reuses_one_opcode_calculation(self):
        before = "def answer():\n" + "    pass\n" * 1000 + "    return 1\n"
        after = before.replace("return 1", "return 2")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        matcher_type = context._BoundedMatcher
        calculate = matcher_type.get_opcodes
        with patch.object(context, "_line_changes", wraps=context._line_changes) as changes, \
                patch.object(context, "_BoundedMatcher", wraps=matcher_type) as matcher, \
                patch.object(matcher_type, "get_opcodes", autospec=True, side_effect=calculate) as opcodes:
            payload = self.project(preimages)
        self.assert_delta_wire(payload)
        changes.assert_called_once_with(before, after)
        matcher.assert_called_once_with(a=["    return 1\n"], b=["    return 2\n"])
        opcodes.assert_called_once()

    def test_50000_repeated_lines_yield_an_exact_compact_reconstructable_delta(self):
        count = 50_000
        before = "def answer():\r\n" + "    pass\r\n" * count + "    return 1"
        after = before[:-1] + "2"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        observation, contract = copy.deepcopy(self.success), copy.deepcopy(self.contract)
        with patch.object(context, "_BoundedMatcher", wraps=context._BoundedMatcher) as matcher:
            payload = self.project(preimages)
        matcher.assert_called_once_with(a=["    return 1"], b=["    return 2"])
        # An explicit independent oracle avoids a quadratic full-file matcher
        # in the regression's assertions; both directions are still rebuilt.
        row, = self.assert_delta_wire(payload, schema_version=context.DELTA_NAVIGATION_SCHEMA_VERSION,
                                     coordinates={"work.py": [(count - 1, 4, count - 1, 4)]})
        hunk, = row["hunks"]
        self.assertEqual(hunk["before_text"], "    pass\r\n" * 3 + "    return 1")
        self.assertEqual(hunk["after_text"], "    pass\r\n" * 3 + "    return 2")
        self.assertEqual(row["locators"], [{"qualified_name": "answer", "kind": "function",
                                          "line": 1, "start_line": 1, "end_line": count + 2}])
        self.assertEqual(self.success, observation)
        self.assertEqual(self.contract, contract)
        self.assertEqual(self.emitted, set())

    def test_adversarial_middle_work_overflow_omits_only_optional_context(self):
        before = "def answer():\n    before = 1\n" + "    pass\n" * 1000 + "    return 1\n"
        after = "def answer():\n    after = 2\n" + "    pass\n" * 1000 + "    return 2\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        observation, contract = copy.deepcopy(self.success), copy.deepcopy(self.contract)
        with patch.object(context, "MAX_MATCH_WORK", 64), \
                patch.object(context._BoundedMatcher, "find_longest_match") as matching, \
                patch.object(context, "_project_file") as definition, \
                patch.object(context, "_project_delta_file") as delta:
            self.assertIsNone(self.project(preimages))
        matching.assert_not_called()
        definition.assert_not_called()
        delta.assert_not_called()
        self.assertEqual(self.success, observation)
        self.assertEqual(self.contract, contract)
        self.assertEqual(self.emitted, set())
        self.assertEqual(context.source_excerpt._read_exact(self.workspace, "work.py"),
                         after.encode("utf-8"))

    def test_match_work_counts_subregions_extensions_and_out_of_range_candidates(self):
        a, b = ["X\n", "b\n", "a\n", "Y\n"], ["b\n", "Z\n", "a\n"]
        expected = difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()
        with patch.object(context, "MAX_MATCH_WORK", 10):
            matcher = context._BoundedMatcher(a=a, b=b)
            self.assertEqual(matcher.get_opcodes(), expected)
            self.assertEqual(matcher._remaining, 0)
        with patch.object(context, "MAX_MATCH_WORK", 9):
            with self.assertRaisesRegex(ValueError, "matching work budget exceeded"):
                context._BoundedMatcher(a=a, b=b).get_opcodes()
        a = b = ["A\n", "x\n", "A\n"]
        with patch.object(context, "MAX_MATCH_WORK", 3):
            matcher = context._BoundedMatcher(a=a, b=b)
            self.assertEqual(matcher.find_longest_match(2, 3, 2, 3), difflib.Match(2, 2, 1))
            self.assertEqual(matcher._remaining, 0)
        with patch.object(context, "MAX_MATCH_WORK", 2):
            with self.assertRaisesRegex(ValueError, "matching work budget exceeded"):
                context._BoundedMatcher(a=a, b=b).find_longest_match(2, 3, 2, 3)

    def test_line_changes_empty_overlap_insert_delete_and_exact_newline_boundaries(self):
        cases = (
            ("", "", []),
            ("x\r\n", "x\r\n", [("equal", 0, 1, 0, 1)]),
            ("", "x\r\n", [("insert", 0, 0, 0, 1)]),
            ("x\r\n", "", [("delete", 0, 1, 0, 0)]),
            ("x\n" * 3, "x\n" * 4, [("equal", 0, 3, 0, 3), ("insert", 3, 3, 3, 4)]),
            ("x\n" * 4, "x\n" * 3, [("equal", 0, 3, 0, 3), ("delete", 3, 4, 3, 3)]),
            ("a\r\nx\r\nz", "a\r\nz", [("equal", 0, 1, 0, 1), ("delete", 1, 2, 1, 1),
                                          ("equal", 2, 3, 1, 2)]),
            ("a\r\nz", "a\r\nx\r\nz", [("equal", 0, 1, 0, 1), ("insert", 1, 1, 1, 2),
                                          ("equal", 1, 2, 2, 3)]),
            ("a\rx\rz", "a\ry\rz", [("equal", 0, 1, 0, 1), ("replace", 1, 2, 1, 2),
                                    ("equal", 2, 3, 2, 3)]),
            ("head\r\nlast", "head\r\nlast\r\n", [("equal", 0, 1, 0, 1), ("replace", 1, 2, 1, 2)]),
            ("head\nlast\n", "head\nlast", [("equal", 0, 1, 0, 1), ("replace", 1, 2, 1, 2)]),
        )
        for before, after, expected in cases:
            with self.subTest(before=repr(before), after=repr(after)):
                changes = context._line_changes(before, after)
                self.assertEqual(changes.opcodes, expected)
                self.assertEqual("".join(changes.old_lines), before)
                self.assertEqual("".join(changes.new_lines), after)

    def test_cached_opcode_grouping_preserves_context_boundaries_and_ownership_input(self):
        for gap in (0, 1, 5, 6, 7, 8):
            with self.subTest(gap=gap):
                old = [f"line_{index}\n" for index in range(gap + 12)]
                new = old.copy()
                new[2], new[gap + 3] = "changed first\n", "changed second\n"
                changes = context._line_changes("".join(old), "".join(new))
                original = changes.opcodes.copy()
                expected = list(difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_grouped_opcodes(3))
                self.assertEqual(list(changes.grouped_opcodes(3)), expected)
                self.assertEqual(list(changes.grouped_opcodes(3)), expected)
                self.assertEqual(changes.opcodes, original)

    def test_changed_function_has_complete_decorated_target_and_deduplicated_support(self):
        before = ("import math\nBASE = 2\n\ndef decorate(fn):\n    return fn\n\n"
                  "def helper(value):\n    return math.floor(value) + BASE\n\n"
                  "@decorate\ndef answer(value):\n    return helper(value) + BASE\n\n"
                  "def unrelated():\n    return 'private unused text'\n")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("return helper(value)", "return helper(value * 2)"))
        observation, contract = copy.deepcopy(self.success), copy.deepcopy(self.contract)
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["locators"], [{"qualified_name": "answer", "kind": "function",
                                           "line": 11, "start_line": 10, "end_line": 12,
                                           "complete": True}])
        text = "".join(span["text"] for span in row["spans"])
        for support in ("import math", "BASE = 2", "def decorate", "def helper", "@decorate"):
            self.assertEqual(text.count(support), 1)
        self.assertNotIn("private unused text", text)
        self.assertEqual(self.success, observation)
        self.assertEqual(self.contract, contract)
        self.assertEqual(self.emitted, set())

    def test_class_method_and_nested_async_function_include_complete_lexical_support(self):
        cases = (
            ("FACTOR = 2\nclass Box:\n    SCALE = 3\n    def helper(self):\n"
             "        return FACTOR * self.SCALE\n    def answer(self):\n"
             "        return self.helper() + 1\n", "Box.answer", "function", "SCALE = 3"),
            ("FACTOR = 2\ndef outer():\n    local = FACTOR\n    async def answer():\n"
             "        return local + 1\n    return answer\n",
             "outer.answer", "async_function", "local = FACTOR"),
        )
        for before, name, kind, support in cases:
            with self.subTest(name=name):
                preimages = self.fixture({"work.py": before})
                self.write("work.py", before.replace("+ 1", "+ 2"))
                row, = self.assert_wire(self.project(preimages))
                self.assertEqual([(item["qualified_name"], item["kind"]) for item in row["locators"]],
                                 [(name, kind)])
                text = "".join(span["text"] for span in row["spans"])
                self.assertIn(support, text)
                self.assertIn("FACTOR = 2", text)
                self.assertEqual(text.count("def answer"), 1)

    def test_large_class_projects_complete_changed_methods_and_only_selected_syntactic_support(self):
        unused = "".join(f"    def unused_{index}(self):\n        return {index}\n"
                         for index in range(1450))
        before = ("UNUSED = 'unneeded global support'\nFACTOR = 2\n"
                  "def decorate(value):\n    return value\nclass Base:\n    pass\n"
                  "@decorate\nclass Box(\n    Base,\n):\n"
                  "    \"\"\"unchanged class context\"\"\"\n    SCALE = FACTOR\n"
                  "    def leaf(self):\n        return FACTOR * self.SCALE\n"
                  "    def helper(self):\n        return self.leaf()\n"
                  "    @decorate\n    def answer(self):\n        return self.helper() + 1\n"
                  "    async def second(self):\n        return Box.SCALE + 1\n"
                  "    def unused_global(self):\n        return UNUSED\n") + unused
        self.assertGreater(len(before.splitlines()), 2900)
        preimages = self.fixture({"work.py": before})
        after = before.replace("self.helper() + 1", "self.helper() + 2").replace(
            "Box.SCALE + 1", "Box.SCALE + 2")
        self.write("work.py", after)
        observation, contract = copy.deepcopy(self.success), copy.deepcopy(self.contract)
        row, = self.assert_wire(self.project(preimages))
        lines = after.splitlines()
        answer_line = lines.index("    def answer(self):") + 1
        second_line = lines.index("    async def second(self):") + 1
        self.assertEqual(row["locators"], [
            {"qualified_name": "Box.answer", "kind": "function", "line": answer_line,
             "start_line": answer_line - 1, "end_line": answer_line + 1, "complete": True},
            {"qualified_name": "Box.second", "kind": "async_function", "line": second_line,
             "start_line": second_line, "end_line": second_line + 1, "complete": True}])
        text = "".join(span["text"] for span in row["spans"])
        for support in ("@decorate\nclass Box(\n    Base,\n):\n", "class Base:\n    pass\n",
                        '    """unchanged class context"""\n', "    SCALE = FACTOR\n",
                        "    def helper(self):\n        return self.leaf()\n",
                        "    def leaf(self):\n        return FACTOR * self.SCALE\n",
                        "FACTOR = 2\n", "def decorate(value):\n    return value\n"):
            self.assertEqual(text.count(support), 1)
        self.assertNotIn("def unused_", text)
        self.assertNotIn("unneeded global support", text)
        self.assertLessEqual(sum(span["end_line"] - span["start_line"] + 1
                                 for span in row["spans"]), context.MAX_LINES)
        self.assertEqual(self.success, observation)
        self.assertEqual(self.contract, contract)
        self.assertEqual(self.emitted, set())

    def test_large_nested_class_uses_qualified_method_and_both_class_contexts(self):
        before = ("BASE = 2\nclass Outer:\n    SCALE = BASE\n    class Inner:\n"
                  "        LIMIT = BASE\n        def answer(self):\n"
                  "            return Outer.Inner.LIMIT + self.LIMIT + 1\n"
                  "        def unused(self):\n" + "            pass\n" * context.MAX_LINES)
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("self.LIMIT + 1", "self.LIMIT + 2"))
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["locators"], [{"qualified_name": "Outer.Inner.answer", "kind": "function",
                                           "line": 6, "start_line": 6, "end_line": 7,
                                           "complete": True}])
        text = "".join(span["text"] for span in row["spans"])
        for support in ("class Outer:", "class Inner:", "SCALE = BASE", "LIMIT = BASE", "BASE = 2"):
            self.assertIn(support, text)
        self.assertNotIn("def unused", text)

    def test_added_decorated_method_in_large_class_does_not_promote_blank_separator_to_class(self):
        before = ("class Box:\n    VALUE = 3\n    def unused(self):\n"
                  + "        pass\n" * context.MAX_LINES)
        preimages = self.fixture({"work.py": before})
        after = before + "\n    @staticmethod\n    def added():\n        return Box.VALUE\n"
        self.write("work.py", after)
        row, = self.assert_wire(self.project(preimages))
        line = after.splitlines().index("    def added():") + 1
        self.assertEqual(row["locators"], [{"qualified_name": "Box.added", "kind": "function",
                                           "line": line, "start_line": line - 1,
                                           "end_line": line + 1, "complete": True}])
        text = "".join(span["text"] for span in row["spans"])
        self.assertIn("    @staticmethod\n    def added():\n        return Box.VALUE\n", text)
        self.assertIn("class Box:\n    VALUE = 3\n", text)
        self.assertNotIn("def unused", text)

    def test_removed_or_renamed_methods_require_complete_class_or_complete_delta(self):
        removed = "    def old(self):\n        return 1\n\n"
        methods = removed + "    def keep(self):\n        return 2\n"
        large = "    def unused(self):\n" + "        pass\n" * context.MAX_LINES
        for padding in ("", large):
            before = "class Box:\n    VALUE = 1\n" + methods + padding
            for after in (before.replace(removed, ""), before.replace("def old(", "def renamed(")):
                with self.subTest(large=bool(padding), renamed="def renamed" in after):
                    preimages = self.fixture({"work.py": before})
                    after = after.replace("return 2", "return 3")
                    self.write("work.py", after)
                    payload = self.project(preimages)
                    if padding:
                        self.assert_delta_wire(payload)
                    else:
                        row, = self.assert_wire(payload)
                        self.assertEqual(row["locators"], [{"qualified_name": "Box", "kind": "class",
                                                           "line": 1, "start_line": 1,
                                                           "end_line": len(after.splitlines()),
                                                           "complete": True}])
                    self.assertEqual(self.emitted, set())

    def test_changed_class_binding_and_method_in_large_class_are_both_complete_targets(self):
        before = ("BASE = 2\nclass Box:\n    VALUE = BASE + 1\n"
                  "    def answer(self):\n        return self.VALUE + 1\n"
                  "    def unused(self):\n" + "        pass\n" * context.MAX_LINES)
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("BASE + 1", "BASE + 2").replace(
            "self.VALUE + 1", "self.VALUE + 2"))
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["locators"], [
            {"qualified_name": "Box.VALUE", "kind": "binding", "line": 3,
             "start_line": 3, "end_line": 3, "complete": True},
            {"qualified_name": "Box.answer", "kind": "function", "line": 4,
             "start_line": 4, "end_line": 5, "complete": True}])
        text = "".join(span["text"] for span in row["spans"])
        self.assertIn("BASE = 2", text)
        self.assertIn("VALUE = BASE + 2", text)
        self.assertNotIn("def unused", text)

    def test_large_class_header_decorator_or_binding_removal_uses_delta_not_partial_methods(self):
        before = ("def register(cls):\n    return cls\nclass Box:\n    VALUE = 1\n"
                  "    def answer(self):\n        return self.VALUE + 1\n"
                  "    def unused(self):\n" + "        pass\n" * context.MAX_LINES)
        changes = (before.replace("class Box:", "class Box(object):"),
                   before.replace("class Box:", "@register\nclass Box:"),
                   before.replace("    VALUE = 1\n", ""),
                   before.replace("VALUE", "RENAMED"))
        for after in changes:
            with self.subTest(after=after[:100]):
                preimages = self.fixture({"work.py": before})
                self.write("work.py", after.replace("+ 1", "+ 2"))
                self.assert_delta_wire(self.project(preimages))
                self.assertEqual(self.emitted, set())

    def test_class_blank_separator_handling_never_omits_changed_multiline_strings(self):
        padding = "    def unused(self):\n" + "        pass\n" * context.MAX_LINES
        for declaration in ('    TEXT = """first\n\nlast"""\n', '    """first\n\nlast"""\n'):
            with self.subTest(binding="TEXT" in declaration):
                before = "class Box:\n" + declaration + "    def answer(self):\n        return 1\n" + padding
                preimages = self.fixture({"work.py": before})
                after = before.replace("first\n\nlast", "first\n\n\nlast").replace("return 1", "return 2")
                self.write("work.py", after)
                payload = self.project(preimages)
                if "TEXT" in declaration:
                    row, = self.assert_wire(payload)
                    self.assertEqual([item["qualified_name"] for item in row["locators"]],
                                     ["Box.TEXT", "Box.answer"])
                    self.assertIn('TEXT = """first\n\n\nlast"""',
                                  "".join(span["text"] for span in row["spans"]))
                else:
                    # A changed docstring requires the whole class, which overflows.
                    self.assert_delta_wire(payload)

    def test_class_definition_overflow_uses_delta_unless_hunk_budget_also_overflows(self):
        cases = (
            ("binding lines", "    VALUES = (\n" + "        1,\n" * context.MAX_LINES + "    )\n", "", "self.VALUE"),
            ("binding bytes", "    TEXT = " + repr("x" * context.MAX_JSON_BYTES) + "\n", "", "self.VALUE"),
            ("sibling", "    def helper(self):\n" + "        pass\n" * context.MAX_LINES + "        return 1\n",
             "", "self.helper()"),
            ("target", "", "        pass\n" * context.MAX_LINES, "self.VALUE"),
        )
        for name, support, body, expression in cases:
            with self.subTest(name=name):
                before = ("class Box:\n    VALUE = 1\n" + support + "    def answer(self):\n" + body
                          + f"        return {expression} + 1\n")
                preimages = self.fixture({"a.py": "def answer():\n    return 1\n", "work.py": before})
                self.write("a.py", "def answer():\n    return 2\n")
                self.write("work.py", before.replace("+ 1", "+ 2"))
                payload = self.project(preimages)
                if name == "binding bytes":
                    # This wide unchanged line is within three lines of the change:
                    # both hunk sides overflow too, so no file may be emitted.
                    self.assertIsNone(payload)
                else:
                    self.assertEqual([row["path"] for row in self.assert_delta_wire(payload)],
                                     ["a.py", "work.py"])
                self.assertEqual(self.emitted, set())

    def test_changed_class_header_is_a_complete_class(self):
        before = "class Box:\n    VALUE = 1\n    def answer(self):\n        return self.VALUE\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("class Box:", "class Box(object):"))
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["locators"], [{"qualified_name": "Box", "kind": "class", "line": 1,
                                           "start_line": 1, "end_line": 4, "complete": True}])

    def test_module_class_import_and_multi_name_bindings(self):
        cases = (
            ("BASE = 2\nLEFT, RIGHT = (BASE, BASE + 1)\n", "+ 1", "+ 3", ["LEFT", "RIGHT"]),
            ("BASE = 2\nLOW = HIGH = BASE + 1\n", "+ 1", "+ 3", ["HIGH", "LOW"]),
            ("VALUE: int = 1\n", "= 1", "= 3", ["VALUE"]),
            ("import math as m\n", "import math as m", "from math import ceil as m", ["m"]),
            ("BASE = 2\nclass Box:\n    VALUE = BASE + 1\n", "+ 1", "+ 3", ["Box.VALUE"]),
        )
        for before, old, new, names in cases:
            with self.subTest(names=names):
                preimages = self.fixture({"work.py": before})
                self.write("work.py", before.replace(old, new))
                row, = self.assert_wire(self.project(preimages))
                self.assertEqual([item["qualified_name"] for item in row["locators"]], names)
                self.assertTrue(all(item["kind"] == "binding" for item in row["locators"]))
                if "BASE" in before:
                    self.assertIn("BASE = 2", "".join(span["text"] for span in row["spans"]))

    def test_added_definition_and_removed_nested_definition_have_surviving_complete_owners(self):
        before = "def outer():\n    def removed():\n        return 1\n    return 1\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", "def outer():\n    return 2\n\ndef added():\n    return outer()\n")
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual([item["qualified_name"] for item in row["locators"]], ["outer", "added"])
        self.assertEqual("".join(span["text"] for span in row["spans"]).count("def outer"), 1)

    def test_physical_crlf_and_unicode_are_preserved(self):
        before = "def answer():\r\n    return '\u00e9'\r\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("\u00e9", "\u00e8"))
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["spans"][0]["text"], before.replace("\u00e9", "\u00e8"))

    def test_unchanged_or_unavailable_preimage_has_no_context(self):
        preimages = self.fixture()
        self.assertIsNone(self.project(preimages))
        self.write("work.py", "def answer():\n    return 2\n")
        self.assertIsNone(self.project(None))

    def test_empty_separators_do_not_hide_changed_blank_lines_inside_string_bindings(self):
        before = 'TEXT = """first\n\nlast"""\n\ndef answer():\n    return TEXT\n'
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("first\n\n", "first\n\n\n").replace(
            "return TEXT", 'return TEXT + "!"'))
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual([item["qualified_name"] for item in row["locators"]], ["TEXT", "answer"])
        self.assertEqual("".join(span["text"] for span in row["spans"]).count('TEXT = """'), 1)
        preimages = self.fixture()
        self.write("work.py", "\n" + self.sources["work.py"] + "\n")
        self.assertIsNone(self.project(preimages))

    def test_shared_physical_lines_and_unowned_multiline_strings_fail_closed(self):
        before = "LEFT = 1; RIGHT = 2\n\ndef answer():\n    return LEFT + RIGHT\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("LEFT = 1", "LEFT = 3"))
        self.assertIsNone(self.project(preimages))
        before = '"""first\n\nlast"""\n\ndef answer():\n    return 1\n'
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("first\n\n", "first\n\n\n").replace("return 1", "return 2"))
        self.assertIsNone(self.project(preimages))
        preimages = self.fixture({"work.py": "class Box: VALUE = 1\n"})
        self.write("work.py", "class Box: VALUE = 2\n")
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual([(item["qualified_name"], item["kind"]) for item in row["locators"]],
                         [("Box", "class")])

    def test_enclosing_support_is_not_truncated_to_make_a_nested_target_fit(self):
        before = "def outer():\n    local = 1\n    def answer():\n        return local + 1\n    return answer\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("local + 1", "local + 2"))
        with patch.object(context, "MAX_LINES", 2):
            self.assertIsNone(self.project(preimages))
        row, = self.assert_wire(self.project(preimages))
        self.assertIn("local = 1", "".join(span["text"] for span in row["spans"]))

    def test_incomplete_unsupported_or_ambiguous_changes_fail_closed(self):
        before = "def answer():\n    return 1\n\ndef keep():\n    return 2\n"
        for after in ("def answer(\n", "def keep():\n    return 2\n",
                      "# changed outside any unit\n" + before,
                      before + "def answer():\n    return 3\n"):
            with self.subTest(after=after):
                preimages = self.fixture({"work.py": before})
                self.write("work.py", after)
                self.assertIsNone(self.project(preimages))
                self.assertEqual(self.emitted, set())
        preimages = self.fixture()
        (self.workspace / "work.py").write_bytes(b"\xff")
        self.assertIsNone(self.project(preimages))

    def test_multiple_files_are_projected_together_or_not_at_all(self):
        preimages = self.fixture({name: "def answer():\n    return 1\n" for name in ("a.py", "b.py")})
        for name in self.sources:
            self.write(name, "def answer():\n    return 2\n")
        payload = self.project(preimages)
        self.assertEqual([row["path"] for row in self.assert_wire(payload)], ["a.py", "b.py"])
        self.write("b.py", "def broken(\n")
        self.assertIsNone(self.project(preimages))
        self.assertEqual(self.emitted, set())

    def test_all_or_none_file_line_byte_and_capture_budgets(self):
        preimages = self.fixture({name: "def answer():\n    return 1\n" for name in ("a.py", "b.py")})
        for name in self.sources:
            self.write(name, "def answer():\n    return '\u00e9'\n")
        payload = self.project(preimages)
        rows = self.assert_wire(payload)
        lines = sum(span["end_line"] - span["start_line"] + 1 for row in rows for span in row["spans"])
        size = len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("ascii"))
        capture_size = sum((self.workspace / name).stat().st_size for name in self.sources)
        # Both delta sides and its metadata exceed these tiny definition budgets too.
        for constant, limit in (("MAX_FILES", 1), ("MAX_LINES", lines-1),
                                ("MAX_JSON_BYTES", size-1), ("MAX_CAPTURE_BYTES", capture_size-1)):
            with self.subTest(constant=constant), patch.object(context, constant, limit):
                self.assertIsNone(self.project(preimages))
                self.assertEqual(self.emitted, set())
        with patch.object(context, "MAX_FILES", 2), patch.object(context, "MAX_LINES", lines), \
                patch.object(context, "MAX_JSON_BYTES", size):
            self.assertEqual(self.project(preimages), payload)

    def test_long_changed_definitions_emit_all_text_changes_without_mutating_verifier_facts(self):
        before = "".join(f"def task_{index}():\n" + "    pass\n" * 50
                         + f"    return {index}\n\n" for index in range(14))
        after = "\n" + "".join(f"def task_{index}():\n" + "    pass\n" * 50
                                 + f"    return {index + 100}\n\n" for index in range(14)) + "\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        definition = context._project_file("work.py", hashlib.sha256(after.encode("utf-8")).hexdigest(),
                                           preimages.pins["work.py"], preimages.sources["work.py"],
                                            context._line_changes(before, after), context.ast.parse(after))
        self.assertEqual(len(definition["locators"]), 14)
        self.assertGreater(sum(span["end_line"] - span["start_line"] + 1
                               for span in definition["spans"]), context.MAX_LINES)
        observation, contract = copy.deepcopy(self.success), copy.deepcopy(self.contract)
        payload = self.project(preimages)
        row, = self.assert_delta_wire(payload)
        self.assertGreaterEqual(len(row["hunks"]), 14)
        self.assertEqual(self.project(preimages), payload)
        self.assertEqual(self.success, observation)
        self.assertEqual(self.contract, contract)
        self.assertEqual(self.emitted, set())

    def test_definition_byte_overflow_uses_delta_without_large_distant_support(self):
        before = "UNUSED = " + repr("x" * context.MAX_JSON_BYTES) + "\ndef answer():\n" + "    pass\n" * 8
        before += "    return UNUSED + 'old'\n"
        after = before.replace("+ 'old'", "+ 'new'")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        definition = context._project_file("work.py", hashlib.sha256(after.encode("utf-8")).hexdigest(),
                                           preimages.pins["work.py"], preimages.sources["work.py"],
                                            context._line_changes(before, after), context.ast.parse(after))
        self.assertLessEqual(sum(span["end_line"] - span["start_line"] + 1
                                 for span in definition["spans"]), context.MAX_LINES)
        self.assertGreater(len(json.dumps(definition, sort_keys=True, ensure_ascii=True).encode("ascii")),
                           context.MAX_JSON_BYTES)
        row, = self.assert_delta_wire(self.project(preimages))
        self.assertNotIn("UNUSED =", "".join(hunk["after_text"] for hunk in row["hunks"]))

    def test_delta_navigation_lists_changed_and_wholly_contained_support_with_fresh_offsets(self):
        before = ("BASE = ALIAS = 2\ndef decorate(fn):\n    return fn\ndef outer():\n"
                  "    class Helper:\n        VALUE = BASE\n        def value(self):\n"
                  "            return self.VALUE\n    @decorate\n    async def answer():\n"
                  + "        pass\n" * context.MAX_LINES
                  + "        return Helper().value() + ALIAS + 1\n    return answer\n")
        after = "\n" + before.replace("ALIAS + 1", "ALIAS + 2")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        payload = self.project(preimages)
        row, = self.assert_delta_wire(payload, schema_version="nokiy_source_postimage_delta_v2")
        end = len(context._lines(after))
        self.assertEqual(row["locators"], [
            {"qualified_name": "decorate", "kind": "function", "line": 3, "start_line": 3, "end_line": 4},
            {"qualified_name": "outer", "kind": "function", "line": 5, "start_line": 5, "end_line": end},
            {"qualified_name": "outer.answer", "kind": "async_function", "line": 11,
             "start_line": 10, "end_line": end - 1}])
        self.assertEqual(row["hunks"][-1]["after_start_line"], row["hunks"][-1]["before_start_line"] + 1)
        legacy, navigation = self.delta_v1_and_navigation(preimages)
        targets, support, units = navigation["work.py"]
        shuffled = {"work.py": (list(reversed(targets)), support, dict(reversed(list(units.items()))))}
        self.assertEqual(context._with_delta_navigation(legacy, shuffled), payload)

    def test_delta_navigation_does_not_promote_adjacent_partial_class_support(self):
        before = ("class Box:\n    VALUE = 1\n    def helper(self):\n"
                  + "        pass\n" * context.MAX_LINES
                  + "        return self.VALUE\n    def answer(self):\n        return self.helper() + 1\n")
        after = before.replace("self.helper() + 1", "self.helper() + 2")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        _, navigation = self.delta_v1_and_navigation(preimages)
        targets, support, units = navigation["work.py"]
        owner = next(unit for unit in units.values() if unit.names == ("Box",))
        self.assertFalse(any(start <= owner.start and owner.end <= end for start, end in support))
        self.assertEqual(context._merge(support + [(unit.start, unit.end) for unit in targets],
                                        len(context._lines(after))), [(owner.start, owner.end)])
        row, = self.assert_delta_wire(self.project(preimages), schema_version="nokiy_source_postimage_delta_v2")
        helper_end = context.MAX_LINES + 4
        self.assertEqual(row["locators"], [
            {"qualified_name": "Box.helper", "kind": "function", "line": 3,
             "start_line": 3, "end_line": helper_end},
            {"qualified_name": "Box.answer", "kind": "function", "line": helper_end + 1,
             "start_line": helper_end + 1, "end_line": helper_end + 2}])

    def test_delta_navigation_invalid_names_kinds_and_coordinates_fall_back_atomically(self):
        before = "\ndef answer():\n" + "    pass\n" * context.MAX_LINES + "    return 1\n"
        preimages = self.fixture({"a.py": before, "b.py": before})
        for name in self.sources:
            self.write(name, before.replace("return 1", "return 2"))
        legacy, navigation = self.delta_v1_and_navigation(preimages)
        snapshot = copy.deepcopy(legacy)
        unit, = navigation["b.py"][0]
        bad_units = [replace(unit, names=(name,)) for name in (
            "", ".answer", "answer.", "answer..part", "bad\nname", "bad\x7fname", "bad\u0085name",
            "\ud800", "x" * 513, 7)]
        bad_units.extend((replace(unit, kind="module"), replace(unit, start=0), replace(unit, start=True),
                          replace(unit, start=2.0), replace(unit, end=True), replace(unit, end=0),
                          replace(unit, start=unit.node.lineno + 1), replace(unit, end=unit.node.lineno - 1)))
        for line in (0, True, unit.end + 1):
            node = copy.copy(unit.node)
            node.lineno = line
            bad_units.append(replace(unit, node=node))
        for index, bad in enumerate(bad_units):
            with self.subTest(invalid_unit=index):
                broken = {**navigation, "b.py": ([bad], [], {})}
                self.assertIs(context._with_delta_navigation(legacy, broken), legacy)
        self.assertIs(context._with_delta_navigation(legacy, {"a.py": navigation["a.py"]}), legacy)
        conflicting = {**navigation, "b.py": ([unit, replace(unit, end=unit.end - 1)], [], {})}
        self.assertIs(context._with_delta_navigation(legacy, conflicting), legacy)
        duplicated = {**navigation, "b.py": ([unit, unit], [(unit.start, unit.end)], {unit.key: unit})}
        self.assertEqual(context._with_delta_navigation(legacy, duplicated), self.project(preimages))
        self.assertEqual(legacy, snapshot)
        self.assertEqual(self.emitted, set())

    def test_delta_navigation_utf8_name_byte_boundary_and_one_byte_overflow(self):
        self.assertEqual(context.MAX_DELTA_NAME_BYTES, 512)
        for base in ("x" * 512, "\u00e9" * 256):
            for suffix in ("", "x"):
                with self.subTest(unicode=base.startswith("\u00e9"), overflow=bool(suffix)):
                    name = base + suffix
                    self.assertEqual(len(name.encode("utf-8")), 512 + len(suffix))
                    before = f"def {name}():\n" + "    pass\n" * context.MAX_LINES + "    return 1\n"
                    preimages = self.fixture({"work.py": before})
                    self.write("work.py", before.replace("return 1", "return 2"))
                    version = "nokiy_source_postimage_delta_v1" if suffix else "nokiy_source_postimage_delta_v2"
                    row, = self.assert_delta_wire(self.project(preimages), schema_version=version)
                    if not suffix:
                        locator, = row["locators"]
                        self.assertEqual(locator["qualified_name"], name)
        self.assertEqual(self.emitted, set())

    def test_delta_navigation_64_locator_limit_is_global_and_falls_back_all_files(self):
        self.assertEqual(context.MAX_DELTA_LOCATORS, 64)
        before = "def support():\n" + "    pass\n" * context.MAX_LINES + "    return 0\n\n"
        before += "".join(f"def task_{index}():\n    return support() + 1\n\n" for index in range(31))
        after = before.replace("support() + 1", "support() + 2")
        preimages = self.fixture({"a.py": before, "b.py": before})
        for name in self.sources:
            self.write(name, after)
        rows = self.assert_delta_wire(self.project(preimages), schema_version="nokiy_source_postimage_delta_v2")
        self.assertEqual([len(row["locators"]) for row in rows], [32, 32])
        legacy, _ = self.delta_v1_and_navigation(preimages)
        with patch.object(context, "MAX_DELTA_LOCATORS", 63):
            self.assertEqual(self.project(preimages), legacy)
        self.write("b.py", after + "def task_31():\n    return support() + 2\n")
        self.assert_delta_wire(self.project(preimages), schema_version="nokiy_source_postimage_delta_v1")
        self.assertEqual(self.emitted, set())

    def test_delta_navigation_combined_json_budget_upgrades_or_falls_back_without_changing_hunks(self):
        self.assertEqual(context.MAX_JSON_BYTES, 32768)
        before = "def answer():\n" + "    pass\n" * context.MAX_LINES + "    return '\u00e9'\n"
        after = before.replace("return '\u00e9'", "return '\u96ea'")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        legacy, _ = self.delta_v1_and_navigation(preimages)
        payload = self.project(preimages)
        self.assert_delta_wire(payload, schema_version="nokiy_source_postimage_delta_v2")
        size = len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("ascii"))
        legacy_size = len(json.dumps(legacy, sort_keys=True, ensure_ascii=True).encode("ascii"))
        self.assertGreater(size, legacy_size)
        with patch.object(context, "MAX_JSON_BYTES", size):
            self.assertEqual(self.project(preimages), payload)
        for limit in (size - 1, legacy_size):
            with self.subTest(limit=limit), patch.object(context, "MAX_JSON_BYTES", limit):
                self.assertEqual(self.project(preimages), legacy)
        with patch.object(context, "MAX_JSON_BYTES", legacy_size - 1):
            self.assertIsNone(self.project(preimages))
        padding = "x" * (context.MAX_JSON_BYTES - size)
        self.write("work.py", after.replace("return '\u96ea'", "return '" + padding + "\u96ea'"))
        exact = self.project(preimages)
        self.assert_delta_wire(exact, schema_version="nokiy_source_postimage_delta_v2")
        self.assertEqual(len(json.dumps(exact, sort_keys=True, ensure_ascii=True).encode("ascii")), 32768)
        self.write("work.py", after.replace("return '\u96ea'", "return '" + padding + "x\u96ea'"))
        fallback, _ = self.delta_v1_and_navigation(preimages)
        self.assertEqual(self.project(preimages), fallback)
        self.assertEqual(self.emitted, set())

    def test_delta_notice_explains_pinned_excerpt_reuse_without_supplying_missing_context(self):
        before = "def answer():\n" + "".join(f"    step_{index} = {index}\n" for index in range(610))
        before += "    return step_609\n"
        after = before.replace("    step_10 = 10\n", "    inserted = -1\n    step_10 = 10\n")
        after = after.replace("    step_30 = 30\n", "")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        payload = self.project(preimages)
        row, = self.assert_delta_wire(payload)
        self.assertIn("after_text is postimage source at after_start_line; before_text is old source.",
                      payload["notice"])
        self.assertIn("For each listed path, previously supplied excerpts matching preimage_sha256 "
                      "can be combined with these hunks for postimage_sha256", payload["notice"])
        self.assertIn("text outside hunks is unchanged, but line offsets may shift", payload["notice"])
        self.assertIn("Unseen surrounding semantics are not supplied.", payload["notice"])
        self.assertIn("Missing or newer evidence requires ordinary granted source_read.", payload["notice"])
        # An already-supplied preimage excerpt is reusable at its shifted location,
        # but the delta does not supply this or another unseen surrounding line.
        old_lines, new_lines = context._lines(before), context._lines(after)
        known_line = 22  # step_20, between the insertion and deletion hunks.
        self.assertFalse(any(hunk["before_start_line"] <= known_line
                             < hunk["before_start_line"] + hunk["before_line_count"]
                             for hunk in row["hunks"]))
        offset = sum(hunk["after_line_count"] - hunk["before_line_count"]
                     for hunk in row["hunks"]
                     if hunk["before_start_line"] + hunk["before_line_count"] <= known_line)
        self.assertEqual(offset, 1)
        self.assertEqual(new_lines[known_line - 1 + offset], old_lines[known_line - 1])
        self.assertEqual(sum(hunk["after_line_count"] - hunk["before_line_count"]
                             for hunk in row["hunks"]), 0)
        delivered = "".join(hunk["after_text"] for hunk in row["hunks"])
        self.assertNotIn(old_lines[known_line - 1], delivered)
        self.assertNotIn("    step_40 = 40\n", delivered)

    def test_delta_insert_delete_replace_preserves_crlf_unicode_and_unterminated_lines(self):
        for newline, final in (("\n", True), ("\r\n", False), ("\r", False), ("\n", False)):
            with self.subTest(newline=repr(newline), final=final):
                before = "def answer():\n" + "".join(f"    step_{index} = {index}\n" for index in range(610))
                before += "    return '\u00e9'" + ("\n" if final else "")
                after = before.replace("    step_10 = 10\n", "    added = '\u96ea\u2028'\n    step_10 = 10\n")
                after = after.replace("    step_30 = 30\n", "").replace("    step_50 = 50\n", "    step_50 = '\u00e8'\n")
                after = after.replace("return '\u00e9'", "return '\u6771\u4eac'")
                before, after = before.replace("\n", newline), after.replace("\n", newline)
                preimages = self.fixture({"work.py": before})
                self.write("work.py", after)
                row, = self.assert_delta_wire(self.project(preimages))
                self.assertEqual([hunk["after_line_count"] - hunk["before_line_count"]
                                  for hunk in row["hunks"]], [1, -1, 0, 0])
                self.assertEqual(row["hunks"][-1]["after_text"].endswith(newline), final)

    def test_delta_hunk_empty_sides_have_one_based_starts_and_zero_counts(self):
        # Exercise the textual formatter alone; top-level deletion stays ineligible
        # for production fallback even though its textual representation is exact.
        for before, after, counts in (("", "VALUE = '\u00e9'\r\n", (0, 1)),
                                      ("VALUE = '\u00e9'\r\n", "", (1, 0)),
                                      ("VALUE = 1", "VALUE = 2", (1, 1)),
                                      ("VALUE = '\u00e9'\r\n", "VALUE = '\u00e9'", (1, 1)),
                                      ("VALUE = '\u00e9'", "VALUE = '\u00e9'\r\n", (1, 1))):
            with self.subTest(counts=counts):
                preimages = self.fixture({"work.py": before})
                self.write("work.py", after)
                row = context._project_delta_file("work.py", hashlib.sha256(after.encode("utf-8")).hexdigest(),
                                                   preimages.pins["work.py"], context._line_changes(before, after))
                self.assert_delta_file(row)
                hunk, = row["hunks"]
                self.assertEqual((hunk["before_start_line"], hunk["after_start_line"]), (1, 1))
                self.assertEqual((hunk["before_line_count"], hunk["after_line_count"]), counts)

    def test_delta_600_line_boundary_counts_both_sides_and_never_truncates(self):
        self.assertEqual(context.MAX_LINES, 600)
        before = "def answer():\n" + "".join(f"    step_{index} = {index}\n" for index in range(610))
        before += "    return 1\n"
        after = "def answer():\n" + "".join(f"    step_{index} = {index + 1000 if 10 <= index < 304 else index}\n"
                                           for index in range(610)) + "    return 1\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        payload = self.project(preimages)
        row, = self.assert_delta_wire(payload)
        self.assertEqual(sum(hunk["before_line_count"] + hunk["after_line_count"]
                             for hunk in row["hunks"]), 600)
        with patch.object(context, "MAX_LINES", 599):
            self.assertIsNone(self.project(preimages))
        after = after.replace("    step_304 = 304\n", "    step_304 = 1304\n")
        self.write("work.py", after)
        self.assertIsNone(self.project(preimages))
        self.assertEqual(self.emitted, set())

    def test_delta_exact_ascii_envelope_byte_boundary_and_one_byte_overflow(self):
        self.assertEqual(context.MAX_JSON_BYTES, 32768)
        before = "def answer():\n" + "    pass\n" * context.MAX_LINES + "    return '\u00e9'\n"
        after = before.replace("return '\u00e9'", "return '\u96ea'")
        preimages = self.fixture({"work.py": before})
        self.write("work.py", after)
        payload, _ = self.delta_v1_and_navigation(preimages)
        row, = self.assert_delta_wire(payload)
        line_count = sum(hunk["before_line_count"] + hunk["after_line_count"] for hunk in row["hunks"])
        size = len(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("ascii"))
        with patch.object(context, "MAX_LINES", line_count), patch.object(context, "MAX_JSON_BYTES", size):
            self.assertEqual(self.project(preimages), payload)
        with patch.object(context, "MAX_JSON_BYTES", size - 1):
            self.assertIsNone(self.project(preimages))
        padding = "x" * (context.MAX_JSON_BYTES - size)
        self.write("work.py", after.replace("return '\u96ea'", "return '" + padding + "\u96ea'"))
        exact = self.project(preimages)
        self.assert_delta_wire(exact)
        self.assertEqual(len(json.dumps(exact, sort_keys=True, ensure_ascii=True).encode("ascii")), 32768)
        self.write("work.py", after.replace("return '\u96ea'", "return '" + padding + "x\u96ea'"))
        self.assertIsNone(self.project(preimages))
        self.assertEqual(self.emitted, set())

    def test_delta_multiple_files_and_budgets_are_all_or_none(self):
        before = "def answer():\n" + "    pass\n" * context.MAX_LINES + "    return 1\n"
        preimages = self.fixture({"a.py": before, "b.py": "def answer():\n    return 1\n"})
        for name, source in self.sources.items():
            self.write(name, source.replace("return 1", "return 2"))
        payload = self.project(preimages)
        rows = self.assert_delta_wire(payload)
        self.assertEqual([row["path"] for row in rows], ["a.py", "b.py"])
        line_count = sum(hunk["before_line_count"] + hunk["after_line_count"]
                         for row in rows for hunk in row["hunks"])
        legacy, _ = self.delta_v1_and_navigation(preimages)
        size = len(json.dumps(legacy, sort_keys=True, ensure_ascii=True).encode("ascii"))
        capture_size = sum((self.workspace / name).stat().st_size for name in self.sources)
        for constant, limit in (("MAX_FILES", 1), ("MAX_LINES", line_count - 1),
                                ("MAX_JSON_BYTES", size - 1), ("MAX_CAPTURE_BYTES", capture_size - 1)):
            with self.subTest(constant=constant), patch.object(context, constant, limit):
                self.assertIsNone(self.project(preimages))
                self.assertEqual(self.emitted, set())
        self.emitted.add(("a" * 64, "a.py", rows[0]["postimage_sha256"]))
        row, = self.assert_wire(self.project(preimages))  # Remaining definitions fit again.
        self.assertEqual(row["path"], "b.py")
        (self.workspace / "a.py").unlink()  # Even deduplicated sources must be rechecked.
        self.assertIsNone(self.project(preimages))
        self.assertEqual(len(self.emitted), 1)

    def test_definition_overflow_never_masks_later_unsupported_changes_or_arbitrary_errors(self):
        large = "def large():\n" + "    pass\n" * context.MAX_LINES + "    return 1\n"
        before = "LEFT = 1; RIGHT = 2\ndef answer():\n    return 1\n\ndef keep():\n    return 2\n"
        bad = ("def answer(\n", "LEFT = 1; RIGHT = 2\ndef keep():\n    return 2\n",
               "# changed outside any unit\n" + before, before + "def answer():\n    return 3\n",
               before.replace("LEFT = 1", "LEFT = 3"), "\n" + before + "\n")
        for after in bad:
            with self.subTest(after=after), patch.object(context, "_project_delta_file") as delta:
                preimages = self.fixture({"a.py": large, "b.py": before})
                self.write("a.py", large.replace("return 1", "return 2"))
                self.write("b.py", after)
                self.assertIsNone(self.project(preimages))
                delta.assert_not_called()
        preimages = self.fixture({"a.py": large, "b.py": before})
        self.write("a.py", large.replace("return 1", "return 2"))
        self.write("b.py", before.replace("return 1", "return 3"))
        for error in (ValueError("ambiguous projection"), RuntimeError("unexpected projection failure")):
            with self.subTest(error=error), patch.object(context, "_project_file", side_effect=error), \
                    patch.object(context, "_project_delta_file") as delta:
                self.assertIsNone(self.project(preimages))
                delta.assert_not_called()
        (self.workspace / "b.py").write_bytes(b"\xff")
        with patch.object(context, "_project_delta_file") as delta:
            self.assertIsNone(self.project(preimages))
            delta.assert_not_called()
        self.assertEqual(self.emitted, set())

    def test_delta_dedup_requires_explicitly_recorded_successful_return_identity(self):
        before = "def answer():\n" + "    pass\n" * context.MAX_LINES + "    return 1\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("return 1", "return 2"))
        payload = self.project(preimages)
        row, = self.assert_delta_wire(payload)
        self.assertEqual(self.project(preimages), payload)
        self.assertEqual(self.emitted, set())
        self.emitted.add((payload["jspace_semantic_sha256"], row["path"], row["postimage_sha256"]))
        self.assertIsNone(self.project(preimages))
        self.write("work.py", before.replace("return 1", "return 3"))
        next_row, = self.assert_delta_wire(self.project(preimages))
        self.assertNotEqual(next_row["postimage_sha256"], row["postimage_sha256"])
        self.assertEqual(len(self.emitted), 1)

    def test_delta_does_not_relax_success_grants_or_final_identity_and_binding_checks(self):
        before = "def answer():\n" + "    pass\n" * context.MAX_LINES + "    return 1\n"
        preimages = self.fixture({"work.py": before})
        self.write("work.py", before.replace("return 1", "return 2"))
        original = copy.deepcopy(self.contract)
        for change in ({"denied_operations": ["read"]}, {"denied_operations": ["modify"]},
                       {"denied_operations": ["command"]}, {"source_read": False}, {"read_scopes": []},
                       {"write_scopes": []}, {"authorization_semantic_sha256": "b" * 64}):
            with self.subTest(change=change), patch.object(context, "_project_delta_file") as delta:
                self.contract = dict(original, **change)
                self.assertIsNone(self.project(preimages))
                delta.assert_not_called()
        self.contract = copy.deepcopy(original)
        for change in ({"success": False}, {"exit_code": 1}, {"outcome": "unknown"},
                       {"process_reaped": False}, {"process_group_empty": False}):
            with self.subTest(change=change), patch.object(context.source_excerpt, "_read_exact") as reads:
                self.assertIsNone(self.project(preimages, dict(self.success, **change)))
                reads.assert_not_called()
        checker = context.source_excerpt.still_current
        for drift in ("source", "binding", "scope"):
            with self.subTest(drift=drift):
                self.contract = copy.deepcopy(original)
                self.write("work.py", before.replace("return 1", "return 2"))
                def changed(workspace, excerpt):
                    if drift == "source":
                        self.write("work.py", before.replace("return 1", "return 3"))
                    current = checker(workspace, excerpt)
                    if drift == "binding":
                        self.contract["authorization_semantic_sha256"] = "b" * 64
                    elif drift == "scope":
                        self.contract["read_scopes"] = []
                    return current
                with patch.object(context.source_excerpt, "still_current", side_effect=changed):
                    self.assertIsNone(self.project(preimages))
        self.assertEqual(self.emitted, set())

    def test_dcf_without_snapshot_captures_fresh_bytes_and_projects_definitions_or_delta(self):
        for padding in (0, context.MAX_LINES):
            with self.subTest(padding=padding):
                before = "def answer():\n" + "    pass\n" * padding + "    return 1\n"
                preimages = self.fixture({"core.py": before}, dcf=True)
                original = copy.deepcopy(self.contract)
                self.assertNotIn("source_snapshot", self.contract["dcf_generation"])
                self.assertEqual(preimages.dcf_generation, self.contract["dcf_generation"])
                self.assertIsNot(preimages.dcf_generation, self.contract["dcf_generation"])
                self.assertEqual(preimages.pins, {"core.py": hashlib.sha256(before.encode()).hexdigest()})
                self.assertIsNone(self.project(preimages))
                self.write("core.py", before.replace("return 1", "return 2"))
                observation = copy.deepcopy(self.success)
                payload = self.project(preimages)
                row, = (self.assert_delta_wire(payload) if padding else self.assert_wire(payload))
                self.assertEqual(row["path"], "core.py")
                self.assertEqual(self.contract, original)
                self.assertEqual(self.success, observation)
                self.assertEqual(self.emitted, set())

    def test_dcf_absence_of_snapshot_is_not_itself_validation_or_an_extra_permission(self):
        self.fixture(dcf=True)
        original = copy.deepcopy(self.contract)
        with patch.object(context.source_excerpt, "_read_exact") as reads:
            for marker in (None, True, {}, dict(original["dcf_generation"], generation_id="other")):
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                                                           validated_dcf_generation=marker))
            updates = (
                {"schema_version": "jspace_contract_v1"}, {"source_read": False},
                {"allowed_operations": ["read", "modify"]},
                {"allowed_operations": ["command", "read"]},
                {"allowed_operations": ["command", "modify"]},
                {"denied_operations": ["read"]}, {"denied_operations": ["modify"]},
                {"denied_operations": ["command"]}, {"denied_operations": None},
                {"repo_root": "/other"}, {"authorization_semantic_sha256": "short"},
                {"read_scopes": ["*.py"]}, {"write_scopes": [str(self.workspace)]},
                {"read_scopes": [None]}, {"write_scopes": []},
            )
            for update in updates:
                with self.subTest(update=update):
                    self.contract = dict(original, **update)
                    self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                        validated_dcf_generation=original["dcf_generation"]))
            for update in ({"context_mode": "local_workspace_jspace"},
                           {"action_freshness": None}, {"generation_id": None}):
                with self.subTest(generation=update):
                    self.contract = copy.deepcopy(original)
                    self.contract["dcf_generation"].update(update)
                    self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                        validated_dcf_generation=self.contract["dcf_generation"]))
            self.contract = copy.deepcopy(original)
            del self.contract["dcf_generation"]["action_freshness"]
            self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                validated_dcf_generation=self.contract["dcf_generation"]))
            reads.assert_not_called()

    def test_dcf_present_malformed_or_stale_snapshot_never_falls_back_to_observed_bytes(self):
        preimages = self.fixture(dcf=True)
        original = copy.deepcopy(self.contract)
        digest = preimages.pins["work.py"]
        snapshots = (None, [], {}, {"work.py": {"sha256": "g" * 64}},
                     {"work.py": {"sha256": "b" * 64}})
        for snapshot in snapshots:
            with self.subTest(snapshot=snapshot):
                self.contract = copy.deepcopy(original)
                self.contract["dcf_generation"]["source_snapshot"] = snapshot
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                    validated_dcf_generation=self.contract["dcf_generation"]))
        self.contract["dcf_generation"]["source_snapshot"] = {"work.py": {"sha256": digest}}
        pinned = context.capture_preimages(self.workspace, self.contract,
            validated_dcf_generation=self.contract["dcf_generation"])
        self.assertIsNotNone(pinned)
        self.assertIsNone(pinned.dcf_generation)
        self.write("work.py", "def answer():\n    return 2\n")
        with patch.object(context.source_excerpt, "_read_exact") as reads:
            # Even a newly supplied matching snapshot cannot replace the captured generation.
            self.assertIsNone(self.project(preimages))
            reads.assert_not_called()

    def test_dcf_exact_capture_file_and_byte_boundaries_are_all_or_none(self):
        self.fixture({name: "def answer():\n    return 1\n" for name in ("a.py", "b.py")}, dcf=True)
        generation = self.contract["dcf_generation"]
        size = sum(len(text.encode("utf-8")) for text in self.sources.values())
        for constant, limit in (("MAX_CAPTURE_FILES", 2), ("MAX_CAPTURE_BYTES", size)):
            with self.subTest(constant=constant), patch.object(context, constant, limit):
                captured = context.capture_preimages(self.workspace, self.contract,
                                                     validated_dcf_generation=generation)
                self.assertEqual(set(captured.pins), {"a.py", "b.py"})
            with self.subTest(overflow=constant), patch.object(context, constant, limit - 1):
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                                                           validated_dcf_generation=generation))

    def test_dcf_exact_grant_intersection_never_discovers_or_reads_other_sources(self):
        self.fixture({name: "def answer():\n    return 1\n" for name in ("public.py", "private.py")}, dcf=True)
        self.contract["read_scopes"] = ["public.py", "*.py", "."]
        self.contract["write_scopes"] = [str(self.workspace / "public.py"), "private.py", "*.py"]
        reader = context.source_excerpt._read_exact
        with patch.object(context.source_excerpt, "_read_exact", wraps=reader) as reads, \
                patch.object(Path, "rglob", side_effect=AssertionError("no discovery")), \
                patch.object(Path, "iterdir", side_effect=AssertionError("no discovery")):
            preimages = context.capture_preimages(self.workspace, self.contract,
                validated_dcf_generation=self.contract["dcf_generation"])
            self.assertEqual(set(preimages.pins), {"public.py"})
            self.write("public.py", "def answer():\n    return 2\n")
            self.write("private.py", "secret = 'not admitted for read'\n")
            row, = self.assert_wire(self.project(preimages))
            self.assertEqual(row["path"], "public.py")
        self.assertTrue(all(call.args[1] == "public.py" for call in reads.call_args_list))

    def test_dcf_capture_and_projection_reject_symlinked_file_or_parent(self):
        for kind in ("file", "parent"):
            with self.subTest(kind=kind):
                name = "file.py" if kind == "file" else "src/work.py"
                preimages = self.fixture({name: "def answer():\n    return 1\n"}, dcf=True)
                path = self.workspace / name if kind == "file" else self.workspace / "src"
                target = self.workspace / ("real.py" if kind == "file" else "real")
                path.rename(target)
                path.symlink_to(target, target_is_directory=kind == "parent")
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract,
                    validated_dcf_generation=self.contract["dcf_generation"]))
                self.assertIsNone(self.project(preimages))

    def test_dcf_generation_binding_scope_and_source_drift_are_fenced_after_final_check(self):
        preimages = self.fixture(dcf=True)
        original = copy.deepcopy(self.contract)
        checker = context.source_excerpt.still_current
        for phase in ("capture", "project"):
            for drift in ("generation", "binding", "scope", "source"):
                with self.subTest(phase=phase, drift=drift):
                    self.contract = copy.deepcopy(original)
                    self.write("work.py", self.sources["work.py"] if phase == "capture"
                               else "def answer():\n    return 2\n")
                    def changed(workspace, excerpt):
                        if drift == "source":
                            self.write("work.py", "def answer():\n    return 3\n")
                        current = checker(workspace, excerpt)
                        if drift == "generation":
                            self.contract["dcf_generation"]["action_freshness"]["source_fingerprints"]["surface"] = "changed"
                        elif drift == "binding":
                            self.contract["authorization_semantic_sha256"] = "b" * 64
                        elif drift == "scope":
                            self.contract["read_scopes"] = []
                        return current
                    with patch.object(context.source_excerpt, "still_current", side_effect=changed):
                        result = (context.capture_preimages(self.workspace, self.contract,
                                  validated_dcf_generation=self.contract["dcf_generation"])
                                  if phase == "capture" else self.project(preimages))
                    self.assertIsNone(result)
        self.assertEqual(self.emitted, set())

    def test_preimage_capture_is_bounded_and_exactly_pinned(self):
        self.fixture({name: "def answer():\n    return 1\n" for name in ("a.py", "b.py")})
        for constant, limit in (("MAX_CAPTURE_FILES", 1), ("MAX_CAPTURE_BYTES", 1)):
            with self.subTest(constant=constant), patch.object(context, constant, limit):
                self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
        self.contract["dcf_generation"]["source_snapshot"]["b.py"]["sha256"] = "b" * 64
        self.assertIsNone(context.capture_preimages(self.workspace, self.contract))

    def test_exact_separate_read_and_write_scopes_do_not_disclose_other_files(self):
        self.fixture({name: "def answer():\n    return 1\n" for name in ("public.py", "private.py")})
        self.contract["read_scopes"] = ["public.py"]
        self.contract["write_scopes"] = [str(self.workspace / "public.py"), "private.py"]
        reader = context.source_excerpt._read_exact
        with patch.object(context.source_excerpt, "_read_exact", wraps=reader) as reads:
            preimages = context.capture_preimages(self.workspace, self.contract)
            self.assertEqual(set(preimages.pins), {"public.py"})
            self.write("public.py", "def answer():\n    return 2\n")
            self.write("private.py", "secret = 'not granted for read'\n")
            row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["path"], "public.py")
        self.assertTrue(all(call.args[1] == "public.py" for call in reads.call_args_list))

    def test_missing_denied_or_nonexact_capabilities_and_pins_disable_context(self):
        preimages = self.fixture()
        before = copy.deepcopy(self.contract)
        self.write("work.py", "def answer():\n    return 2\n")
        cases = [
            {"source_read": False}, {"source_read": 1}, {"repo_root": "/other"},
            {"allowed_operations": ["command", "read"]}, {"allowed_operations": "command read modify"},
            {"denied_operations": None}, {"denied_operations": ["read"]},
            {"denied_operations": ["modify"]}, {"denied_operations": ["command"]},
            {"read_scopes": []}, {"write_scopes": []}, {"write_scopes": ["."]},
            {"read_scopes": ["*.py"]}, {"read_scopes": ["work.py/"]},
            {"read_scopes": [str(self.workspace / "work.py")]},
            {"write_scopes": ["../work.py"]}, {"write_scopes": [str(self.workspace)]},
            {"read_scopes": [None]}, {"write_scopes": "work.py"},
            {"authorization_semantic_sha256": "short"}, {"dcf_generation": {}},
            {"dcf_generation": {"source_snapshot": {"work.py": {"sha256": "g" * 64}}}},
        ]
        for updates in cases:
            with self.subTest(updates=updates):
                self.contract = copy.deepcopy(before)
                self.contract.update(updates)
                with patch.object(context.source_excerpt, "_read_exact") as reads:
                    self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
                    self.assertIsNone(self.project(preimages))
                reads.assert_not_called()
        self.contract = copy.deepcopy(before)
        self.contract["dcf_generation"]["source_snapshot"]["work.py"]["sha256"] = "b" * 64
        self.assertIsNone(self.project(preimages))

    def test_missing_denied_file_and_symlink_fail_closed_at_capture_and_projection(self):
        preimages = self.fixture()
        path = self.workspace / "work.py"
        path.unlink()
        self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
        self.assertIsNone(self.project(preimages))
        target = self.workspace / "target.py"
        target.write_text("def answer():\n    return 2\n")
        path.symlink_to(target)
        self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
        self.assertIsNone(self.project(preimages))
        with patch.object(context.source_excerpt, "_read_exact", side_effect=PermissionError("denied")):
            self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
            self.assertIsNone(self.project(preimages))

    def test_symlinked_parent_directory_and_workspace_are_not_exact_sources(self):
        preimages = self.fixture({"src/work.py": "def answer():\n    return 1\n"})
        directory = self.workspace / "src"
        directory.rename(self.workspace / "real")
        directory.symlink_to(self.workspace / "real", target_is_directory=True)
        self.assertIsNone(self.project(preimages))
        self.assertIsNone(context.capture_preimages(self.workspace, self.contract))
        link = self.workspace / "workspace_link"
        link.symlink_to(self.workspace, target_is_directory=True)
        contract = dict(self.contract, repo_root=str(link))
        self.assertIsNone(context.capture_preimages(link, contract))

    def test_source_drift_during_capture_and_projection_is_not_current_evidence(self):
        preimages = self.fixture()
        reader = context.source_excerpt._read_exact
        for phase in ("capture", "project"):
            with self.subTest(phase=phase):
                self.write("work.py", self.sources["work.py"] if phase == "capture"
                           else "def answer():\n    return 2\n")
                changed = False
                def drift(workspace, name):
                    nonlocal changed
                    data = reader(workspace, name)
                    if not changed:
                        changed = True
                        self.write(name, "def answer():\n    return 3\n")
                    return data
                with patch.object(context.source_excerpt, "_read_exact", side_effect=drift):
                    result = (context.capture_preimages(self.workspace, self.contract)
                              if phase == "capture" else self.project(preimages))
                self.assertIsNone(result)
                self.assertEqual(self.emitted, set())

    def test_binding_and_scope_drift_are_rechecked_even_after_final_source_check(self):
        preimages = self.fixture()
        original = copy.deepcopy(self.contract)
        checker = context.source_excerpt.still_current
        for phase in ("capture", "project"):
            for drift in ("binding", "scope"):
                with self.subTest(phase=phase, drift=drift):
                    self.contract = copy.deepcopy(original)
                    self.write("work.py", self.sources["work.py"] if phase == "capture"
                               else "def answer():\n    return 2\n")
                    def changed_binding(workspace, excerpt):
                        current = checker(workspace, excerpt)
                        if drift == "binding":
                            self.contract["authorization_semantic_sha256"] = "b" * 64
                        else:
                            self.contract["read_scopes"] = []
                        return current
                    with patch.object(context.source_excerpt, "still_current", side_effect=changed_binding):
                        result = (context.capture_preimages(self.workspace, self.contract)
                                  if phase == "capture" else self.project(preimages))
                    self.assertIsNone(result)
        self.contract = dict(original, authorization_semantic_sha256="b" * 64)
        self.assertIsNone(self.project(preimages))

    def test_failure_unknown_or_missing_cleanup_evidence_cannot_read_or_emit_context(self):
        preimages = self.fixture()
        self.write("work.py", "def answer():\n    return 2\n")
        observations = [None, [], {}, dict(self.success, success=False), dict(self.success, success=1),
                        dict(self.success, exit_code=1), dict(self.success, exit_code=True),
                        dict(self.success, outcome="unknown"), dict(self.success, process_reaped=False),
                        dict(self.success, process_group_empty=False)]
        for key in ("success", "exit_code", "outcome", "process_reaped", "process_group_empty"):
            missing = dict(self.success)
            del missing[key]
            observations.append(missing)
        for observation in observations:
            with self.subTest(observation=observation), \
                    patch.object(context.source_excerpt, "_read_exact") as reads:
                self.assertFalse(context.known_success(observation))
                self.assertIsNone(context.project(self.workspace, self.contract, preimages,
                                                  observation, self.emitted))
                reads.assert_not_called()
        self.assertEqual(self.emitted, set())

    def test_dedup_is_only_for_explicitly_recorded_successful_return_identities(self):
        preimages = self.fixture()
        self.write("work.py", "def answer():\n    return 2\n")
        payload = self.project(preimages)
        row, = self.assert_wire(payload)
        self.assertEqual(self.emitted, set())
        self.assertEqual(self.project(preimages), payload)
        self.emitted.add((payload["jspace_semantic_sha256"], row["path"], row["postimage_sha256"]))
        self.assertIsNone(self.project(preimages))
        self.write("work.py", "def answer():\n    return 3\n")
        next_row, = self.assert_wire(self.project(preimages))
        self.assertNotEqual(next_row["postimage_sha256"], row["postimage_sha256"])
        self.assertEqual(len(self.emitted), 1)

    def test_dedup_does_not_hide_new_files_or_skipped_file_identity_drift(self):
        preimages = self.fixture({name: "def answer():\n    return 1\n" for name in ("a.py", "b.py")})
        self.write("a.py", "def answer():\n    return 2\n")
        row, = self.assert_wire(self.project(preimages))
        self.emitted.add(("a" * 64, "a.py", row["postimage_sha256"]))
        self.write("b.py", "def answer():\n    return 2\n")
        row, = self.assert_wire(self.project(preimages))
        self.assertEqual(row["path"], "b.py")
        (self.workspace / "a.py").unlink()
        self.assertIsNone(self.project(preimages))
        self.assertEqual(len(self.emitted), 1)


if __name__ == "__main__":
    unittest.main()
