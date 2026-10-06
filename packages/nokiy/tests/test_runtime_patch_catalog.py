"""Validate the shipped patch examples against the current command-run contract."""
import json
import os
from pathlib import Path
import re
import unittest


class RuntimePatchCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        entry = Path("/Users/nokiy/.local/bin/nokiy-embedded-run").resolve()
        image = json.loads((entry.parents[2] / "runtime-image.json").read_text())
        artifacts = image["artifacts"]
        prompt = next(Path(v["path"]) for v in artifacts.values()
                      if v["path"].endswith("/commands/apply_patch/prompt.md"))
        schema = next(Path(v["path"]) for v in artifacts.values()
                      if v["path"].endswith("/command_run/schema.json"))
        cls.prompt_path = Path(os.environ.get("NOKIY_TEST_PATCH_PROMPT", str(prompt)))
        cls.schema_path = Path(os.environ.get("NOKIY_TEST_COMMAND_SCHEMA", str(schema)))
        cls.text = cls.prompt_path.read_text()
        cls.schema = json.loads(cls.schema_path.read_text())["input_schema"]
        cls.examples = [json.loads(raw) for raw in re.findall(
            r"```json\n(.*?)\n```", cls.text, re.DOTALL)]

    def assert_current_shape(self, value):
        self.assertIsInstance(value, dict)
        self.assertTrue(set(self.schema["required"]).issubset(value))
        self.assertTrue(set(value).issubset(self.schema["properties"]))
        commands = value["commands"]
        spec = self.schema["properties"]["commands"]
        self.assertIsInstance(commands, list)
        self.assertGreaterEqual(len(commands), spec["minItems"])
        self.assertLessEqual(len(commands), spec["maxItems"])
        item = spec["items"]
        for command in commands:
            self.assertTrue(set(item["required"]).issubset(command))
            self.assertTrue(set(command).issubset(item["properties"]))
            self.assertEqual(command["command_type"], "apply_patch")
            self.assertIsInstance(command["command_line"], str)
            self.assertIs(type(command["step"]), int)
            self.assertGreaterEqual(command["step"], item["properties"]["step"]["minimum"])

    def test_every_example_matches_current_schema(self):
        self.assertEqual(len(self.examples), 2)
        for example in self.examples:
            with self.subTest(example=example):
                self.assert_current_shape(example)

    def test_patch_payload_is_raw_not_shell(self):
        for example in self.examples:
            for command in example["commands"]:
                patch = command["command_line"]
                self.assertTrue(patch.startswith("*** Begin Patch\n"))
                self.assertTrue(patch.endswith("*** End Patch\n"))
                self.assertNotIn("<<", patch)

    def test_no_concurrent_patches_to_the_same_file(self):
        for example in self.examples:
            targets = set()
            for command in example["commands"]:
                paths = re.findall(r"^\*\*\* Update File: (.+)$",
                                   command["command_line"], re.MULTILINE)
                self.assertTrue(paths)
                for path in paths:
                    key = (command["step"], path)
                    self.assertNotIn(key, targets)
                    targets.add(key)

    def test_legacy_outer_wrapper_is_rejected(self):
        with self.assertRaises(AssertionError):
            self.assert_current_shape({"requests": self.examples[0]})

    def test_legacy_command_alias_is_rejected(self):
        value = json.loads(json.dumps(self.examples[0]))
        command = value["commands"][0]
        command["command"] = command.pop("command_type")
        with self.assertRaises(AssertionError):
            self.assert_current_shape(value)

    def test_examples_retain_independent_batching_and_ordered_edits(self):
        self.assertEqual([c["step"] for c in self.examples[0]["commands"]], [1, 1])
        self.assertEqual([c["step"] for c in self.examples[1]["commands"]], [1, 2])


if __name__ == "__main__":
    unittest.main()
