"""Offline behavioral and authority regression contract for the real caller copy."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import local_context as local
from codex_collaboration_harness import source_excerpt


class SourceContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.file = self.root / 'input.txt'
        self.file.write_text(''.join('line %03d\n' % n for n in range(1, 502)))
        self.action = {
            'mission': dict(mission_id='i26', task_id='a', mode='DELIVERY',
                            objective='Deliver exact source context', current_predicate='context_ready'),
            'context_summary': 'Read only the admitted exact source.',
            'operations': ['read', 'command'], 'read_scopes': ['input.txt'],
            'write_scopes': [], 'target_paths': ['input.txt'],
            'command_templates': [{'argv': ['cat', 'input.txt'], 'effects': ['read'],
                'targets': [{'operation': 'read', 'path': 'input.txt', 'argv_index': 1}]}],
        }

    def ranges(self, pairs):
        self.action['source_sections'] = [dict(path='input.txt', start_line=a, end_line=b) for a, b in pairs]

    def compile(self):
        return local.compile_context(self.root, self.action)

    def text(self, capsule):
        return json.loads(capsule['context_summary'].split(local._SECTIONS_MARKER)[1])

    def test_sixteen_small_ranges_fit_without_new_grants(self):
        plain = self.compile()[1]
        self.ranges([(n, n) for n in range(1, 17)])
        capsule, contract = self.compile()
        local.verify_context(self.root, capsule, contract)
        self.assertEqual(len(self.text(capsule)), 16)
        self.assertEqual(contract['authorization_semantic_sha256'], plain['authorization_semantic_sha256'])
        for key in ('read_scopes', 'write_scopes', 'allowed_operations', 'declared_targets'):
            self.assertEqual(contract[key], plain[key])
        self.assertNotIn('source_sections', contract)

    def test_four_hundred_lines_fit_and_preserve_order_text_hashes(self):
        self.ranges([(1, 400), (450, 451)])
        capsule, contract = self.compile()
        local.verify_context(self.root, capsule, contract)
        rows = self.text(capsule)
        expected = ''.join(self.file.read_text().splitlines(keepends=True)[:400])
        self.assertEqual(rows[0]['text'], expected)
        self.assertEqual(rows[0]['section_sha256'], hashlib.sha256(expected.encode()).hexdigest())
        self.assertEqual(rows[0]['source_sha256'], hashlib.sha256(self.file.read_bytes()).hexdigest())
        self.assertEqual(rows[1]['start_line'], 450)

    def test_usable_ascii_context_above_old_ten_k_limit(self):
        self.file.write_text('x' * 10_500 + '\n')
        self.ranges([(1, 1)])
        capsule, contract = self.compile()
        self.assertLessEqual(len(capsule['context_summary']), 12_000)
        self.assertEqual(self.text(capsule)[0]['text'], self.file.read_text())
        local.verify_context(self.root, capsule, contract)

    def test_each_file_read_once_per_invocation_and_fresh_on_verify(self):
        self.ranges([(1, 2), (7, 8), (10, 11)])
        with patch.object(source_excerpt, '_read_exact', wraps=source_excerpt._read_exact) as read:
            capsule, contract = self.compile()
            self.assertEqual(read.call_count, 1)
            local.verify_context(self.root, capsule, contract)
            self.assertEqual(read.call_count, 2)

    def test_distinct_files_are_not_conflated(self):
        other = self.root / 'other.txt'
        other.write_text('other\nsecond\n')
        self.action['read_scopes'].append('other.txt')
        self.action['target_paths'].append('other.txt')
        self.ranges([(1, 1), (2, 2)])
        self.action['source_sections'] += [dict(path='other.txt', start_line=1, end_line=1)]
        with patch.object(source_excerpt, '_read_exact', wraps=source_excerpt._read_exact) as read:
            capsule, _ = self.compile()
            self.assertEqual(read.call_count, 2)
        self.assertEqual(self.text(capsule)[-1]['text'], 'other\n')

    def test_hard_count_and_range_bounds_remain(self):
        for pairs in ([(n, n) for n in range(1, 18)], [(1, 401)], [], [(0, 1)],
                      [(True, 1)], [(1, 1), (1, 1)], [(502, 502)]):
            with self.subTest(pairs=pairs), self.assertRaises(caller.EmbeddedNokiyError):
                self.ranges(pairs)
                self.compile()

    def test_aggregate_excerpt_bytes_are_bounded(self):
        self.file.write_text(('x' * 6_000 + '\n') * 2)
        self.ranges([(1, 1), (2, 2)])
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.compile()

    def test_json_escaping_and_caller_prefix_count_in_summary(self):
        for text, prefix in [('x' * 11_000, 'prefix' * 300), ('\u4e2d' * 2_000, 'bounded')]:
            with self.subTest(prefix_length=len(prefix)):
                self.file.write_text(text)
                self.action['context_summary'] = prefix
                self.ranges([(1, 1)])
                with self.assertRaises(caller.EmbeddedNokiyError):
                    self.compile()

    def test_invalid_utf8_and_symlink_are_rejected(self):
        self.ranges([(1, 1)])
        self.file.write_bytes(b'\xff\n')
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.compile()
        self.file.unlink()
        target = self.root / 'target.txt'
        target.write_text('same\n')
        self.file.symlink_to(target)
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.compile()

    def test_ungranted_and_extra_locator_fields_rejected(self):
        for locator in (dict(path='other.txt', start_line=1, end_line=1),
                        dict(path='./input.txt', start_line=1, end_line=1),
                        dict(path='input.txt', start_line=1, end_line=1, extra=True)):
            self.action['source_sections'] = [locator]
            with self.subTest(locator=locator), self.assertRaises(caller.EmbeddedNokiyError):
                self.compile()

    def test_full_file_drift_outside_range_and_snapshot_race_rejected(self):
        self.ranges([(1, 1)])
        capsule, contract = self.compile()
        self.file.write_text(self.file.read_text() + 'later\n')
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.verify_context(self.root, capsule, contract)
        with patch.object(source_excerpt, '_read_exact', return_value=b'different\n'):
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.compile()

    def test_context_and_metadata_tamper_rejected(self):
        self.ranges([(1, 2)])
        capsule, contract = self.compile()
        changed = copy.deepcopy(capsule)
        changed['context_summary'] = changed['context_summary'].replace('line 001', 'line 999')
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.verify_context(self.root, changed, contract)
        changed = copy.deepcopy(capsule)
        changed['dcf_generation']['source_sections'][0]['section_sha256'] = '0' * 64
        with self.assertRaises(caller.EmbeddedNokiyError):
            local.verify_context(self.root, changed, contract)

    def test_absent_sections_unchanged_and_input_not_mutated(self):
        before = copy.deepcopy(self.action)
        capsule, contract = self.compile()
        self.assertEqual(self.action, before)
        self.assertNotIn('source_sections', capsule['dcf_generation'])
        self.assertNotIn(local._SECTIONS_MARKER, capsule['context_summary'])
        local.verify_context(self.root, capsule, contract)


if __name__ == '__main__':
    unittest.main()
