"""Offline metadata-only native surface query boundary tests."""

import copy
from contextlib import ExitStack
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'nokiy-native-host.py'
SPEC = importlib.util.spec_from_file_location('nokiy_surface_query_host', SOURCE)
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


class SurfaceQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nokiy-surface-query-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        self.home = self.root / 'home'
        self.workspace.mkdir()
        self.entry = self.home / '.local' / 'bin' / 'nokiy-embedded-run'
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text('Offline stub; never executed.\n', encoding='utf-8')
        (self.workspace / 'source.py').write_text('source fixture\n', encoding='utf-8')
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {
            'HOME': str(self.home), 'CODEX_HOME': str(self.root),
            'CODEX_THREAD_ID': '01234567-89ab-cdef-0123-456789abcdef',
        }))
        self.call = stack.enter_context(mock.patch.object(host.subprocess, 'call', return_value=0))
        self.prepare = stack.enter_context(mock.patch.object(
            host, 'prepare_action', side_effect=AssertionError('Query must never prepare')))
        self.process = stack.enter_context(mock.patch.object(
            host.subprocess, 'run', side_effect=AssertionError('Query must never start worker')))
        self.execv = stack.enter_context(mock.patch.object(
            host.os, 'execv', side_effect=AssertionError('Unexpected worker exec')))

    def payload(self):
        return {'operation': 'query_surfaces', 'workspace': str(self.workspace),
                'targets': ['source.py']}

    def envelope(self, payload, version='nokiy_native_tool_v2'):
        result = {'schema_version': version, 'worker': 'irrelevant-invalid-worker',
                  'service_tier': 'irrelevant-invalid-tier', 'payload': payload}
        if version != 'nokiy_native_tool_v1':
            result['reasoning_effort'] = 'irrelevant-invalid-effort'
        if version == 'nokiy_native_tool_v3':
            result['workflow'] = 'nokiy'
        return result

    def assert_no_effect_route(self):
        self.prepare.assert_not_called()
        self.process.assert_not_called()
        self.execv.assert_not_called()
        self.assertFalse((self.root / 'nokiy-native-locks').exists())

    def test_query_uses_installed_caller_before_model_checks_and_no_serial_lock(self):
        for version in ('nokiy_native_tool_v1', 'nokiy_native_tool_v2', 'nokiy_native_tool_v3'):
            with self.subTest(version=version):
                payload = self.payload()
                original = copy.deepcopy(payload)
                self.assertEqual(host.run(self.envelope(payload, version)), 0)
                self.assertEqual(payload, original)
                self.call.assert_called_with([
                    str(self.entry), 'query-surfaces', '--workspace', str(self.workspace),
                    '--target=source.py',
                ])
        self.assert_no_effect_route()

    def test_navigation_dispatches_same_caller_without_worker_or_prepare(self):
        for version in ('nokiy_native_tool_v1', 'nokiy_native_tool_v2', 'nokiy_native_tool_v3'):
            for fields in ({'query': 'name'}, {}):
                with self.subTest(version=version, fields=fields):
                    payload = {'operation': 'navigate', 'workspace': str(self.workspace),
                               'read_paths': ['source.py'], **fields}
                    self.assertEqual(host.run(self.envelope(payload, version)), 0)
                    self.call.assert_called_with([
                        str(self.entry), 'navigate', '--workspace', str(self.workspace),
                        '--query=' + fields.get('query', ''), '--offset', '0',
                        '--projection=full', '--read-path=source.py',
                    ])
        self.assert_no_effect_route()

    def test_navigation_forwards_batches_and_projection_without_normalizing(self):
        terms = ['Zed', 'alpha.*', 'Zed', 'a' * 256, 'é' * 128, '😀' * 64]
        payload = {'operation': 'navigate', 'workspace': str(self.workspace),
                   'read_paths': ['source.py'], 'queries': terms, 'projection': 'locations',
                   'offset': 2, 'expected_map_sha256': 'a' * 64}
        original = copy.deepcopy(payload)
        self.assertEqual(host.run(self.envelope(payload)), 0)
        self.assertEqual(payload, original)
        self.call.assert_called_once_with([
            str(self.entry), 'navigate', '--workspace', str(self.workspace),
            '--query=', '--offset', '2', '--projection=locations', '--read-path=source.py',
            '--query-any=Zed', '--query-any=alpha.*', '--query-any=Zed',
            '--query-any=' + 'a' * 256, '--query-any=' + 'é' * 128, '--query-any=' + '😀' * 64,
            '--expected-map-sha256=' + 'a' * 64,
        ])
        self.assert_no_effect_route()

    def test_navigation_accepts_empty_scalar_with_maximum_batch_and_default_full(self):
        payload = {'operation': 'navigate', 'workspace': str(self.workspace),
                   'read_paths': ['source.py'], 'query': '', 'queries': ['same'] * 32}
        self.call.return_value = 7
        self.assertEqual(host.run(self.envelope(payload)), 7)
        self.call.assert_called_once_with([
            str(self.entry), 'navigate', '--workspace', str(self.workspace),
            '--query=', '--offset', '0', '--projection=full', '--read-path=source.py',
            *(['--query-any=same'] * 32),
        ])
        self.assert_no_effect_route()

    def test_navigation_source_snapshot_passes_unchanged_without_becoming_a_cursor(self):
        payload = {'operation': 'navigate', 'workspace': str(self.workspace),
                   'read_paths': ['source.py'], 'queries': ['changed'], 'projection': 'full',
                   'expected_source_snapshot_sha256': 'b' * 64}
        self.assertEqual(host.run(self.envelope(payload)), 0)
        self.call.assert_called_once_with([
            str(self.entry), 'navigate', '--workspace', str(self.workspace), '--query=',
            '--offset', '0', '--projection=full', '--read-path=source.py', '--query-any=changed',
            '--expected-source-snapshot-sha256=' + 'b' * 64,
        ])
        self.call.reset_mock()
        with self.assertRaises(ValueError):
            host.run(self.envelope(dict(payload, offset=1)))
        self.call.assert_not_called()
        both = dict(payload, offset=1, expected_map_sha256='a' * 64)
        self.assertEqual(host.run(self.envelope(both)), 0)
        self.assertEqual(self.call.call_args.args[0][-2:], [
            '--expected-map-sha256=' + 'a' * 64, '--expected-source-snapshot-sha256=' + 'b' * 64])
        self.assert_no_effect_route()

    def test_navigation_rejects_invalid_batches_projection_mixed_fields_and_cursor(self):
        payload = {'operation': 'navigate', 'workspace': str(self.workspace), 'read_paths': ['source.py']}
        invalid = [
            {'action': {}}, {'offset': 1}, {'offset': -1}, {'expected_map_sha256': 'wrong'},
            *({'expected_source_snapshot_sha256': value} for value in ('wrong', 'A' * 64, [], 1)),
            *({'queries': value} for value in (None, 'name', {}, [], ['name'] * 33,
                [''], [None], [1], ['a' * 257], ['é' * 129], ['😀' * 65], ['\ud800'])),
            *({'query': value} for value in (None, 1, [], 'a' * 513)),
            {'query': 'name', 'queries': ['other']},
            *({'projection': value} for value in (None, 1, {}, 'compact', '')),
        ]
        for extra in invalid:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                host.run(self.envelope(dict(payload, **extra)))
        self.call.assert_not_called()
        self.assert_no_effect_route()

    def test_thread_and_envelope_binding_remain_required(self):
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': ''}), self.assertRaises(ValueError):
            host.run(self.envelope(self.payload()))
        envelope = self.envelope(self.payload())
        envelope['native_thread_id'] = 'borrowed'
        with self.assertRaises(ValueError):
            host.run(envelope)
        self.call.assert_not_called()
        self.assert_no_effect_route()

    def test_exact_fields_and_target_bounds_are_rejected_before_caller(self):
        invalid = [None, [], {}, 'source.py', [], ['source.py'] * 2,
                   ['source.py'] * 5, [None], [''], ['.'], ['..'], ['/absolute.py'],
                   ['a/../source.py'], ['a/./source.py'], ['a//source.py'], ['source.py/'],
                   ['.git/config'], ['a/.git/config'], ['*.py'], ['a?.py'], ['[a].py'],
                   ['{a}.py'], ['a\\b.py'], ['a\x00b.py'], ['a\nb.py'], ['a\rb.py'],
                   ['a' * 513]]
        for targets in invalid:
            payload = self.payload()
            payload['targets'] = targets
            with self.subTest(targets=targets), self.assertRaises(ValueError):
                host.run(self.envelope(payload))
        for field in ('draft', 'action', 'surface_id', 'input_id', 'credentials', 'native_thread_id'):
            payload = self.payload()
            payload[field] = 'not allowed'
            with self.subTest(field=field), self.assertRaises(ValueError):
                host.run(self.envelope(payload))
        for field in ('workspace', 'targets'):
            payload = self.payload()
            del payload[field]
            with self.subTest(missing=field), self.assertRaises(ValueError):
                host.run(self.envelope(payload))
        self.call.assert_not_called()
        self.assert_no_effect_route()

    def test_workspace_alias_queries_the_canonical_workspace(self):
        # A symlinked spelling names the same directory: query it physically instead of
        # refusing; target ancestry is still checked below the canonical workspace.
        alias = self.root / 'alias'
        alias.symlink_to(self.workspace, target_is_directory=True)
        payload = self.payload()
        payload['workspace'] = str(alias)
        self.assertEqual(host.run(self.envelope(payload)), 0)
        self.call.assert_called_once_with([
            str(self.entry), 'query-surfaces', '--workspace', str(self.workspace),
            '--target=source.py',
        ])
        self.assert_no_effect_route()

    def test_target_symlink_ancestry_and_malformed_workspaces_are_rejected(self):
        linked = self.workspace / 'linked.py'
        linked.symlink_to(self.workspace / 'source.py')
        nested = self.workspace / 'nested'
        nested.symlink_to(self.workspace, target_is_directory=True)
        for workspace, target in [(str(self.workspace), 'linked.py'), (str(self.workspace), 'nested/source.py')]:
            with self.subTest(workspace=workspace, target=target), self.assertRaises(ValueError):
                host.run(self.envelope({'operation': 'query_surfaces', 'workspace': workspace,
                                        'targets': [target]}))
        for workspace in (None, {}, 'relative', str(self.workspace) + '/',
                          str(self.workspace) + '/.', str(self.workspace) + '/../workspace',
                          '//' + str(self.workspace).lstrip('/')):
            with self.subTest(workspace=workspace), self.assertRaises((TypeError, ValueError)):
                host.run(self.envelope({'operation': 'query_surfaces', 'workspace': workspace,
                                        'targets': ['source.py']}))
        self.call.assert_not_called()
        self.assert_no_effect_route()

    def test_unicode_targets_use_character_bound_and_argparse_safe_exact_arguments(self):
        name = '/'.join(['\u96ea' * 60] * 3 + ['\u96ea' * 60 + '.py'])
        file = self.workspace / name
        file.parent.mkdir(parents=True)
        file.write_text('Unicode path fixture\n', encoding='utf-8')
        (self.workspace / '-leading.py').write_text('Leading hyphen fixture\n', encoding='utf-8')
        self.assertGreater(len(name.encode('utf-8')), 512)
        self.assertLess(len(name), 512)
        payload = {'operation': 'query_surfaces', 'workspace': str(self.workspace),
                   'targets': [name, '-leading.py']}
        self.assertEqual(host.run(self.envelope(payload)), 0)
        self.call.assert_called_once_with([
            str(self.entry), 'query-surfaces', '--workspace', str(self.workspace),
            '--target=' + name, '--target=-leading.py',
        ])
        self.assert_no_effect_route()

    def test_caller_diagnostic_exit_code_is_preserved_without_retry(self):
        self.call.return_value = 3
        self.assertEqual(host.run(self.envelope(self.payload())), 3)
        self.call.assert_called_once()
        self.assert_no_effect_route()


if __name__ == '__main__':
    unittest.main()
