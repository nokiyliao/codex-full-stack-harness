"""Caller count diagnostics remain typed, public, and safe through recovery."""
import copy
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('context_limit_reference', ROOT / 'test_nokiy_native_reference.py')
existing = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(existing)
host = existing.host

ISSUES = [{'path': 'action.read_scopes', 'reason': 'context_file_limit_exceeded'}]
LIMIT = {'maximum': 32, 'actual': 37,
         'counted_scopes': ['read_scopes', 'write_scopes', 'target_paths'],
         'next_action': 'narrow_exact_context_files'}


class ContextFileLimitTests(existing.NativeReferenceTests):
    def failure(self, **changes):
        return {'schema_version': 'tura_embedded_failure_v1', 'status': 'BLOCKED_PREEXECUTION',
                'authority_effect': 'none', 'first_typed_blocker': 'NOKIY_LOCAL_CONTEXT_INVALID',
                'issues': copy.deepcopy(ISSUES), 'context_file_limit': copy.deepcopy(LIMIT),
                'secret': 'NEVER_ECHO_THIS', **changes}

    def prepare(self, payload):
        return host.prepare_action(self.envelope(payload), self.entry, self.thread,
            self.worker, self.effort, self.tier, payload, host.normalize_draft(payload['draft']))

    def round_trip(self, failure, name, payload=None):
        payload = payload or self.payload()
        fresh = self._fresh_rejection(failure, name, payload=payload)
        artifact = Path(payload['draft']['artifact_root'])
        inputs = artifact / fresh['preparation_ref']['input_id']
        self.assertEqual(json.loads((inputs / 'prepare-outcome.json').read_text()), fresh)
        before = {path.name: path.read_bytes() for path in inputs.iterdir()}
        with mock.patch.object(host.subprocess, 'run', side_effect=AssertionError('no replay')), \
             mock.patch.object(host.os, 'execv', side_effect=AssertionError('no worker')), \
             redirect_stdout(io.StringIO()) as output:
            request, cached = self.prepare(payload)
            self.assertIsNone(request)
            self.assertEqual(host.read_preparation({'operation': 'read_preparation',
                'artifact_root': str(artifact), 'input_id': inputs.name}, self.thread), 0)
        inspected = json.loads(output.getvalue())
        self.assertEqual(cached, fresh)
        self.assertIs(inspected['preparation_rerun'], False)
        self.assertEqual(before, {path.name: path.read_bytes() for path in inputs.iterdir()})
        for result in (fresh, cached, inspected):
            self.assertIs(result['provider_execution_started'], False)
            self.assertIs(result['same_input_retry_allowed'], False)
            self.assertNotIn('NEVER_ECHO_THIS', json.dumps(result))
        self.recovery_process.assert_not_called()
        self.execv.assert_not_called()
        return fresh, cached, inspected

    def test_precise_issue_and_caller_counts_survive_all_projections(self):
        payload = self.payload()
        reads = [f'source_{index}.py' for index in range(37)]
        for name in reads:
            (self.workspace / name).write_text('# temporary source fixture\n')
        payload['action'].update(operations=['read', 'command', 'modify'],
            read_scopes=reads, write_scopes=reads[:3], target_paths=reads[:3])
        original = copy.deepcopy(payload['action'])
        for result in self.round_trip(self.failure(), 'count-rejection', payload):
            self.assertEqual(result['issues'], ISSUES)
            self.assertEqual(result['context_file_limit'], LIMIT)
        self.assertEqual(payload['action'], original)
        self.prepare_process.assert_called_once()

    def test_count_diagnostic_does_not_define_a_host_cap(self):
        limit = dict(LIMIT, maximum=2, actual=3)
        for result in self.round_trip(self.failure(context_file_limit=limit), 'caller-bound'):
            self.assertEqual(result['context_file_limit'], limit)

    def test_optional_facts_and_legacy_issues_are_not_reinterpreted(self):
        cases = [
            ('optional', ISSUES, None),
            ('legacy', [{'path': 'action', 'reason': 'invalid_shape'}], LIMIT),
            ('other', [{'path': 'action.read_scopes', 'reason': 'exact_file_unavailable'}], LIMIT),
            ('unknown', [{'path': 'action.read_scopes', 'reason': 'NEVER_ECHO_THIS'}], LIMIT),
            ('private-issue', [dict(ISSUES[0], private='NEVER_ECHO_THIS')], LIMIT),
        ]
        for name, issues, limit in cases:
            with self.subTest(name=name):
                failure = self.failure(issues=issues)
                if limit is None:
                    failure.pop('context_file_limit')
                for result in self.round_trip(failure, name):
                    self.assertNotIn('context_file_limit', result)
                    if name in {'unknown', 'private-issue'}:
                        self.assertNotIn('issues', result)
                    else:
                        self.assertEqual(result['issues'], issues)

    def test_malformed_count_facts_are_dropped_without_losing_the_issue(self):
        malformed = [None, [],
            dict(LIMIT, maximum=True), dict(LIMIT, actual=True),
            dict(LIMIT, maximum='32'), dict(LIMIT, actual='37'),
            dict(LIMIT, maximum=32.0), dict(LIMIT, actual=37.0),
            dict(LIMIT, maximum=0), dict(LIMIT, actual=32),
            dict(LIMIT, private='NEVER_ECHO_THIS'),
            {key: value for key, value in LIMIT.items() if key != 'maximum'},
            dict(LIMIT, counted_scopes=['read_scopes']),
            dict(LIMIT, next_action='NEVER_ECHO_THIS')]
        for index, limit in enumerate(malformed):
            with self.subTest(limit=limit):
                for result in self.round_trip(self.failure(context_file_limit=limit), f'invalid-{index}'):
                    self.assertEqual(result['issues'], ISSUES)
                    self.assertNotIn('context_file_limit', result)
        # Recovery's strict JSON decoder already rejects nonfinite JSON; the live
        # projection must also drop them before they can become cached public facts.
        for index, value in enumerate((float('nan'), float('inf'))):
            outcome = self._fresh_rejection(self.failure(context_file_limit=dict(LIMIT, actual=value)),
                                            f'nonfinite-{index}')
            self.assertEqual(outcome['issues'], ISSUES)
            self.assertNotIn('context_file_limit', outcome)

    def test_cached_count_facts_are_revalidated_without_rewriting_evidence(self):
        payload = self.payload()
        fresh = self._fresh_rejection(self.failure(), 'cached-validation', payload=payload)
        path = Path(payload['draft']['artifact_root']) / fresh['preparation_ref']['input_id'] / 'prepare-outcome.json'
        cases = [dict(fresh, context_file_limit=dict(LIMIT, private='NEVER_ECHO_THIS')),
                 dict(fresh, context_file_limit=dict(LIMIT, actual=True)),
                 dict(fresh, issues=[{'path': 'action', 'reason': 'invalid_shape'}]),
                 dict(fresh, first_typed_blocker='NOKIY_OTHER_FAILURE')]
        for cached in cases:
            with self.subTest(cached=cached):
                path.write_text(json.dumps(cached))
                before = path.read_bytes()
                with mock.patch.object(host.subprocess, 'run', side_effect=AssertionError('no replay')):
                    request, result = self.prepare(payload)
                self.assertIsNone(request)
                self.assertNotIn('context_file_limit', result)
                self.assertNotIn('NEVER_ECHO_THIS', json.dumps(result))
                self.assertEqual(path.read_bytes(), before)

    def test_untrusted_failure_envelopes_do_not_publish_counts(self):
        for field, value in [('schema_version', 'unknown'), ('status', 'EXECUTED'),
                             ('authority_effect', 'workspace'),
                             ('first_typed_blocker', 'NOKIY_OTHER_FAILURE')]:
            with self.subTest(field=field):
                result = self._fresh_rejection(self.failure(**{field: value}), 'untrusted-' + field)
                self.assertNotIn('issues', result)
                self.assertNotIn('context_file_limit', result)


def load_tests(loader, tests, pattern):
    # Reuse the reference fixtures without rediscovering their whole suite here.
    return unittest.TestSuite(ContextFileLimitTests(name) for name in ContextFileLimitTests.__dict__
                              if name.startswith('test_'))
