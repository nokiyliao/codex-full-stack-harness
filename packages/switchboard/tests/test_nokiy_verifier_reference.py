import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('host', Path(__file__).resolve().parents[1] / 'src/nokiy-native-host.py')
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name).resolve() / 'catalog.json'
        self.command = {'argv': ['/usr/bin/true'], 'network': False}
        self.file.write_text(json.dumps({'commands': {'check': self.command}}))
        self.ref = {'catalog_ref': {'path': str(self.file),
                    'sha256': hashlib.sha256(self.file.read_bytes()).hexdigest(), 'command': 'check'}}
        self.workspace = self.file.parent / 'workspace'
        self.workspace.mkdir()
        self.external = self.file.with_name('check.py')
        self.external.write_text('Boundary fixture; never executed.\n')

    def expand(self, command):
        return host.expand_verifier_references({'payload': {'operation': 'run',
            'action': {'verifier_commands': [command], 'write_scopes': []}}})

    def test_exact_expansion_is_same_as_inline_and_independent(self):
        result = self.expand(self.ref)
        self.assertEqual(result, self.expand(self.command))
        result['payload']['action']['verifier_commands'][0]['argv'].append('changed')
        self.assertEqual(self.expand(self.ref), self.expand(self.command))

    def test_inline_request_preserves_identity_without_serialization(self):
        envelope = {'payload': {'operation': 'run', 'action': {'verifier_commands': [self.command]}}}
        self.assertIs(host.expand_verifier_references(envelope), envelope)

    def test_rejects_override_drift_missing_and_alias(self):
        with self.assertRaises(ValueError): self.expand({**self.ref, 'network': True})
        with self.assertRaises(ValueError):
            self.expand({'catalog_ref': {**self.ref['catalog_ref'], 'command': 'absent'}})
        self.file.write_text('{}')
        with self.assertRaises(ValueError): self.expand(self.ref)
        target = self.file.with_name('target.json')
        self.file.rename(target)
        self.file.symlink_to(target)
        with self.assertRaises((ValueError, OSError)): self.expand(self.ref)

    def test_duplicate_catalog_keys_rejected(self):
        self.file.write_text('{"commands":{},"commands":{"check":{}}}')
        self.ref['catalog_ref']['sha256'] = hashlib.sha256(self.file.read_bytes()).hexdigest()
        with self.assertRaises(ValueError): self.expand(self.ref)

    def test_batch_members_and_final_gate_expand_without_scope_changes(self):
        member = {'action': {'verifier_commands': [self.ref], 'write_scopes': ['one.py']}}
        result = host.expand_verifier_references({'payload': {'operation': 'run_batch',
            'members': [member], 'post_batch_verifier': member}})['payload']
        for item in (result['members'][0], result['post_batch_verifier']):
            self.assertEqual(item['action']['verifier_commands'], [self.command])
            self.assertEqual(item['action']['write_scopes'], ['one.py'])

    def batch_pins(self):
        pin = {'path': str(self.external), 'sha256': hashlib.sha256(self.external.read_bytes()).hexdigest(),
               'scope': 'verifier_only'}
        command = {'argv': ['/usr/bin/true', str(self.external)], 'pinned_files': [pin], 'network': False}
        members = [{'action': {'verifier_commands': [copy.deepcopy(command)],
                               'read_scopes': ['member.py'], 'write_scopes': ['member.py']}}
                   for _ in range(2)]
        action = {'verifier_commands': [{key: value for key, value in command.items() if key != 'pinned_files'}],
                  'read_scopes': [], 'write_scopes': [], 'forbidden_effects': ['deployment']}
        return action, members, pin

    def test_batch_reuses_only_common_explicit_pins_after_catalog_expansion(self):
        action, members, pin = self.batch_pins()
        command = members[0]['action']['verifier_commands'][0]
        self.file.write_text(json.dumps({'commands': {'check': command}}))
        self.ref['catalog_ref']['sha256'] = hashlib.sha256(self.file.read_bytes()).hexdigest()
        for member in members:
            member['action']['verifier_commands'] = [self.ref]
        envelope = {'payload': {'operation': 'run_batch', 'members': members,
                                'post_batch_verifier': {'action': action}}}
        original = copy.deepcopy(envelope)
        expanded = host.expand_verifier_references(envelope)['payload']
        with mock.patch.object(host, 'sha', side_effect=AssertionError('Must not recompute declared digest')):
            completed, repairs = host.complete_batch_verifier_pins(
                expanded['post_batch_verifier']['action'], expanded['members'], self.workspace)
        self.assertEqual(completed, {**action, 'verifier_commands': [{**action['verifier_commands'][0],
                                                                    'pinned_files': [pin]}]})
        self.assertEqual(repairs, [{'path': 'action.verifier_commands', 'rule': 'batch_verifier_pins_reused'}])
        self.assertEqual(envelope, original)
        self.assertNotIn('pinned_files', expanded['post_batch_verifier']['action']['verifier_commands'][0])
        completed['verifier_commands'][0]['pinned_files'][0]['sha256'] = '0' * 64
        self.assertEqual(expanded['members'][0]['action']['verifier_commands'][0]['pinned_files'], [pin])

    def test_explicit_pin_fields_and_multiple_final_commands_are_untouched(self):
        for explicit in (None, [], [{'path': 'explicit.py'}]):
            action, members, _ = self.batch_pins()
            action['verifier_commands'][0]['pinned_files'] = explicit
            with self.subTest(explicit=explicit):
                self.assertEqual(host.complete_batch_verifier_pins(action, members, self.workspace), (action, []))
        action, members, _ = self.batch_pins()
        action['verifier_commands'].append(copy.deepcopy(action['verifier_commands'][0]))
        self.assertEqual(host.complete_batch_verifier_pins(action, members, self.workspace), (action, []))

    def test_all_members_must_explicitly_declare_the_same_complete_pin(self):
        for replacement in (None, [], [{'path': str(self.external)}],
                            [{'path': str(self.external), 'scope': 'verifier_only', 'sha256': '0' * 64}]):
            action, members, _ = self.batch_pins()
            members[1]['action']['verifier_commands'][0]['pinned_files'] = replacement
            with self.subTest(replacement=replacement):
                self.assertEqual(host.complete_batch_verifier_pins(action, members, self.workspace), (action, []))
        for field, value in (('scope', 'read'), ('sha256', 'not-a-digest'), ('extra', True)):
            action, members, _ = self.batch_pins()
            for member in members:
                member['action']['verifier_commands'][0]['pinned_files'][0][field] = value
            with self.subTest(field=field):
                self.assertEqual(host.complete_batch_verifier_pins(action, members, self.workspace), (action, []))

    def test_conflicting_second_grant_blocks_a_common_pin(self):
        action, members, pin = self.batch_pins()
        conflict = copy.deepcopy(members[0]['action']['verifier_commands'][0])
        conflict['pinned_files'] = [{**pin, 'sha256': '0' * 64}]
        members[0]['action']['verifier_commands'].append(conflict)
        self.assertEqual(host.complete_batch_verifier_pins(action, members, self.workspace), (action, []))

    def test_unrelated_second_grant_does_not_revoke_an_explicit_common_input(self):
        for extra in ({'argv': ['/usr/bin/true']},
                      {'argv': ['/usr/bin/true'], 'pinned_files': None},
                      {'argv': ['/usr/bin/true'], 'pinned_files': []}):
            action, members, pin = self.batch_pins()
            members[0]['action']['verifier_commands'].append(extra)
            with self.subTest(extra=extra):
                completed, _ = host.complete_batch_verifier_pins(action, members, self.workspace)
                self.assertEqual(completed['verifier_commands'][0]['pinned_files'], [pin])

    def test_unmatched_pins_are_not_copied_and_argv_is_not_inferred(self):
        action, members, pin = self.batch_pins()
        for argv in (['/usr/bin/true', self.external.name], ['/usr/bin/true', '--input=' + pin['path']],
                     ['/usr/bin/true', str(self.file)]):
            action['verifier_commands'][0]['argv'] = argv
            with self.subTest(argv=argv):
                self.assertEqual(host.complete_batch_verifier_pins(action, members, self.workspace), (action, []))
        action['verifier_commands'][0]['argv'] = ['/usr/bin/true', pin['path'], str(self.file)]
        completed, _ = host.complete_batch_verifier_pins(action, members, self.workspace)
        self.assertEqual(completed['verifier_commands'][0]['argv'], action['verifier_commands'][0]['argv'])
        self.assertEqual(completed['verifier_commands'][0]['pinned_files'], [pin])

    def test_noncanonical_missing_symlink_and_workspace_pins_are_not_reused(self):
        inside = self.workspace / 'inside.py'
        inside.write_text('Never executed.\n')
        alias = self.external.with_name('alias.py')
        alias.symlink_to(self.external)
        for name in (str(inside), str(alias), str(self.external.with_name('missing.py')),
                     str(self.external.parent) + '/./check.py',
                     str(self.external.parent) + '/workspace/../check.py'):
            action, members, _ = self.batch_pins()
            action['verifier_commands'][0]['argv'][1] = name
            for member in members:
                member['action']['verifier_commands'][0]['pinned_files'][0]['path'] = name
            with self.subTest(name=name):
                self.assertEqual(host.complete_batch_verifier_pins(action, members, self.workspace), (action, []))


if __name__ == '__main__':
    unittest.main()
