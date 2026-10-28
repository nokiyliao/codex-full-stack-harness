# SPDX-License-Identifier: MIT
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import source_excerpt


class SourceExcerptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        (self.workspace / "pkg").mkdir()
        self.name = "pkg/example.py"
        (self.workspace / self.name).write_text(
            "VALUE = 1\n\ndef target(value):\n    return value + VALUE\n", encoding="utf-8"
        )
        self.locator = {"path": self.name, "line": 3, "generation_id": "current"}
        self.contract = {"allowed_operations": ["read"], "denied_operations": ["command"],
                         "read_scopes": [self.name], "write_scopes": [],
                         "command_templates": [], "dcf_generation": {"generation_id": "current"}}

    def test_exact_granted_definition_is_bounded_and_recheckable(self):
        result = source_excerpt.extract(self.workspace, self.contract, self.locator)
        self.assertEqual((result["start_line"], result["end_line"]), (3, 4))
        self.assertEqual(result["text"], "def target(value):\n    return value + VALUE\n")
        self.assertEqual(set(result), {"path", "start_line", "end_line", "source_sha256", "text"})
        self.assertTrue(source_excerpt.still_current(self.workspace, result))
        (self.workspace / self.name).write_text("VALUE = 2\n\ndef target(value):\n    return value\n")
        self.assertFalse(source_excerpt.still_current(self.workspace, result))

    def test_no_exact_grant_or_command_capability_is_rejected(self):
        for change in ({"read_scopes": ["pkg/**"]}, {"write_scopes": [self.name]},
                       {"allowed_operations": ["read", "command"]},
                       {"dcf_generation": {"generation_id": "old"}}):
            with self.subTest(change=change):
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, source_excerpt.CODE):
                    source_excerpt.extract(self.workspace, {**self.contract, **change}, self.locator)

    def test_symlink_and_wrong_definition_line_are_rejected(self):
        (self.workspace / "pkg/link.py").symlink_to("example.py")
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, source_excerpt.CODE):
            source_excerpt.extract(
                self.workspace, {**self.contract, "read_scopes": ["pkg/link.py"]},
                {**self.locator, "path": "pkg/link.py"},
            )
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, source_excerpt.CODE):
            source_excerpt.extract(self.workspace, self.contract, {**self.locator, "line": 1})

    def test_oversized_definition_is_not_silently_truncated(self):
        body = "".join(f"    value += {index}\n" for index in range(121))
        (self.workspace / self.name).write_text("def target(value):\n" + body + "    return value\n")
        with self.assertRaisesRegex(source_excerpt.ExcerptBudgetExceeded, source_excerpt.CODE):
            source_excerpt.extract(self.workspace, self.contract, {**self.locator, "line": 1})

    def test_embedded_excerpt_is_rechecked_against_current_source(self):
        excerpt = source_excerpt.extract(self.workspace, self.contract, self.locator)
        context = {"context_summary": "Review source." + source_excerpt.CONTEXT_MARKER
                   + json.dumps(excerpt)}
        source_excerpt.verify_context_excerpt(self.workspace, context, self.contract)
        (self.workspace / self.name).write_text(
            "VALUE = 2\n\ndef target(value):\n    return value + VALUE\n", encoding="utf-8"
        )
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, source_excerpt.CODE):
            source_excerpt.verify_context_excerpt(self.workspace, context, self.contract)

    def test_tampered_or_ambiguous_embedded_excerpt_is_rejected(self):
        excerpt = source_excerpt.extract(self.workspace, self.contract, self.locator)
        excerpt["text"] = "def target(value):\n    return 0\n"
        context = {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(excerpt)}
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, source_excerpt.CODE):
            source_excerpt.verify_context_excerpt(self.workspace, context, self.contract)
        context["context_summary"] += source_excerpt.CONTEXT_MARKER + json.dumps(excerpt)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, source_excerpt.CODE):
            source_excerpt.verify_context_excerpt(self.workspace, context, self.contract)


    def write_source(self, text):
        (self.workspace / self.name).write_text(text, encoding="utf-8")
        line = next(index for index, value in enumerate(text.splitlines(), 1)
                    if value.startswith("def target("))
        return {**self.locator, "line": line}

    def verify(self, excerpt, contract=None):
        source_excerpt.verify_context_excerpt(
            self.workspace,
            {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(excerpt)},
            self.contract if contract is None else contract,
        )

    def test_transitive_support_is_exact_same_file_and_never_executed(self):
        text = ("import missing_package as backend\n"
                "from missing_package import factory as build\n"
                "BASE = 2\n"
                "LIMIT: int = BASE + 1\n"
                "class Box:\n"
                "    def value(self):\n"
                "        return backend.run(LIMIT)\n"
                "def helper():\n"
                "    return build(Box())\n"
                "def target():\n"
                "    return helper()\n"
                "raise RuntimeError('must not execute')\n")
        locator = self.write_source(text)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract(self.workspace, self.contract, locator,
                                            include_dependencies=True)
        read.assert_called_once_with(self.workspace, self.name)
        spans = result["supporting_spans"]
        self.assertEqual([(span["start_line"], span["end_line"]) for span in spans],
                         [(1, 1), (2, 2), (3, 3), (4, 4), (5, 7), (8, 9)])
        lines = text.splitlines(keepends=True)
        for span in spans:
            self.assertEqual(span["text"], "".join(lines[span["start_line"] - 1:span["end_line"]]))
        self.assertEqual(result["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.verify(result)

    def test_recursive_helpers_and_shared_bindings_are_deduplicated(self):
        locator = self.write_source(
            "LEFT = RIGHT = 1\n"
            "def first(n):\n"
            "    return second(n - LEFT) if n else RIGHT\n"
            "def second(n):\n"
            "    return first(n - RIGHT) if n else LEFT\n"
            "def target():\n"
            "    return first(2) + second(2) + target()\n")
        result = source_excerpt.extract(self.workspace, self.contract, locator,
                                        include_dependencies=True)
        self.assertEqual([(span["start_line"], span["end_line"])
                          for span in result["supporting_spans"]], [(1, 1), (2, 3), (4, 5)])
        self.verify(result)

    def test_decorators_defaults_annotations_and_async_helpers(self):
        locator = self.write_source(
            "from missing_package import decorate\n"
            "VALUE = 1\n"
            "class Kind: pass\n"
            "@decorate\n"
            "async def helper(value: Kind = VALUE):\n"
            "    return value\n"
            "@decorate\n"
            "def target():\n"
            "    return helper()\n")
        result = source_excerpt.extract(self.workspace, self.contract, locator,
                                        include_dependencies=True)
        self.assertEqual([(span["start_line"], span["end_line"])
                          for span in result["supporting_spans"]],
                         [(1, 1), (2, 2), (3, 3), (4, 6), (7, 7)])
        self.assertEqual(result["start_line"], 8)
        self.verify(result)

    def test_nested_target_does_not_duplicate_enclosing_support(self):
        self.write_source("class Box:\n    def method(self):\n        return Box()\n"
                          "def target(): pass\n")
        result = source_excerpt.extract(self.workspace, self.contract,
                                        {**self.locator, "line": 2}, include_dependencies=True)
        self.assertEqual(result["supporting_spans"],
                         [{"start_line": 1, "end_line": 1, "text": "class Box:\n"}])
        self.verify(result)

    def test_aggregate_line_budget_accepts_boundary_and_refuses_overflow(self):
        for body_lines in (117, 118):
            with self.subTest(body_lines=body_lines):
                locator = self.write_source("def helper():\n" + "    pass\n" * body_lines
                                            + "def target():\n    return helper()\n")
                source_excerpt.extract(self.workspace, self.contract, locator)
                if body_lines == 117:
                    result = source_excerpt.extract(self.workspace, self.contract, locator,
                                                    include_dependencies=True)
                    self.verify(result)
                else:
                    with self.assertRaisesRegex(source_excerpt.ExcerptBudgetExceeded,
                                                source_excerpt.CODE):
                        source_excerpt.extract(self.workspace, self.contract, locator,
                                               include_dependencies=True)

    def test_aggregate_utf8_byte_budget(self):
        template = "VALUE = '{}'\ndef target():\n    return VALUE\n"
        room = source_excerpt.MAX_EXCERPT_BYTES - len(template.format("").encode())
        content = "é" * (room // 2) + "x" * (room % 2)
        for extra in ("", "x"):
            with self.subTest(extra=extra):
                locator = self.write_source(template.format(content + extra))
                source_excerpt.extract(self.workspace, self.contract, locator)
                if not extra:
                    result = source_excerpt.extract(self.workspace, self.contract, locator,
                                                    include_dependencies=True)
                    self.assertEqual(len(result["text"].encode()) + sum(
                        len(span["text"].encode()) for span in result["supporting_spans"]),
                        source_excerpt.MAX_EXCERPT_BYTES)
                else:
                    with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                        source_excerpt.extract(self.workspace, self.contract, locator,
                                               include_dependencies=True)

    def test_source_read_command_admission_is_opt_in_and_scoped(self):
        contract = {**self.contract, "allowed_operations": ["read", "command"],
                    "source_read": True, "verifier_grants": []}
        result = source_excerpt.extract(self.workspace, contract, self.locator,
                                        include_dependencies=True)
        self.verify(result, contract)
        evidence_only = {**contract, "focused_verifiers": [{"declared": True, "result_status": "pass"}]}
        self.verify(source_excerpt.extract(self.workspace, evidence_only, self.locator,
                                           include_dependencies=True), evidence_only)
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.extract(self.workspace, contract, self.locator)
        for change in ({"source_read": False}, {"source_read": 1},
                       {"source_read": None}, {"read_scopes": self.name + "/other"},
                       {"write_scopes": [self.name]}, {"write_scopes": None},
                       {"allowed_operations": ["read", "command", "modify"]},
                       {"allowed_operations": ["read", "modify"]},
                       {"allowed_operations": ["command"]},
                       {"command_templates": ["arbitrary command"]},
                       {"verifier_grants": ["arbitrary verifier"]},
                       {"verifier_command_templates": ["arbitrary verifier"]},
                       {"denied_operations": ["read"]}, {"read_scopes": ["pkg/**"]},
                       {"dcf_generation": {"generation_id": "old"}}):
            with self.subTest(change=change):
                with patch.object(source_excerpt, "_read_exact") as read:
                    with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                        source_excerpt.extract(self.workspace, {**contract, **change}, self.locator,
                                               include_dependencies=True)
                    self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)
                    read.assert_not_called()

    def test_empty_support_and_original_read_only_contract_still_verify(self):
        locator = self.write_source("def target():\n    return 1\n")
        result = source_excerpt.extract(self.workspace, self.contract, locator,
                                        include_dependencies=True)
        self.assertEqual(result["supporting_spans"], [])
        self.verify(result)
        self.verify(source_excerpt.extract(self.workspace, self.contract, locator))

    def test_supporting_spans_are_strictly_validated_and_reextracted(self):
        result = source_excerpt.extract(self.workspace, self.contract, self.locator,
                                        include_dependencies=True)
        span = result["supporting_spans"][0]
        malformed = (None, {}, [None], [], [span, span],
                     [{**span, "text": "VALUE = 2\n"}], [{**span, "start_line": True}],
                     [{**span, "end_line": 0}], [{**span, "path": "other.py"}],
                     [{**span, "extra": 1}], [{"start_line": 1, "end_line": 1}])
        for spans in malformed:
            with self.subTest(spans=spans):
                with self.assertRaises(caller.EmbeddedNokiyError):
                    self.verify({**result, "supporting_spans": spans})
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.verify({**result, "unknown": True})
        (self.workspace / self.name).write_text(
            "VALUE = 2\n\ndef target(value):\n    return value + VALUE\n", encoding="utf-8")
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            self.verify(result)
        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)

    def test_drift_beyond_budget_is_integrity_failure_not_capacity_refusal(self):
        result = source_excerpt.extract(self.workspace, self.contract, self.locator,
                                        include_dependencies=True)
        (self.workspace / self.name).write_text(
            "VALUE = '" + "x" * source_excerpt.MAX_EXCERPT_BYTES
            + "'\n\ndef target(value):\n    return value + VALUE\n", encoding="utf-8")
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            self.verify(result)
        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)


class SupportNavigationTests(unittest.TestCase):
    setUp = SourceExcerptTests.setUp

    def verify(self, excerpt, contract, *, after_execution=False):
        return source_excerpt.verify_context_excerpt(self.workspace,
            {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(excerpt)},
            contract, after_execution=after_execution)

    def edit_contract(self):
        return {**self.contract, "allowed_operations": ["read", "modify", "command"],
                "denied_operations": ["create", "delete"], "source_read": True,
                "write_scopes": [self.name]}

    def prepare(self, source, line=1, *, enabled=True):
        (self.workspace / self.name).write_text(source, encoding="utf-8")
        locator = {**self.locator, "line": line, "target": "symbol:pkg.example.target"}
        return source_excerpt.extract_edits(self.workspace, self.edit_contract(), [locator],
                                             support_navigation=enabled)

    def partial_source(self):
        return "def target(value):\n    return helper(value)\n\ndef helper(value):\n" + "    pass\n" * 130

    def test_partial_metadata_keeps_source_and_legacy_envelope_exact(self):
        source = self.partial_source()
        legacy = self.prepare(source, enabled=False)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract_edits(self.workspace, self.edit_contract(),
                [{**self.locator, "line": 1, "target": "symbol:pkg.example.target"}],
                support_navigation=True)
        read.assert_called_once_with(self.workspace, self.name)
        without_navigation = {key: value for key, value in result.items() if key != "support_navigation"}
        self.assertEqual(without_navigation, legacy)
        self.assertEqual(result["support_navigation"]["kind"], source_excerpt.SUPPORT_NAVIGATION_KIND)
        self.assertEqual(result["support_navigation"]["candidate_count"], 1)
        self.assertEqual(result["support_navigation"]["locators"], [{"path": self.name,
            "name": "helper", "kind": "function", "start_line": 4, "end_line": 134}])
        self.verify(result, self.edit_contract())
        self.verify(legacy, self.edit_contract())

    def test_complete_support_adds_no_navigation(self):
        source = "def target(value):\n    return helper(value)\ndef helper(value):\n    return value\n"
        self.assertEqual(self.prepare(source), self.prepare(source, enabled=False))
        self.assertNotIn("support_navigation", self.prepare(source))

    def test_direct_only_candidates_include_decorators_and_parent_methods(self):
        source = ("def target(value):\n    return helper(value)\n@decorator\n"
                  "async def helper(value):\n    return indirect(value)\n"
                  + "    pass\n" * 125 + "def indirect(value):\n    return value\n")
        result = self.prepare(source)
        self.assertEqual([row["name"] for row in result["support_navigation"]["locators"]], ["helper"])
        self.assertEqual(result["support_navigation"]["locators"][0]["start_line"], 3)
        source = ("class Box:\n    def target(self):\n        return self.helper()\n"
                  "    def helper(self):\n" + "        pass\n" * 130)
        result = self.prepare(source, 2)
        self.assertEqual([row["name"] for row in result["support_navigation"]["locators"]], ["Box.helper"])
        self.verify(result, self.edit_contract())

    def test_imports_and_dynamic_names_do_not_open_another_file(self):
        source = ("from other import external\ndef target(value):\n    return helper(external(value))\n"
                  "def helper(value):\n" + "    pass\n" * 130)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = self.prepare(source, 2)
        read.assert_called_once_with(self.workspace, self.name)
        self.assertEqual([row["name"] for row in result["support_navigation"]["locators"]], ["helper"])

    def test_count_and_byte_bounds_do_not_evict_preloaded_source(self):
        helpers = ["helper" + str(index) for index in range(40)]
        source = "def target():\n    return (" + ", ".join(name + "()" for name in helpers) + ")\n"
        source += "".join("def " + name + "():\n" + "    pass\n" * 4 for name in helpers)
        legacy = self.prepare(source, enabled=False)
        result = self.prepare(source)
        navigation = result["support_navigation"]
        self.assertEqual(navigation["candidate_count"], 40)
        self.assertLessEqual(len(navigation["locators"]), source_excerpt.MAX_SUPPORT_LOCATORS)
        self.assertLessEqual(len(json.dumps(navigation, sort_keys=True).encode()),
                             source_excerpt.MAX_SUPPORT_LOCATOR_BYTES)
        self.assertLessEqual(len(json.dumps(result, sort_keys=True).encode()), source_excerpt.MAX_ENVELOPE_BYTES)
        self.assertEqual(result["files"], legacy["files"])
        self.verify(result, self.edit_contract())

        # Reuse the same large candidate set for the read-only partial shape.
        readonly = source_excerpt.extract(self.workspace, self.contract,
            {**self.locator, "line": 1}, include_dependencies=True, allow_definitions_only=True)
        self.assertEqual(readonly["kind"], source_excerpt.READONLY_PARTIAL_KIND)
        self.assertEqual(readonly["files"], result["files"])
        self.assertEqual(readonly["support_navigation"], navigation)
        self.verify(readonly, self.contract)

    def test_tampering_is_integrity_failure_not_navigation_fallback(self):
        result = self.prepare(self.partial_source())
        original = result["support_navigation"]
        row = original["locators"][0]
        changes = [None, {}, {**original, "candidate_count": True},
                   {**original, "kind": "complete_dependencies"},
                   {**original, "locators": []}, {**original, "locators": [row, row]},
                   {**original, "locators": [{**row, "path": "other.py"}]},
                   {**original, "locators": [{**row, "start_line": True}]},
                   {**original, "locators": [{**row, "name": "unknown"}]},
                   {**original, "locators": [{**row, "end_line": row["end_line"] + 1}]}]
        for value in changes:
            with self.subTest(value=value):
                with self.assertRaises(caller.EmbeddedNokiyError) as error:
                    self.verify({**result, "support_navigation": value}, self.edit_contract())
                self.assertNotIsInstance(error.exception, source_excerpt.ExcerptBudgetExceeded)

    def test_postimage_remains_fresh_and_navigation_is_not_a_write_grant(self):
        result = self.prepare(self.partial_source())
        contract = self.edit_contract()
        original = copy.deepcopy(contract)
        self.verify(result, contract)
        (self.workspace / self.name).write_text("def target():\n    return 2\n")
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.verify(result, contract)
        postimage = self.verify(result, contract, after_execution=True)
        self.assertTrue(postimage["files"][0]["changed"])
        self.assertEqual(contract, original)

    def test_option_requires_boolean_and_grants_still_precede_all_reads(self):
        self.prepare(self.partial_source())
        rows = [{**self.locator, "line": 1, "target": "symbol:pkg.example.target"}]
        for enabled in (None, 1, "true"):
            with self.subTest(enabled=enabled):
                with self.assertRaises(caller.EmbeddedNokiyError):
                    source_excerpt.extract_edits(self.workspace, self.edit_contract(), rows,
                                                 support_navigation=enabled)
        with patch.object(source_excerpt, "_read_exact") as read:
            with self.assertRaises(caller.EmbeddedNokiyError):
                source_excerpt.extract_edits(self.workspace,
                    {**self.edit_contract(), "read_scopes": ["pkg/**"]}, rows, support_navigation=True)
        read.assert_not_called()


class ReadonlyPartialSourceExcerptTests(unittest.TestCase):
    setUp = SourceExcerptTests.setUp
    write_source = SourceExcerptTests.write_source
    verify = SourceExcerptTests.verify
    partial_source = SupportNavigationTests.partial_source

    def prepare(self, source):
        locator = self.write_source(source)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract(self.workspace, self.contract, locator,
                include_dependencies=True, allow_definitions_only=True)
        read.assert_called_once_with(self.workspace, self.name)
        return result

    def test_support_capacity_retains_complete_decorated_target(self):
        target = "@decorate\ndef target(value):\n    return helper(value)\n"
        for capacity, body in (("lines", "    pass\n" * 130),
                               ("bytes", "    return '" + "x" * source_excerpt.MAX_EXCERPT_BYTES + "'\n")):
            with self.subTest(capacity=capacity):
                source = target + "\ndef helper(value):\n" + body
                original = copy.deepcopy(self.contract)
                result = self.prepare(source)
                self.assertEqual(result["kind"], source_excerpt.READONLY_PARTIAL_KIND)
                self.assertEqual(result["notice"], source_excerpt.PARTIAL_READONLY_NOTICE)
                self.assertEqual(result["locator"], {**self.locator, "line": 2})
                self.assertEqual(result["files"], [{"path": self.name,
                    "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                    "spans": [{"start_line": 1, "end_line": 3, "text": target}]}])
                self.assertNotIn("supporting_spans", result)
                self.assertEqual(result["support_navigation"], {
                    "kind": source_excerpt.SUPPORT_NAVIGATION_KIND, "candidate_count": 1,
                    "locators": [{"path": self.name, "name": "helper", "kind": "function",
                                  "start_line": 5, "end_line": len(source.splitlines())}]})
                self.assertTrue(source_excerpt.still_current(self.workspace, result))
                with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
                    self.verify(result)
                read.assert_called_once_with(self.workspace, self.name)
                for options in ({}, {"allow_definitions_only": False}):
                    with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                        source_excerpt.extract(self.workspace, self.contract, result["locator"],
                            include_dependencies=True, **options)
                body_only = source_excerpt.extract(self.workspace, self.contract, result["locator"])
                self.assertEqual(body_only["text"], target.removeprefix("@decorate\n"))
                self.assertNotIn("supporting_spans", body_only)
                self.assertEqual(self.contract, original)

    def test_partial_tampering_and_required_field_deletion_are_integrity_failures(self):
        result = self.prepare("@decorate\n" + self.partial_source())
        file = result["files"][0]
        span = file["spans"][0]
        locator = result["locator"]
        navigation = result["support_navigation"]
        row = navigation["locators"][0]
        changes = (
            {"notice": "complete dependency evidence"}, {"notice": None},
            {"target": "symbol:pkg.example.other"}, {"files": None}, {"files": []},
            {"files": [{**file, "path": "pkg/other.py"}]},
            {"files": [{**file, "source_sha256": "0" * 64}]},
            {"files": [{**file, "spans": []}]},
            {"files": [{**file, "spans": [{**span, "text": "def target(): pass\n"}]}]},
            {"files": [{**file, "spans": [{**span, "start_line": span["start_line"] + 1}]}]},
            {"files": [{**file, "spans": [{**span, "end_line": span["end_line"] + 1}]}]},
            {"files": [{**file, "spans": [{**span, "start_line": True}]}]},
            {"locator": {**locator, "path": "pkg/other.py"}},
            {"locator": {**locator, "line": 5}}, {"locator": {**locator, "line": True}},
            {"locator": {**locator, "generation_id": "old"}},
            {"support_navigation": None}, {"support_navigation": {}},
            {"support_navigation": {**navigation, "kind": "complete_dependencies"}},
            {"support_navigation": {**navigation, "candidate_count": True}},
            {"support_navigation": {**navigation, "candidate_count": 1.0}},
            {"support_navigation": {**navigation, "locators": []}},
            {"support_navigation": {**navigation, "locators": [row, row]}},
            {"support_navigation": {**navigation, "locators": [{**row, "path": "pkg/other.py"}]}},
            {"support_navigation": {**navigation, "locators": [{**row, "name": "unknown"}]}},
            {"support_navigation": {**navigation, "locators": [{**row, "start_line": True}]}},
            {"support_navigation": {**navigation, "locators": [{**row, "end_line": row["end_line"] + 1}]}},
        )
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(caller.EmbeddedNokiyError) as error:
                    self.verify({**result, **change})
                self.assertNotIsInstance(error.exception, source_excerpt.ExcerptBudgetExceeded)
        for field in ("notice", "locator", "files", "support_navigation"):
            with self.subTest(deleted=field):
                with self.assertRaises(caller.EmbeddedNokiyError) as error:
                    self.verify({key: value for key, value in result.items() if key != field})
                self.assertNotIsInstance(error.exception, source_excerpt.ExcerptBudgetExceeded)

    def test_partial_source_drift_is_not_capacity_or_edit_postimage_fallback(self):
        result = self.prepare(self.partial_source())
        with (self.workspace / self.name).open("a", encoding="utf-8") as stream:
            stream.write("\nUNRELATED = 9\n")
        self.assertFalse(source_excerpt.still_current(self.workspace, result))
        for after_execution in (False, True):
            with self.subTest(after_execution=after_execution):
                with self.assertRaises(caller.EmbeddedNokiyError) as error:
                    source_excerpt.verify_context_excerpt(self.workspace,
                        {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)},
                        self.contract, after_execution=after_execution)
                self.assertNotIsInstance(error.exception, source_excerpt.ExcerptBudgetExceeded)

    def test_partial_requires_current_exact_read_admission_before_any_read(self):
        result = self.prepare(self.partial_source())
        for change in ({"read_scopes": ["pkg/**"]}, {"read_scopes": []},
                       {"denied_operations": ["read"]},
                       {"dcf_generation": {"generation_id": "old"}}):
            with self.subTest(change=change):
                contract = {**self.contract, **change}
                with patch.object(source_excerpt, "_read_exact") as read:
                    for operation in (
                        lambda: source_excerpt.extract(self.workspace, contract, result["locator"],
                            include_dependencies=True, allow_definitions_only=True),
                        lambda: self.verify(result, contract),
                    ):
                        with self.assertRaises(caller.EmbeddedNokiyError) as error:
                            operation()
                        self.assertNotIsInstance(error.exception, source_excerpt.ExcerptBudgetExceeded)
                    read.assert_not_called()

    def test_partial_symlink_invalid_utf8_and_ast_errors_are_not_capacity(self):
        path = self.workspace / self.name
        for failure in ("symlink", "invalid_utf8", "ast"):
            with self.subTest(failure=failure):
                if path.is_symlink():
                    path.unlink()
                result = self.prepare(self.partial_source())
                if failure == "symlink":
                    referent = path.with_name("real.py")
                    path.rename(referent)
                    path.symlink_to(referent.name)
                elif failure == "invalid_utf8":
                    path.write_bytes(b"\xff")
                else:
                    path.write_text("def target(:\n", encoding="utf-8")
                for operation in (
                    lambda: source_excerpt.extract(self.workspace, self.contract, result["locator"],
                        include_dependencies=True, allow_definitions_only=True),
                    lambda: self.verify(result),
                ):
                    with self.assertRaises(caller.EmbeddedNokiyError) as error:
                        operation()
                    self.assertNotIsInstance(error.exception, source_excerpt.ExcerptBudgetExceeded)

    def test_oversized_decorated_target_is_never_truncated(self):
        targets = (
            "@decorate\ndef target():\n" + "    pass\n" * 118 + "    return helper()\n",
            "@decorate('" + "x" * source_excerpt.MAX_EXCERPT_BYTES
            + "')\ndef target():\n    return helper()\n",
        )
        for index, target in enumerate(targets):
            with self.subTest(index=index):
                locator = self.write_source(target + "def helper():\n    pass\n")
                # The undecorated body fits; the complete decorated target does not.
                source_excerpt.extract(self.workspace, self.contract, locator)
                with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                    source_excerpt.extract(self.workspace, self.contract, locator,
                        include_dependencies=True, allow_definitions_only=True)

    def test_partial_serialized_envelope_overflow_is_capacity(self):
        target = "@decorate\ndef target():\n    return helper('" + "é" * 5000 + "')\n"
        self.assertLessEqual(len(target.encode("utf-8")), source_excerpt.MAX_EXCERPT_BYTES)
        self.assertGreater(len(json.dumps(target, ensure_ascii=True).encode()),
                           source_excerpt.MAX_ENVELOPE_BYTES)
        locator = self.write_source(target + "def helper(value):\n" + "    pass\n" * 130)
        with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
            source_excerpt.extract(self.workspace, self.contract, locator,
                include_dependencies=True, allow_definitions_only=True)

    def test_automatic_full_roundtrip_preserves_legacy_line_splitting(self):
        for separator in ("\f", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                source = (f"VALUE = 'a{separator}b'\r\n@decorate\r\ndef target():\r\n"
                          f"    value = 'c{separator}d'\r\n    return value + VALUE\r\n")
                (self.workspace / self.name).write_bytes(source.encode("utf-8"))
                locator = {**self.locator, "line": 3}
                legacy = source_excerpt.extract(self.workspace, self.contract, locator,
                    include_dependencies=True)
                automatic = source_excerpt.extract(self.workspace, self.contract, locator,
                    include_dependencies=True, allow_definitions_only=True)
                self.assertEqual(automatic, legacy)
                self.assertNotIn("kind", automatic)
                self.assertEqual(legacy["text"], "".join(source.splitlines(keepends=True)[2:5]))
                body_only = source_excerpt.extract(self.workspace, self.contract, locator)
                self.assertEqual(body_only["text"], legacy["text"])
                self.assertNotIn("supporting_spans", body_only)
                self.verify(automatic)

    def test_partial_roundtrip_retains_exact_physical_lines_and_line_endings(self):
        for separator in ("\f", "\u2028", "\u2029"):
            for newline in ("\n", "\r\n", "\r"):
                with self.subTest(separator=repr(separator), newline=repr(newline)):
                    target = (f"@decorate{newline}def target(value):{newline}"
                              f"    label = 'a{separator}b'{newline}"
                              f"    return helper(value, label){newline}")
                    source = (f"VALUE = 'c{separator}d'{newline}" + target + newline
                              + f"def helper(value, label):{newline}" + f"    pass{newline}" * 130)
                    raw = source.encode("utf-8")
                    (self.workspace / self.name).write_bytes(raw)
                    locator = {**self.locator, "line": 3}
                    with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
                        result = source_excerpt.extract(self.workspace, self.contract, locator,
                            include_dependencies=True, allow_definitions_only=True)
                    read.assert_called_once_with(self.workspace, self.name)
                    self.assertEqual(result["kind"], source_excerpt.READONLY_PARTIAL_KIND)
                    self.assertEqual(result["locator"], locator)
                    self.assertEqual(result["files"], [{"path": self.name,
                        "source_sha256": hashlib.sha256(raw).hexdigest(),
                        "spans": [{"start_line": 2, "end_line": 5, "text": target}]}])
                    self.assertTrue(source_excerpt.still_current(self.workspace, result))
                    self.verify(result)


class EditSourceExcerptTests(unittest.TestCase):
    """Edit envelopes reuse the old verifier and fixture, not duplicate tests."""

    setUp = SourceExcerptTests.setUp
    write_source = SourceExcerptTests.write_source
    verify = SourceExcerptTests.verify

    def edit_contract(self, names=None):
        names = [self.name] if names is None else names
        return {**self.contract, "allowed_operations": ["read", "modify", "command"],
                "denied_operations": ["create", "delete"], "source_read": True,
                "read_scopes": list(names), "write_scopes": list(names)}

    def edit_locator(self, line=3, target="target", name=None):
        return {**self.locator, "path": self.name if name is None else name,
                "line": line, "target": "symbol:pkg.example." + target}

    def test_envelope_allocation_fits_four_complete_targets_above_legacy_raw_cap(self):
        text = "VALUE = 7\ndef decorate(function):\n    return function\n\n"
        rows = []
        definitions = []
        for index in range(4):
            rows.append(self.edit_locator(len(text.splitlines()) + 2, f"target{index}"))
            definition = (f"@decorate\ndef target{index}():\n    value = '" + "x" * 2700
                          + "'\n" + "    pass\n" * 76 + "    return value, VALUE\n")
            definitions.append(definition)
            text += definition + "\n"
        (self.workspace / self.name).write_text(text, encoding="utf-8")
        contract = self.edit_contract()
        before = copy.deepcopy(contract)
        legacy = source_excerpt.extract_edits(self.workspace, contract, rows,
                                              per_target_budget=True, support_navigation=True)
        self.assertEqual(legacy["deferred_locators"], [rows[-1]])
        self.assertNotIn("allocation_mode", legacy)
        self.assertEqual(legacy, source_excerpt.extract_edits(self.workspace, contract, rows,
            per_target_budget=True, support_navigation=True, envelope_bounded=False))
        self.verify(legacy, contract)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract_edits(self.workspace, contract, rows[::-1],
                per_target_budget=True, support_navigation=True, envelope_bounded=True)
        read.assert_called_once_with(self.workspace, self.name)
        self.assertEqual(result["allocation_mode"], "envelope_bounded")
        self.assertEqual(result["byte_budget"], source_excerpt.MAX_ENVELOPE_BYTES)
        self.assertEqual(result["line_budget"], 480)
        self.assertNotIn("deferred_locators", result)
        selected = "".join(span["text"] for span in result["files"][0]["spans"])
        self.assertGreater(len(selected.encode()), source_excerpt.MAX_EXCERPT_BYTES)
        self.assertLessEqual(len(json.dumps(result, sort_keys=True, ensure_ascii=True).encode()),
                             source_excerpt.MAX_ENVELOPE_BYTES)
        for definition in definitions:
            self.assertEqual(selected.count(definition), 1)
        self.assertEqual(selected.count("def decorate("), 1)
        self.assertEqual(selected.count("VALUE = 7"), 1)
        self.assertEqual(contract, before)
        self.verify(result, contract)
        # Byte allocation does not implicitly increase the aggregate line budget.
        bounded = source_excerpt.extract_edits(self.workspace, contract, rows, envelope_bounded=True)
        self.assertNotIn("line_budget", bounded)
        self.assertEqual(bounded["deferred_locators"], rows[1:])
        self.verify(bounded, contract)

    def test_envelope_allocation_does_not_change_single_target_legacy_or_readonly_limits(self):
        locator = self.write_source("def target():\n    return '" + "x" * 13000 + "'\n")
        rows = [self.edit_locator(1)]
        contract = self.edit_contract()
        with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
            source_excerpt.extract_edits(self.workspace, contract, rows)
        result = source_excerpt.extract_edits(self.workspace, contract, rows, envelope_bounded=True)
        self.assertNotIn("line_budget", result)
        self.assertEqual(result["files"][0]["spans"][0]["text"],
                         (self.workspace / self.name).read_text())
        self.verify(result, contract)
        for support in (False, True):
            with self.subTest(support=support), self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                source_excerpt.extract(self.workspace, self.contract, locator, include_dependencies=support)

    def test_envelope_allocation_serialized_overflow_defers_or_refuses_whole_definitions(self):
        names = [self.name, "pkg/second.py"]
        rows = [self.edit_locator(1), self.edit_locator(1, "second", names[1])]
        contract = self.edit_contract(names)
        template = "def target():\n    return '{}'\n"
        for size in (3500, 4100):
            text = template.format("é" * size)
            for name in names:
                (self.workspace / name).write_text(text, encoding="utf-8")
            with self.subTest(size=size):
                if size == 4100:
                    with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                        source_excerpt.extract_edits(self.workspace, contract, rows,
                            per_target_budget=True, envelope_bounded=True)
                    continue
                result = source_excerpt.extract_edits(self.workspace, contract, rows,
                    per_target_budget=True, envelope_bounded=True)
                self.assertEqual(result["deferred_locators"], [rows[1]])
                self.assertEqual(result["files"][0]["spans"],
                                 [{"start_line": 1, "end_line": 2, "text": text}])
                self.assertEqual(result["files"][1]["spans"], [])
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=True).encode()),
                                     source_excerpt.MAX_ENVELOPE_BYTES)
                self.verify(result, contract)
                oversized = copy.deepcopy(result)
                oversized.pop("deferred_locators")
                oversized["notice"] = source_excerpt.EDIT_NOTICE
                oversized["files"][1]["spans"] = copy.deepcopy(result["files"][0]["spans"])
                self.assertLess(2 * len(text.encode()), source_excerpt.MAX_ENVELOPE_BYTES)
                self.assertGreater(len(json.dumps(oversized, ensure_ascii=True).encode()),
                                   source_excerpt.MAX_ENVELOPE_BYTES)
                for after_execution in (False, True):
                    with patch.object(source_excerpt, "_read_exact") as read:
                        with self.assertRaises(caller.EmbeddedNokiyError):
                            source_excerpt.verify_context_excerpt(self.workspace,
                                {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(oversized)},
                                contract, after_execution=after_execution)
                        read.assert_not_called()

    def test_envelope_mode_and_budget_are_validated_before_preflight_or_readback(self):
        self.write_source("def target():\n    return '" + "x" * 13000 + "'\n")
        contract = self.edit_contract()
        result = source_excerpt.extract_edits(self.workspace, contract, [self.edit_locator(1)],
                                             envelope_bounded=True)
        self.verify(result, contract)
        invalid = [{**result, "allocation_mode": value} for value in (None, False, {}, "legacy")]
        invalid += [{**result, "byte_budget": value} for value in
                    (True, 24576.0, 0, source_excerpt.MAX_EXCERPT_BYTES, 24577, "24576", None)]
        invalid += [{key: value for key, value in result.items() if key not in removed}
                    for removed in (("allocation_mode",), ("byte_budget",),
                                    ("allocation_mode", "byte_budget"))]
        invalid.append({**result, "line_budget": 120})
        for bad in invalid:
            for after_execution in (False, True):
                with self.subTest(bad=bad.keys(), after_execution=after_execution):
                    with patch.object(source_excerpt, "_read_exact") as read:
                        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                            source_excerpt.verify_context_excerpt(self.workspace,
                                {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(bad)},
                                contract, after_execution=after_execution)
                        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)
                        read.assert_not_called()
        for value in (1, None, "true"):
            with patch.object(source_excerpt, "_read_exact") as read:
                with self.assertRaises(caller.EmbeddedNokiyError):
                    source_excerpt.extract_edits(self.workspace, contract, [self.edit_locator(1)],
                                                 envelope_bounded=value)
                read.assert_not_called()

    def test_envelope_preimage_still_checks_exact_bytes_and_fresh_postedit_readback(self):
        self.write_source("def target():\n    return '" + "x" * 13000 + "'\n")
        contract = self.edit_contract()
        result = source_excerpt.extract_edits(self.workspace, contract, [self.edit_locator(1)],
                                             envelope_bounded=True)
        for field in ("text", "source_sha256"):
            bad = copy.deepcopy(result)
            if field == "text":
                bad["files"][0]["spans"][0]["text"] = "def target():\n    return 0\n"
            else:
                bad["files"][0]["source_sha256"] = "0" * 64
            with self.subTest(field=field), self.assertRaises(caller.EmbeddedNokiyError):
                self.verify(bad, contract)
        with (self.workspace / self.name).open("a") as stream:
            stream.write("\nUNRELATED = 9\n")
        self.assertFalse(source_excerpt.still_current(self.workspace, result))
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            self.verify(result, contract)
        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)
        context = {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)}
        for change in ({"read_scopes": ["pkg/**"]}, {"write_scopes": ["pkg/**"]},
                       {"dcf_generation": {"generation_id": "old"}},
                       {"denied_operations": ["modify"]}, {"source_read": False}):
            for after_execution in (False, True):
                with self.subTest(change=change, after_execution=after_execution):
                    with patch.object(source_excerpt, "_read_exact") as read:
                        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                            source_excerpt.verify_context_excerpt(self.workspace, context,
                                {**contract, **change}, after_execution=after_execution)
                        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)
                        read.assert_not_called()
        readback = source_excerpt.verify_context_excerpt(self.workspace, context, contract,
                                                        after_execution=True)
        self.assertEqual(readback["mission_acceptance"], "parent_owned")
        self.assertEqual(readback["files"], [{"path": self.name,
            "preimage_sha256": result["files"][0]["source_sha256"], "changed": True,
            "postimage_sha256": hashlib.sha256((self.workspace / self.name).read_bytes()).hexdigest()}])

    def test_multi_symbol_same_file_union_is_ordered_and_deduplicated(self):
        self.write_source("VALUE = 2\n"
                          "def helper(value):\n    return value * VALUE\n"
                          "def target(value):\n    return helper(value)\n")
        rows = [self.edit_locator(4), self.edit_locator(2, "helper")]
        contract = self.edit_contract()
        before = copy.deepcopy(contract)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract_edits(self.workspace, contract, rows)
        read.assert_called_once_with(self.workspace, self.name)
        self.assertEqual(result, source_excerpt.extract_edits(self.workspace, contract, rows[::-1]))
        self.assertEqual(result, source_excerpt.extract_edits(self.workspace, contract, rows + rows))
        self.assertEqual(len(result["files"]), 1)
        spans = result["files"][0]["spans"]
        self.assertEqual([(span["start_line"], span["end_line"]) for span in spans], [(1, 5)])
        self.assertEqual(spans[0]["text"], (self.workspace / self.name).read_text())
        self.assertEqual(contract, before)
        self.verify(result, contract)

    def test_enclosing_and_nested_edit_targets_share_source_once(self):
        self.write_source("class Box:\n    def method(self):\n        return Box()\n"
                          "def target(): pass\n")
        result = source_excerpt.extract_edits(self.workspace, self.edit_contract(),
            [self.edit_locator(2, "Box.method"), self.edit_locator(1, "Box")])
        self.assertEqual(result["files"][0]["spans"], [{"start_line": 1, "end_line": 3,
            "text": "class Box:\n    def method(self):\n        return Box()\n"}])
        self.verify(result, self.edit_contract())

    def test_four_files_have_one_combined_line_budget(self):
        names = [f"pkg/file{index}.py" for index in range(4)]
        rows = [self.edit_locator(1, f"target{index}", name) for index, name in enumerate(names)]
        contract = self.edit_contract(names[::-1])
        for name in names:
            (self.workspace / name).write_text("def target():\n" + "    pass\n" * 29)
        result = source_excerpt.extract_edits(self.workspace, contract, rows[::-1])
        self.assertEqual([row["path"] for row in result["files"]], names)
        self.assertEqual(sum(span["end_line"] - span["start_line"] + 1
                             for row in result["files"] for span in row["spans"]), 120)
        self.verify(result, contract)
        with (self.workspace / names[-1]).open("a") as stream:
            stream.write("    pass\n")
        partial = source_excerpt.extract_edits(self.workspace, contract, rows)
        self.assertEqual(partial["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(partial["deferred_locators"], [rows[-1]])
        self.assertEqual(partial["files"][-1]["spans"], [])
        self.verify(partial, contract)

    def test_multi_target_line_ceiling_keeps_old_envelopes_and_byte_caps(self):
        names = [f"pkg/target{i}.py" for i in range(4)]
        rows = [self.edit_locator(1, f"target{i}", name) for i, name in enumerate(names)]
        contract = self.edit_contract(names)
        original_contract = copy.deepcopy(contract)
        for name in names:
            (self.workspace / name).write_text("def target():\n" + "    pass\n" * 45)
        legacy = source_excerpt.extract_edits(self.workspace, contract, rows)
        self.assertNotIn("line_budget", legacy)
        self.assertEqual(legacy["deferred_locators"], rows[2:])
        self.verify(legacy, contract)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract_edits(self.workspace, contract, rows[::-1],
                                                 per_target_budget=True)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(result["line_budget"], 480)
        self.assertNotIn("deferred_locators", result)
        self.assertEqual(sum(span["end_line"] - span["start_line"] + 1
                             for row in result["files"] for span in row["spans"]), 184)
        self.assertLess(sum(len(span["text"].encode()) for row in result["files"]
                            for span in row["spans"]), source_excerpt.MAX_EXCERPT_BYTES)
        self.assertEqual(contract, original_contract)
        self.verify(result, contract)
        self.verify(legacy, contract)

    def test_target_aliases_do_not_inflate_line_ceiling(self):
        self.write_source("def helper():\n    return 1\n"
                          "def target():\n    return helper()\n")
        helper = self.edit_locator(1, "helper")
        rows = [helper, self.edit_locator(3), {**helper, "target": "symbol:pkg.example.alias"}]
        result = source_excerpt.extract_edits(self.workspace, self.edit_contract(), rows,
                                             per_target_budget=True)
        self.assertEqual(result["line_budget"], 240)
        self.assertEqual(result, source_excerpt.extract_edits(
            self.workspace, self.edit_contract(), rows + [helper], per_target_budget=True))
        self.verify(result, self.edit_contract())

    def test_multi_target_budget_never_preloads_an_oversized_definition(self):
        self.write_source("def helper():\n    return 1\n"
                          "def target():\n" + "    pass\n" * 120)
        rows = [self.edit_locator(1, "helper"), self.edit_locator(3)]
        for envelope_bounded in (False, True):
            with self.subTest(envelope_bounded=envelope_bounded):
                result = source_excerpt.extract_edits(self.workspace, self.edit_contract(), rows,
                    per_target_budget=True, envelope_bounded=envelope_bounded)
                self.assertEqual(result["line_budget"], 240)
                self.assertEqual(result["deferred_locators"], [rows[1]])
                self.assertEqual(result["files"][0]["spans"][0]["text"],
                                 "def helper():\n    return 1\n")
                self.verify(result, self.edit_contract())

    def test_new_budget_metadata_is_exact_and_cannot_be_removed_from_large_context(self):
        self.write_source("def helper():\n" + "    pass\n" * 79 +
                          "def target():\n" + "    pass\n" * 79)
        rows = [self.edit_locator(1, "helper"), self.edit_locator(81)]
        result = source_excerpt.extract_edits(self.workspace, self.edit_contract(), rows,
                                             per_target_budget=True)
        self.verify(result, self.edit_contract())
        for value in (True, 0, 120, 241, 480, "240", None):
            with self.subTest(value=value), self.assertRaises(caller.EmbeddedNokiyError):
                self.verify({**result, "line_budget": value}, self.edit_contract())
        removed = {key: value for key, value in result.items() if key != "line_budget"}
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.verify(removed, self.edit_contract())
        post = source_excerpt.verify_context_excerpt(self.workspace,
            {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)},
            self.edit_contract(), after_execution=True)
        self.assertFalse(post["files"][0]["changed"])

    def test_single_target_opt_in_is_byte_identical_to_legacy(self):
        rows = [self.edit_locator()]
        legacy = source_excerpt.extract_edits(self.workspace, self.edit_contract(), rows)
        self.assertEqual(legacy, source_excerpt.extract_edits(
            self.workspace, self.edit_contract(), rows, per_target_budget=True))
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.verify({**legacy, "line_budget": 120}, self.edit_contract())
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.extract_edits(self.workspace, self.edit_contract(), rows,
                                         per_target_budget=1)

    def test_multi_target_byte_and_json_caps_still_defer_complete_definitions(self):
        names = [self.name, "pkg/second.py"]
        rows = [self.edit_locator(1), self.edit_locator(1, "second", names[1])]
        contract = self.edit_contract(names)
        for name in names:
            (self.workspace / name).write_text("def target():\n    return '" + "x" * 8000 + "'\n")
        result = source_excerpt.extract_edits(self.workspace, contract, rows,
                                             per_target_budget=True)
        self.assertEqual(result["deferred_locators"], [rows[1]])
        self.verify(result, contract)
        (self.workspace / names[0]).write_text(
            "def target():\n    return '" + "\u00e9" * 6000 + "'\n", encoding="utf-8")
        result = source_excerpt.extract_edits(self.workspace, contract, rows,
                                             per_target_budget=True)
        self.assertEqual(result["deferred_locators"], [rows[0]])
        self.assertLessEqual(len(json.dumps(result, sort_keys=True, ensure_ascii=True).encode()),
                             source_excerpt.MAX_ENVELOPE_BYTES)
        self.verify(result, contract)

    def test_combined_utf8_budget_and_serialized_envelope_budget(self):
        names = [self.name, "pkg/second.py"]
        rows = [self.edit_locator(1), self.edit_locator(1, "second", names[1])]
        contract = self.edit_contract(names)
        template = "def target():\n    return '{}'\n"
        fixed = len(template.format("").encode())
        room = source_excerpt.MAX_EXCERPT_BYTES - 2 * fixed
        (self.workspace / names[0]).write_text(template.format("é" * 1000), encoding="utf-8")
        (self.workspace / names[1]).write_text(template.format("x" * (room - 2000)), encoding="utf-8")
        result = source_excerpt.extract_edits(self.workspace, contract, rows)
        self.assertEqual(sum(len(span["text"].encode()) for row in result["files"]
                             for span in row["spans"]), source_excerpt.MAX_EXCERPT_BYTES)
        self.verify(result, contract)
        (self.workspace / names[1]).write_text(template.format("x" * (room - 1999)))
        partial = source_excerpt.extract_edits(self.workspace, contract, rows)
        self.assertEqual(partial["deferred_locators"], [rows[1]])
        self.assertIn("é", partial["files"][0]["spans"][0]["text"])
        self.verify(partial, contract)
        (self.workspace / self.name).write_text(template.format("é" * 6000), encoding="utf-8")
        self.assertLess((self.workspace / self.name).stat().st_size, source_excerpt.MAX_EXCERPT_BYTES)
        with self.assertRaisesRegex(source_excerpt.ExcerptBudgetExceeded, "serialized"):
            source_excerpt.extract_edits(self.workspace, self.edit_contract(), [rows[0]])

    def test_large_definition_falls_back_without_partial_context(self):
        (self.workspace / self.name).write_text("def target():\n" + "    pass\n" * 120)
        with self.assertRaisesRegex(source_excerpt.ExcerptBudgetExceeded, "ordinary source_read"):
            source_excerpt.extract_edits(self.workspace, self.edit_contract(), [self.edit_locator(1)])

    def test_support_overflow_retains_complete_decorated_target_only(self):
        source = ("def helper():\n" + "    pass\n" * 121 +
                  "    return 1\n\ndef decorator(fn):\n    return fn\n" +
                  "@decorator\ndef target():\n    return helper()\n")
        row = self.write_source(source)
        result = source_excerpt.extract_edits(self.workspace, self.edit_contract(),
                                               [self.edit_locator(row["line"])])
        self.assertEqual(result["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(result["deferred_locators"], [])
        self.assertEqual(result["files"][0]["spans"], [{"start_line": row["line"] - 1,
            "end_line": row["line"] + 1,
            "text": "@decorator\ndef target():\n    return helper()\n"}])
        self.verify(result, self.edit_contract())
        for change in ("notice", "deferred", "text", "hash"):
            tampered = copy.deepcopy(result)
            if change == "notice":
                tampered["notice"] = source_excerpt.EDIT_NOTICE
            elif change == "deferred":
                tampered["deferred_locators"] = [self.edit_locator(row["line"])]
            elif change == "text":
                tampered["files"][0]["spans"][0]["text"] += "# spoof\n"
            else:
                tampered["files"][0]["source_sha256"] = "0" * 64
            with self.subTest(change=change), self.assertRaises(caller.EmbeddedNokiyError):
                self.verify(tampered, self.edit_contract())
        with (self.workspace / self.name).open("a") as stream:
            stream.write("\nUNRELATED = 9\n")
        self.assertFalse(source_excerpt.still_current(self.workspace, result))
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.verify(result, self.edit_contract())
        post = source_excerpt.verify_context_excerpt(self.workspace,
            {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)},
            self.edit_contract(), after_execution=True)
        self.assertEqual(post["kind"], "edit_postimage")
        self.assertTrue(post["files"][0]["changed"])

    def test_nested_fallback_deduplicates_complete_definitions(self):
        self.write_source("class Box:\n    def method(self):\n        return Box()\n" +
                          "def target():\n" + "    pass\n" * 120 + "    return Box()\n")
        # The class loads the oversized target, but its two overlapping edit
        # definitions fit together in definitions-only context.
        path = self.workspace / self.name
        path.write_text(path.read_text().replace("return Box()\n", "return target()\n", 1))
        result = source_excerpt.extract_edits(self.workspace, self.edit_contract(),
            [self.edit_locator(1, "Box"), self.edit_locator(2, "Box.method")])
        self.assertEqual(result["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(result["deferred_locators"], [])
        self.assertEqual(result["files"][0]["spans"][0]["text"],
                         "class Box:\n    def method(self):\n        return target()\n")
        self.verify(result, self.edit_contract())

    def test_multiple_definitions_defer_unfitted_targets_in_canonical_order(self):
        names = [f"pkg/target{i}.py" for i in range(4)]
        rows = [self.edit_locator(1, f"target{i}", name) for i, name in enumerate(names)]
        contract = self.edit_contract(names)
        for name in names:
            (self.workspace / name).write_text("def target():\n" + "    pass\n" * 45)
        result = source_excerpt.extract_edits(self.workspace, contract, rows[::-1])
        self.assertEqual(result["deferred_locators"], rows[2:])
        self.assertEqual([bool(row["spans"]) for row in result["files"]],
                         [True, True, False, False])
        self.verify(result, contract)
        altered = copy.deepcopy(result)
        altered["deferred_locators"].reverse()
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.verify(altered, contract)

    def test_unicode_support_overflow_respects_json_envelope(self):
        # The source bytes fit, but escaped Unicode in the full JSON does not.
        source = ("SUPPORT = '" + "é" * 6000 + "'\n"
                  "def target():\n    return SUPPORT\n")
        row = self.write_source(source)
        result = source_excerpt.extract_edits(self.workspace, self.edit_contract(),
                                               [self.edit_locator(row["line"])])
        self.assertEqual(result["notice"], source_excerpt.PARTIAL_EDIT_NOTICE)
        self.assertEqual(result["files"][0]["spans"][0]["text"],
                         "def target():\n    return SUPPORT\n")
        self.assertLessEqual(len(json.dumps(result, sort_keys=True, ensure_ascii=True).encode()),
                             source_excerpt.MAX_ENVELOPE_BYTES)
        self.verify(result, self.edit_contract())

    def test_oversized_first_target_does_not_mask_later_malformed_file(self):
        later = "pkg/second.py"
        (self.workspace / self.name).write_text("def target():\n" + "    pass\n" * 120)
        (self.workspace / later).write_text("def target(:\n    pass\n")
        rows = [self.edit_locator(1), self.edit_locator(1, "second", later)]
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            source_excerpt.extract_edits(self.workspace, self.edit_contract([self.name, later]), rows)
        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)

    def test_edit_spans_preserve_physical_lines_and_original_bytes(self):
        raw = "VALUE = 'a\u2028b'\r\ndef target():\r\n    return VALUE\r\n".encode("utf-8")
        (self.workspace / self.name).write_bytes(raw)
        result = source_excerpt.extract_edits(self.workspace, self.edit_contract(),
                                            [self.edit_locator(2)])
        self.assertEqual(result["files"][0]["spans"],
                         [{"start_line": 1, "end_line": 3, "text": raw.decode("utf-8")}])
        self.verify(result, self.edit_contract())

    def test_invalid_edit_scopes_and_navigation_fail_before_source_reads(self):
        contract = self.edit_contract()
        row = self.edit_locator()
        changes = ({"read_scopes": ["pkg/**"]}, {"write_scopes": ["pkg/**"]},
                   {"read_scopes": ["pkg/extra.py"]}, {"write_scopes": []},
                   {"read_scopes": self.name}, {"write_scopes": [self.name, self.name]},
                   {"allowed_operations": ["read", "modify", "delete"]},
                   {"allowed_operations": ["read"]}, {"denied_operations": ["read"]},
                   {"denied_operations": ["modify"]}, {"source_read": False},
                   {"source_read": 1}, {"dcf_generation": None},
                   {"dcf_generation": {"generation_id": "old"}})
        invalid_rows = ([], [row] * 5, [{**row, "line": True}], [{**row, "line": 0}],
                        [{**row, "generation_id": "old"}], [{**row, "target": "fuzzy"}],
                        [row, {**row, "line": 4}], [{**row, "path": "pkg/../example.py"}],
                        [{**row, "path": "pkg/*.py"}], [{**row, "path": "pkg//example.py"}])
        for changed, rows in [(dict(contract, **change), [row]) for change in changes] + [
                (contract, rows) for rows in invalid_rows]:
            for envelope_bounded in (False, True):
                with self.subTest(contract=changed, rows=rows, envelope_bounded=envelope_bounded):
                    with patch.object(source_excerpt, "_read_exact") as read:
                        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
                            source_excerpt.extract_edits(self.workspace, changed, rows,
                                                         envelope_bounded=envelope_bounded)
                        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)
                        read.assert_not_called()

    def test_symlinks_and_bad_later_locator_are_not_capacity_fallbacks(self):
        (self.workspace / self.name).write_text("def target():\n" + "    pass\n" * 120)
        link = "pkg/zlink.py"
        (self.workspace / link).symlink_to("example.py")
        contract = self.edit_contract([self.name, link])
        rows = [self.edit_locator(1), self.edit_locator(1, "link", link)]
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            source_excerpt.extract_edits(self.workspace, contract, rows)
        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)
        (self.workspace / link).unlink()
        (self.workspace / link).write_text("VALUE = 1\n")
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            source_excerpt.extract_edits(self.workspace, contract, rows)
        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)
        (self.workspace / "alias").symlink_to("pkg", target_is_directory=True)
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.extract_edits(self.workspace, self.edit_contract(["alias/example.py"]),
                                        [self.edit_locator(1, name="alias/example.py")])

    def test_changed_source_and_tampered_edit_envelopes_fail_closed(self):
        contract = self.edit_contract()
        result = source_excerpt.extract_edits(self.workspace, contract, [self.edit_locator()])
        self.verify(result, contract)
        for change in ("text", "hash", "overlap", "bool", "files", "notice", "locators"):
            bad = copy.deepcopy(result)
            if change == "text":
                bad["files"][0]["spans"][0]["text"] = "VALUE = 9\n"
            elif change == "hash":
                bad["files"][0]["source_sha256"] = "0" * 64
            elif change == "overlap":
                bad["files"][0]["spans"] *= 2
            elif change == "bool":
                bad["files"][0]["spans"][0]["start_line"] = True
            elif change == "files":
                bad["files"] *= 2
            elif change == "notice":
                bad["notice"] = "Complete post-edit source"
            else:
                bad["locators"] *= 2
            with self.subTest(change=change), self.assertRaises(caller.EmbeddedNokiyError):
                self.verify(bad, contract)
        # Even a change outside the displayed spans invalidates the whole-file SHA.
        with (self.workspace / self.name).open("a") as stream:
            stream.write("\nUNRELATED = 9\n")
        self.assertFalse(source_excerpt.still_current(self.workspace, result))
        with self.assertRaises(caller.EmbeddedNokiyError) as raised:
            self.verify(result, contract)
        self.assertNotIsInstance(raised.exception, source_excerpt.ExcerptBudgetExceeded)

    def test_realistic_multifunction_edit_requires_fresh_readback(self):
        source = ("import json\n"
                  "DEFAULT_LIMIT = 20\n\n"
                  "def normalize_limit(value):\n"
                  "    return max(1, min(int(value), DEFAULT_LIMIT))\n\n"
                  "def render_page(items, limit):\n"
                  "    count = normalize_limit(limit)\n"
                  "    return json.dumps(items[:count])\n\n"
                  "def target(items, request):\n"
                  "    return render_page(items, request.get('limit', DEFAULT_LIMIT))\n")
        self.write_source(source)
        rows = [self.edit_locator(4, "normalize_limit"), self.edit_locator(7, "render_page"),
                self.edit_locator(11)]
        contract = self.edit_contract()
        result = source_excerpt.extract_edits(self.workspace, contract, rows)
        self.verify(result, contract)
        text = "".join(span["text"] for span in result["files"][0]["spans"])
        for symbol in ("normalize_limit", "render_page", "target"):
            self.assertEqual(text.count("def " + symbol + "("), 1)
        self.assertIn("fresh readback", result["notice"])
        edited = source.replace("DEFAULT_LIMIT = 20", "DEFAULT_LIMIT = 50").replace(
            "int(value)", "int(value or DEFAULT_LIMIT)")
        (self.workspace / self.name).write_text(edited)
        with self.assertRaisesRegex(caller.EmbeddedNokiyError, "preimage changed"):
            self.verify(result, contract)
        refreshed = source_excerpt.extract_edits(self.workspace, contract, rows)
        self.verify(refreshed, contract)
        self.assertNotEqual(result["files"][0]["source_sha256"], refreshed["files"][0]["source_sha256"])

    def test_edit_context_does_not_read_additional_authorized_test_or_task_files(self):
        contract = self.edit_contract()
        contract["read_scopes"] += ["TASK.md", "tests/test_example.py"]
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract_edits(self.workspace, contract, [self.edit_locator()])
        read.assert_called_once_with(self.workspace, self.name)
        self.verify(result, contract)

    def test_post_execution_edit_readback_records_changed_and_unchanged_files(self):
        names = [self.name, "pkg/second.py"]
        (self.workspace / names[1]).write_text("def second(): return 1\n")
        contract = self.edit_contract(names)
        result = source_excerpt.extract_edits(self.workspace, contract,
            [self.edit_locator(), self.edit_locator(1, "second", names[1])])
        context = {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)}
        source_excerpt.verify_context_excerpt(self.workspace, context, contract)
        (self.workspace / self.name).write_text("def target(): return 99\n")
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.verify_context_excerpt(self.workspace, context, contract)
        readback = source_excerpt.verify_context_excerpt(
            self.workspace, context, contract, after_execution=True)
        self.assertEqual(readback["mission_acceptance"], "parent_owned")
        self.assertEqual([row["changed"] for row in readback["files"]], [True, False])
        self.assertEqual(readback["files"][0]["postimage_sha256"],
                         hashlib.sha256((self.workspace / self.name).read_bytes()).hexdigest())
        (self.workspace / self.name).unlink()
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.verify_context_excerpt(self.workspace, context, contract, after_execution=True)
        (self.workspace / self.name).symlink_to("second.py")
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.verify_context_excerpt(self.workspace, context, contract, after_execution=True)

    def test_post_execution_readonly_excerpt_still_rejects_drift(self):
        result = source_excerpt.extract(self.workspace, self.contract, self.locator)
        context = {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)}
        (self.workspace / self.name).write_text("def target(): return 99\n")
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.verify_context_excerpt(
                self.workspace, context, self.contract, after_execution=True)


class ReadonlyOutlineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.source = self.workspace / "example.py"
        self.source.write_text(
            "@decorate\nclass Service:\n    async def run(self):\n"
            "        def inner():\n            return 'PRIVATE_BODY'\n"
            "        return inner()\n\ndef standalone():\n    pass\n"
        )
        self.contract = {"source_read": True, "allowed_operations": ["read", "command"],
                         "denied_operations": ["modify", "create", "delete"],
                         "read_scopes": ["example.py"], "write_scopes": [],
                         "command_templates": [], "dcf_generation": {"generation_id": "current"}}

    def write_sources(self, sources):
        self.contract["read_scopes"] = list(sources)
        for name, text in sources.items():
            path = self.workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_exact_source_outline_contains_all_qualified_ranges_but_no_bodies(self):
        before = copy.deepcopy(self.contract)
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract_outline(self.workspace, self.contract)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(result["files"], [{"path": "example.py",
            "source_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "definitions": [["Service", "c", 1, 6],
                            ["Service.run", "f", 3, 6],
                            ["Service.run.inner", "f", 4, 5],
                            ["standalone", "f", 8, 9]]}])
        self.assertNotIn("PRIVATE_BODY", json.dumps(result))
        self.assertEqual(self.contract, before)
        context = {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)}
        source_excerpt.verify_context_excerpt(self.workspace, context, self.contract)

    def test_outline_rejects_tampering_and_source_drift_before_and_after_execution(self):
        result = source_excerpt.extract_outline(self.workspace, self.contract)
        changed = copy.deepcopy(result)
        changed["files"][0]["definitions"][0][2] = True
        for after in (False, True):
            with self.subTest(after=after), self.assertRaises(caller.EmbeddedNokiyError):
                source_excerpt.verify_context_excerpt(self.workspace,
                    {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(changed)},
                    self.contract, after_execution=after)
        self.source.write_text("def replaced(): pass\n")
        for after in (False, True):
            with self.subTest(after=after), self.assertRaises(caller.EmbeddedNokiyError):
                source_excerpt.verify_context_excerpt(self.workspace,
                    {"context_summary": source_excerpt.CONTEXT_MARKER + json.dumps(result)},
                    self.contract, after_execution=after)

    def test_outline_never_probes_ungranted_or_mutable_scopes(self):
        changes = [{"read_scopes": ["../example.py"]}, {"read_scopes": ["*.py"]},
                   {"read_scopes": ["example.py", "example.py"]}, {"read_scopes": ["a.txt"]},
                   {"read_scopes": []}, {"write_scopes": ["example.py"]},
                   {"source_read": False}, {"allowed_operations": ["read", "command", "modify"]},
                   {"denied_operations": ["read"]}, {"denied_operations": ["command"]},
                   {"dcf_generation": {}}, {"command_templates": ["anything"]},
                   {"verifier_commands": [{"argv": ["any"]}]}]
        for change in changes:
            with self.subTest(change=change), patch.object(source_excerpt, "_read_exact") as read:
                with self.assertRaises(caller.EmbeddedNokiyError):
                    source_excerpt.extract_outline(self.workspace, {**self.contract, **change})
                read.assert_not_called()

    def test_outline_has_no_import_execution_and_no_partial_truncation(self):
        self.source.write_text("raise RuntimeError('must not execute')\ndef valid(): pass\n")
        result = source_excerpt.extract_outline(self.workspace, self.contract)
        self.assertEqual(result["files"][0]["definitions"], [["valid", "f", 2, 2]])
        with patch.object(source_excerpt, "MAX_ENVELOPE_BYTES", 50):
            with self.assertRaises(source_excerpt.ExcerptBudgetExceeded):
                source_excerpt.extract_outline(self.workspace, self.contract)
        self.source.unlink()
        self.source.symlink_to("/etc/passwd")
        with self.assertRaises(caller.EmbeddedNokiyError):
            source_excerpt.extract_outline(self.workspace, self.contract)

    def test_outline_supplies_import_tuple_alias_and_constant_locations_together(self):
        self.write_sources({
            "example.py": (
                "from pkg import common\n"
                "import pkg.engine as direct\n"
                "from pkg.engine import compute\n"
                "from outside import forbidden\n"
                "m, require = common.m, common.require\n"
                "RAW_PINS = {'sensitive': '/never/open/this'}\n"
                "THREAD_ENV = ('PRIVATE_VALUE',)\n"
                "def target(): return m.compute()\n"
            ),
            "pkg/common.py": "from pkg import engine as m\nrequire = m.check\n",
            "pkg/engine.py": "raise RuntimeError('must not execute')\ndef compute(): pass\n",
        })
        (self.workspace / "outside.py").write_text("PRIVATE_UNGRANTED_SOURCE\n")
        with patch.object(source_excerpt, "_read_exact", wraps=source_excerpt._read_exact) as read:
            result = source_excerpt.extract_outline(self.workspace, self.contract)
        self.assertEqual([call.args[1] for call in read.call_args_list], sorted(self.contract["read_scopes"]))
        first = result["files"][0]
        self.assertEqual(first["imports"], [
            [1, 1, "from pkg import common"],
            [2, 2, "import pkg.engine as direct"],
            [3, 3, "from pkg.engine import compute"],
        ])
        self.assertEqual(first["aliases"], [[5, 5, "m, require = (common.m, common.require)"]])
        self.assertEqual(first["globals"], [[["RAW_PINS"], 6, 6], [["THREAD_ENV"], 7, 7]])
        self.assertEqual(result["files"][1]["imports"], [[1, 1, "from pkg import engine as m"]])
        self.assertEqual(result["files"][1]["aliases"], [[2, 2, "require = m.check"]])
        for excluded in ("PRIVATE_VALUE", "PRIVATE_UNGRANTED_SOURCE", "/never/open/this", "forbidden"):
            self.assertNotIn(excluded, source_excerpt.encode_context(result))
        source_excerpt.verify_context_excerpt(self.workspace,
            {"context_summary": source_excerpt.CONTEXT_MARKER + source_excerpt.encode_context(result)},
            self.contract)

    def test_relative_import_matching_uses_only_declared_granted_paths(self):
        self.write_sources({
            "pkg/sub/a.py": "from . import b\nfrom .. import root\nfrom .... import invalid\n",
            "pkg/sub/b.py": "pass\n",
            "pkg/root.py": "pass\n",
            "pkg/__init__.py": "from . import root\n",
        })
        result = source_excerpt.extract_outline(self.workspace, self.contract)
        rows = {row["path"]: row for row in result["files"]}
        self.assertEqual(rows["pkg/sub/a.py"]["imports"], [
            [1, 1, "from . import b"], [2, 2, "from .. import root"]])
        self.assertEqual(rows["pkg/__init__.py"]["imports"], [[1, 1, "from . import root"]])

    def test_bindings_do_not_resolve_dynamic_or_conditional_values_or_erase_rebindings(self):
        self.write_sources({
            "example.py": (
                "from pkg import common\n"
                "m = common.m\n"
                "m = other.m\n"
                "dynamic = load_module()\n"
                "if enabled:\n    conditional = common.m\n"
                "class Service:\n    nested = common.m\n"
                "annotated: object = common.m\n"
                "count += other.count\n"
                "def target():\n    local = common.m\n"
            ), "pkg/common.py": "pass\n",
        })
        row = source_excerpt.extract_outline(self.workspace, self.contract)["files"][0]
        self.assertEqual(row["aliases"], [
            [2, 2, "m = common.m"], [3, 3, "m = other.m"], [9, 9, "annotated: object = common.m"]])
        self.assertEqual(row["globals"], [[["dynamic"], 4, 4], [["count"], 10, 10]])
        self.assertNotIn("load_module", json.dumps(row))
        self.assertNotIn("conditional", json.dumps(row))
        self.assertNotIn("nested =", json.dumps(row))
        self.assertNotIn("local =", json.dumps(row))

    def test_import_alias_and_global_metadata_are_authenticated(self):
        self.write_sources({"example.py": "import common\nm = common.m\nVALUE = 42\n",
                            "common.py": "pass\n"})
        result = source_excerpt.extract_outline(self.workspace, self.contract)
        for key in ("imports", "aliases", "globals"):
            changed = copy.deepcopy(result)
            next(row for row in changed["files"] if row["path"] == "example.py")[key][0][-1] = "tampered"
            for after in (False, True):
                with self.subTest(key=key, after=after), self.assertRaises(caller.EmbeddedNokiyError):
                    source_excerpt.verify_context_excerpt(self.workspace,
                        {"context_summary": source_excerpt.CONTEXT_MARKER + source_excerpt.encode_context(changed)},
                        self.contract, after_execution=after)

    def test_outline_distinguishes_verified_syntax_from_unread_implementation(self):
        self.write_sources({
            "example.py": "import common\ncheck = common.check\nVALUE = 42\n"
                          "def target(): return check(VALUE)\n",
            "common.py": "def check(value):\n    return value > 0\n",
        })
        result = source_excerpt.extract_outline(self.workspace, self.contract)
        notice = result["notice"]
        self.assertIn("verified source syntax", notice)
        self.assertIn("without rereading solely to confirm that syntax", notice)
        self.assertIn("locators, not body/value evidence", notice)
        self.assertIn("do not prove runtime resolution or absence of rebinding", notice)
        self.assertIn("helper/exception definitions with their callers before final synthesis", notice)
        self.assertIn("not every symbol or whole files", notice)
        encoded = source_excerpt.encode_context(result)
        self.assertNotIn("return value > 0", encoded)
        self.assertNotIn("VALUE = 42", encoded)
        source_excerpt.verify_context_excerpt(self.workspace,
            {"context_summary": source_excerpt.CONTEXT_MARKER + encoded}, self.contract)

    def test_outline_evidence_guidance_cannot_be_replaced_by_a_claim_of_complete_bodies(self):
        result = source_excerpt.extract_outline(self.workspace, self.contract)
        result["notice"] = "All listed definitions have already been read in full."
        for after in (False, True):
            with self.subTest(after=after), self.assertRaises(caller.EmbeddedNokiyError):
                source_excerpt.verify_context_excerpt(self.workspace,
                    {"context_summary": source_excerpt.CONTEXT_MARKER
                     + source_excerpt.encode_context(result)},
                    self.contract, after_execution=after)

    def test_compact_encoding_preserves_outline_data_and_other_excerpt_wire_format(self):
        result = source_excerpt.extract_outline(self.workspace, self.contract)
        encoded = source_excerpt.encode_context(result)
        self.assertEqual(json.loads(encoded), result)
        self.assertLess(len(encoded), len(json.dumps(result, sort_keys=True, ensure_ascii=True)))
        legacy = {"path": "example.py", "text": "def target(): return 1\n", "start_line": 1}
        self.assertEqual(source_excerpt.encode_context(legacy), json.dumps(legacy, sort_keys=True, ensure_ascii=True))


if __name__ == "__main__":
    unittest.main()
