import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location("verify_release", Path(__file__).resolve().parents[1] / "scripts/verify_release.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleaseVerificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "releases").mkdir()
        (self.root / "source").mkdir()
        self.file = self.root / "source/module.py"
        self.file.write_text("print('source')\n")
        self.manifest = {"components": {"source": {"files": {"module.py": hashlib.sha256(self.file.read_bytes()).hexdigest()}}}}
        self.write_manifest()
        (self.root / "releases/CURRENT.json").write_text(json.dumps({"manifest": "fixture.json"}))

    def write_manifest(self):
        (self.root / "releases/fixture.json").write_text(json.dumps(self.manifest))

    def test_valid_source(self):
        self.assertEqual(MODULE.verify(self.root), 1)

    def test_changed_source(self):
        self.file.write_text("changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            MODULE.verify(self.root)

    def test_unlisted_source(self):
        (self.root / "source/extra.py").write_text("extra")
        with self.assertRaisesRegex(ValueError, "Unlisted"):
            MODULE.verify(self.root)

    def test_traversal_and_symlinks(self):
        for path in ("../outside", "/absolute", "source/../module.py"):
            with self.assertRaises(ValueError):
                MODULE.source_path(self.root, path)
        (self.root / "source/link.py").symlink_to(self.file)
        with self.assertRaises(ValueError):
            MODULE.source_path(self.root, "source/link.py")

    def test_manifest_pointer_is_local(self):
        (self.root / "releases/CURRENT.json").write_text(json.dumps({"manifest": "../outside.json"}))
        with self.assertRaises(ValueError):
            MODULE.verify(self.root)

    def test_executable_bit(self):
        self.manifest["components"]["source"]["modes"] = {"module.py": "100755"}
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "Executable bit mismatch"):
            MODULE.verify(self.root)
        self.file.chmod(0o755)
        self.assertEqual(MODULE.verify(self.root), 1)


if __name__ == "__main__":
    unittest.main()
