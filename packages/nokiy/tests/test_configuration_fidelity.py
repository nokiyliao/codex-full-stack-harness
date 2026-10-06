# SPDX-License-Identifier: MIT
"""Focused tests for configured-file evidence, with no native configuration IO."""

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_collaboration_harness.configuration_fidelity import (
    ConfigurationParseError,
    ConfigurationReadError,
    InvalidConfigurationSnapshot,
    compare_configurations,
    snapshot_configuration,
)


class ConfigurationFidelityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def snapshot(self, text, name="sample.toml"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return snapshot_configuration(path)

    def assert_boundary(self, evidence):
        self.assertIs(evidence["diagnostic_only"], True)
        self.assertEqual(evidence["permission_effect"], "none")
        self.assertEqual(evidence["comparison_eligibility"], "not_granted")
        self.assertEqual(evidence["writer"], "unknown")
        self.assertEqual(evidence["provenance"]["kind"], "configured_file")
        self.assertIs(evidence["provenance"]["native_effective_settings"], False)

    def test_reads_given_file_once_and_tags_provenance(self):
        path = self.root / "only.toml"
        raw = b"answer = 42\n"
        with patch.object(Path, "read_bytes", autospec=True, return_value=raw) as reader:
            evidence = snapshot_configuration(path)
        reader.assert_called_once_with(path)
        self.assertEqual(evidence["schema"], "configuration_fidelity_snapshot_v1")
        self.assertEqual(evidence["provenance"]["path"], str(path))
        self.assertEqual(evidence["raw_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(len(evidence["canonical_parsed_sha256"]), 64)
        self.assert_boundary(evidence)

    def test_unchanged_content_and_json_roundtrip(self):
        before = self.snapshot("answer = 42\n")
        after = json.loads(json.dumps(self.snapshot("answer = 42\n")))
        result = compare_configurations(before, after)
        self.assertEqual(result["schema"], "configuration_fidelity_comparison_v1")
        self.assertIs(result["raw_identical"], True)
        self.assertIs(result["semantic_identical"], True)
        for field in ("changed_paths", "added_paths", "removed_paths"):
            self.assertEqual(result[field], [])
        self.assert_boundary(result)
        json.dumps(result, allow_nan=False)

    def test_formatting_and_table_order_do_not_change_parsed_identity(self):
        before = self.snapshot('answer = 0x2a\nname = "demo"\n[table]\ny = 2\nx = 1\n')
        after = self.snapshot("# comment\nname = 'demo'\nanswer = 4_2\n[table]\nx=1\ny=2\n")
        result = compare_configurations(before, after)
        self.assertIs(result["raw_identical"], False)
        self.assertIs(result["semantic_identical"], True)
        self.assertEqual(result["changed_paths"], [])

    def test_nested_and_literal_dotted_keys_cannot_alias(self):
        before = self.snapshot(
            '"model.name" = "old-literal"\n[model]\nname = "old-nested"\n'
            '"effort.level" = "old-deep-literal"\n'
        )
        after = self.snapshot(
            '"model.name" = "new-literal"\n[model]\nname = "new-nested"\n'
            '"effort.level" = "new-deep-literal"\n'
        )
        paths = [["model", "effort.level"], ["model", "name"], ["model.name"]]
        self.assertEqual([item["path"] for item in before["fingerprints"]], paths)
        self.assertEqual(compare_configurations(before, after)["changed_paths"], paths)

    def test_empty_string_keys_are_valid_path_components(self):
        before = self.snapshot('"" = 1\n[table]\n"" = 2\n')
        after = self.snapshot('"" = 3\n[table]\n"" = 4\n')
        self.assertEqual(
            compare_configurations(before, after)["changed_paths"], [[""], ["table", ""]]
        )

    def test_added_removed_keys_and_empty_tables(self):
        before = self.snapshot("kept = 1\nremoved = 2\n[empty_removed]\n[fill]\n")
        after = self.snapshot("kept = 1\nadded = 3\n[empty_added]\n[fill]\nchild = 4\n")
        result = compare_configurations(before, after)
        self.assertEqual(result["changed_paths"], [])
        self.assertEqual(result["added_paths"], [["added"], ["empty_added"], ["fill", "child"]])
        self.assertEqual(result["removed_paths"], [["empty_removed"], ["fill"], ["removed"]])
        self.assertIs(result["semantic_identical"], False)

    def test_empty_root_and_nested_empty_tables_are_observable(self):
        empty = self.snapshot("")
        comment = self.snapshot("# still empty\n")
        nested = self.snapshot("[outer.inner]\n")
        self.assertEqual([item["path"] for item in empty["fingerprints"]], [[]])
        self.assertIs(compare_configurations(empty, comment)["semantic_identical"], True)
        forward = compare_configurations(empty, nested)
        self.assertEqual(forward["removed_paths"], [[]])
        self.assertEqual(forward["added_paths"], [["outer", "inner"]])
        backward = compare_configurations(nested, empty)
        self.assertEqual(backward["added_paths"], [[]])
        self.assertEqual(backward["removed_paths"], [["outer", "inner"]])

    def test_scalar_list_and_date_types_are_distinct(self):
        pairs = [
            ("true", "1"),
            ("1", "1.0"),
            ("1.0", '"1.0"'),
            ("1", "[1]"),
            ("[1]", "[true]"),
            ("[]", '""'),
            ("[1, 2]", "[2, 1]"),
            ("1979-05-27", '"1979-05-27"'),
            ("07:32:00", '"07:32:00"'),
            ("1979-05-27T07:32:00", "1979-05-27"),
            ("1979-05-27T07:32:00", "1979-05-27T07:32:00Z"),
        ]
        for old, new in pairs:
            with self.subTest(old=old, new=new):
                result = compare_configurations(
                    self.snapshot(f"value = {old}\n"), self.snapshot(f"value = {new}\n")
                )
                self.assertIs(result["semantic_identical"], False)
                self.assertEqual(result["changed_paths"], [["value"]])

    def test_special_floats_are_deterministic_and_type_sensitive(self):
        pairs = [
            ("nan", "+nan", True),
            ("inf", "+inf", True),
            ("1.0", "1e0", True),
            ("nan", "-nan", False),
            ("inf", "-inf", False),
            ("inf", "nan", False),
            ("1.0", "inf", False),
            ("0.0", "-0.0", False),
        ]
        for old, new, identical in pairs:
            with self.subTest(old=old, new=new):
                before = self.snapshot(f"value = {old}\n")
                after = self.snapshot(f"value = {new}\n")
                self.assertIs(compare_configurations(before, after)["semantic_identical"], identical)
                json.dumps(before, allow_nan=False)

    def test_datetime_and_string_spelling_normalize_without_values(self):
        before = self.snapshot('value = 1979-05-27T07:32:00.100Z\ntext = "\\u0061"\n')
        after = self.snapshot("value = 1979-05-27 07:32:00.1+00:00\ntext = 'a'\n")
        result = compare_configurations(before, after)
        self.assertIs(result["raw_identical"], False)
        self.assertIs(result["semantic_identical"], True)

    def test_arrays_of_tables_are_canonical_lists_not_invented_key_paths(self):
        before = self.snapshot('[[providers]]\nname = "p"\nmodel = "old"\n')
        reordered = self.snapshot('[[providers]]\nmodel = "old"\nname = "p"\n')
        changed = self.snapshot('[[providers]]\nmodel = "new"\nname = "p"\n')
        self.assertIs(compare_configurations(before, reordered)["semantic_identical"], True)
        self.assertEqual(compare_configurations(before, changed)["changed_paths"], [["providers"]])

    def test_model_provider_effort_tier_requirements_approval_and_sandbox_drift(self):
        before = self.snapshot(
            'model = "old-model"\nmodel_provider = "old-provider"\n'
            'model_reasoning_effort = "low"\nservice_tier = "old-tier"\n'
            'approval_policy = "untrusted"\nsandbox_mode = "read-only"\n'
            '[requirements]\nallow_network = false\n'
            '[model_providers.private]\nbase_url = "old-endpoint"\n'
        )
        after = self.snapshot(
            'model = "new-model"\nmodel_provider = "new-provider"\n'
            'model_reasoning_effort = "high"\nservice_tier = "new-tier"\n'
            'approval_policy = "never"\nsandbox_mode = "workspace-write"\n'
            '[requirements]\nallow_network = true\n'
            '[model_providers.private]\nbase_url = "new-endpoint"\n'
        )
        result = compare_configurations(before, after)
        self.assertEqual(result["changed_paths"], [
            ["approval_policy"], ["model"], ["model_provider"],
            ["model_providers", "private", "base_url"], ["model_reasoning_effort"],
            ["requirements", "allow_network"], ["sandbox_mode"], ["service_tier"],
        ])
        self.assert_boundary(result)

    def test_snapshots_and_diagnostics_never_include_plaintext_values(self):
        secrets = ["CANARY_token_73", "CANARY_list_74", "CANARY_nested_75", "CANARY_table_76"]
        text = (
            f'token = "{secrets[0]}"\nitems = ["{secrets[1]}"]\n'
            f'[auth]\npassword = "{secrets[2]}"\n'
            f'[[providers]]\ncredential = "{secrets[3]}"\n'
        )
        before = self.snapshot(text)
        after = self.snapshot(text.replace("CANARY", "CHANGED"))
        serialized = json.dumps([before, after, compare_configurations(before, after)])
        for secret in secrets + [secret.replace("CANARY", "CHANGED") for secret in secrets]:
            self.assertNotIn(secret, serialized)
        for entry in before["fingerprints"]:
            self.assertEqual(set(entry), {"path", "sha256"})

    def test_comparison_is_pure_and_different_files_do_not_grant_eligibility(self):
        before = self.snapshot("value = 1\n", "before.toml")
        after = self.snapshot("value = 1\n", "after.toml")
        originals = copy.deepcopy([before, after])
        with patch.object(Path, "read_bytes", side_effect=AssertionError("Unexpected read")):
            result = compare_configurations(before, after)
        self.assertEqual([before, after], originals)
        self.assertIs(result["raw_identical"], True)
        self.assertNotEqual(result["provenance"]["before_path"], result["provenance"]["after_path"])
        self.assert_boundary(result)

    def test_malformed_toml_errors_are_typed_and_sanitized(self):
        secret = "CANARY_malformed_secret_77"
        for text in (f'token = "{secret}"\nbroken = [\n', f'token = "{secret}"\ntoken = 1\n'):
            with self.subTest(text_type="invalid TOML"):
                with self.assertRaises(ConfigurationParseError) as raised:
                    self.snapshot(text)
                self.assertNotIn(secret, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

    def test_invalid_utf8_is_a_sanitized_parse_error(self):
        path = self.root / "invalid.toml"
        path.write_bytes(b"\xffCANARY_invalid_utf8_78")
        with self.assertRaises(ConfigurationParseError) as raised:
            snapshot_configuration(path)
        self.assertNotIn("CANARY", str(raised.exception))
        self.assertIsNone(raised.exception.__context__)

    def test_read_errors_do_not_echo_paths_or_os_messages(self):
        secret = "CANARY_os_message_79"
        for error in (PermissionError(secret), FileNotFoundError(secret), ValueError(secret)):
            with self.subTest(error_type=type(error).__name__):
                with patch.object(Path, "read_bytes", side_effect=error):
                    with self.assertRaises(ConfigurationReadError) as raised:
                        snapshot_configuration(self.root / f"{secret}.toml")
                self.assertNotIn(secret, str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.assertIsNone(raised.exception.__context__)

    def test_non_path_input_is_rejected_without_echo(self):
        with self.assertRaises(ConfigurationReadError) as raised:
            snapshot_configuration("CANARY_not_a_path_80")
        self.assertNotIn("CANARY", str(raised.exception))

    def test_malformed_comparison_snapshots_are_rejected_without_echo(self):
        valid = self.snapshot("a = 1\nb = 2\n")
        malformed = [None, [], {}, "CANARY_invalid_snapshot_81"]

        def add_case(edit):
            evidence = copy.deepcopy(valid)
            edit(evidence)
            malformed.append(evidence)

        add_case(lambda item: item.pop("schema"))
        add_case(lambda item: item.update(schema="CANARY_wrong_schema_82"))
        add_case(lambda item: item.update(extra="CANARY_extra_field_83"))
        add_case(lambda item: item.update(diagnostic_only=1))
        add_case(lambda item: item.update(permission_effect="granted"))
        add_case(lambda item: item.update(comparison_eligibility="granted"))
        add_case(lambda item: item.update(writer="CANARY_inferred_writer_84"))
        add_case(lambda item: item.update(raw_sha256="short"))
        add_case(lambda item: item.update(raw_sha256="A" * 64))
        add_case(lambda item: item.update(canonical_parsed_sha256="0" * 64))
        add_case(lambda item: item.update(fingerprints=[]))
        add_case(lambda item: item["provenance"].update(native_effective_settings=True))
        add_case(lambda item: item["provenance"].update(path=None))
        add_case(lambda item: item["fingerprints"][0].update(path="a"))
        add_case(lambda item: item["fingerprints"][0].update(path=[1]))
        add_case(lambda item: item["fingerprints"][0].update(path=[]))
        add_case(lambda item: item["fingerprints"][0].update(sha256=123))
        add_case(lambda item: item["fingerprints"][0].update(sha256="x" * 64))
        add_case(lambda item: item["fingerprints"][0].update(value="CANARY_injected_value_85"))
        add_case(lambda item: item["fingerprints"].append(copy.deepcopy(item["fingerprints"][0])))
        add_case(lambda item: item["fingerprints"].reverse())
        add_case(lambda item: item["fingerprints"][1].update(path=["a", "child"]))
        for index, evidence in enumerate(malformed):
            with self.subTest(case=index):
                for before, after in ((valid, evidence), (evidence, valid)):
                    with self.assertRaises(InvalidConfigurationSnapshot) as raised:
                        compare_configurations(before, after)
                    self.assertNotIn("CANARY", str(raised.exception))

    def test_fingerprint_tampering_is_detected_by_manifest_digest(self):
        before = self.snapshot("value = 1\n")
        tampered = copy.deepcopy(before)
        tampered["fingerprints"][0]["sha256"] = "0" * 64
        with self.assertRaises(InvalidConfigurationSnapshot):
            compare_configurations(before, tampered)

    def test_identical_raw_hash_with_different_parsed_content_is_rejected(self):
        before = self.snapshot("value = 1\n")
        after = self.snapshot("value = 2\n")
        after["raw_sha256"] = before["raw_sha256"]
        with self.assertRaises(InvalidConfigurationSnapshot):
            compare_configurations(before, after)


if __name__ == "__main__":
    unittest.main()
