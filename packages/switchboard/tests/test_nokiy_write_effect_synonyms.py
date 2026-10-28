"""Write-effect synonyms resolve like "write" instead of costing a coordinator round.

Live 2026-10-22 (i6/i7b, Nokiy Ultra): Astra coordinators declared operations ["read", "command",
"edit"] or ["read", "command", "update"]; every first run_batch came back NOKIY_NATIVE_INPUT_INVALID
action.operations unsupported_operation and the coordinator spent a full round correcting one word.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'src'
spec = importlib.util.spec_from_file_location('write_effect_host', SRC / 'nokiy-native-host.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class WriteEffectSynonymTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='nokiy-write-effects-')
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name).resolve()
        (self.workspace / 'present.py').write_text('x')
        (self.workspace / 'pkg').mkdir()

    def test_every_synonym_resolves_by_target_existence(self):
        for effect in host.WRITE_EFFECTS:
            for targets, expected in ((['present.py'], ['read', 'command', 'modify']),
                                      (['absent.py'], ['read', 'command', 'create']),
                                      (['present.py', 'absent.py'], ['read', 'command', 'create', 'modify'])):
                with self.subTest(effect=effect, targets=targets):
                    action = {'operations': ['read', 'command', effect], 'write_scopes': targets, 'target_paths': targets}
                    resolved, repairs = host.validate_action_format(action, self.workspace)
                    self.assertEqual(resolved['operations'], expected)
                    changed = '_'.join(op for op in expected if op in ('create', 'modify'))
                    self.assertEqual(repairs, [{'path': 'action.operations', 'rule': effect + '_to_' + changed}])
                    self.assertEqual(action['operations'], ['read', 'command', effect], 'input is not mutated')

    def test_live_shapes_from_ultra_dispatches(self):
        for operations in (['read', 'command', 'edit'], ['read', 'command', 'update'],
                           ['read', 'command', 'edit', 'create']):
            with self.subTest(operations=operations):
                action = {'operations': operations, 'write_scopes': ['present.py', 'new_test.py'],
                          'target_paths': ['present.py', 'new_test.py'], 'read_scopes': ['present.py'],
                          'source_read': True}
                resolved, _ = host.validate_action_format(action, self.workspace)
                self.assertEqual(sorted(resolved['operations']), ['command', 'create', 'modify', 'read'])

    def test_mixed_synonyms_and_enum_values_are_deduplicated(self):
        resolved, repairs = host.validate_action_format(
            {'operations': ['edit', 'modify', 'update'], 'write_scopes': ['present.py']}, self.workspace)
        self.assertEqual(resolved['operations'], ['modify'])
        self.assertEqual(repairs[0]['rule'], 'edit_to_modify')

    def test_without_an_exact_target_a_synonym_keeps_the_typed_refusal(self):
        for action in ({'operations': ['edit'], 'write_scopes': []},
                       {'operations': ['update'], 'write_scopes': ['pkg/**']},
                       {'operations': ['patch'], 'write_scopes': ['src/*.py']},
                       {'operations': ['change'], 'write_scopes': ['pkg']}):
            with self.subTest(action=action), self.assertRaises(host.IntakeError) as caught:
                host.validate_action_format(action, self.workspace)
            self.assertEqual(caught.exception.issues, [{'path': 'action.operations', 'reason': 'unsupported_operation'}])

    def test_unrelated_unknown_operations_stay_unsupported(self):
        for word in ('execute', 'deploy', 'Edit', 'remove'):
            with self.subTest(word=word), self.assertRaises(host.IntakeError):
                host.validate_action_format({'operations': ['read', word], 'write_scopes': ['present.py']}, self.workspace)


if __name__ == '__main__':
    unittest.main()

