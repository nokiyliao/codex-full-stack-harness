"""Provider-free host preparation; caller admission/execution is stubbed."""
import copy
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('post_batch_reference_fixtures', ROOT / 'test_nokiy_native_reference.py')
fixtures = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fixtures)
host = fixtures.host


class PostBatchVerifierTransportTests(fixtures.NativeReferenceTests):
    def setUp(self):
        super().setUp()  # Existing fixture allocates beneath the verifier's TMPDIR.
        self.final_root = self.root / 'final-artifacts'
        self.final_root.mkdir()
        (self.final_root / 'scratch').mkdir()
        (self.workspace / 'source.py').write_text('VALUE = 1\n')
        self.binary = self.root / 'python3'
        self.binary.write_text('Boundary stub; never executed.\n')
        ordinary = self.payload()
        self.verifier = {key: value for key, value in ordinary.items() if key != 'operation'}
        self.verifier['draft']['artifact_root'] = str(self.final_root)
        self.verifier['action']['mission']['task_id'] = 'combined-tests'
        self.verifier['action']['verifier_commands'] = [{
            'argv': [str(self.binary), '-m', 'unittest'],
            'executable_sha256': host.sha(self.binary), 'pinned_files': [],
            'scratch_root': str(self.final_root / 'scratch'),
            'python_import_roots': [str(self.workspace)], 'cwd': str(self.workspace),
            'timeout_seconds': 60, 'network': False,
        }]
        self.preparations = []
        self.preparation_modes = []
        self.execv.side_effect = SystemExit('captured caller boundary')

    @contextmanager
    def caller_boundary(self, failure=None):
        """Use real prepare_action and existing fixture-owned native executable stubs."""
        image = self.home / 'runtime-image'
        image.write_text('Never executed runtime image fixture.')
        (self.home / 'caller-binding.json').write_text(json.dumps({'runtime_image': {
            'path': str(image), 'sha256': host.sha(image)}}))
        native_root = Path('/Applications/ChatGPT.app/Contents/Resources')
        native = native_root / 'codex'
        original_exists, original_resolve, original_sha = Path.exists, Path.resolve, host.sha

        def prepare(command, **kwargs):
            self.assertEqual(command[0], str(self.entry))
            self.assertIn(command[1], {'prepare', 'prepare-post-batch-verifier'})
            self.preparation_modes.append(command[1])
            request = json.loads(Path(command[command.index('--request') + 1]).read_bytes())
            action = json.loads(Path(command[command.index('--action') + 1]).read_bytes())
            self.preparations.append((request, action))
            if failure is not None:
                return SimpleNamespace(returncode=2, stdout=json.dumps(failure), stderr='')
            prepared = Path(command[command.index('--output-dir') + 1])
            prepared.mkdir()
            path = prepared / 'request.json'
            path.write_text(host.canonical(request))
            request_id = 'tura_embedded_' + host.sha(path)
            path.with_name('preparation.json').write_text(json.dumps({
                'status': 'PREPARED', 'request_id': request_id,
                'preflight': {'status': 'READY', 'request_id': request_id,
                              'request_sha256': request_id.removeprefix('tura_embedded_')},
            }))
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')

        self.prepare_process.side_effect = prepare
        with mock.patch.object(Path, 'exists', autospec=True, side_effect=lambda path:
                               False if path.is_relative_to(native_root) else original_exists(path)), \
                mock.patch.object(Path, 'resolve', autospec=True, side_effect=lambda path, strict=False:
                                  native if path.is_relative_to(native_root) else original_resolve(path, strict=strict)), \
                mock.patch.object(host, 'sha', side_effect=lambda path:
                                  'c' * 64 if Path(path) == native else original_sha(path)), \
                mock.patch.object(host, 'worker_capacity', return_value=3):
            yield

    def dispatch(self, payload):
        with redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(SystemExit, 'captured caller boundary'):
                host.run(self.envelope(payload))
        return json.loads(output.getvalue())

    def compact(self, ordinary):
        compact = copy.deepcopy(ordinary)
        members = compact.pop('members')
        common = members[0]
        compact['shared'] = {
            'schema_version': 'nokiy_batch_shared_v1',
            'draft': {'workspace': common['draft']['workspace']},
            'action': {'context_summary': common['action']['context_summary'],
                       'mission': {k: v for k, v in common['action']['mission'].items() if k != 'task_id'}},
        }
        for member in members:
            member['draft'].pop('workspace')
            member['action'].pop('context_summary')
            member['action']['mission'] = {'task_id': member['action']['mission']['task_id']}
        compact['member_deltas'] = members
        return compact

    def test_omission_keeps_original_plan_identity_and_member_preparation(self):
        payload = self.missing_batch_payload()
        original = copy.deepcopy(payload)
        identity = {'thread': self.thread, 'max_parallel_workers': 3,
                    'envelopes': [self.envelope({'operation': 'run', **m}) for m in payload['members']]}
        expected_name = 'native-batch-plan-' + hashlib.sha256(host.canonical(identity).encode()).hexdigest() + '.json'
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action', side_effect=self.prepared_batch_member) as prepare:
            reference = self.dispatch(payload)
        plan = Path(reference['plan_path'])
        self.assertEqual(plan.name, expected_name)
        self.assertNotIn('post_batch_verifier', json.loads(plan.read_bytes()))
        self.assertEqual(prepare.call_count, 3)
        self.assertEqual(payload, original)
        self.assertEqual(self.execv.call_args.args[1][1], 'run-batch')
        self.prepare_process.assert_not_called()

    def test_large_compact_batch_keeps_per_member_identity_admission_and_recovery(self):
        payload = self.compact(self.missing_batch_payload())
        payload['shared']['draft']['prompt'] = 'shared context\n' * 2800
        expanded = host.expand_batch_input(self.envelope(payload))
        self.assertGreater(len(host.canonical(expanded['payload']).encode()), host.MAX_PAYLOAD_BYTES)
        identity = {'thread': self.thread, 'max_parallel_workers': 3,
                    'envelopes': [self.envelope({'operation': 'run', **m}) for m in expanded['payload']['members']]}
        expected = 'native-batch-plan-' + hashlib.sha256(host.canonical(identity).encode()).hexdigest() + '.json'
        original = copy.deepcopy(payload)
        with self.caller_boundary():
            reference = self.dispatch(payload)
            self.assertEqual(Path(reference['plan_path']).name, expected)
            self.assertEqual(self.prepare_process.call_count, 3)
            for request, _ in self.preparations:
                self.assertTrue(request['prompt'].startswith(payload['shared']['draft']['prompt']))
                self.assertEqual((request['native_thread_id'], request['model'], request['reasoning_effort']),
                                 (self.thread, self.worker, self.effort))
            self.assertEqual(self.dispatch(payload), reference)
            self.assertEqual(self.prepare_process.call_count, 3)
            self.assertEqual(self.execv.call_args.args[1][1], 'read-batch')
        self.assertEqual(payload, original)

    def test_one_dispatch_prepares_gate_and_members_with_host_identity(self):
        payload = self.missing_batch_payload()
        # The gate may use the same automatic artifact leaf creation as members.
        packet = copy.deepcopy(self.verifier)
        packet['draft']['artifact_root'] = str(Path(payload['artifact_root']) / 'final')
        packet['action']['verifier_commands'][0].pop('scratch_root')
        payload['post_batch_verifier'] = packet
        original = copy.deepcopy(payload)
        with self.caller_boundary():
            published = self.dispatch(payload)
        plan = json.loads(Path(published['plan_path']).read_bytes())
        self.assertEqual(len(plan['members']), 3)  # Gate never consumes the fourth worker slot.
        self.assertEqual(self.prepare_process.call_count, 4)
        self.assertEqual(self.preparation_modes, ['prepare'] * 4)
        final_request, final_action = self.preparations[0]
        self.assertEqual(final_action['mission']['task_id'], 'combined-tests')
        self.assertEqual(final_request['authority_effect'], 'none')
        for request, _ in self.preparations:
            self.assertEqual((request['native_thread_id'], request['execution_profile'], request['model'],
                              request['reasoning_effort'], request['service_tier']),
                             (self.thread, 'direct', self.worker, self.effort, self.tier))
        locator = plan['post_batch_verifier']
        self.assertEqual(set(locator), {'request_path', 'request_file_sha256', 'request_id',
                                        'artifact_root', 'native_thread_id'})
        self.assertEqual(locator['native_thread_id'], self.thread)
        self.assertEqual(locator['request_file_sha256'], host.sha(locator['request_path']))
        self.assertEqual(locator['request_id'], host.prepared_request_id(Path(locator['request_path'])))
        grant = final_action['verifier_commands'][0]
        self.assertEqual(grant['argv'], packet['action']['verifier_commands'][0]['argv'])
        self.assertEqual(grant['executable_sha256'], host.sha(self.binary))
        self.assertEqual(grant['python_import_roots'], [str(self.workspace)])
        self.assertEqual(grant['cwd'], str(self.workspace))
        self.assertEqual(grant['scratch_root'], str(Path(locator['artifact_root']) / 'verifier-0'))
        self.assertEqual(self.execv.call_args.args[1][1], 'run-batch')
        self.assertEqual(payload, original)
        self.recovery_process.assert_not_called()

    def test_shared_external_pins_preserve_original_plan_and_input_identities(self):
        payload = self.missing_batch_payload()
        external = self.root / 'combined-check.py'
        external.write_text('Boundary fixture; never executed.\n')
        pin = {'path': str(external), 'sha256': host.sha(external), 'scope': 'verifier_only'}
        payload['post_batch_verifier'] = copy.deepcopy(self.verifier)
        final_command = payload['post_batch_verifier']['action']['verifier_commands'][0]
        final_command['argv'] = [str(self.binary), '-B', str(external)]
        final_command.pop('pinned_files')
        for member in payload['members']:
            member['action']['verifier_commands'] = [{**copy.deepcopy(final_command), 'pinned_files': [dict(pin)]}]
        original = copy.deepcopy(payload)
        identity = {'thread': self.thread, 'max_parallel_workers': 3,
                    'envelopes': [self.envelope({'operation': 'run', **member}) for member in payload['members']],
                    'post_batch_verifier': payload['post_batch_verifier']}
        expected_name = 'native-batch-plan-' + hashlib.sha256(host.canonical(identity).encode()).hexdigest() + '.json'
        final_payload = {'operation': 'run', **payload['post_batch_verifier']}
        original_digest = hashlib.sha256(host.canonical({
            'thread': self.thread, 'envelope': self.envelope(final_payload)}).encode()).hexdigest()
        with self.caller_boundary():
            published = self.dispatch(payload)
            prepared_count = self.prepare_process.call_count
            self.assertEqual(prepared_count, 4)  # Final and all members still receive separate admissions.
            self.assertEqual(Path(published['plan_path']).name, expected_name)
            request, action = self.preparations[0]
            self.assertEqual(action['verifier_commands'][0], {**final_command, 'pinned_files': [pin]})
            for key in ('read_scopes', 'write_scopes', 'target_paths', 'forbidden_effects'):
                self.assertEqual(action[key], original['post_batch_verifier']['action'][key])
            self.assertEqual((request['native_thread_id'], request['model'], request['reasoning_effort']),
                             (self.thread, self.worker, self.effort))
            repairs = list(self.final_root.glob('native-input-*/input-repairs.json'))
            self.assertEqual(len(repairs), 1)
            recorded = json.loads(repairs[0].read_bytes())
            self.assertEqual(recorded['original_input_sha256'], original_digest)
            self.assertIn({'path': 'action.verifier_commands', 'rule': 'batch_verifier_pins_reused'},
                          recorded['repairs'])
            self.assertEqual(payload, original)
            external.unlink()
            with mock.patch.object(host, 'complete_batch_verifier_pins',
                                   side_effect=AssertionError('Original plan must recover before normalization')):
                self.assertEqual(self.dispatch(payload), published)
            self.assertEqual(self.prepare_process.call_count, prepared_count)
            self.assertEqual(self.execv.call_args.args[1][1], 'read-batch')
            self.assertEqual(payload, original)

    def test_parent_subjects_bind_plan_without_adding_verifier_writes(self):
        payload = self.missing_batch_payload()
        payload['post_batch_verifier'] = copy.deepcopy(self.verifier)
        payload['post_batch_verifier']['parent_source_targets'] = ['parent.py']
        with self.caller_boundary():
            published = self.dispatch(payload)
        plan = json.loads(Path(published['plan_path']).read_bytes())
        self.assertEqual(plan['post_batch_verifier']['parent_source_targets'], ['parent.py'])
        self.assertEqual(self.preparations[0][1].get('write_scopes', []), [])
        self.assertNotIn('parent_source_targets', self.preparations[0][1])
        self.assertEqual(host.validate_prepared_post_batch_verifier(
            plan['post_batch_verifier'], self.thread)['parent_source_targets'], ['parent.py'])

    def test_invalid_parent_subjects_reject_before_preparation(self):
        for names in ([], ['../escape.py'], ['/absolute.py'], ['same.py', 'same.py'], ['dir/**'], [None]):
            packet = {**self.verifier, 'parent_source_targets': names}
            with self.subTest(names=names), self.assertRaises(ValueError):
                host.validate_post_batch_verifier(packet)
        self.prepare_process.assert_not_called()

    def test_parent_reference_and_inline_recover_the_same_plan_without_reprepare(self):
        payload = self.missing_batch_payload()
        payload['post_batch_verifier'] = copy.deepcopy(self.verifier)
        payload['post_batch_verifier']['parent_source_targets'] = ['parent.py']
        reference = dict(self.reference(payload), operation='run_batch_ref', parent_completion=True)
        with self.caller_boundary():
            published = self.dispatch(reference)
            prepared_count = len(self.preparations)
            self.assertGreater(prepared_count, 0)
            plan_path = Path(published['plan_path'])
            original = plan_path.read_bytes()
            plan = json.loads(original)
            self.assertEqual(plan['post_batch_verifier']['parent_source_targets'], ['parent.py'])
            for packet in (payload, dict(self.reference(payload), operation='run_batch_ref', parent_completion=True)):
                self.assertEqual(self.dispatch(packet), published)
                self.assertEqual(len(self.preparations), prepared_count)
                self.assertEqual(plan_path.read_bytes(), original)
                self.assertEqual(self.execv.call_args.args[1][1], 'read-batch')
        self.assertEqual(self.preparations[0][1].get('write_scopes', []), [])

    def test_external_postgate_scratch_selects_dedicated_preparation_only(self):
        payload = self.missing_batch_payload()
        packet = copy.deepcopy(self.verifier)
        scratch = self.root / 'exact-final-output'
        scratch.mkdir()
        packet['action']['verifier_commands'][0]['scratch_root'] = str(scratch)
        payload['post_batch_verifier'] = packet
        with self.caller_boundary():
            self.dispatch(payload)
        self.assertEqual(self.preparation_modes, ['prepare-post-batch-verifier'] + ['prepare'] * 3)
        self.assertEqual(self.preparations[0][1]['verifier_commands'][0]['scratch_root'], str(scratch))
        ordinary, repairs = host.complete_verifier_commands(packet['action'], packet['draft'], self.workspace)
        self.assertEqual(ordinary['verifier_commands'][0]['scratch_root'], str(self.final_root / 'verifier-0'))

    def test_missing_sibling_final_artifact_leaf_is_created_without_an_extra_dispatch(self):
        payload = self.missing_batch_payload()
        packet = copy.deepcopy(self.verifier)
        leaf = self.root / 'new-final-artifacts'
        packet['draft']['artifact_root'] = str(leaf)
        packet['action']['verifier_commands'][0].pop('scratch_root')
        payload['post_batch_verifier'] = packet
        with self.caller_boundary():
            self.dispatch(payload)
        self.assertTrue(leaf.is_dir())
        self.assertEqual(self.prepare_process.call_count, 4)
        self.assertEqual(self.preparations[0][0]['artifact_root'], str(leaf))

    def test_missing_final_parent_is_not_created_recursively(self):
        payload = self.missing_batch_payload()
        packet = copy.deepcopy(self.verifier)
        parent = self.root / 'absent-parent'
        packet['draft']['artifact_root'] = str(parent / 'final')
        packet['action']['verifier_commands'][0].pop('scratch_root')
        payload['post_batch_verifier'] = packet
        with self.caller_boundary(), self.assertRaises(ValueError):
            self.dispatch(payload)
        self.assertFalse(parent.exists())
        self.prepare_process.assert_not_called()

    def test_internal_postgate_scratch_preserves_ordinary_dcf_preparation(self):
        payload = self.missing_batch_payload()
        packet = copy.deepcopy(self.verifier)
        packet['surface_id'] = 'existing-dcf-surface'
        payload['post_batch_verifier'] = packet
        with self.caller_boundary():
            self.dispatch(payload)
        self.assertEqual(self.preparation_modes, ['prepare'] * 4)
        self.assertEqual(self.preparations[0][1]['verifier_commands'][0]['scratch_root'],
                         str(self.final_root / 'scratch'))

    def test_inline_compact_and_reference_recover_original_plan_without_replay(self):
        payload = self.missing_batch_payload()
        payload['post_batch_verifier'] = copy.deepcopy(self.verifier)
        original = copy.deepcopy(payload)
        compact = self.compact(payload)
        self.assertEqual(host.expand_batch_input(self.envelope(compact)), self.envelope(payload))
        reference = dict(self.reference(compact), operation='run_batch_ref')
        identity = {'thread': self.thread, 'max_parallel_workers': 3, 'post_batch_verifier': self.verifier,
                    'envelopes': [self.envelope({'operation': 'run', **m}) for m in payload['members']]}
        with self.caller_boundary():
            published = self.dispatch(reference)
            plan = Path(published['plan_path'])
            data = plan.read_bytes()
            self.assertEqual(host.sha(plan), published['plan_sha256'])
            self.assertEqual(plan.name, 'native-batch-plan-' + hashlib.sha256(host.canonical(identity).encode()).hexdigest() + '.json')
            value = json.loads(data)
            locator = value['post_batch_verifier']
            self.assertEqual(locator['artifact_root'], str(self.final_root))
            self.assertEqual(self.prepare_process.call_count, 4)
            self.assertEqual(self.execv.call_args.args[1][1], 'run-batch')
            self.prepare_process.reset_mock()
            self.prepare_process.side_effect = AssertionError('Recovery must not reprepare')
            # Remove both original preparation inputs and prepared requests. Recovery
            # must reach the caller's read-only UNKNOWN path without refreshing them.
            shutil.rmtree(self.final_root)
            for member in payload['members']:
                for child in Path(member['draft']['artifact_root']).iterdir():
                    shutil.rmtree(child)
            self.binary.unlink()
            (self.home / 'caller-binding.json').unlink()
            second_reference = dict(self.reference(payload), operation='run_batch_ref')
            with mock.patch.object(host, 'worker_capacity', return_value=1):
                for repeated in (payload, compact, second_reference):
                    self.assertEqual(self.dispatch(repeated), published)
                    self.assertEqual(self.execv.call_args.args[1], [str(self.entry), 'read-batch',
                                     '--plan', str(plan), '--plan-sha256', published['plan_sha256'], '--summary'])
                    self.assertEqual(plan.read_bytes(), data)
            self.prepare_process.assert_not_called()
            with self.assertRaises(SystemExit):
                host.run(self.envelope({'operation': 'read_batch', **published}))
            self.assertEqual(self.execv.call_args.args[1][1], 'read-batch')
        self.assertEqual(payload, original)
        self.prepare_process.assert_not_called()
        self.recovery_process.assert_not_called()

    def test_changed_published_verifier_owner_rejects_without_preparation(self):
        payload = self.missing_batch_payload()
        payload['post_batch_verifier'] = dict(self.verifier)
        with self.caller_boundary():
            published = self.dispatch(payload)
            plan = Path(published['plan_path'])
            changed = json.loads(plan.read_bytes())
            changed['post_batch_verifier']['native_thread_id'] = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
            plan.write_text(host.canonical(changed))
            self.prepare_process.reset_mock()
            self.execv.reset_mock()
            with self.assertRaisesRegex(ValueError, 'owner or request identity differs'):
                host.run(self.envelope(payload))
            self.prepare_process.assert_not_called()
            self.execv.assert_not_called()

    def test_invalid_write_effect_owner_and_grant_packets_reject_before_preparation(self):
        payload = self.missing_batch_payload()
        cases = [None, {}, {'request_path': '/old/locator'},
                 {**self.verifier, 'operation': 'run'}, {**self.verifier, 'native_thread_id': self.thread}]
        for section, field, value in [
                ('draft', 'authority_effect', 'workspace'), ('draft', 'model', self.worker),
                ('action', 'native_thread_id', self.thread), ('action', 'credentials', {}),
                ('action', 'operations', ['create']), ('action', 'allowed_operations', ['modify']),
                ('action', 'write_scopes', ['source.py']), ('action', 'target_paths', ['source.py']),
                ('action', 'verifier_commands', []), ('action', 'verifier_commands', [{}, {}]),
                ('action', 'command_templates', [{'argv': ['pwd'], 'effects': ['broker'], 'targets': []}]),
                ('action', 'command_templates', [{'argv': ['pwd'], 'effects': ['read'],
                                                 'targets': [{'operation': 'modify', 'path': 'source.py'}]}]),
                ('draft', 'artifact_root', payload['members'][0]['draft']['artifact_root']),
                ('draft', 'artifact_root', str(self.workspace))]:
            packet = copy.deepcopy(self.verifier)
            packet[section][field] = value
            cases.append(packet)
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action') as prepare:
            for spec in cases:
                with self.subTest(spec=spec), self.assertRaises((ValueError, OSError, host.IntakeError)):
                    host.run(self.envelope({**payload, 'post_batch_verifier': spec}))
                self.assertEqual(list(Path(payload['artifact_root']).iterdir()), [])
            prepare.assert_not_called()
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()

    def test_typed_prerequisite_rejection_stops_before_members_and_is_not_prepared_twice(self):
        payload = self.missing_batch_payload()
        payload['post_batch_verifier'] = copy.deepcopy(self.verifier)
        payload['post_batch_verifier']['action']['verifier_commands'][0]['executable_sha256'] = '0' * 64
        failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                   'authority_effect': 'none', 'first_typed_blocker': 'JSPACE_VERIFIER_GRANT_INVALID'}
        with self.caller_boundary(failure=failure):
            for _ in range(2):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(host.run(self.envelope(payload)), 2)
                outcome = json.loads(output.getvalue())
                self.assertEqual(outcome['first_typed_blocker'], failure['first_typed_blocker'])
                self.assertFalse(outcome['provider_execution_started'])
            self.assertEqual(self.prepare_process.call_count, 1)
            self.assertEqual(len(self.preparations), 1)
            self.assertEqual(self.preparations[0][1]['verifier_commands'][0]['executable_sha256'], '0' * 64)
        self.execv.assert_not_called()
        self.recovery_process.assert_not_called()
        self.assertFalse(list(Path(payload['artifact_root']).glob('native-batch-plan-*.json')))


def load_tests(loader, tests, pattern):
    # Original tests are discovered separately; add only this focused transport coverage.
    return unittest.TestSuite(PostBatchVerifierTransportTests(name)
                              for name in PostBatchVerifierTransportTests.__dict__ if name.startswith('test_'))


if __name__ == '__main__':
    unittest.main()
