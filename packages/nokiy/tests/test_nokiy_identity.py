# SPDX-License-Identifier: MIT
"""Owned names may change; durable identities and external ABI must not."""
from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_core, native_nokiy

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "codex_collaboration_harness"


class NokiyIdentityTests(unittest.TestCase):
    def test_all_packaged_cli_targets_resolve_under_nokiy(self):
        scripts = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["scripts"]
        self.assertEqual(set(scripts), {"nokiy-taskpacket", "nokiy-graph-tool", "nokiy-embedded-run"})
        for target in scripts.values():
            module, symbol = target.split(":")
            self.assertTrue(callable(getattr(importlib.import_module(module), symbol)))
        self.assertEqual(full_core.EXECUTOR_NAME, "nokiy")

    def test_public_python_names_and_source_paths_have_no_old_brand(self):
        import codex_collaboration_harness as package
        from codex_collaboration_harness import adapters
        for surface in (package, adapters):
            for name in surface.__all__:
                self.assertNotIn("tura", name.lower())
                self.assertTrue(hasattr(surface, name), name)
        for path in PACKAGE.rglob("*"):
            if "__pycache__" not in path.parts and path.is_file():
                self.assertNotIn("tura", path.relative_to(PACKAGE).as_posix().lower())

    def test_mcp_tool_names_are_nokiy(self):
        tree = ast.parse((PACKAGE / "graph_entry.py").read_text())
        names = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        self.assertTrue({"nokiy_graph_execute", "nokiy_graph_prepare"} <= names)
        self.assertFalse({"tura_graph_execute", "tura_graph_prepare"} & names)

    def test_historical_terminal_is_read_without_rebranding_or_launch(self):
        digest = "a" * 64
        request_id = "tura_embedded_" + digest
        terminal = {"schema_version": "tura_embedded_terminal_v1", "request_id": request_id,
                    "request_sha256": digest, "executor_name": "TaskCore",
                    "execution_backend": "tura_full_core", "status": "RESULT_AVAILABLE"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = root / request_id
            directory.mkdir()
            path = directory / "terminal.json"
            path.write_text(json.dumps(terminal))
            before = path.read_bytes()
            with patch.object(caller.subprocess, "Popen", side_effect=AssertionError("must not launch")):
                self.assertEqual(caller.read_terminal(root, request_id), terminal)
            self.assertEqual(path.read_bytes(), before)

    def test_wire_and_callback_identity_values_are_stable(self):
        self.assertEqual(caller.REQUEST_SCHEMA_VERSION, "tura_embedded_request_v3")
        self.assertEqual(native_nokiy.NATIVE_NOKIY_TERMINAL_MARKER, "[TURA_NATIVE_TERMINAL_V1]")
        self.assertEqual(caller.NATIVE_TERMINAL_SCHEMA, "tura_native_codex_terminal_envelope_v2")
        self.assertTrue({"tura_router", "tura_session_db", "tura_exec"} <= full_core.REQUIRED)

    def test_backend_executable_check_survives_public_rename(self):
        from test_embedded_nokiy import EmbeddedNokiyFixture
        fixture = EmbeddedNokiyFixture()
        fixture.setUp()
        try:
            (fixture.runtime / "tura_router").chmod(0o600)
            with self.assertRaisesRegex(caller.EmbeddedNokiyError, "not executable"):
                caller.preflight(caller.load_request(fixture.request_path))
        finally:
            fixture.tearDown()


if __name__ == "__main__":
    unittest.main()
