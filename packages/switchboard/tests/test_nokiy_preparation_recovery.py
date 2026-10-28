"""Original rejection inspection must not enter preparation or provider execution."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'src/nokiy-native-host.py'
SPEC = importlib.util.spec_from_file_location('preparation_host', SOURCE)
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


class PreparationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='nokiy-preparation-reader-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.input_id = 'native-input-' + 'a' * 64
        self.inputs = self.root / self.input_id
        self.inputs.mkdir()
        self.thread = '00000000-0000-0000-0000-000000000001'
        self.failure = {'schema_version': 'tura_embedded_failure_v1',
                        'status': 'BLOCKED_PREEXECUTION', 'authority_effect': 'none',
                        'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                        'detail_sha256': 'b' * 64, 'secret': 'NEVER_ECHO_THIS'}
        self.outcome = {'schema_version': 'nokiy_native_prepare_outcome_v1',
                        'status': 'REJECTED_PREPARATION', 'phase': 'prepare',
                        'provider_execution_started': False, 'same_input_retry_allowed': False,
                        'exit_code': 2, 'first_typed_blocker': self.failure['first_typed_blocker']}
        self.write('draft.json', {'native_thread_id': self.thread, 'prompt': 'NEVER_ECHO_THIS'})
        self.write('action.json', {'context_summary': 'NEVER_ECHO_THIS'})
        self.refresh()

    def write(self, name, value):
        (self.inputs / name).write_text(json.dumps(value), encoding='utf-8')

    def refresh(self):
        stdout = json.dumps(self.failure).encode()
        (self.inputs / 'prepare.stdout').write_bytes(stdout)
        (self.inputs / 'prepare.stderr').write_bytes(b'')
        self.outcome['diagnostic_sha256'] = hashlib.sha256(stdout).hexdigest()
        self.write('prepare-outcome.json', self.outcome)

    def inspect(self):
        return host.preparation_evidence(self.root, self.input_id, self.thread)

    def test_opaque_historical_rejection_preserved_without_echo_or_replay(self):
        before = {p.name: p.read_bytes() for p in self.inputs.iterdir()}
        result = self.inspect()
        self.assertEqual(result['status'], 'EVIDENCE_VERIFIED')
        self.assertEqual(result['diagnostic_status'], 'ORIGINAL_DETAIL_UNAVAILABLE')
        self.assertIs(result['preparation_rerun'], False)
        self.assertIs(result['provider_execution_started'], False)
        self.assertIs(result['same_input_retry_allowed'], False)
        self.assertNotIn('NEVER_ECHO_THIS', json.dumps(result))
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.inputs.iterdir()})

    def test_valid_typed_child_evidence_exposed(self):
        self.failure['diagnostics'] = {'stage': 'dcf_compile', 'returncode': 2,
            'stdout_bytes': 123, 'stderr_bytes': 0, 'stdout_sha256': 'c' * 64,
            'stderr_sha256': hashlib.sha256(b'').hexdigest(),
            'child_error_code': 'JSPACE_SOURCE_READ_FILE_INVALID'}
        self.failure['issues'] = [{'path': 'action.read_scopes', 'reason': 'exact_file_unavailable'}]
        self.refresh()
        result = self.inspect()
        self.assertEqual(result['diagnostic_status'], 'TYPED_CAUSE_AVAILABLE')
        self.assertEqual(result['issues'], self.failure['issues'])
        self.assertEqual(result['diagnostics'], self.failure['diagnostics'])

    def test_namespaced_child_evidence_is_metadata_only_without_replay(self):
        for code in ('JSPACE_COMMAND_EFFECT_UNVERIFIED',
                     'JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT',
                     'TASK_CONTEXT_FIELDS_INVALID', 'JSPACE_FUTURE_TYPED_FAILURE'):
            with self.subTest(code=code):
                self.failure['diagnostics'] = {'stage': 'dcf_navigated_prepare',
                    'returncode': 2, 'stdout_bytes': 123, 'stderr_bytes': 0,
                    'stdout_sha256': 'c' * 64, 'stderr_sha256': hashlib.sha256(b'').hexdigest(),
                    'child_error_code': code}
                self.failure['issues'] = [{'path': 'action.operations', 'reason': 'scope_mismatch'}]
                self.refresh()
                before = {p.name: p.read_bytes() for p in self.inputs.iterdir()}
                with mock.patch.object(host.subprocess, 'run', side_effect=AssertionError('no replay')), \
                     mock.patch.object(host.os, 'execv', side_effect=AssertionError('no worker')):
                    result = self.inspect()
                self.assertEqual(result['diagnostic_status'], 'TYPED_CAUSE_AVAILABLE')
                self.assertEqual(result['diagnostics'], self.failure['diagnostics'])
                self.assertEqual(result['issues'], self.failure['issues'])
                self.assertEqual(result['first_typed_blocker'], self.failure['first_typed_blocker'])
                self.assertIs(result['provider_execution_started'], False)
                self.assertIs(result['same_input_retry_allowed'], False)
                self.assertIs(result['preparation_rerun'], False)
                self.assertNotIn('NEVER_ECHO_THIS', json.dumps(result))
                self.assertEqual(before, {p.name: p.read_bytes() for p in self.inputs.iterdir()})

    def test_unknown_diagnostics_not_echoed(self):
        self.failure['issues'] = [{'path': 'NEVER_ECHO_THIS', 'reason': 'secret'}]
        self.failure['diagnostics'] = {'secret': 'NEVER_ECHO_THIS'}
        self.refresh()
        self.assertEqual(self.inspect()['diagnostic_status'], 'ORIGINAL_DETAIL_UNAVAILABLE')

    def test_capability_failure_metadata_is_verified_without_replay(self):
        self.failure['diagnostics'] = {'stage': 'dcf_compile', 'returncode': 2,
            'stdout_bytes': 123, 'stderr_bytes': 0, 'stdout_sha256': 'c' * 64,
            'stderr_sha256': hashlib.sha256(b'').hexdigest(),
            'child_error_code': 'JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT',
            'capability_diagnostic': {'capability_id': 'verifier-gates', 'failed_field': 'freshness_status',
                'observed': 'unverified', 'expected': 'current', 'generation_id': 'generation-a'}}
        self.refresh()
        before = {p.name: p.read_bytes() for p in self.inputs.iterdir()}
        with mock.patch.object(host.subprocess, 'run', side_effect=AssertionError('no replay')):
            result = self.inspect()
        self.assertEqual(result['diagnostics'], self.failure['diagnostics'])
        self.assertEqual(result['diagnostic_status'], 'TYPED_CAUSE_AVAILABLE')
        self.assertIs(result['provider_execution_started'], False)
        self.assertIs(result['same_input_retry_allowed'], False)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.inputs.iterdir()})
        for field, value in (('observed', 'PRIVATE_RAW_ERROR'), ('generation_id', '/private/secret'),
                             ('extra', 'PRIVATE_RAW_ERROR'), ('failed_field', 'row.data')):
            changed = {**self.failure['diagnostics'], 'capability_diagnostic': {
                **self.failure['diagnostics']['capability_diagnostic'], field: value}}
            self.assertIsNone(host._safe_preparation_diagnostics(changed))

    def test_hash_only_diagnostics_do_not_prove_a_known_cause(self):
        self.failure['diagnostics'] = {'stage': 'dcf_compile', 'returncode': 2,
            'stdout_bytes': 123, 'stderr_bytes': 0, 'stdout_sha256': 'c' * 64,
            'stderr_sha256': hashlib.sha256(b'').hexdigest()}
        self.refresh()
        result = self.inspect()
        self.assertEqual(result['diagnostic_status'], 'ORIGINAL_DETAIL_UNAVAILABLE')
        self.assertEqual(result['diagnostics'], self.failure['diagnostics'])

    def test_foreign_thread_rejected(self):
        self.write('draft.json', {'native_thread_id': 'foreign'})
        with self.assertRaisesRegex(ValueError, 'different native thread'):
            self.inspect()

    def test_tampered_diagnostic_rejected(self):
        (self.inputs / 'prepare.stderr').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'diagnostic identity mismatch'):
            self.inspect()

    def test_unknown_or_executed_outcome_rejected(self):
        for key, value in [('provider_execution_started', True), ('phase', 'run'),
                           ('same_input_retry_allowed', True), ('exit_code', 0),
                           ('first_typed_blocker', 'FOREIGN')]:
            with self.subTest(key=key):
                original = self.outcome[key]
                self.outcome[key] = value
                self.refresh()
                with self.assertRaises(ValueError):
                    self.inspect()
                self.outcome[key] = original

    def test_prepared_request_cannot_be_read_as_rejection(self):
        (self.root / ('native-prepared-' + 'a' * 64)).mkdir()
        with self.assertRaisesRegex(ValueError, 'original request recovery'):
            self.inspect()

    def test_symlinked_diagnostic_rejected(self):
        outside = self.root / 'outside'
        outside.write_bytes((self.inputs / 'prepare.stdout').read_bytes())
        (self.inputs / 'prepare.stdout').unlink()
        (self.inputs / 'prepare.stdout').symlink_to(outside)
        with self.assertRaises(OSError):
            self.inspect()

    def test_duplicate_json_keys_rejected(self):
        (self.inputs / 'draft.json').write_text('{"native_thread_id":"x","native_thread_id":"y"}')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.inspect()

    def test_unsafe_or_oversized_reference_rejected(self):
        with self.assertRaisesRegex(ValueError, 'input ID'):
            host.preparation_evidence(self.root, '../draft.json', self.thread)
        (self.inputs / 'action.json').write_bytes(b' ' * (host.MAX_PAYLOAD_BYTES + 1))
        with self.assertRaisesRegex(ValueError, 'bounded regular'):
            self.inspect()

    def test_reader_dispatch_starts_no_subprocess_or_preparation(self):
        payload = {'operation': 'read_preparation', 'artifact_root': str(self.root), 'input_id': self.input_id}
        envelope = {'schema_version': 'nokiy_native_tool_v2', 'worker': 'gpt-6.1-sol',
                    'reasoning_effort': 'max', 'service_tier': 'default', 'payload': payload}
        with mock.patch.dict(host.os.environ, {'CODEX_THREAD_ID': self.thread}), \
             mock.patch.object(host.subprocess, 'run', side_effect=AssertionError('no preparation')), \
             mock.patch.object(host.subprocess, 'call', side_effect=AssertionError('no worker')), \
             mock.patch.object(host.os, 'execv', side_effect=AssertionError('no worker')), \
             redirect_stdout(io.StringIO()) as output:
            self.assertEqual(host.execute(envelope), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'EVIDENCE_VERIFIED')

    def test_payload_cannot_override_thread_or_grant_effects(self):
        payload = {'operation': 'read_preparation', 'artifact_root': str(self.root),
                   'input_id': self.input_id, 'native_thread_id': self.thread}
        with self.assertRaisesRegex(ValueError, 'reference required'):
            host.read_preparation(payload, self.thread)


if __name__ == '__main__':
    unittest.main()
