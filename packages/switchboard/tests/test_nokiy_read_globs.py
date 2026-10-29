"""Bounded filename reads reach full preparation without creating a directory grant."""
import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('read_glob_reference', ROOT / 'test_nokiy_native_reference.py')
existing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(existing)
host = existing.host


class ReadGlobTests(existing.NativeReferenceTests):
    def setUp(self):
        super().setUp()
        self.tests_dir = self.workspace / 'tests'
        self.tests_dir.mkdir()
        for name in ('test_resident_z.py', 'test_closeout_a.py', 'test_resident_a.py', '.test_resident_private.py'):
            (self.tests_dir / name).write_text('# temporary fixture\n')
        (self.tests_dir / 'test_resident_directory.py').mkdir()

    def glob_payload(self):
        payload = self.payload()
        payload['action'].update(
            operations=['read', 'command', 'create'],
            read_scopes=['tests/planned.py', 'tests/test_resident*.py', 'tests/test_closeout*.py'],
            write_scopes=['tests/planned.py'], target_paths=['tests/planned.py'])
        return payload

    def test_deterministic_exact_reads_preserve_planned_writes_and_discovery(self):
        action = self.glob_payload()['action']
        action['read_scopes'] += ['tests/test_resident_?.py', 'tests/test_[cr]*.py', 'tests/**']
        action['read_directories'] = ['tests']
        before = copy.deepcopy(action)
        normalized, repairs = host.normalize_read_globs(action, self.workspace)
        self.assertEqual(normalized['read_scopes'], [
            'tests/planned.py', 'tests/test_resident_a.py', 'tests/test_resident_z.py',
            'tests/test_closeout_a.py', 'tests/**'])
        self.assertEqual(action, before)
        self.assertEqual({k: v for k, v in normalized.items() if k != 'read_scopes'},
                         {k: v for k, v in action.items() if k != 'read_scopes'})
        self.assertEqual(len(repairs), 1)
        self.assertEqual(host.normalize_read_globs(normalized, self.workspace), (normalized, []))
        discovery, _ = host.normalize_local_discovery(normalized, self.workspace)
        self.assertEqual(discovery['read_scopes'], normalized['read_scopes'][:-1])
        compiler = self.workspace / 'scripts/ops/dcf.py'
        compiler.parent.mkdir(parents=True)
        compiler.write_text('# DCF marker, never executed\n')
        self.assertEqual(host.normalize_local_discovery(normalized, self.workspace), (normalized, []))

    def test_unsafe_patterns_have_specific_metadata_only_diagnostics(self):
        cases = {
            '/tests/test*.py': 'read_glob_absolute_path',
            'tests/../test*.py': 'read_glob_unsafe_path',
            './tests/test*.py': 'read_glob_unsafe_path',
            'tests//test*.py': 'read_glob_unsafe_path',
            'tests/test\n*.py': 'read_glob_unsafe_path',
            'tests\\test*.py': 'read_glob_unsafe_path',
            '.git/test*.py': 'read_glob_hidden_component',
            'tests/.secret*.py': 'read_glob_hidden_component',
            't*/test.py': 'read_glob_literal_directory_required',
            '**/test*.py': 'read_glob_literal_directory_required',
            '*.py': 'read_glob_literal_directory_required',
            'tests/test**.py': 'read_glob_unsupported_pattern',
            'tests/test{a,b}.py': 'read_glob_unsupported_pattern',
            'tests/missing*.py': 'read_glob_no_matches',
            'absent/test*.py': 'read_glob_directory_unavailable',
        }
        for pattern, reason in cases.items():
            with self.subTest(pattern=pattern), self.assertRaises(host.IntakeError) as caught:
                host.normalize_read_globs({'read_scopes': [pattern]}, self.workspace)
            receipt = caught.exception.receipt()
            self.assertEqual(receipt['issues'], [{'path': 'action.read_scopes[0]', 'reason': reason}])
            self.assertFalse(receipt['provider_execution_started'])
            self.assertNotIn(pattern, json.dumps(receipt['issues']))

    def test_symlink_ancestry_and_matching_symlinks_are_refused(self):
        (self.workspace / 'linked').symlink_to(self.tests_dir, target_is_directory=True)
        (self.tests_dir / 'linked').symlink_to(self.tests_dir, target_is_directory=True)
        (self.tests_dir / 'test_link.py').symlink_to(self.tests_dir / 'test_resident_a.py')
        (self.tests_dir / 'test_dangling.py').symlink_to(self.tests_dir / 'missing.py')
        for pattern, reason in (
                ('linked/test*.py', 'read_glob_symlink_ancestry'),
                ('tests/linked/test*.py', 'read_glob_symlink_ancestry'),
                ('tests/test_link*.py', 'read_glob_matching_symlink'),
                ('tests/test_dangling*.py', 'read_glob_matching_symlink')):
            with self.subTest(pattern=pattern), self.assertRaises(host.IntakeError) as caught:
                host.normalize_read_globs({'read_scopes': [pattern]}, self.workspace)
            self.assertEqual(caught.exception.issues[0]['reason'], reason)

    def test_full_run_and_batch_prepare_exact_reads_under_original_identity(self):
        single = self.glob_payload()
        batch = self.batch_payload('glob-batch')
        for member in batch['members']:
            member['action'] = copy.deepcopy(single['action'])
        batch['max_parallel_workers'] = 1
        self.execv.side_effect = SystemExit('captured execution; no provider')
        for payload in (single, batch):
            before = copy.deepcopy(payload)
            with self.preparation_boundary(), mock.patch.object(host, 'worker_capacity', return_value=3), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    host.run(self.envelope(payload))
            members = [payload] if payload['operation'] == 'run' else [
                {'operation': 'run', **member} for member in payload['members']]
            for member in members:
                artifact = Path(member['draft']['artifact_root'])
                inputs = next(artifact.glob('native-input-*'))
                action = json.loads((inputs / 'action.json').read_text())
                self.assertEqual(action, dict(member['action'], read_scopes=[
                    'tests/planned.py', 'tests/test_resident_a.py', 'tests/test_resident_z.py',
                    'tests/test_closeout_a.py']))
                # The pre-existing key is independent of the current glob matches.
                self.assertEqual(inputs.name, 'native-input-' + self.digest(member))
                provenance = json.loads((inputs / 'input-repairs.json').read_text())
                self.assertEqual(provenance['original_input_sha256'], self.digest(member, normalized=False))
            self.assertEqual(payload, before)
        self.assertEqual(self.prepare_process.call_count, 4)
        (self.tests_dir / 'test_resident_later.py').write_text('# changed listing\n')
        self.prepare_process.reset_mock()
        with redirect_stdout(io.StringIO()):
            for payload in (single, batch):
                with self.assertRaises(SystemExit):
                    host.run(self.envelope(payload))
        self.prepare_process.assert_not_called()
        self.assertEqual(self.execv.call_args.args[1][1], 'read-batch')

    def test_original_glob_rejection_is_recovered_without_expansion_or_replay(self):
        payload = self.glob_payload()
        inputs = self.artifact / ('native-input-' + self.digest(payload))
        inputs.mkdir()
        failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                   'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                   'issues': [{'path': 'action.read_scopes', 'reason': 'unsupported_glob'}]}
        stdout = json.dumps(failure)
        outcome = {'schema_version': 'nokiy_native_prepare_outcome_v1', 'status': 'REJECTED_PREPARATION',
                   'phase': 'prepare', 'provider_execution_started': False, 'same_input_retry_allowed': False,
                   'exit_code': 2, 'first_typed_blocker': failure['first_typed_blocker'],
                   'diagnostic_sha256': hashlib.sha256(stdout.encode()).hexdigest()}
        for name, data in (('draft.json', dict(payload['draft'], native_thread_id=self.thread)),
                           ('action.json', payload['action']), ('prepare-outcome.json', outcome)):
            (inputs / name).write_text(json.dumps(data))
        (inputs / 'prepare.stdout').write_text(stdout)
        (inputs / 'prepare.stderr').write_text('')
        before = {p.name: p.read_bytes() for p in inputs.iterdir()}
        with mock.patch.object(host, 'normalize_read_globs', side_effect=AssertionError('no expansion')), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(host.run(self.envelope(payload)), 2)
            result = host.preparation_evidence(self.artifact, inputs.name, self.thread)
        self.assertEqual(json.loads(output.getvalue())['preparation_ref']['input_id'], inputs.name)
        self.assertEqual(result['issues'], failure['issues'])
        self.assertFalse(result['preparation_rerun'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in inputs.iterdir()})
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def test_expansion_obeys_existing_byte_bounds_without_dropping_reads(self):
        payload = self.glob_payload()
        for index in range(10):
            (self.tests_dir / ('test_resident_' + 'x' * 100 + str(index) + '.py')).write_text('# fixture\n')
        limit = len(host.checked_expansion_bytes(self.envelope(payload), 'Original')) + 128
        with mock.patch.object(host, 'MAX_PAYLOAD_BYTES', limit), \
                self.assertRaisesRegex(ValueError, 'Read glob expansion request exceeds ordinary payload byte bound'):
            host.run(self.envelope(payload))
        self.assertEqual(list(self.artifact.iterdir()), [])
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()


def load_tests(loader, tests, pattern):
    # The shared reference suite is discovered once, in its own module.
    return unittest.TestSuite(ReadGlobTests(name) for name in ReadGlobTests.__dict__ if name.startswith('test_'))
