import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_collaboration_harness.full_core import _provider_prompt


class InspectSQLiteGuidanceTests(unittest.TestCase):
    def test_live_wal_requires_readonly_not_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.sqlite"
            writer = sqlite3.connect(path)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("PRAGMA wal_autocheckpoint=0")
                writer.execute("CREATE TABLE facts(value INTEGER)")
                writer.commit()
                writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                writer.execute("INSERT INTO facts VALUES (42)")
                writer.commit()
                for suffix, expected in (("?mode=ro", 1), ("?mode=ro&immutable=1", 0)):
                    reader = sqlite3.connect(path.as_uri() + suffix, uri=True)
                    try:
                        reader.execute("PRAGMA query_only=ON")
                        self.assertEqual(reader.execute("SELECT count(*) FROM facts").fetchone()[0], expected)
                        with self.assertRaises(sqlite3.OperationalError):
                            reader.execute("INSERT INTO facts VALUES (99)")
                    finally:
                        reader.close()
            finally:
                writer.close()

    def test_worker_guidance_preserves_live_wal_visibility(self):
        prompt = _provider_prompt("Inspect current facts", {
            "allowed_operations": ["read", "command"],
            "inspect_shell": {"evidence_root": "/evidence", "network": "none"}})
        self.assertIn("PRAGMA query_only=ON", prompt)
        self.assertIn("verified immutable snapshot", prompt)
        self.assertNotIn("file:<path>?mode=ro&immutable=1", prompt)
