"""The host must know every typed preparation issue the installed caller can emit.

One unknown (path, reason) pair makes the host drop every issue of a rejection, so the
coordinator would see an untyped failure and guess. The caller is released separately, so
this compares the live host source with the caller that ~/.local/bin/nokiy-embedded-run runs.
"""
import importlib.util
import json
from pathlib import Path
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'nokiy-native-host.py'
SPEC = importlib.util.spec_from_file_location('nokiy_vocabulary_host', SOURCE)
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)
ENTRY = Path.home() / '.local' / 'bin' / 'nokiy-embedded-run'


class IssueVocabularyTests(unittest.TestCase):
    def test_installed_caller_issue_pairs_are_known_to_the_host(self):
        if not ENTRY.exists():
            self.skipTest('no installed Nokiy caller')
        python = ENTRY.resolve().parents[1] / 'bin' / 'python'
        if not python.exists():
            self.skipTest('installed caller has no interpreter')
        result = subprocess.run([str(python), '-I', '-B', '-c',
            'import json; from codex_collaboration_harness import embedded_nokiy as e; '
            'print(json.dumps(sorted(e._ACTION_ISSUE_PAIRS)))'],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        caller_pairs = {tuple(pair) for pair in json.loads(result.stdout)}
        self.assertTrue(caller_pairs)
        self.assertEqual(caller_pairs - host._ACTION_ISSUE_PAIRS, set())


if __name__ == '__main__':
    unittest.main()
