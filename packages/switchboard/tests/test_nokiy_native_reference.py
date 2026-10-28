"""Focused native reference intake regression tests."""

import base64
import copy
from contextlib import ExitStack, contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'nokiy-native-host.py'
SPEC = importlib.util.spec_from_file_location('nokiy_reference_host', SOURCE)
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


class NativeReferenceTests(unittest.TestCase):
    def test_parent_completion_reference_preserves_contract_and_requires_exact_mode(self):
        packet = {'operation': 'run_batch', 'artifact_root': '/artifacts', 'members': [],
                  'post_batch_verifier': {'parent_source_targets': ['parent.py']}}
        path = self.root / 'parent-batch.json'
        data = json.dumps(packet).encode()
        path.write_bytes(data)
        reference = {'operation': 'run_batch_ref', 'input_path': str(path),
                     'input_sha256': hashlib.sha256(data).hexdigest()}
        with self.assertRaisesRegex(ValueError, 'mode must match'):
            host.expand_run_reference(self.envelope(reference))
        admitted = dict(reference, parent_completion=True)
        expanded = host.expand_run_reference(self.envelope(admitted))
        self.assertEqual(expanded, self.envelope(packet))
        for value in (False, 1, 'true', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                host.expand_run_reference(self.envelope(dict(reference, parent_completion=value)))
        path.write_bytes(data + b' ')
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            host.expand_run_reference(self.envelope(admitted))
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nokiy-reference-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.artifact = self.root / 'artifacts'
        self.home = self.root / 'home'
        self.codex_home = self.root / 'codex-home'
        for path in (self.workspace, self.artifact, self.home, self.codex_home):
            path.mkdir()
        self.entry = self.home / '.local' / 'bin' / 'nokiy-embedded-run'
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text('Boundary stub; never executed.\n', encoding='utf-8')
        self.thread = '01234567-89ab-cdef-0123-456789abcdef'
        self.worker = 'gpt-6-sol'
        self.effort = 'max'
        self.tier = 'priority'
        self.counter = 0
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'CODEX_HOME': str(self.codex_home),
            'CODEX_THREAD_ID': self.thread,
        }))
        # A missed stub fails locally, rather than ever entering an installed
        # caller, application executable, shell or provider.
        self.prepare_process = stack.enter_context(mock.patch.object(
            host.subprocess, 'run', side_effect=AssertionError('Unexpected prepare subprocess')))
        self.recovery_process = stack.enter_context(mock.patch.object(
            host.subprocess, 'call', side_effect=AssertionError('Unexpected recovery subprocess')))
        self.execv = stack.enter_context(mock.patch.object(host.os, 'execv', return_value=None))

    def payload(self):
        return {
            'operation': 'run',
            'draft': {
                'workspace': str(self.workspace), 'artifact_root': str(self.artifact),
                'prompt': 'Inspect "snow 雪"\\path\né 😀; never call a provider.',
                'authority_effect': 'none',
            },
            'action': {
                'mission': {'mission_id': 'parent', 'task_id': 'bounded', 'mode': 'DELIVERY',
                            'objective': 'inspect bytes', 'current_predicate': 'intake'},
                'context_summary': 'Exact Unicode 雪 é 😀, quotes " and \\, newline\n.',
                'operations': ['read', 'command'], 'read_scopes': ['source.py'], 'write_scopes': [],
                'target_paths': [], 'source_read': True, 'command_templates': [],
                'forbidden_effects': ['deployment', 'broker', 'order'],
            },
            'surface_id': 'exact-test-surface',
        }

    def envelope(self, payload, version='nokiy_native_tool_v2'):
        result = {'schema_version': version, 'worker': self.worker,
                  'service_tier': self.tier, 'payload': payload}
        if version != 'nokiy_native_tool_v1':
            result['reasoning_effort'] = self.effort
        if version == 'nokiy_native_tool_v3':
            result['workflow'] = 'nokiy'
        return result

    def test_legacy_action_reports_fields_before_preparation_without_echoing_commands(self):
        payload = self.payload()
        payload['action']['source_read'] = False
        payload['action']['command_templates'] = ['curl SECRET_TOKEN', 'jq *']
        with self.assertRaises(host.IntakeError) as caught:
            host.execute(self.envelope(payload))
        receipt = caught.exception.receipt()
        self.assertEqual(receipt['issues'], [
            {'path': 'action.command_templates[0]', 'reason': 'exact_scoped_command_or_http_read_required'},
            {'path': 'action.command_templates[1]', 'reason': 'exact_scoped_command_or_http_read_required'},
        ])
        self.assertEqual(receipt['status'], 'NEEDS_REPREPARATION')
        self.assertIn('read_http', receipt['available_capabilities'])
        self.assertIs(receipt['provider_execution_started'], False)
        self.assertEqual(receipt['phase'], 'validate')
        self.assertNotIn('SECRET_TOKEN', json.dumps(receipt))
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()
        self.assertEqual(list(self.artifact.iterdir()), [])

    def test_action_format_does_not_grant_commands_or_source_reader(self):
        action = self.payload()['action']
        action.pop('source_read')
        action['command_templates'] = [{'argv': ['curl', 'https://example.invalid'],
                                       'effects': ['read'], 'targets': []}]
        original = copy.deepcopy(action)
        host.validate_action_format(action)
        self.assertEqual(action, original)
        self.assertNotIn('source_read', action)
        self.prepare_process.assert_not_called()

    def test_action_format_rejects_false_like_opt_ins_and_malformed_nested_templates(self):
        for value in (1, 'true', None):
            with self.subTest(value=value), self.assertRaises(host.IntakeError):
                host.validate_action_format({'source_read': value})
        for template in (
                {'argv': [], 'effects': ['read'], 'targets': []},
                {'argv': ['cat', 'a'], 'effects': 'read', 'targets': []},
                {'argv': ['touch', 'a'], 'effects': ['create'], 'targets': ['a']},
                {'argv': ['cat', 'a'], 'effects': ['read'], 'targets': [], 'extra': True}):
            with self.subTest(template=template), self.assertRaises(host.IntakeError):
                host.validate_action_format({'command_templates': [template]})
        host.validate_action_format({'source_read': True, 'operations': ['read', 'command'],
                                     'command_templates': []})

    def test_operations_reject_real_failed_shape_before_any_preparation(self):
        payload = self.payload()
        payload['action']['operations'].append('write')
        original = copy.deepcopy(payload)
        with self.assertRaises(host.IntakeError) as caught:
            host.execute(self.envelope(payload))
        receipt = caught.exception.receipt()
        self.assertEqual(receipt['issues'], [
            {'path': 'action.operations', 'reason': 'unsupported_operation'}])
        self.assertEqual(receipt['action_format']['properties']['operations']['items']['enum'],
                         ['read', 'create', 'modify', 'delete', 'command'])
        self.assertIs(receipt['provider_execution_started'], False)
        self.assertEqual(payload, original)
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()
        self.assertEqual(list(self.artifact.iterdir()), [])

    def test_operation_shape_and_unknown_values_never_echo_or_repair_effects(self):
        for field in ('operations', 'allowed_operations'):
            for value in (None, 'read', {}, True, [1], ['read', 'SECRET_TOKEN']):
                with self.subTest(field=field, value=value):
                    action = {field: value}
                    before = copy.deepcopy(action)
                    with self.assertRaises(host.IntakeError) as caught:
                        host.validate_action_format(action)
                    self.assertNotIn('SECRET_TOKEN', json.dumps(caught.exception.receipt()))
                    self.assertEqual(action, before)
        with self.assertRaises(host.IntakeError):
            host.validate_action_format({'operations': []})
        self.assertEqual(host.validate_action_format({'allowed_operations': []}),
                         ({'allowed_operations': []}, []))

    def test_source_read_supplies_read_and_command_only_without_templates(self):
        for operations, expected in ((['read'], ['read', 'command']), (['command'], ['command', 'read'])):
            with self.subTest(operations=operations):
                action, repairs = host.validate_action_format(
                    {'source_read': True, 'operations': operations, 'command_templates': []})
                self.assertEqual(action['operations'], expected)
                self.assertEqual(repairs, [{'path': 'action.operations', 'rule': 'source_read_implies_read_command'}])
        # No explicit operations, or a command template riding along, still names the dependency.
        pwd = {'argv': ['pwd'], 'effects': ['read'], 'targets': []}
        for action in ({'source_read': True}, {'source_read': True, 'operations': ['read'], 'command_templates': [pwd]}):
            with self.subTest(action=action), self.assertRaises(host.IntakeError) as caught:
                host.validate_action_format(action)
            self.assertEqual(caught.exception.issues, [
                {'path': 'action.operations', 'reason': 'source_read_requires_read_and_command'}])
        payload = self.payload()
        payload['action'].pop('operations')
        before = copy.deepcopy(payload)
        with self.assertRaises(host.IntakeError):
            host.execute(self.envelope(payload))
        self.assertEqual(payload, before)
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()
        self.assertEqual(list(self.artifact.iterdir()), [])

    def test_write_targets_rank_by_write_admission_when_reported(self):
        payload = {'status': 'SURFACE_CANDIDATES', 'targets': ['engine/a.py', 'tests/live/t.py', 'r.py'],
                   'candidates': [['live', 'engine'], ['tests'], ['live']],
                   'write_candidates': [['engine', 'live'], ['live', 'tests'], ['live']]}
        writes = {'engine/a.py', 'tests/live/t.py'}
        self.prepare_process.side_effect = None
        self.prepare_process.return_value = host.subprocess.CompletedProcess([], 0, json.dumps(payload), '')
        surface, resolution = host._query_ranked_surface(self.entry, self.workspace, payload['targets'], writes)
        self.assertEqual((surface, resolution['status']), ('live', 'SELECTED'))
        # Without reported write admission, membership alone spans surfaces as before.
        self.prepare_process.return_value = host.subprocess.CompletedProcess(
            [], 0, json.dumps({key: value for key, value in payload.items() if key != 'write_candidates'}), '')
        surface, resolution = host._query_ranked_surface(self.entry, self.workspace, payload['targets'], writes)
        self.assertEqual((surface, resolution['status']), (None, 'WRITES_SPAN_SURFACES'))

    def test_operator_writes_take_a_context_surface_when_writes_span_or_tie(self):
        # V430: the caller admits exact writes outside the context surface and reports each
        # write's writable owners, so spanning writes and ties no longer cost a round trip.
        span = {'status': 'SURFACE_CANDIDATES', 'targets': ['ui/app.py', 'engine/a.py', 'config/c.json'],
                'candidates': [['live', 'projection'], ['engine', 'live'], ['policy']],
                'write_candidates': [[], ['engine'], ['policy']],
                'writable_candidates': [['live'], ['engine', 'live'], ['policy']]}
        tie = {'status': 'SURFACE_CANDIDATES', 'targets': ['engine/a.py', 'engine/b.py'],
               'candidates': [['engine', 'live'], ['engine', 'live']],
               'write_candidates': [['engine', 'live'], ['engine', 'live']],
               'writable_candidates': [['engine', 'live'], ['engine', 'live']]}
        self.prepare_process.side_effect = None
        # Each surface covers one write row here: coverage, then total rows, then the first ID.
        for payload, expected, legacy in ((span, ('engine', 'SELECTED_OPERATOR_WRITES'), 'WRITES_SPAN_SURFACES'),
                                          (tie, ('engine', 'SELECTED_TIE'), 'TIED')):
            writes = set(payload['targets'])
            with self.subTest(expected=expected):
                self.prepare_process.return_value = host.subprocess.CompletedProcess([], 0, json.dumps(payload), '')
                surface, resolution = host._query_ranked_surface(self.entry, self.workspace, payload['targets'], writes)
                self.assertEqual((surface, resolution['status']), expected)
                self.assertIn(resolution['status'], host._RESELECTABLE)
                # A caller without operator write admission reports no writable owners: unchanged.
                old = {key: value for key, value in payload.items() if key != 'writable_candidates'}
                self.prepare_process.return_value = host.subprocess.CompletedProcess([], 0, json.dumps(old), '')
                surface, resolution = host._query_ranked_surface(self.entry, self.workspace, payload['targets'], writes)
                self.assertEqual((surface, resolution['status']), (None, legacy))

    def test_operator_writes_to_files_only_a_read_only_surface_owns_take_it_as_context(self):
        # V430 (2026-10-20): dashboard UI files belonged only to the read-only projection surface,
        # so a writing action had no candidate at all and every dispatch was refused.
        payload = {'status': 'SURFACE_CANDIDATES', 'targets': ['ui/view.tsx', 'ui/app.py'],
                   'candidates': [['projection'], ['projection']],
                   'write_candidates': [[], []], 'writable_candidates': [[], []]}
        writes = set(payload['targets'])
        self.prepare_process.side_effect = None
        self.prepare_process.return_value = host.subprocess.CompletedProcess([], 0, json.dumps(payload), '')
        surface, resolution = host._query_ranked_surface(self.entry, self.workspace, payload['targets'], writes)
        self.assertEqual((surface, resolution['status']), ('projection', 'SELECTED'))
        old = {key: value for key, value in payload.items() if key != 'writable_candidates'}
        self.prepare_process.return_value = host.subprocess.CompletedProcess([], 0, json.dumps(old), '')
        surface, resolution = host._query_ranked_surface(self.entry, self.workspace, payload['targets'], writes)
        # Without operator write admission no surface admits these writes: unchanged.
        self.assertEqual((surface, resolution['status']), (None, 'EMPTY'))

    def test_write_resolves_to_modify_or_create_by_declared_target(self):
        (self.workspace / 'present.py').write_text('x')
        for targets, expected in ((['present.py'], ['read', 'modify', 'command']),
                                  (['absent.py'], ['read', 'create', 'command']),
                                  (['present.py', 'absent.py'], ['read', 'create', 'modify', 'command'])):
            with self.subTest(targets=targets):
                action = {'operations': ['read', 'write', 'command'], 'write_scopes': targets, 'target_paths': targets}
                resolved, repairs = host.validate_action_format(action, self.workspace)
                self.assertEqual(resolved['operations'], expected)
                self.assertEqual(repairs, [{'path': 'action.operations',
                                            'rule': 'write_to_' + '_'.join(op for op in expected if op in ('create', 'modify'))}])
                self.assertEqual(action['operations'], ['read', 'write', 'command'], 'input is not mutated')
        (self.workspace / 'pkg').mkdir()
        for targets in ([], ['src/*.py'], ['../escape.py'], ['pkg']):
            with self.subTest(targets=targets), self.assertRaises(host.IntakeError) as caught:
                host.validate_action_format({'operations': ['write'], 'write_scopes': targets}, self.workspace)
            self.assertEqual(caught.exception.issues, [{'path': 'action.operations', 'reason': 'unsupported_operation'}])
        with self.assertRaises(host.IntakeError):
            host.validate_action_format({'operations': ['write'], 'write_scopes': ['present.py']})

    def test_write_with_directory_scopes_resolves_by_exact_target_paths(self):
        # Shape sent on 2026-10-18: write_scopes ["dir/**"] together with the exact target_paths.
        (self.workspace / 'job').mkdir()
        (self.workspace / 'job' / 'present.py').write_text('x')
        for targets, expected in ((['job/present.py'], ['read', 'modify', 'command']),
                                  (['job/new.py', 'job/PLAN.json'], ['read', 'create', 'command']),
                                  (['job/present.py', 'job/new.py'], ['read', 'create', 'modify', 'command'])):
            with self.subTest(targets=targets):
                action = {'operations': ['read', 'write', 'command'], 'write_scopes': ['job/**'],
                          'target_paths': targets}
                resolved, repairs = host.validate_action_format(action, self.workspace)
                self.assertEqual(resolved['operations'], expected)
                self.assertEqual(repairs, [{'path': 'action.operations',
                                            'rule': 'write_to_' + '_'.join(op for op in expected if op in ('create', 'modify'))}])
        # Without an exact target the directory scope says nothing about create or modify.
        for action in ({'operations': ['write'], 'write_scopes': ['job/**']},
                       {'operations': ['write'], 'write_scopes': ['job/**'], 'target_paths': ['job']},
                       {'operations': ['write'], 'write_scopes': ['job'], 'target_paths': ['job/*.py']}):
            with self.subTest(action=action), self.assertRaises(host.IntakeError) as caught:
                host.validate_action_format(action, self.workspace)
            self.assertEqual(caught.exception.issues, [{'path': 'action.operations', 'reason': 'unsupported_operation'}])

    def test_read_preparation_of_a_prepared_input_reads_its_original_result(self):
        digest = 'e' * 64
        artifact = host.directory(str(self.artifact))
        (artifact / ('native-input-' + digest)).mkdir()
        prepared = artifact / ('native-prepared-' + digest)
        prepared.mkdir()
        (prepared / 'capsule.json').write_text('{}')
        request_id = 'tura_embedded_' + 'f' * 64
        (artifact / request_id).mkdir()
        identity = artifact / request_id / 'request-identity.json'
        identity.write_text(json.dumps({'request_id': request_id,
                                        'context_capsule': {'path': str(prepared / 'capsule.json'), 'sha256': '0' * 64}}))
        self.recovery_process.side_effect = None
        self.recovery_process.return_value = 0
        payload = {'operation': 'read_preparation', 'artifact_root': str(self.artifact),
                   'input_id': 'native-input-' + digest}
        self.assertEqual(host.read_preparation(payload, self.thread), 0)
        self.assertEqual(self.recovery_process.call_args.args[0][1:],
                         ['read-result', '--artifact-root', str(artifact), '--request-id', request_id, '--summary'])
        # Without one matching original request the prepared input keeps its refusal.
        identity.write_text(json.dumps({'request_id': request_id, 'context_capsule': {'path': '/elsewhere'}}))
        with self.assertRaises(ValueError):
            host.read_preparation(payload, self.thread)

    def test_operation_preflight_leaves_contextual_authority_to_compiler(self):
        for action in (
                {}, {'operations': ['read']},
                {'operations': ['delete']},
                {'operations': ['read', 'command'], 'source_read': True,
                 'denied_operations': ['command']},
                {'operations': ['read'], 'allowed_operations': ['command'], 'source_read': True},
                {'allowed_operations': ['command'], 'source_read': True}):
            with self.subTest(action=action):
                before = copy.deepcopy(action)
                self.assertEqual(host.validate_action_format(action), (before, []))
                self.assertEqual(action, before)

    def test_inspect_actions_leave_subsumed_source_read_to_the_caller(self):
        for action in (
                {'inspect': {}, 'operations': ['read'], 'source_read': True},
                {'inspect': {}, 'operations': ['read', 'command'], 'source_read': 'yes',
                 'read_scopes': [], 'write_scopes': [], 'target_paths': [], 'command_templates': []}):
            with self.subTest(action=action):
                before = copy.deepcopy(action)
                self.assertEqual(host.validate_action_format(action), (before, []))
                self.assertEqual(action, before)
        with self.assertRaises(host.IntakeError) as caught:
            host.validate_action_format({'inspect': {'network': 'all'}, 'operations': ['read'], 'source_read': True})
        self.assertEqual(caught.exception.issues, [
            {'path': 'action.operations', 'reason': 'source_read_requires_read_and_command'}])

    def test_equivalent_legacy_formats_are_adapted_without_widening_grants(self):
        action = self.payload()['action']
        action.update(source_read=False, command_templates=['cat source.py', 'pwd'])
        original = copy.deepcopy(action)
        normalized, repairs = host.validate_action_format(action)
        self.assertEqual(action, original)
        self.assertNotIn('source_read', normalized)
        self.assertEqual(normalized['read_scopes'], action['read_scopes'])
        self.assertEqual(normalized['write_scopes'], [])
        self.assertEqual(normalized['command_templates'], [
            {'argv': ['cat', 'source.py'], 'effects': ['read'],
             'targets': [{'operation': 'read', 'path': 'source.py', 'argv_index': 1}]},
            {'argv': ['pwd'], 'effects': ['read'], 'targets': []},
        ])
        self.assertEqual(len(repairs), 3)
        self.assertEqual(host.validate_action_format(normalized), (normalized, []))

    def test_read_operand_string_targets_become_typed_targets(self):
        # Shapes coordinators sent on 2026-10-12..18: string targets naming argv operands.
        action = {'command_templates': [
            {'argv': ['cat', 'AGENTS.md'], 'effects': ['read'], 'targets': ['AGENTS.md']},
            {'argv': ['rg', '-n', '--glob', '*.py', 'g17', 'tests/a', 'tests/b'], 'effects': ['read'],
             'targets': ['tests/a', 'tests/b']}]}
        original = copy.deepcopy(action)
        normalized, repairs = host.validate_action_format(action)
        self.assertEqual(action, original)
        self.assertEqual([t['targets'] for t in normalized['command_templates']], [
            [{'operation': 'read', 'path': 'AGENTS.md', 'argv_index': 1}],
            [{'operation': 'read', 'path': 'tests/a', 'argv_index': 5},
             {'operation': 'read', 'path': 'tests/b', 'argv_index': 6}]])
        self.assertEqual(repairs, [
            {'path': f'action.command_templates[{i}].targets', 'rule': 'read_operand_strings_to_typed_targets'}
            for i in range(2)])
        self.assertEqual(host.validate_action_format(normalized), (normalized, []))

    def test_unbindable_read_targets_are_omitted_for_the_compiler_or_inspect(self):
        # git show REV:path and a test module name are not argv operands; the caller routes such
        # read-only commands to inspect when the workspace cannot verify them.
        for template in (
                {'argv': ['git', 'show', 'abc123:engine/live.py'], 'effects': ['read'], 'targets': ['engine/live.py']},
                {'argv': ['python', '-B', '-m', 'unittest', 'test_metrics'], 'effects': ['read'],
                 'targets': ['metrics.py', 'test_metrics.py']},
                {'argv': ['cat', 'a', 'a'], 'effects': ['read'], 'targets': ['a']}):
            with self.subTest(template=template):
                normalized, repairs = host.validate_action_format({'command_templates': [template]})
                self.assertEqual(normalized['command_templates'], [{**template, 'targets': []}])
                self.assertEqual(repairs, [{'path': 'action.command_templates[0].targets',
                                            'rule': 'unbindable_read_targets_omitted'}])

    def test_string_targets_of_a_writing_command_name_the_typed_target_format(self):
        # Targets that name argv operands can be typed by the coordinator, so the correction stands.
        for template in (
                {'argv': ['touch', 'out.txt'], 'effects': ['create'], 'targets': ['out.txt']},
                {'argv': ['rm', 'a', 'b'], 'effects': ['delete'], 'targets': ['a', 'b']}):
            with self.subTest(template=template), self.assertRaises(host.IntakeError) as caught:
                host.validate_action_format({'command_templates': [template]})
            receipt = caught.exception.receipt()
            self.assertEqual(receipt['issues'], [
                {'path': 'action.command_templates[0].targets', 'reason': 'typed_target_objects_required'}])
            self.assertEqual(set(receipt['command_target_format']['target']), {'operation', 'path', 'argv_index'})
        with self.assertRaises(host.IntakeError) as caught:
            host.validate_action_format({'command_templates': [{'argv': ['cat', 'a'], 'effects': ['read'],
                                                                'targets': [7]}]})
        self.assertNotIn('command_target_format', caught.exception.receipt())

    def test_unbindable_write_targets_are_omitted_for_parent_routing(self):
        # Shapes sent on 2026-10-17/18: a script or test run that writes into a directory. No typed
        # target can name the files it writes; the caller hands such commands to the parent.
        for template in (
                {'argv': ['/venv/bin/python', '-B', '-W', 'error', 'job/run.py'], 'effects': ['read', 'write'],
                 'targets': ['job']},
                {'argv': ['python', '-m', 'pytest', '-q', 'job/test_run.py'], 'effects': ['read', 'write'],
                 'targets': ['job']},
                {'argv': ['touch', 'out.txt'], 'effects': ['create'], 'targets': ['out.txt', 'log/']}):
            with self.subTest(template=template):
                original = copy.deepcopy(template)
                normalized, repairs = host.validate_action_format({'command_templates': [template]})
                self.assertEqual(template, original)
                self.assertEqual(normalized['command_templates'], [{**template, 'targets': []}])
                self.assertEqual(repairs, [{'path': 'action.command_templates[0].targets',
                                            'rule': 'unbindable_write_targets_omitted'}])
                self.assertEqual(host.validate_action_format(normalized), (normalized, []))

    def test_legacy_unscoped_shell_redirect_and_glob_never_become_commands(self):
        for command in ('cat missing.py', 'cat source.py > result', 'cat *',
                        'pwd; rm -rf .', 'curl https://example.invalid/*'):
            with self.subTest(command=command), self.assertRaises(host.CapabilityGapError):
                host.validate_action_format({'read_scopes': ['source.py'], 'command_templates': [command]})
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()

    def test_local_directory_normalization_preserves_existing_grant_and_exact_reads(self):
        action = self.payload()['action']
        action.update(operations=['read', 'command'], read_directories=['diagnostics'],
                      read_scopes=['diagnostics/**'])
        original = copy.deepcopy(action)
        normalized, repairs = host.normalize_local_discovery(action, self.workspace)
        self.assertEqual(action, original)
        self.assertEqual(normalized['read_directories'], ['diagnostics'])
        self.assertEqual(normalized['read_scopes'], [])
        self.assertNotIn('source_read', normalized)
        self.assertEqual(normalized['write_scopes'], [])
        self.assertEqual(len(repairs), 2)
        action['read_scopes'].append('source.py')
        normalized, repairs = host.normalize_local_discovery(action, self.workspace)
        self.assertEqual(normalized['read_scopes'], ['source.py'])
        self.assertIs(normalized['source_read'], True)
        self.assertEqual(len(repairs), 1)

    def test_directory_normalization_does_not_downgrade_dcf_or_guess_roots(self):
        action = self.payload()['action']
        action.update(operations=['read', 'command'], read_directories=['diagnostics'],
                      read_scopes=['diagnostics/**'])
        compiler = self.workspace / 'scripts/ops/dcf.py'
        compiler.parent.mkdir(parents=True)
        compiler.write_text('DCF marker only')
        self.assertEqual(host.normalize_local_discovery(action, self.workspace), (action, []))
        nested = self.workspace / 'nested'
        nested.mkdir()
        self.assertEqual(host.normalize_local_discovery(action, nested), (action, []))
        compiler.unlink()
        for directory in ('.', '**', '../outside', '.credentials', 'diagnostics//sub'):
            invalid = dict(action, read_directories=[directory])
            self.assertEqual(host.normalize_local_discovery(invalid, self.workspace), (invalid, []))

    def reference(self, payload=None, data=None):
        if data is None:
            data = json.dumps(self.payload() if payload is None else payload,
                              ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
        self.counter += 1
        path = self.root / (f"input-{self.counter}-雪'\".json")
        path.write_bytes(data)
        return {'operation': 'run_ref', 'input_path': str(path),
                'input_sha256': hashlib.sha256(data).hexdigest()}

    def digest(self, payload, normalized=True):
        value = copy.deepcopy(payload)
        if normalized:
            value['draft'] = host.normalize_draft(value['draft'])
            value['action'] = host.validate_action_format(value['action'])[0]
            value['action'] = host.normalize_local_discovery(value['action'], self.workspace)[0]
        return hashlib.sha256(host.canonical({
            'thread': self.thread, 'envelope': self.envelope(value),
        }).encode()).hexdigest()

    def cached_request(self, payload, normalized=True, overrides=None):
        digest = self.digest(payload, normalized)
        prepared = self.artifact / ('native-prepared-' + digest)
        prepared.mkdir(exist_ok=True)
        request = {'native_thread_id': self.thread, 'model': self.worker,
                   'reasoning_effort': self.effort, 'service_tier': self.tier,
                   'execution_profile': 'direct'}
        request.update(overrides or {})
        path = prepared / 'request.json'
        path.write_text(host.canonical(request) + '\n', encoding='utf-8')
        request_id = 'tura_embedded_' + 'ab' * 32
        receipt = {'status': 'PREPARED', 'request_id': request_id,
                   'preflight': {'status': 'READY', 'request_id': request_id,
                                 'request_sha256': request_id.removeprefix('tura_embedded_')}}
        (prepared / 'preparation.json').write_text(json.dumps(receipt), encoding='utf-8')
        return path, request_id

    def assert_blocked(self, reference, version='nokiy_native_tool_v2'):
        self.execv.reset_mock()
        with mock.patch.object(host, 'serial_execution') as serial, \
                mock.patch.object(host, 'execute') as execution:
            with self.assertRaises((ValueError, OSError)):
                host.run(self.envelope(reference, version))
            serial.assert_not_called()
            execution.assert_not_called()
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()
        self.recovery_process.assert_not_called()

    def test_inline_and_reference_have_identical_preparation_inputs(self):
        for version in ('nokiy_native_tool_v1', 'nokiy_native_tool_v2', 'nokiy_native_tool_v3'):
            for surface in (False, True):
                with self.subTest(version=version, surface=surface):
                    payload = self.payload()
                    if not surface:
                        payload.pop('surface_id')
                    inline = self.envelope(payload, version)
                    reference = self.envelope(self.reference(payload), version)
                    original = copy.deepcopy(reference)
                    environment = dict(os.environ)
                    with mock.patch.object(host, 'prepare_action',
                                           return_value=(self.root / 'request.json', None)) as prepare:
                        host.run(inline)
                        expected = copy.deepcopy(prepare.call_args.args)
                        prepare.reset_mock()
                        host.run(reference)
                        prepare.assert_called_once_with(*expected)
                    self.assertEqual(expected[0], inline)
                    self.assertEqual(expected[2:6], (self.thread, self.worker, self.effort, self.tier))
                    self.assertEqual(expected[6], payload)
                    self.assertEqual(expected[7], host.normalize_draft(payload['draft']))
                    self.assertEqual(reference, original)
                    self.assertEqual(dict(os.environ), environment)
                    self.assertEqual(self.execv.call_args.args[0], str(self.entry))

    def test_exact_bytes_are_read_once_and_hash_checked_before_parsing(self):
        payload = self.payload()
        data = (json.dumps(payload, ensure_ascii=False, indent=2).replace('\n', '\r\n')
                + '\r\n\t').encode('utf-8')
        reference = self.reference(data=data)
        real_fdopen = os.fdopen
        streams = []

        def tracked_stream(*args, **kwargs):
            stream = real_fdopen(*args, **kwargs)
            spy = mock.MagicMock(wraps=stream)
            spy.__enter__.return_value = spy
            spy.__exit__.side_effect = stream.__exit__
            streams.append(spy)
            return spy

        with mock.patch.object(host.os, 'fdopen', side_effect=tracked_stream), \
                mock.patch.object(host.json, 'loads', wraps=json.loads) as parse, \
                mock.patch.object(host, 'prepare_action',
                                  return_value=(self.root / 'request.json', None)) as prepare:
            host.run(self.envelope(reference))
            parse.assert_called_once()
            self.assertEqual(parse.call_args.args[0], data.decode('utf-8'))
            self.assertEqual(prepare.call_args.args[6], payload)
        self.assertEqual(len(streams), 1)
        streams[0].read.assert_called_once_with(96 * 1024 + 1)
        reference['input_sha256'] = hashlib.sha256(host.canonical(payload).encode()).hexdigest()
        self.assertNotEqual(reference['input_sha256'], hashlib.sha256(data).hexdigest())
        with mock.patch.object(host.json, 'loads', wraps=json.loads) as parse:
            self.assert_blocked(reference)
            parse.assert_not_called()

    def test_location_escaping_and_whitespace_do_not_change_identity_or_caller_id(self):
        payload = self.payload()
        path, request_id = self.cached_request(payload)
        bodies = [json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode(),
                  (json.dumps(payload, ensure_ascii=True, indent=2) + '\n\t ').encode()]
        references = [self.reference(data=data) for data in bodies]
        self.assertNotEqual(references[0]['input_sha256'], references[1]['input_sha256'])
        original_names = set(self.artifact.iterdir())
        for value in [payload, *references]:
            host.run(self.envelope(value))
            self.assertEqual(self.execv.call_args.args, (str(self.entry), [str(self.entry), 'run',
                             '--request', str(path), '--summary']))
            self.assertEqual(host.prepared_request_id(path), request_id)
        self.assertEqual(set(self.artifact.iterdir()), original_names)
        self.prepare_process.assert_not_called()

    def test_default_normalization_and_legacy_false_share_the_inline_identity(self):
        payload = self.payload()
        payload['draft']['model_acceleration'] = False
        path, request_id = self.cached_request(payload)
        explicit = copy.deepcopy(payload)
        explicit['draft'] = host.normalize_draft(explicit['draft'])
        for value in (payload, self.reference(payload), explicit, self.reference(explicit)):
            host.run(self.envelope(value))
            self.assertIn(str(path), self.execv.call_args.args[1])
            self.assertEqual(host.prepared_request_id(path), request_id)
        self.prepare_process.assert_not_called()

    def test_pre_upgrade_digest_and_original_request_id_are_reused(self):
        payload = self.payload()
        self.assertNotEqual(self.digest(payload), self.digest(payload, normalized=False))
        path, request_id = self.cached_request(payload, normalized=False)
        original_names = set(self.artifact.iterdir())
        for value in (payload, self.reference(payload)):
            host.run(self.envelope(value))
            self.assertIn(str(path), self.execv.call_args.args[1])
            self.assertEqual(host.prepared_request_id(path), request_id)
        self.assertEqual(set(self.artifact.iterdir()), original_names)
        self.prepare_process.assert_not_called()

    def external_verifier_payload(self):
        external = self.root / 'validator.py'
        external.write_text('pass\n')
        scratch = self.artifact / 'verifier-0'
        scratch.mkdir()
        payload = self.payload()
        payload['action']['read_scopes'].append(str(external))
        payload['action']['verifier_commands'] = [{
            'argv': ['/never-execute/python3', str(external)], 'executable_sha256': '0' * 64,
            'pinned_files': [{'path': str(external), 'scope': 'verifier_only',
                              'sha256': host.sha(external)}],
            'network': False, 'timeout_seconds': 300, 'scratch_root': str(scratch),
        }]
        return payload

    def external_verifier_cache(self, state='prepared'):
        payload = self.external_verifier_payload()
        # The old host removed the explicit read as a verifier dependency without inlining it.
        legacy = copy.deepcopy(payload)
        legacy['action']['read_scopes'] = ['source.py']
        legacy['draft'] = host.normalize_draft(legacy['draft'])
        legacy['action'] = host.validate_action_format(legacy['action'], self.workspace)[0]
        digest = self.digest(legacy)
        inputs = self.artifact / ('native-input-' + digest)
        inputs.mkdir()
        repairs = [{'path': 'action.read_scopes', 'rule': 'external_read_to_verifier_dependency', 'count': 1}]
        request = {**legacy['draft'], 'runtime_image': {'path': '/not-executed', 'sha256': '0' * 64},
                   'codex': {'path': '/not-executed', 'sha256': '0' * 64},
                   'native_thread_id': self.thread, 'model': self.worker, 'reasoning_effort': self.effort,
                   'execution_profile': 'direct', 'service_tier': self.tier, 'allow_provider_network': True}
        (inputs / 'draft.json').write_text(host.canonical(request) + '\n')
        (inputs / 'action.json').write_text(host.canonical(legacy['action']) + '\n')
        (inputs / 'input-repairs.json').write_text(host.canonical({
            'original_input_sha256': self.digest(payload, normalized=False),
            'normalized_input_sha256': digest, 'repairs': repairs}) + '\n')
        path, request_id = self.artifact / ('native-prepared-' + digest) / 'request.json', None
        if state != 'uncertain':
            outcome = {'schema_version': 'nokiy_native_prepare_outcome_v1', 'phase': 'prepare',
                'status': 'PREPARED' if state == 'prepared' else 'REJECTED_PREPARATION',
                'provider_execution_started': False, 'same_input_retry_allowed': False,
                'safe_to_correct_input': state != 'prepared', 'exit_code': 0 if state == 'prepared' else 2,
                'input_repairs': repairs}
            if state == 'prepared':
                path, request_id = self.cached_request(legacy)
                path.write_text(host.canonical(request) + '\n')
            else:
                failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                           'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_PREPARATION_FAILED'}
                stdout = host.canonical(failure)
                (inputs / 'prepare.stdout').write_text(stdout)
                (inputs / 'prepare.stderr').write_text('')
                outcome.update(first_typed_blocker=failure['first_typed_blocker'],
                               diagnostic_sha256=hashlib.sha256(stdout.encode()).hexdigest())
            (inputs / 'prepare-outcome.json').write_text(host.canonical(outcome) + '\n')
        return payload, path, request_id, inputs

    def test_explicit_verifier_read_reuses_its_pre_upgrade_normalized_request(self):
        payload, path, request_id, _ = self.external_verifier_cache()
        original_names = set(self.artifact.iterdir())
        before = {p: p.read_bytes() for p in self.artifact.rglob('*') if p.is_file()}
        for value in (payload, self.reference(payload)):
            host.run(self.envelope(value))
            self.assertIn(str(path), self.execv.call_args.args[1])
            self.assertEqual(host.prepared_request_id(path), request_id)
        self.assertEqual(set(self.artifact.iterdir()), original_names)
        self.assertEqual(before, {p: p.read_bytes() for p in self.artifact.rglob('*') if p.is_file()})
        self.prepare_process.assert_not_called()
        self.recovery_process.assert_not_called()

    def test_prior_normalized_uncertain_input_is_not_reprepared(self):
        payload, _, _, _ = self.external_verifier_cache('uncertain')
        for value in (payload, self.reference(payload)):
            with self.assertRaisesRegex(ValueError, 'PREPARATION_UNCERTAIN'):
                host.run(self.envelope(value))
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def test_prior_normalized_prepared_request_without_outcome_keeps_original_identity(self):
        payload, path, request_id, inputs = self.external_verifier_cache()
        (inputs / 'prepare-outcome.json').unlink()
        host.run(self.envelope(payload))
        self.assertIn(str(path), self.execv.call_args.args[1])
        self.assertEqual(host.prepared_request_id(path), request_id)
        self.prepare_process.assert_not_called()

    def test_prior_normalization_rejection_is_preserved_without_retry(self):
        payload, _, _, inputs = self.external_verifier_cache('rejected')
        for value in (payload, self.reference(payload)):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(host.run(self.envelope(value)), 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result['status'], 'REJECTED_PREPARATION')
            self.assertEqual(result['preparation_ref']['input_id'], inputs.name)
            self.assertIs(result['same_input_retry_allowed'], False)
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def test_diagnostic_repair_provenance_is_not_an_execution_identity(self):
        payload, path, _, inputs = self.external_verifier_cache()
        marker = inputs / 'input-repairs.json'
        outside = self.root / 'malformed-diagnostic'
        outside.write_bytes(b'{')
        marker.unlink()
        marker.symlink_to(outside)
        host.run(self.envelope(payload))
        self.assertIn(str(path), self.execv.call_args.args[1])
        self.prepare_process.assert_not_called()

    def test_omitted_external_pin_drift_after_observation_blocks_preparation(self):
        payload = self.external_verifier_payload()
        pin = payload['action']['verifier_commands'][0]['pinned_files'][0]
        pin.pop('sha256')
        original = host.complete_verifier_commands

        def changed(*args, **kwargs):
            Path(pin['path']).write_text('changed before pin completion\n')
            return original(*args, **kwargs)

        with mock.patch.object(host, 'complete_verifier_commands', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'REPAIR_UNCERTAIN.*external pin changed'):
                host.run(self.envelope(payload))
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def test_verifier_pin_drift_between_projections_blocks_preparation(self):
        payload = self.external_verifier_payload()
        executable = self.root / 'test-python'
        executable.write_text('original program, never executed\n')
        command = payload['action']['verifier_commands'][0]
        command['argv'][0] = str(executable)
        command.pop('executable_sha256')
        original = host.complete_verifier_commands
        calls = []

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                executable.write_text('changed program, never executed\n')
            return result

        with mock.patch.object(host, 'complete_verifier_commands', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'REPAIR_UNCERTAIN.*verifier inputs changed'):
                host.run(self.envelope(payload))
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def baseline_host(self):
        source = SOURCE.parents[2] / 'baseline/src/nokiy-native-host.py'
        if not source.is_file():
            self.skipTest('Frozen pre-upgrade host evidence is not present')
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                         '97fa5fe3461bdea509f463d494ddf3953aa0c8aad1eb80ff515a87a02222e51c')
        self.assertEqual(hashlib.sha256(source.with_name('nokiy-native-contract.json').read_bytes()).hexdigest(),
                         'f95d35bb2ef9b2de1aaeea7f27f5945fb768a934401b0e8a30c711af250a4961')
        spec = importlib.util.spec_from_file_location('pre_upgrade_native_host', source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @contextmanager
    def preparation_boundary(self, older=None):
        image = self.home / 'runtime-image'
        image.write_text('Never executed runtime image fixture.')
        (self.home / 'caller-binding.json').write_text(json.dumps({'runtime_image': {
            'path': str(image), 'sha256': host.sha(image)}}))
        native_root = Path('/Applications/ChatGPT.app/Contents/Resources')
        native = native_root / 'codex'
        original_exists, original_resolve, original_sha = Path.exists, Path.resolve, host.sha

        def native_exists(path):
            return False if path.is_relative_to(native_root) else original_exists(path)

        def native_resolve(path, strict=False):
            return native if path.is_relative_to(native_root) else original_resolve(path, strict=strict)

        def native_sha(path):
            return 'c' * 64 if Path(path) == native else original_sha(path)

        def prepare(command, **kwargs):
            draft = Path(command[command.index('--request') + 1])
            prepared = Path(command[command.index('--output-dir') + 1])
            prepared.mkdir()
            (prepared / 'request.json').write_bytes(draft.read_bytes())
            digest = hashlib.sha256(draft.read_bytes()).hexdigest()
            request_id = 'tura_embedded_' + digest
            (prepared / 'preparation.json').write_text(host.canonical({
                'status': 'PREPARED', 'request_id': request_id,
                'preflight': {'status': 'READY', 'request_id': request_id, 'request_sha256': digest}}))
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(Path, 'exists', autospec=True, side_effect=native_exists))
            stack.enter_context(mock.patch.object(Path, 'resolve', autospec=True, side_effect=native_resolve))
            stack.enter_context(mock.patch.object(host, 'sha', side_effect=native_sha))
            if older is not None:
                stack.enter_context(mock.patch.object(older, 'sha', side_effect=native_sha))
            self.prepare_process.side_effect = prepare
            yield

    def test_actual_auto_unicode_request_recovers_without_a_second_preparation(self):
        payload = self.external_verifier_payload()
        payload['draft']['prompt'] = '\u96ea' * 18000
        envelope = self.envelope(payload, 'nokiy_native_tool_v1')
        envelope['service_tier'] = 'auto'
        with self.preparation_boundary():
            host.run(envelope)
            path = Path(self.execv.call_args.args[1][3])
            before = path.read_bytes()
            self.assertGreater(len(before), host.MAX_PAYLOAD_BYTES)
            self.assertLess(len(json.loads(before)['prompt'].encode()), 64 * 1024)
            host.run(envelope)
            self.assertEqual(Path(self.execv.call_args.args[1][3]), path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads(before)['service_tier'], 'default')
        self.assertEqual(self.prepare_process.call_count, 1)

    def test_canonical_old_and_new_hosts_share_the_preparation_key_in_both_orders(self):
        older = self.baseline_host()
        payload = self.external_verifier_payload()
        with self.preparation_boundary(older):
            for first, second in ((older, host), (host, older)):
                with self.subTest(first=first.__name__):
                    artifact = self.artifact / first.__name__
                    artifact.mkdir()
                    current = copy.deepcopy(payload)
                    current['draft']['artifact_root'] = str(artifact)
                    current['action']['verifier_commands'][0]['scratch_root'] = str(artifact / 'verifier-0')
                    self.prepare_process.reset_mock()
                    first.run(self.envelope(current))
                    path = Path(self.execv.call_args.args[1][3])
                    before = path.read_bytes()
                    second.run(self.envelope(current))
                    self.assertEqual(Path(self.execv.call_args.args[1][3]), path)
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(self.prepare_process.call_count, 1)
                    self.assertEqual(len(list(artifact.glob('native-input-*'))), 1)
                    self.assertEqual(len(list(artifact.glob('native-prepared-*'))), 1)
                    inputs = next(artifact.glob('native-input-*'))
                    repairs = json.loads((inputs / 'input-repairs.json').read_text())
                    stored = json.loads((inputs / 'draft.json').read_text())
                    action = json.loads((inputs / 'action.json').read_text())
                    actual_draft = {k: v for k, v in stored.items() if k in host.CONTRACT['draft']['properties']}
                    actual = {**self.envelope(current), 'payload': {**current, 'draft': actual_draft, 'action': action}}
                    actual_sha = hashlib.sha256(host.canonical({'thread': self.thread, 'envelope': actual}).encode()).hexdigest()
                    self.assertEqual(repairs['normalized_input_sha256'], actual_sha)
                    self.assertEqual(repairs.get('preparation_input_sha256', actual_sha),
                                     inputs.name.removeprefix('native-input-'))
                    self.assertEqual(host.INLINE_EVIDENCE_NOTE in stored['prompt'], first is host)

    def test_old_new_publication_race_claims_one_key_even_after_the_last_check(self):
        older = self.baseline_host()
        payload = self.external_verifier_payload()
        original_mkdir = Path.mkdir
        with self.preparation_boundary(older):
            for delayed, winner in ((host, older), (older, host)):
                with self.subTest(delayed=delayed.__name__):
                    artifact = self.artifact / ('race-' + delayed.__name__)
                    artifact.mkdir()
                    current = copy.deepcopy(payload)
                    current['draft']['artifact_root'] = str(artifact)
                    current['action']['verifier_commands'][0]['scratch_root'] = str(artifact / 'verifier-0')
                    waiting, release = threading.Event(), threading.Event()

                    def gated_mkdir(path, *args, **kwargs):
                        if path.name.startswith('native-input-') and threading.current_thread().name.startswith('delayed'):
                            waiting.set()
                            if not release.wait(5):
                                raise AssertionError('publication barrier timed out')
                        return original_mkdir(path, *args, **kwargs)

                    self.prepare_process.reset_mock()
                    with mock.patch.object(Path, 'mkdir', autospec=True, side_effect=gated_mkdir), \
                            ThreadPoolExecutor(max_workers=1, thread_name_prefix='delayed') as pool:
                        future = pool.submit(delayed.run, self.envelope(current))
                        try:
                            self.assertTrue(waiting.wait(5), 'delayed host must reach its exclusive claim')
                            winner.run(self.envelope(current))
                            path = Path(self.execv.call_args.args[1][3])
                        finally:
                            release.set()
                        with self.assertRaises(FileExistsError):
                            future.result(timeout=5)
                    delayed.run(self.envelope(current))
                    self.assertEqual(Path(self.execv.call_args.args[1][3]), path)
                    self.assertEqual(self.prepare_process.call_count, 1)
                    self.assertEqual(len(list(artifact.glob('native-input-*'))), 1)
                    self.assertEqual(len(list(artifact.glob('native-prepared-*'))), 1)

    def test_uncertain_or_ambiguous_preparation_is_never_reprepared(self):
        payload = self.payload()
        inputs = self.artifact / ('native-input-' + self.digest(payload, normalized=False))
        inputs.mkdir()
        for value in (payload, self.reference(payload)):
            with self.assertRaisesRegex(ValueError, 'NOKIY_NATIVE_PREPARATION_UNCERTAIN'):
                host.run(self.envelope(value))
        self.cached_request(payload)
        with self.assertRaisesRegex(ValueError, 'NOKIY_NATIVE_PREPARATION_AMBIGUOUS'):
            host.run(self.envelope(self.reference(payload)))
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()

    def test_rejected_preparation_retains_the_same_outcome(self):
        payload = self.payload()
        inputs = self.artifact / ('native-input-' + self.digest(payload))
        inputs.mkdir()
        outcome = {'status': 'REJECTED_PREPARATION', 'provider_execution_started': False,
                   'safe_to_correct_input': True, 'same_input_retry_allowed': False, 'exit_code': 2}
        (inputs / 'prepare-outcome.json').write_text(json.dumps(outcome), encoding='utf-8')
        for value in (payload, self.reference(payload)):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(host.run(self.envelope(value)), 2)
            self.assertEqual(json.loads(output.getvalue()), {**outcome, 'preparation_ref': {
                'artifact_root': str(self.artifact), 'input_id': inputs.name}})
            self.assertEqual(json.loads((inputs / 'prepare-outcome.json').read_text()), outcome)
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()

    def test_known_rejected_legacy_input_can_recover_its_unique_corrected_request(self):
        payload = self.payload()
        payload['action'].update(source_read=False, command_templates=['cat source.py'])
        inputs = self.artifact / ('native-input-' + self.digest(payload, normalized=False))
        inputs.mkdir()
        failure = json.dumps({'schema_version': 'tura_embedded_failure_v1',
                              'status': 'BLOCKED_PREEXECUTION', 'authority_effect': 'none'})
        outcome = {'schema_version': 'nokiy_native_prepare_outcome_v1', 'phase': 'prepare',
                   'status': 'REJECTED_PREPARATION', 'provider_execution_started': False,
                   'diagnostic_sha256': hashlib.sha256(failure.encode()).hexdigest()}
        (inputs / 'prepare-outcome.json').write_text(json.dumps(outcome))
        (inputs / 'prepare.stdout').write_text(failure)
        (inputs / 'prepare.stderr').write_text('')
        (inputs / 'action.json').write_text(json.dumps(payload['action']))
        (inputs / 'draft.json').write_text(json.dumps({'native_thread_id': self.thread}))
        request, request_id = self.cached_request(payload)
        for value in (payload, self.reference(payload)):
            host.run(self.envelope(value))
            self.assertIn(str(request), self.execv.call_args.args[1])
            self.assertEqual(host.prepared_request_id(request), request_id)
        self.prepare_process.assert_not_called()
        (inputs / 'prepare.stdout').write_text('tampered')
        self.execv.reset_mock()
        with self.assertRaisesRegex(ValueError, 'REPAIR_UNCERTAIN'):
            host.run(self.envelope(payload))
        self.execv.assert_not_called()

    def test_cached_rejection_keeps_typed_diagnostics_without_raw_error_text(self):
        payload = self.payload()
        inputs = self.artifact / ('native-input-' + self.digest(payload))
        inputs.mkdir()
        diagnostics = {
            'stage': 'dcf_compile', 'returncode': 2,
            'stdout_bytes': 400, 'stderr_bytes': 0,
            'stdout_sha256': 'a' * 64, 'stderr_sha256': hashlib.sha256(b'').hexdigest(),
            'child_error_code': 'JSPACE_SOURCE_READ_FILE_INVALID',
        }
        outcome = {
            'schema_version': 'nokiy_native_prepare_outcome_v1', 'phase': 'prepare',
            'status': 'REJECTED_PREPARATION', 'provider_execution_started': False,
            'same_input_retry_allowed': False,
            'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
            'diagnostics': diagnostics,
            'issues': [{'path': 'action.read_scopes', 'reason': 'exact_file_unavailable'}],
        }
        (inputs / 'prepare-outcome.json').write_text(json.dumps(outcome))
        for value in (payload, self.reference(payload)):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(host.run(self.envelope(value)), 2)
            self.assertEqual(json.loads(output.getvalue()), {**outcome, 'preparation_ref': {
                'artifact_root': str(self.artifact), 'input_id': inputs.name}})
            self.assertEqual(json.loads((inputs / 'prepare-outcome.json').read_text()), outcome)
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def test_cached_rejection_preserves_namespaced_codes_without_retry_or_grants(self):
        payload = self.payload()
        inputs = self.artifact / ('native-input-' + self.digest(payload))
        inputs.mkdir()
        for code in ('JSPACE_COMMAND_EFFECT_UNVERIFIED',
                     'JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT',
                     'TASK_CONTEXT_FIELDS_INVALID', 'JSPACE_FUTURE_TYPED_FAILURE'):
            with self.subTest(code=code):
                diagnostics = {'stage': 'dcf_compile', 'returncode': 2,
                    'stdout_bytes': 400, 'stderr_bytes': 0, 'stdout_sha256': 'a' * 64,
                    'stderr_sha256': hashlib.sha256(b'').hexdigest(), 'child_error_code': code}
                outcome = {'schema_version': 'nokiy_native_prepare_outcome_v1',
                    'phase': 'prepare', 'status': 'REJECTED_PREPARATION',
                    'provider_execution_started': False, 'same_input_retry_allowed': False,
                    'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                    'diagnostics': diagnostics}
                outcome_path = inputs / 'prepare-outcome.json'
                outcome_path.write_text(json.dumps(outcome))
                before = outcome_path.read_bytes()
                for value in (payload, self.reference(payload)):
                    with redirect_stdout(io.StringIO()) as output:
                        self.assertEqual(host.run(self.envelope(value)), 2)
                    result = json.loads(output.getvalue())
                    self.assertEqual(result, {**outcome, 'preparation_ref': {
                        'artifact_root': str(self.artifact), 'input_id': inputs.name}})
                    self.assertEqual(outcome_path.read_bytes(), before)
        self.prepare_process.assert_not_called()
        self.recovery_process.assert_not_called()
        self.execv.assert_not_called()

    def test_preparation_diagnostics_accept_bounded_namespace_not_enumeration(self):
        valid = {'stage': 'dcf_compile', 'returncode': -255,
                 'stdout_bytes': 0, 'stderr_bytes': 2**63 - 1,
                 'stdout_sha256': 'a' * 64, 'stderr_sha256': 'b' * 64}
        for namespace in ('JSPACE', 'TASK_CONTEXT'):
            for suffix in ('A', 'A' * 96, 'A_0', 'FUTURE_TYPED_FAILURE'):
                with self.subTest(namespace=namespace, suffix=suffix):
                    diagnostics = dict(valid, child_error_code=namespace + '_' + suffix)
                    self.assertEqual(host._safe_preparation_diagnostics(diagnostics), diagnostics)
        self.assertEqual(host._safe_preparation_diagnostics(dict(valid, returncode=255)),
                         dict(valid, returncode=255))

    def test_fresh_rejection_persists_namespaced_diagnostics_without_worker_execution(self):
        image = self.home / 'runtime-image'
        image.write_text('Never executed runtime image fixture.')
        (self.home / 'caller-binding.json').write_text(json.dumps({'runtime_image': {
            'path': str(image), 'sha256': host.sha(image)}}))
        native_root = Path('/Applications/ChatGPT.app/Contents/Resources')
        native = native_root / 'codex'
        original_exists, original_resolve, original_sha = Path.exists, Path.resolve, host.sha

        def native_exists(path):
            return False if path.is_relative_to(native_root) else original_exists(path)

        def native_resolve(path, strict=False):
            return native if path.is_relative_to(native_root) else original_resolve(path, strict=strict)

        def native_sha(path):
            return 'c' * 64 if Path(path) == native else original_sha(path)

        with mock.patch.object(Path, 'exists', autospec=True, side_effect=native_exists), \
             mock.patch.object(Path, 'resolve', autospec=True, side_effect=native_resolve), \
             mock.patch.object(host, 'sha', side_effect=native_sha):
            for index, code in enumerate(('JSPACE_COMMAND_EFFECT_UNVERIFIED',
                    'JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT', 'TASK_CONTEXT_FIELDS_INVALID',
                    'JSPACE_FUTURE_TYPED_FAILURE')):
                with self.subTest(code=code):
                    artifact = self.artifact / ('fresh-' + str(index))
                    artifact.mkdir()
                    payload = self.payload()
                    payload['draft']['artifact_root'] = str(artifact)
                    diagnostics = {'stage': 'dcf_compile', 'returncode': 2,
                        'stdout_bytes': 400, 'stderr_bytes': 0, 'stdout_sha256': 'a' * 64,
                        'stderr_sha256': hashlib.sha256(b'').hexdigest(), 'child_error_code': code}
                    failure = {'schema_version': 'tura_embedded_failure_v1',
                        'status': 'BLOCKED_PREEXECUTION', 'authority_effect': 'none',
                        'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                        'diagnostics': diagnostics, 'secret': 'NEVER_ECHO_THIS'}
                    self.prepare_process.side_effect = None
                    self.prepare_process.return_value = SimpleNamespace(
                        returncode=2, stdout=json.dumps(failure), stderr='')
                    draft = host.normalize_draft(payload['draft'])
                    request, outcome = host.prepare_action(self.envelope(payload), self.entry,
                        self.thread, self.worker, self.effort, self.tier, payload, draft)
                    self.assertIsNone(request)
                    self.assertEqual(outcome['status'], 'REJECTED_PREPARATION')
                    self.assertEqual(outcome['diagnostics'], diagnostics)
                    self.assertIs(outcome['provider_execution_started'], False)
                    self.assertIs(outcome['same_input_retry_allowed'], False)
                    self.assertNotIn('NEVER_ECHO_THIS', json.dumps(outcome))
                    persisted = artifact / outcome['preparation_ref']['input_id'] / 'prepare-outcome.json'
                    self.assertEqual(json.loads(persisted.read_text()), outcome)
                    inspected = host.preparation_evidence(artifact,
                        outcome['preparation_ref']['input_id'], self.thread)
                    self.assertEqual(inspected['diagnostic_status'], 'TYPED_CAUSE_AVAILABLE')
                    self.assertEqual(inspected['diagnostics'], diagnostics)
        self.assertEqual(self.prepare_process.call_count, 4)
        self.recovery_process.assert_not_called()
        self.execv.assert_not_called()

    def test_preparation_diagnostics_reject_secret_fields_and_untrusted_codes(self):
        class StringSubclass(str):
            pass

        valid = {
            'stage': 'dcf_navigated_prepare', 'returncode': 2,
            'stdout_bytes': 123, 'stderr_bytes': 0,
            'stdout_sha256': 'a' * 64, 'stderr_sha256': 'b' * 64,
            'child_error_code': 'JSPACE_SOURCE_READ_SCOPE_INVALID',
        }
        self.assertEqual(host._safe_preparation_diagnostics(valid), valid)
        invalid = [None, [], dict(valid, error='SECRET_TOKEN'),
                   dict(valid, child_error_code='SECRET_TOKEN'),
                   dict(valid, child_error_code=StringSubclass('JSPACE_ACTION_SHAPE_INVALID')),
                   dict(valid, child_error_code=True), dict(valid, child_error_code=None),
                   dict(valid, stage='SECRET_TOKEN'), dict(valid, returncode=True),
                   dict(valid, returncode=0), dict(valid, returncode=256),
                   dict(valid, returncode=-256), dict(valid, returncode='2'),
                   dict(valid, stdout_bytes=True), dict(valid, stderr_bytes=-1),
                   dict(valid, stdout_bytes=2**63), dict(valid, stdout_sha256='A' * 64),
                   dict(valid, stderr_sha256='b' * 63)]
        for code in ('JSPACE_', 'TASK_CONTEXT_', 'JSPACE_0INVALID', 'JSPACE_lowercase',
                     'TASK_CONTEXT_A' + 'A' * 96, 'JSPACE_A' + 'A' * 96,
                     'JSPACE_ACTION_SHAPE_INVALID\n', 'TASK_CONTEXT_FIELDS_INVALID trailing',
                     ' JSPACE_ACTION_SHAPE_INVALID', 'NOKIY_MUTATION_GRANTED',
                     'JSPACE_INVALID;rm -rf .', 'JSPACE_\u00c9', 'TASK_CONTEXT_A\x00'):
            invalid.append(dict(valid, child_error_code=code))
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(host._safe_preparation_diagnostics(value))

    def _fresh_rejection(self, failure, artifact_name, *, payload=None):
        """Run one fresh preparation whose caller rejects with failure; no worker can start."""
        self.prepare_process.side_effect = None
        self.prepare_process.return_value = SimpleNamespace(
            returncode=2, stdout=json.dumps(failure), stderr='')
        request, outcome = self._fresh_preparation(artifact_name, payload=payload)
        self.assertIsNone(request)
        self.assertEqual(outcome['status'], 'REJECTED_PREPARATION')
        self.execv.assert_not_called()
        return outcome

    def _fresh_preparation(self, artifact_name, *, payload=None):
        """Run one fresh preparation through the configured prepare_process mock."""
        image = self.home / 'runtime-image'
        image.write_text('Never executed runtime image fixture.')
        (self.home / 'caller-binding.json').write_text(json.dumps({'runtime_image': {
            'path': str(image), 'sha256': host.sha(image)}}))
        native_root = Path('/Applications/ChatGPT.app/Contents/Resources')
        native = native_root / 'codex'
        original_exists, original_resolve, original_sha = Path.exists, Path.resolve, host.sha

        def native_exists(path):
            return False if path.is_relative_to(native_root) else original_exists(path)

        def native_resolve(path, strict=False):
            return native if path.is_relative_to(native_root) else original_resolve(path, strict=strict)

        def native_sha(path):
            return 'c' * 64 if Path(path) == native else original_sha(path)

        artifact = self.artifact / artifact_name
        artifact.mkdir()
        payload = payload or self.payload()
        payload['draft']['artifact_root'] = str(artifact)
        with mock.patch.object(Path, 'exists', autospec=True, side_effect=native_exists), \
             mock.patch.object(Path, 'resolve', autospec=True, side_effect=native_resolve), \
             mock.patch.object(host, 'sha', side_effect=native_sha):
            draft = host.normalize_draft(payload['draft'])
            return host.prepare_action(self.envelope(payload), self.entry,
                self.thread, self.worker, self.effort, self.tier, payload, draft)

    def test_full_stack_rejection_keeps_typed_issues_without_dcf_diagnostics(self):
        failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                   'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                   'issues': [{'path': 'action.write_scopes', 'reason': 'not_in_target_paths'}],
                   'secret': 'NEVER_ECHO_THIS'}
        outcome = self._fresh_rejection(failure, 'issues-no-diagnostics')
        self.assertEqual(outcome['issues'], failure['issues'])
        self.assertNotIn('diagnostics', outcome)
        self.assertNotIn('NEVER_ECHO_THIS', json.dumps(outcome))
        # Other blockers still carry no caller issues unless DCF diagnostics vouch for them.
        failure = dict(failure, first_typed_blocker='NOKIY_FULL_CORE_ARTIFACT_WORKSPACE_OVERLAP')
        self.assertNotIn('issues', self._fresh_rejection(failure, 'other-blocker'))

    def test_every_known_issue_pair_survives_and_unknown_reasons_drop(self):
        for path, reason in sorted(host._ACTION_ISSUE_PAIRS):
            issue = [{'path': path, 'reason': reason}]
            self.assertEqual(host._safe_action_issues(issue), issue)
        for issue in ({'path': 'action.read_scopes', 'reason': 'SECRET_TOKEN'},
                      {'path': 'action.read_scopes', 'reason': 'exact_file_too_large', 'name': 'x'},
                      {'path': 'surface_id', 'reason': 'unsafe_path'}):
            self.assertIsNone(host._safe_action_issues([issue]))

    def test_stale_capability_diagnostic_reaches_the_coordinator(self):
        capability = {'capability_id': 'surface-map', 'failed_field': 'projection_status',
                      'observed': 'blocked', 'expected': 'pass',
                      'generation_id': '20261018T104920253312Z-87460e0652-0246192c67'}
        diagnostics = {'stage': 'dcf_compile', 'returncode': 2, 'stdout_bytes': 400, 'stderr_bytes': 0,
                       'stdout_sha256': 'a' * 64, 'stderr_sha256': hashlib.sha256(b'').hexdigest(),
                       'child_error_code': 'JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT',
                       'capability_diagnostic': capability}
        self.assertEqual(host._safe_preparation_diagnostics(diagnostics), diagnostics)
        queried = dict(diagnostics, stage='dcf_surface_query')
        self.assertEqual(host._safe_preparation_diagnostics(queried), queried)
        for bad in (dict(capability, observed='pass'), dict(capability, capability_id='SECRET_TOKEN'),
                    dict(capability, generation_id='bad id'), dict(capability, extra='x'),
                    dict(capability, failed_field='unknown'), 'SECRET_TOKEN'):
            with self.subTest(bad=bad):
                self.assertIsNone(host._safe_preparation_diagnostics(
                    dict(diagnostics, capability_diagnostic=bad)))
        self.assertIsNone(host._safe_preparation_diagnostics(
            dict(diagnostics, child_error_code='JSPACE_SOURCE_READ_FILE_INVALID')))
        failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                   'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                   'diagnostics': diagnostics}
        self.assertEqual(self._fresh_rejection(failure, 'stale-capability')['diagnostics'], diagnostics)

    def test_rejection_reports_the_automatic_surface_choice_only_when_omitted(self):
        failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                   'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED'}
        resolution = {'status': 'TIED', 'ranked': [['a', 2], ['b', 2]], 'files_queried': 2,
                      'write_targets_queried': 0, 'tied': ['a', 'b']}
        payload = self.payload()
        payload.pop('surface_id')
        with mock.patch.object(host, 'resolve_surface_detail', return_value=(None, resolution)) as resolver:
            outcome = self._fresh_rejection(failure, 'surface-choice', payload=payload)
        resolver.assert_called_once()
        self.assertEqual(outcome['surface_resolution'], resolution)
        self.assertNotIn('--surface-id', self.prepare_process.call_args.args[0])
        with mock.patch.object(host, 'resolve_surface_detail') as resolver:
            outcome = self._fresh_rejection(failure, 'explicit-surface')
        resolver.assert_not_called()
        self.assertNotIn('surface_resolution', outcome)

    def test_rejected_explicit_surface_reports_write_covering_surfaces(self):
        diagnostics = {'stage': 'dcf_compile', 'returncode': 2, 'stdout_bytes': 400, 'stderr_bytes': 0,
                       'stdout_sha256': 'a' * 64, 'stderr_sha256': hashlib.sha256(b'').hexdigest(),
                       'child_error_code': 'JSPACE_WRITE_SCOPE_OUTSIDE_SURFACE'}
        failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                   'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                   'diagnostics': diagnostics}
        resolution = {'status': 'SELECTED', 'selected': 'writes_surface', 'ranked': [['writes_surface', 2]],
                      'files_queried': 2, 'write_targets_queried': 1}
        with mock.patch.object(host, 'resolve_surface_detail', return_value=(None, resolution)) as resolver:
            outcome = self._fresh_rejection(failure, 'explicit-rejected')
        resolver.assert_called_once()
        self.assertEqual(outcome['surface_resolution'], {**resolution, 'rejected_surface': 'exact-test-surface'})
        self.assertIn('--surface-id', self.prepare_process.call_args.args[0])
        for code in ('JSPACE_SOURCE_READ_FILE_INVALID', 'JSPACE_MODULE_UNAVAILABLE'):
            with self.subTest(code=code), mock.patch.object(host, 'resolve_surface_detail') as resolver:
                other = dict(failure, diagnostics=dict(diagnostics, child_error_code=code))
                outcome = self._fresh_rejection(other, 'explicit-' + code.lower())
                resolver.assert_not_called()
                self.assertNotIn('surface_resolution', outcome)
                self.assertEqual(outcome['diagnostics']['child_error_code'], code)

    def _surface_refusal(self):
        diagnostics = {'stage': 'dcf_compile', 'returncode': 2, 'stdout_bytes': 400, 'stderr_bytes': 0,
                       'stdout_sha256': 'a' * 64, 'stderr_sha256': hashlib.sha256(b'').hexdigest(),
                       'child_error_code': 'JSPACE_WRITE_SCOPE_OUTSIDE_SURFACE'}
        failure = {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                   'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                   'diagnostics': diagnostics}
        resolution = {'status': 'SELECTED', 'selected': 'writes_surface', 'ranked': [['writes_surface', 2]],
                      'files_queried': 2, 'write_targets_queried': 1}
        return failure, resolution

    def test_a_refused_explicit_surface_is_prepared_once_more_with_the_unique_covering_surface(self):
        failure, resolution = self._surface_refusal()
        calls = []

        def prepare(command, **kwargs):
            calls.append(list(command))
            if len(calls) == 1:
                return SimpleNamespace(returncode=2, stdout=json.dumps(failure), stderr='')
            prepared = Path(command[command.index('--output-dir') + 1])
            prepared.mkdir()
            (prepared / 'request.json').write_text(json.dumps({
                'native_thread_id': self.thread, 'model': self.worker, 'reasoning_effort': self.effort,
                'service_tier': self.tier, 'execution_profile': 'direct'}))
            return SimpleNamespace(returncode=0, stdout='{}', stderr='')

        self.prepare_process.side_effect = prepare
        with mock.patch.object(host, 'resolve_surface_detail', return_value=('writes_surface', resolution)):
            request, outcome = self._fresh_preparation('reselected')
        self.assertIsNone(outcome)
        self.assertEqual(request.name, 'request.json')
        self.assertEqual(len(calls), 2, 'exactly one more preparation')
        self.assertEqual(calls[0][-2:], ['--surface-id', 'exact-test-surface'])
        self.assertEqual(calls[1][:-2], calls[0][:-2])
        self.assertEqual(calls[1][-2:], ['--surface-id', 'writes_surface'])
        stored = json.loads((request.parents[1] / ('native-input-' + request.parent.name.split('native-prepared-')[1])
                             / 'prepare-outcome.json').read_text())
        self.assertEqual(stored['status'], 'PREPARED')
        self.assertEqual(stored['surface_resolution'], {**resolution, 'rejected_surface': 'exact-test-surface',
                                                        'status': 'RESELECTED', 'selected': 'writes_surface'})
        self.assertRegex(stored['rejected_surface_diagnostic_sha256'], r'^[0-9a-f]{64}$')
        self.execv.assert_not_called()

    def test_a_second_refusal_keeps_the_first_and_names_the_reselected_surface(self):
        failure, resolution = self._surface_refusal()
        with mock.patch.object(host, 'resolve_surface_detail', return_value=('writes_surface', resolution)):
            outcome = self._fresh_rejection(failure, 'reselect-refused')
        self.assertEqual(self.prepare_process.call_count, 2)
        self.assertEqual(outcome['surface_resolution'], {**resolution, 'rejected_surface': 'exact-test-surface'})
        self.assertEqual(outcome['reselected_surface_rejected'], 'writes_surface')
        self.assertEqual(outcome['diagnostics']['child_error_code'], 'JSPACE_WRITE_SCOPE_OUTSIDE_SURFACE')

    def _verifier_refusal(self):
        diagnostics = {'stage': 'dcf_compile', 'returncode': 2, 'stdout_bytes': 151, 'stderr_bytes': 0,
                       'stdout_sha256': 'b' * 64, 'stderr_sha256': hashlib.sha256(b'').hexdigest(),
                       'child_error_code': 'JSPACE_VERIFIER_GRANT_INVALID'}
        return {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_FULL_STACK_PREPARATION_FAILED',
                'diagnostics': diagnostics}

    def _verifier_payload(self):
        payload = self.payload()
        payload['action']['verifier_commands'] = [{'argv': ['/usr/bin/python3', '-B', '-m', 'unittest', 'test_source'],
                                                   'python_import_roots': [str(self.workspace)]}]
        return payload

    def test_requested_verifier_refusal_preserves_the_grant_and_reports_recovery(self):
        failure = self._verifier_refusal()
        payload = self._verifier_payload()
        payload['action']['context_summary'] = 'Required worker tests must pass before completion.'
        outcome = self._fresh_rejection(failure, 'verifier-rejected', payload=payload)
        self.assertEqual(self.prepare_process.call_count, 1)
        command = self.prepare_process.call_args.args[0]
        action_path = Path(command[command.index('--action') + 1])
        action = json.loads(action_path.read_text())
        self.assertEqual(action['context_summary'], payload['action']['context_summary'])
        self.assertEqual(action['verifier_commands'][0]['argv'], payload['action']['verifier_commands'][0]['argv'])
        self.assertEqual(outcome['diagnostics'], failure['diagnostics'])
        self.assertIs(outcome['provider_execution_started'], False)
        self.assertIs(outcome['same_input_retry_allowed'], False)
        self.assertIn('Correct the rejected grant once', outcome['verifier_recovery'])
        self.assertIn('explicitly assign the required checks', outcome['verifier_recovery'])
        self.assertIn('nokiy_native', outcome['verifier_recovery'])
        self.assertIn('not skipped', outcome['verifier_recovery'])
        self.assertFalse((action_path.parent / 'action-without-verifiers.json').exists())
        self.assertEqual(json.loads((action_path.parent / 'prepare-outcome.json').read_text()), outcome)
        self.assertEqual((action_path.parent / 'prepare.stdout').read_text(), json.dumps(failure))
        self.execv.assert_not_called()

    def test_invalid_sha_and_out_of_scope_verifiers_are_not_downgraded(self):
        for index, reason in enumerate(('sha256_mismatch', 'scope_mismatch')):
            with self.subTest(reason=reason):
                self.prepare_process.reset_mock()
                failure = dict(self._verifier_refusal(), issues=[{
                    'path': 'action.verifier_commands.pinned_files', 'reason': reason}])
                payload = self._verifier_payload()
                payload['action']['verifier_commands'][0]['pinned_files'] = [{
                    'path': '/outside/required-check.py', 'sha256': '0' * 64, 'scope': 'verifier_only'}]
                outcome = self._fresh_rejection(failure, 'invalid-verifier-' + str(index), payload=payload)
                self.assertEqual(self.prepare_process.call_count, 1)
                self.assertEqual(outcome['issues'], failure['issues'])
                command = self.prepare_process.call_args.args[0]
                action = json.loads(Path(command[command.index('--action') + 1]).read_text())
                self.assertEqual(action['verifier_commands'][0]['pinned_files'],
                                 payload['action']['verifier_commands'][0]['pinned_files'])
                self.execv.assert_not_called()

    def test_single_and_batch_rejections_are_visible_without_worker_dispatch(self):
        rejected = self._fresh_rejection(self._verifier_refusal(), 'dispatch-rejected',
                                         payload=self._verifier_payload())
        for batch in (False, True):
            with self.subTest(batch=batch):
                single = self._verifier_payload()
                payload = ({'operation': 'run_batch', 'artifact_root': str(self.artifact),
                            'members': [{key: value for key, value in single.items() if key != 'operation'}]}
                           if batch else single)
                with mock.patch.object(host, 'prepare_action', return_value=(None, rejected)), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertNotEqual(host.run(self.envelope(payload)), 0)
                self.assertEqual(json.loads(output.getvalue()), rejected)
                self.execv.assert_not_called()

    def test_plain_actions_keep_one_preparation_and_no_verifier_recovery(self):
        outcome = self._fresh_rejection(self._verifier_refusal(), 'no-verifier', payload=self.payload())
        self.assertEqual(self.prepare_process.call_count, 1)
        self.assertNotIn('verifier_recovery', outcome)

    def test_uncertain_legacy_input_is_never_reprepared(self):
        payload = self.payload()
        payload['action'].update(source_read=False, command_templates=['cat source.py'])
        inputs = self.artifact / ('native-input-' + self.digest(payload, normalized=False))
        inputs.mkdir()
        with self.assertRaisesRegex(ValueError, 'REPAIR_UNCERTAIN'):
            host.run(self.envelope(payload))
        self.prepare_process.assert_not_called()
        self.execv.assert_not_called()

    def test_known_local_directory_rejection_recovers_only_its_corrected_identity(self):
        payload = self.payload()
        payload.pop('surface_id')
        payload['action'].update(operations=['read', 'command'],
                                 read_scopes=['diagnostics/**'], read_directories=['diagnostics'])
        inputs = self.artifact / ('native-input-' + self.digest(payload, normalized=False))
        inputs.mkdir()
        failure = json.dumps({'schema_version': 'tura_embedded_failure_v1',
                              'status': 'BLOCKED_PREEXECUTION', 'authority_effect': 'none'})
        outcome = {'schema_version': 'nokiy_native_prepare_outcome_v1', 'phase': 'prepare',
                   'status': 'REJECTED_PREPARATION', 'provider_execution_started': False,
                   'diagnostic_sha256': hashlib.sha256(failure.encode()).hexdigest()}
        (inputs / 'prepare-outcome.json').write_text(json.dumps(outcome))
        (inputs / 'prepare.stdout').write_text(failure)
        (inputs / 'prepare.stderr').write_text('')
        (inputs / 'action.json').write_text(json.dumps(payload['action']))
        (inputs / 'draft.json').write_text(json.dumps({'native_thread_id': self.thread}))
        request, request_id = self.cached_request(payload)
        host.run(self.envelope(payload))
        self.assertIn(str(request), self.execv.call_args.args[1])
        self.assertEqual(host.prepared_request_id(request), request_id)
        self.prepare_process.assert_not_called()
        outcome['provider_execution_started'] = True
        (inputs / 'prepare-outcome.json').write_text(json.dumps(outcome))
        self.execv.reset_mock()
        with self.assertRaisesRegex(ValueError, 'REPAIR_UNCERTAIN'):
            host.run(self.envelope(payload))
        self.execv.assert_not_called()

    def test_http_evidence_binds_native_thread_without_a_worker_or_caller(self):
        payload = {'operation': 'read_http', 'artifact_root': str(self.artifact),
                   'urls': ['https://example.invalid/current'], 'observation_id': 'current-v1'}
        for version in ('nokiy_native_tool_v2', 'nokiy_native_tool_v3'):
            with self.subTest(version=version), mock.patch.object(host, 'read_http', return_value=0) as read:
                self.assertEqual(host.run(self.envelope(payload, version)), 0)
                read.assert_called_once_with(payload, self.thread)
        self.prepare_process.assert_not_called()
        self.recovery_process.assert_not_called()
        self.execv.assert_not_called()

    def test_http_helper_drift_stops_before_module_load(self):
        with mock.patch.dict(host.CONTRACT, {'read_http_helper_sha256': '0' * 64}), \
                mock.patch.object(host.importlib.util, 'spec_from_file_location') as load:
            with self.assertRaisesRegex(ValueError, 'HTTP_HELPER_IDENTITY_MISMATCH'):
                host.read_http({}, self.thread)
            load.assert_not_called()

    def test_cached_model_thread_effort_tier_and_profile_remain_bound(self):
        payload = self.payload()
        for field in ('native_thread_id', 'model', 'reasoning_effort', 'service_tier', 'execution_profile'):
            with self.subTest(field=field):
                self.cached_request(payload, overrides={field: 'injected'})
                with self.assertRaisesRegex(ValueError, 'Prepared request identity mismatch'):
                    host.run(self.envelope(self.reference(payload)))
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()

    def test_reference_cannot_bypass_the_serial_lock(self):
        payload = self.payload()
        inline = self.envelope(payload, 'nokiy_native_tool_v3')
        reference = self.envelope(self.reference(payload), 'nokiy_native_tool_v3')
        with mock.patch.object(host, 'prepare_action',
                               return_value=(self.root / 'request.json', None)) as prepare:
            with host.serial_execution(inline):
                with self.assertRaisesRegex(ValueError, 'NOKIY_SERIAL_ACTION_IN_PROGRESS'):
                    host.run(reference)
            prepare.assert_not_called()
            self.execv.assert_not_called()
            host.run(reference)
            prepare.assert_called_once()
            self.assertEqual(prepare.call_args.args[0], inline)
        self.assertTrue((self.codex_home / 'nokiy-native-locks' / (self.thread + '.lock')).is_file())

    def test_reference_holds_the_same_lock_while_execute_runs(self):
        inline = self.envelope(self.payload(), 'nokiy_native_tool_v3')
        reference = self.envelope(self.reference(inline['payload']), 'nokiy_native_tool_v3')

        def attempt_concurrent(expanded):
            self.assertEqual(expanded, inline)
            with self.assertRaisesRegex(ValueError, 'NOKIY_SERIAL_ACTION_IN_PROGRESS'):
                host.run(inline)
            return 17

        with mock.patch.object(host, 'execute', side_effect=attempt_concurrent) as execute:
            self.assertEqual(host.run(reference), 17)
            execute.assert_called_once_with(inline)

    def test_reference_rejects_invalid_hashes_and_extra_wrapper_fields_before_read(self):
        reference = self.reference()
        invalid = [dict(reference, input_sha256=value) for value in
                   (None, 7, 'a' * 63, 'a' * 65, 'AB' * 32, 'g' * 64, 'a' * 64 + '\n')]
        invalid += [dict(reference, **{key: 'injected'}) for key in
                    ('draft', 'action', 'surface_id', 'model', 'native_thread_id', 'HOME', 'CODEX_HOME')]
        invalid += [{'operation': 'run_ref', 'input_path': reference['input_path']},
                    {'operation': 'run_ref', 'input_sha256': reference['input_sha256']}]
        with mock.patch.object(host, 'reference_bytes') as read:
            for value in invalid:
                with self.subTest(value=value):
                    self.assert_blocked(value)
            read.assert_not_called()
        envelope = dict(self.envelope(reference), native_thread_id='injected')
        with mock.patch.object(host, 'reference_bytes') as read, \
                mock.patch.object(host, 'execute') as execute:
            with self.assertRaisesRegex(ValueError, 'Unknown native tool envelope'):
                host.run(envelope)
            read.assert_not_called()
            execute.assert_not_called()

    def test_reference_rejects_noncanonical_symlink_and_nonregular_paths(self):
        reference = self.reference()
        path = Path(reference['input_path'])
        leaf = self.root / 'leaf-link.json'
        leaf.symlink_to(path)
        parent = self.root / 'parent-link'
        parent.symlink_to(self.root, target_is_directory=True)
        fifo = self.root / 'fifo'
        os.mkfifo(fifo)
        invalid_paths = [None, 7, '', path.name, '/', '//' + str(path).lstrip('/'),
                         str(self.root) + '/./' + path.name, str(self.root) + '//'+ path.name,
                         str(self.root) + '/workspace/../' + path.name, str(path) + '/',
                         str(leaf), str(parent / path.name), str(self.workspace), str(fifo),
                         str(self.root / 'missing.json'), str(path) + '\x00']
        for value in invalid_paths:
            with self.subTest(path=value):
                self.assert_blocked(dict(reference, input_path=value))

    def test_reference_rejects_envelopes_nested_refs_batches_recovery_and_injection(self):
        payload = self.payload()
        invalid = [None, [], {}, self.envelope(payload)]
        invalid += [dict(payload, operation=operation) for operation in
                    ('run_ref', 'run_batch', 'read_result', 'read_batch')]
        invalid += [dict(payload, **{key: 'injected'}) for key in
                    ('input_path', 'input_sha256', 'members', 'model', 'worker', 'native_thread_id',
                     'thread_id', 'HOME', 'CODEX_HOME', 'CODEX_THREAD_ID')]
        invalid += [dict(payload, draft=dict(payload['draft'], **{key: 'injected'})) for key in
                    ('model', 'native_thread_id', 'HOME', 'CODEX_HOME', 'CODEX_THREAD_ID')]
        invalid += [dict(payload, action=dict(payload['action'], **{key: 'injected'})) for key in
                    ('model', 'worker', 'reasoning_effort', 'service_tier', 'native_thread_id',
                     'thread_id', 'thread', 'HOME', 'CODEX_HOME', 'CODEX_THREAD_ID')]
        invalid += [{'operation': 'run', 'draft': payload['draft']},
                    dict(payload, action=[]), dict(payload, surface_id='')]
        for value in invalid:
            with self.subTest(value=value):
                self.assert_blocked(self.reference(data=json.dumps(value).encode('utf-8')))

    def test_reference_rejects_malformed_utf8_json_duplicate_keys_and_nonfinite_numbers(self):
        draft = json.dumps(self.payload()['draft']).encode('utf-8')
        prefix = b'{"operation":"run","draft":' + draft + b',"action":'
        invalid = [b'', b'\xff', b'\xef\xbb\xbf{}', b'{', b'{} {}', b'null', b'[]',
                   b'{"operation":"run","oper\\u0061tion":"run","draft":' + draft + b',"action":{}}',
                   prefix + b'{"context_summary":"a","context\\u005fsummary":"b"}}']
        invalid += [prefix + b'{"probe":' + token + b'}}' for token in
                    (b'NaN', b'Infinity', b'-Infinity', b'1e400', b'-1e400')]
        for data in invalid:
            with self.subTest(data=data[:80]):
                self.assert_blocked(self.reference(data=data))

    def test_parser_recursion_failure_is_typed_before_execution(self):
        reference = self.reference()
        # Parser depth varies by Python release; exercise the error, not an
        # implementation-specific nesting depth for otherwise valid JSON.
        with mock.patch.object(host.json, 'loads', side_effect=RecursionError):
            self.assert_blocked(reference)

    def test_number_types_match_inline_json_stringify_transport(self):
        for token, inline in ((b'1.0', 1), (b'-0.0', 0), (b'1e2', 100),
                              (b'1.25', 1.25), (b'1e-7', 1e-7),
                              (b'1e21', 1e21)):
            payload = self.payload()
            payload['action']['probe'] = inline
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            data = body.replace(b'"probe": ' + json.dumps(inline).encode(), b'"probe": ' + token)
            with self.subTest(token=token):
                expanded = host.expand_run_reference(self.envelope(self.reference(data=data)))
                self.assertEqual(host.canonical(expanded), host.canonical(self.envelope(payload)))
        for unsafe in (9007199254740993, 1000000000000000128, 1e20):
            payload['action']['probe'] = unsafe
            self.assert_blocked(self.reference(payload))

    def test_reference_keeps_the_96kib_byte_limit_even_if_file_grows(self):
        payload = self.payload()
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        data = body + b' ' * (96 * 1024 - len(body))
        self.assertEqual(len(data), 96 * 1024)
        with mock.patch.object(host, 'prepare_action',
                               return_value=(self.root / 'request.json', None)) as prepare:
            host.run(self.envelope(self.reference(data=data)))
            self.assertEqual(prepare.call_args.args[6], payload)
        oversized = self.reference(data=data + b' ')
        self.assert_blocked(oversized)
        info = Path(oversized['input_path']).stat()
        with mock.patch.object(host.os, 'fstat', return_value=SimpleNamespace(
                st_mode=info.st_mode, st_size=0)):
            self.assert_blocked(oversized)

    def test_expansion_and_main_keep_the_160kib_encoded_envelope_limit(self):
        payload = self.payload()
        payload['action']['context_summary'] = 'x' * (90 * 1024)
        reference = self.reference(payload)
        self.assertLessEqual(Path(reference['input_path']).stat().st_size, 96 * 1024)
        envelope = dict(self.envelope(reference), worker='x' * (40 * 1024))
        with mock.patch.object(host, 'execute') as execute, \
                mock.patch.object(host, 'serial_execution') as serial:
            with self.assertRaisesRegex(ValueError, 'Expanded reference envelope exceeds bound'):
                host.run(envelope)
            execute.assert_not_called()
            serial.assert_not_called()
        with mock.patch.object(host.sys, 'argv', ['host', '--payload', 'A' * (160 * 1024 + 1)]), \
                mock.patch.object(host, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'Payload exceeds bound'):
                host.main()
            run.assert_not_called()

    def test_main_routes_references_without_changing_environment_or_binding(self):
        reference = self.envelope(self.reference())
        encoded = base64.urlsafe_b64encode(json.dumps(reference, ensure_ascii=False).encode()).decode().rstrip('=')
        environment = dict(os.environ)
        with mock.patch.object(host.sys, 'argv', ['host', '--payload', encoded]), \
                mock.patch.object(host, 'prepare_action',
                                  return_value=(self.root / 'request.json', None)) as prepare:
            host.main()
            self.assertEqual(prepare.call_args.args[0], self.envelope(self.payload()))
            self.assertEqual(prepare.call_args.args[2:6], (self.thread, self.worker, self.effort, self.tier))
        self.assertEqual(dict(os.environ), environment)

    def test_unicode_budget_is_utf8_not_ascii_escape_serialization(self):
        payload = self.payload()
        payload['action']['context_summary'] = '雪' * (20 * 1024) + '😀' * (6 * 1024)
        reference = self.reference(payload)
        self.assertLessEqual(Path(reference['input_path']).stat().st_size, 96 * 1024)
        escaped = json.dumps(self.envelope(payload), ensure_ascii=True).encode('utf-8')
        self.assertGreater(len(base64.urlsafe_b64encode(escaped)), 160 * 1024)
        with mock.patch.object(host, 'prepare_action',
                               return_value=(self.root / 'request.json', None)) as prepare:
            host.run(self.envelope(payload))
            expected = copy.deepcopy(prepare.call_args.args)
            prepare.reset_mock()
            host.run(self.envelope(reference))
            prepare.assert_called_once_with(*expected)

    def test_reference_does_not_manufacture_a_missing_native_thread(self):
        reference = self.reference()
        for version in ('nokiy_native_tool_v2', 'nokiy_native_tool_v3'):
            with mock.patch.dict(os.environ, CODEX_THREAD_ID=''), \
                    mock.patch.object(host, 'prepare_action') as prepare:
                with self.assertRaisesRegex(ValueError, '[Tt]hread'):
                    host.run(self.envelope(reference, version))
                prepare.assert_not_called()
        self.execv.assert_not_called()

    def test_legacy_auto_tier_normalization_is_unchanged(self):
        payload = self.payload()
        inline = dict(self.envelope(payload, 'nokiy_native_tool_v1'), service_tier='auto')
        reference = dict(self.envelope(self.reference(payload), 'nokiy_native_tool_v1'), service_tier='auto')
        with mock.patch.object(host, 'prepare_action',
                               return_value=(self.root / 'request.json', None)) as prepare:
            host.run(inline)
            expected = copy.deepcopy(prepare.call_args.args)
            self.assertEqual(expected[4:6], ('max', 'default'))
            prepare.reset_mock()
            host.run(reference)
            prepare.assert_called_once_with(*expected)

    def test_existing_inline_read_result_recovery_is_unchanged(self):
        request_id = 'tura_embedded_' + 'ab' * 32
        payload = {'operation': 'read_result', 'artifact_root': str(self.artifact), 'request_id': request_id}
        for version in ('nokiy_native_tool_v1', 'nokiy_native_tool_v2', 'nokiy_native_tool_v3'):
            with mock.patch.object(host.subprocess, 'call', return_value=0) as call:
                self.assertEqual(host.run(self.envelope(payload, version)), 0)
                call.assert_called_once_with([str(self.entry), 'read-result', '--artifact-root',
                                              str(self.artifact), '--request-id', request_id, '--summary'])
        self.execv.assert_not_called()

    def test_batch_reference_preserves_inline_plan_and_recovers_without_preparation(self):
        payload = self.missing_batch_payload()
        reference = dict(self.reference(payload), operation='run_batch_ref')
        original = self.envelope(payload)
        self.assertEqual(host.expand_run_reference(self.envelope(reference)), original)
        before = copy.deepcopy(payload)
        self.execv.side_effect = SystemExit('captured exec')
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action', side_effect=self.prepared_batch_member) as prepare, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                host.run(self.envelope(reference))
            self.assertEqual(prepare.call_count, 3)
            receipt = json.loads(output.getvalue())
            self.assertEqual(self.execv.call_args.args[1][1], 'run-batch')
            plan = Path(receipt['plan_path'])
            plan_bytes = plan.read_bytes()
            prepare.reset_mock()
            # Both inline and a different reference file recover the same plan,
            # even after capacity falls. They cannot dispatch another batch.
            second = dict(self.reference(payload), operation='run_batch_ref')
            for repeated in (payload, second):
                output.seek(0)
                output.truncate()
                with mock.patch.object(host, 'worker_capacity', return_value=1):
                    with self.assertRaises(SystemExit):
                        host.run(self.envelope(repeated))
                prepare.assert_not_called()
                self.assertEqual(json.loads(output.getvalue()), receipt)
                self.assertEqual(plan.read_bytes(), plan_bytes)
                self.assertEqual(self.execv.call_args.args[1][1], 'read-batch')
        self.assertEqual(payload, before)
        self.prepare_process.assert_not_called()

    def test_batch_reference_rejects_non_direct_before_reading_file(self):
        reference = dict(self.reference(self.batch_payload()), operation='run_batch_ref')
        for version in ('nokiy_native_tool_v1', 'nokiy_native_tool_v3'):
            with mock.patch.object(host, 'reference_bytes') as read:
                with self.assertRaisesRegex(ValueError, 'Direct-only'):
                    host.run(self.envelope(reference, version))
                read.assert_not_called()
        self.execv.assert_not_called()

    def test_batch_reference_rejects_wrong_operations_and_member_grants(self):
        payload = self.missing_batch_payload()
        invalid = [self.payload(), self.envelope(payload)]
        invalid += [{'operation': op} for op in ('run_ref', 'run_batch_ref', 'read_batch', 'closeout')]
        injected = copy.deepcopy(payload)
        injected['members'][-1]['action']['native_thread_id'] = self.thread
        invalid.append(injected)
        bad_member = copy.deepcopy(payload)
        bad_member['members'][-1]['operation'] = 'run_batch'
        invalid.append(bad_member)
        for value in invalid:
            reference = dict(self.reference(value), operation='run_batch_ref')
            with self.assertRaises(ValueError):
                host.run(self.envelope(reference))
            self.assertTrue(all(not Path(m['draft']['artifact_root']).exists() for m in payload['members']))
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()

    def test_batch_reference_retains_strict_parser_byte_hash_and_path_bounds(self):
        payload = self.missing_batch_payload()
        data = json.dumps(payload).encode()
        bad_data = [b'{"operation":"run_batch","operation":"run_batch"}',
                    b'{"operation":"run_batch","members":NaN}',
                    b'{"operation":"run_batch","members":9007199254740993}',
                    b'\xff', data + b' ' * host.MAX_PAYLOAD_BYTES]
        references = [dict(self.reference(data=value), operation='run_batch_ref') for value in bad_data]
        good = dict(self.reference(payload), operation='run_batch_ref')
        references.append(dict(good, input_sha256='0' * 64))
        link = self.root / 'linked-input.json'
        link.symlink_to(good['input_path'])
        references.append(dict(good, input_path=str(link)))
        for value in references:
            with self.assertRaises((ValueError, OSError)):
                host.run(self.envelope(value))
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()
        self.assertTrue(all(not Path(m['draft']['artifact_root']).exists() for m in payload['members']))

    def test_batch_reference_capacity_and_parallelism_reject_before_effects(self):
        payload = self.missing_batch_payload()
        for parallel in (0, 4, True, '2'):
            reference = dict(self.reference(dict(payload, max_parallel_workers=parallel)), operation='run_batch_ref')
            with self.assertRaises(ValueError):
                host.run(self.envelope(reference))
        reference = dict(self.reference(payload), operation='run_batch_ref')
        with mock.patch.object(host, 'worker_capacity', return_value=2):
            with self.assertRaisesRegex(ValueError, 'capacity'):
                host.run(self.envelope(reference))
        self.execv.assert_not_called()
        self.prepare_process.assert_not_called()
        self.assertTrue(all(not Path(m['draft']['artifact_root']).exists() for m in payload['members']))

    def batch_payload(self, prefix='batch'):
        members = []
        for index in range(3):
            payload = self.payload()
            artifact = self.artifact / prefix / f'member-{index + 1}'
            artifact.mkdir(parents=True, exist_ok=True)
            payload['draft']['artifact_root'] = str(artifact)
            payload['action']['mission']['task_id'] = f'member-{index + 1}'
            members.append({key: value for key, value in payload.items() if key != 'operation'})
        return {'operation': 'run_batch', 'artifact_root': str(self.artifact),
                'max_parallel_workers': 3, 'members': members}

    def missing_batch_payload(self, prefix='automatic'):
        payload = self.batch_payload(prefix)
        payload['artifact_root'] = str(self.artifact / prefix)
        for member in payload['members']:
            Path(member['draft']['artifact_root']).rmdir()
        return payload

    def prepared_batch_member(self, envelope, entry, thread, worker, effort, tier, single, draft):
        self.assertEqual((entry, thread, worker, effort, tier),
                         (self.entry, self.thread, self.worker, self.effort, self.tier))
        path = Path(draft['artifact_root']) / 'request.json'
        path.write_text(host.canonical(single), encoding='utf-8')
        digest = host.sha(path)
        request_id = 'tura_embedded_' + digest
        path.with_name('preparation.json').write_text(json.dumps({
            'status': 'PREPARED', 'request_id': request_id,
            'preflight': {'status': 'READY', 'request_id': request_id,
                          'request_sha256': digest},
        }), encoding='utf-8')
        return path, None

    def test_batch_plan_parent_allows_disjoint_members_and_original_plan_recovery(self):
        payload = self.batch_payload()
        before = copy.deepcopy(payload)
        environment = dict(os.environ)

        self.execv.side_effect = SystemExit('captured exec without execution')
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action', side_effect=self.prepared_batch_member) as prepare, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(SystemExit, 'captured exec'):
                host.execute(self.envelope(payload))
            self.assertEqual(prepare.call_count, 3)
            receipt = json.loads(output.getvalue())
            plan = Path(receipt['plan_path'])
            self.assertEqual(plan.parent, self.artifact)
            self.assertEqual(host.sha(plan), receipt['plan_sha256'])
            data = json.loads(plan.read_text())
            self.assertEqual(data['max_parallel_workers'], 3)
            self.assertEqual([row['artifact_root'] for row in data['members']],
                             [row['draft']['artifact_root'] for row in payload['members']])
            self.assertEqual(len({row['request_id'] for row in data['members']}), 3)
            self.assertEqual(self.execv.call_args.args[1][1], 'run-batch')
            prepare.reset_mock()
            self.execv.reset_mock()
            output.seek(0)
            output.truncate()
            with self.assertRaisesRegex(SystemExit, 'captured exec'):
                host.execute(self.envelope(payload))
            prepare.assert_not_called()
            self.assertEqual(json.loads(output.getvalue()), receipt)
            self.assertEqual(self.execv.call_args.args[1], [str(self.entry), 'read-batch',
                             '--plan', str(plan), '--plan-sha256', receipt['plan_sha256'], '--summary'])
        self.assertEqual(payload, before)
        self.assertEqual(dict(os.environ), environment)
        self.prepare_process.assert_not_called()
        self.recovery_process.assert_not_called()

    def test_batch_creates_only_direct_members_without_parent_setup_round(self):
        payload = self.missing_batch_payload()
        before = copy.deepcopy(payload)
        self.execv.side_effect = SystemExit('captured exec')
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action', side_effect=self.prepared_batch_member) as prepare, \
                redirect_stdout(io.StringIO()) as output:
            with self.assertRaisesRegex(SystemExit, 'captured exec'):
                host.execute(self.envelope(payload))
            self.assertEqual(prepare.call_count, 3)
            receipt = json.loads(output.getvalue())
            plan = json.loads(Path(receipt['plan_path']).read_text())
            for member, row in zip(payload['members'], plan['members']):
                directory = Path(member['draft']['artifact_root'])
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(row['artifact_root'], str(directory))
                self.assertEqual(json.loads((directory / 'request.json').read_text()), {'operation': 'run', **member})
            self.assertEqual(self.execv.call_args.args[1][1], 'run-batch')
        self.assertEqual(payload, before)
        self.prepare_process.assert_not_called()
        self.recovery_process.assert_not_called()

    def test_batch_members_may_use_disjoint_workspaces(self):
        payload = self.missing_batch_payload('multi')
        other = self.root / 'workspace-b'
        other.mkdir()
        payload['members'][1]['draft']['workspace'] = str(other)
        self.execv.side_effect = SystemExit('captured exec')
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action', side_effect=self.prepared_batch_member) as prepare, \
                redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(SystemExit, 'captured exec'):
                host.execute(self.envelope(payload))
            self.assertEqual(prepare.call_count, 3)
        self.assertEqual(self.execv.call_args.args[1][1], 'run-batch')

    def test_batch_artifact_inside_another_member_workspace_is_rejected_before_preparation(self):
        payload = self.batch_payload('nested-workspace')
        payload['members'][1]['draft']['workspace'] = str(self.artifact / 'nested-workspace')
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action') as prepare:
            with self.assertRaisesRegex(ValueError, 'must not overlap any member workspace'):
                host.execute(self.envelope(payload))
            prepare.assert_not_called()
        self.execv.assert_not_called()

    def test_existing_batch_recovery_never_recreates_missing_members(self):
        payload = self.missing_batch_payload()
        self.execv.side_effect = SystemExit('captured exec')
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action', side_effect=self.prepared_batch_member) as prepare, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                host.execute(self.envelope(payload))
            missing = Path(payload['members'][1]['draft']['artifact_root'])
            (missing / 'request.json').unlink()
            (missing / 'preparation.json').unlink()
            missing.rmdir()
            prepare.reset_mock()
            with mock.patch.object(host.os, 'mkdir', side_effect=AssertionError('recovery mutation')):
                with self.assertRaises(SystemExit):
                    host.execute(self.envelope(payload))
            prepare.assert_not_called()
            self.assertEqual(self.execv.call_args.args[1][1], 'read-batch')
            self.assertFalse(missing.exists())

    def test_all_batch_validation_precedes_directory_creation(self):
        for case in ('action', 'capacity', 'identical', 'nested', 'outside', 'symlink', 'workspace'):
            with self.subTest(case=case):
                payload = self.missing_batch_payload(case)
                second = payload['members'][1]
                if case == 'action':
                    second['action']['model'] = 'forbidden override'
                elif case == 'identical':
                    second['draft']['artifact_root'] = payload['members'][0]['draft']['artifact_root']
                elif case == 'nested':
                    second['draft']['artifact_root'] += '/grandchild'
                elif case == 'outside':
                    second['draft']['artifact_root'] = str(self.root / 'outside')
                elif case == 'symlink':
                    Path(second['draft']['artifact_root']).symlink_to(self.workspace, target_is_directory=True)
                elif case == 'workspace':
                    payload['artifact_root'] = str(self.workspace)
                    for index, member in enumerate(payload['members']):
                        member['draft']['artifact_root'] = str(self.workspace / f'member-{index}')
                before = set(self.root.rglob('*'))
                with mock.patch.object(host, 'worker_capacity', return_value=2 if case == 'capacity' else 3), \
                        mock.patch.object(host, 'prepare_action') as prepare:
                    with self.assertRaises((ValueError, OSError)):
                        host.execute(self.envelope(payload))
                    prepare.assert_not_called()
                self.execv.assert_not_called()
                self.assertEqual(set(self.root.rglob('*')), before)

    def test_single_run_still_requires_existing_artifact_root(self):
        payload = self.payload()
        payload['draft']['artifact_root'] = str(self.artifact / 'missing')
        with mock.patch.object(host, 'prepare_action') as prepare:
            with self.assertRaises((ValueError, OSError)):
                host.execute(self.envelope(payload))
            prepare.assert_not_called()
        self.assertFalse(Path(payload['draft']['artifact_root']).exists())

    def test_directory_creation_collision_stops_before_preparation(self):
        payload = self.missing_batch_payload()
        collided = Path(payload['members'][0]['draft']['artifact_root'])
        real_mkdir = os.mkdir

        def concurrent_creation(path, *args, **kwargs):
            real_mkdir(path, *args, **kwargs)
            raise FileExistsError('concurrent artifact creation')

        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action') as prepare, \
                mock.patch.object(host.os, 'mkdir', side_effect=concurrent_creation):
            with self.assertRaises(FileExistsError):
                host.execute(self.envelope(payload))
            prepare.assert_not_called()
        self.assertTrue(collided.is_dir())
        self.assertFalse(Path(payload['members'][1]['draft']['artifact_root']).exists())
        self.execv.assert_not_called()

    def test_directory_root_replacement_stops_before_preparation(self):
        payload = self.missing_batch_payload()
        root = Path(payload['artifact_root'])
        displaced = root.with_name('displaced')
        real_mkdir = os.mkdir

        def root_replacement(path, *args, **kwargs):
            root.rename(displaced)
            real_mkdir(root, 0o700)
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action') as prepare, \
                mock.patch.object(host.os, 'mkdir', side_effect=root_replacement):
            with self.assertRaisesRegex(ValueError, 'root changed'):
                host.execute(self.envelope(payload))
            prepare.assert_not_called()
        self.assertEqual(list(root.iterdir()), [])
        self.execv.assert_not_called()

    def test_batch_overlapping_or_symlink_members_fail_before_any_preparation(self):
        for case in ('identical', 'nested', 'symlink'):
            with self.subTest(case=case):
                payload = self.batch_payload(case)
                first = Path(payload['members'][0]['draft']['artifact_root'])
                if case == 'identical':
                    replacement = first
                elif case == 'nested':
                    replacement = first / 'child'
                    replacement.mkdir()
                else:
                    replacement = self.artifact / 'alias'
                    replacement.symlink_to(first, target_is_directory=True)
                payload['members'][1]['draft']['artifact_root'] = str(replacement)
                before = set(self.artifact.rglob('*'))
                with mock.patch.object(host, 'prepare_action') as prepare:
                    with self.assertRaisesRegex(ValueError, 'overlap|canonical'):
                        host.execute(self.envelope(payload))
                    prepare.assert_not_called()
                self.execv.assert_not_called()
                self.prepare_process.assert_not_called()
                self.recovery_process.assert_not_called()
                self.assertEqual(set(self.artifact.rglob('*')), before)


if __name__ == '__main__':
    unittest.main()
