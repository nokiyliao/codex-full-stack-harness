"""An inherited setting must never reset or extend a bounded worker deadline."""
import json
from dataclasses import replace
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_full_core as fixtures
from codex_collaboration_harness import full_core as core
from codex_collaboration_harness import embedded_nokiy as caller
from codex_collaboration_harness import full_stack, local_context


class ExecutionBudgetTests(unittest.TestCase):
    def test_supervisor_mints_one_request_bound_deadline_not_inherited_value(self):
        request = SimpleNamespace(timeout_seconds=360, request_sha256='a' * 64)
        with patch.dict(os.environ, {core.BUDGET_ENV: 'foreign', 'HOME': '/native-home',
                                     'CODEX_THREAD_ID': 'native-thread'}), \
                patch.object(core.time, 'time_ns', return_value=1_000_000_000_000):
            env = core._supervisor_environment(request)
            self.assertEqual(json.loads(env[core.BUDGET_ENV]), {
                'schema_version': core.BUDGET_SCHEMA, 'request_sha256': 'a' * 64,
                'timeout_ms': 360000, 'deadline_unix_ms': 1360000, 'reserve_ms': 10000})
            self.assertEqual(env['HOME'], '/native-home')
            self.assertEqual(env['CODEX_THREAD_ID'], 'native-thread')
            self.assertEqual(os.environ[core.BUDGET_ENV], 'foreign')

    def test_short_action_has_proportionate_reserve(self):
        request = SimpleNamespace(timeout_seconds=10, request_sha256='a' * 64)
        value = json.loads(core._supervisor_environment(request)[core.BUDGET_ENV])
        self.assertEqual(value['reserve_ms'], 1000)

    def test_null_supervisor_scrubs_inherited_budget_without_minting(self):
        request = SimpleNamespace(timeout_seconds=None, request_sha256='a' * 64)
        with patch.dict(os.environ, {core.BUDGET_ENV: 'foreign', 'HOME': '/native-home',
                                     'CODEX_THREAD_ID': 'native-thread'}), \
                patch.object(core.time, 'time_ns', side_effect=AssertionError('deadline minted')):
            env = core._supervisor_environment(request)
            self.assertNotIn(core.BUDGET_ENV, env)
            self.assertEqual(env['HOME'], '/native-home')
            self.assertEqual(env['CODEX_THREAD_ID'], 'native-thread')
            self.assertEqual(os.environ[core.BUDGET_ENV], 'foreign')

    def fixture(self):
        f = fixtures.FullCoreTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def test_runtime_environment_only_forwards_explicit_budget_unchanged(self):
        f = self.fixture()
        request = f._request()
        runtime = caller.verify_runtime_image(request.runtime_image, required_artifacts=core.REQUIRED)
        state = request.artifact_root / 'state'
        with patch.dict(os.environ, {core.BUDGET_ENV: 'foreign'}):
            self.assertNotIn(core.BUDGET_ENV, core._environment(request, runtime, state))
            raw = core._supervisor_environment(request)[core.BUDGET_ENV]
            first = core._environment(request, runtime, state, execution_budget=raw)
            second = core._environment(request, runtime, state, execution_budget=raw)
            self.assertEqual(first[core.BUDGET_ENV], raw)
            self.assertEqual(second[core.BUDGET_ENV], raw)
            self.assertEqual(first['TURA_EXEC_ROUTER_READ_TIMEOUT_SECS'], str(request.timeout_seconds))

    def test_null_runtime_omits_budget_and_router_timeout_even_if_inherited(self):
        f = self.fixture()
        request = replace(f._request(), timeout_seconds=None)
        runtime = caller.verify_runtime_image(request.runtime_image, required_artifacts=core.REQUIRED)
        with patch.dict(os.environ, {core.BUDGET_ENV: 'foreign',
                                     'TURA_EXEC_ROUTER_READ_TIMEOUT_SECS': '1'}):
            env = core._environment(request, runtime, request.artifact_root / 'state',
                                    execution_budget='foreign-explicit')
        self.assertNotIn(core.BUDGET_ENV, env)
        self.assertNotIn('TURA_EXEC_ROUTER_READ_TIMEOUT_SECS', env)
        self.assertEqual(env['TURA_COMMAND_RUN_SANDBOX'], 'true')
        self.assertEqual(env['TURA_GATEWAY_CALLBACKS'], '0')

    def test_full_stack_defaults_new_full_core_lifetime_but_keeps_explicit_values(self):
        f = self.fixture()
        draft = f._request().to_wire(include_identity=False)
        for key in ('context_capsule', 'jspace_contract', 'timeout_seconds'):
            draft.pop(key)
        draft_path, action_path = f.f.root / 'draft.json', f.f.root / 'action.json'
        action_path.write_text(json.dumps({'mission': {'task_id': 'nullable-default'},
                                          'include_source_excerpt': False}))
        compiled = (json.loads(f.f.context.read_text()), json.loads(f.f.jspace.read_text()))
        for profile in ('direct', 'balanced'):
            for label, timeout in (('missing', None), ('null', None), ('short', 10), ('long', 900)):
                with self.subTest(profile=profile, lifetime=label):
                    value = dict(draft, execution_profile=profile)
                    if label != 'missing':
                        value['timeout_seconds'] = timeout
                    draft_path.write_text(json.dumps(value))
                    output = f.f.root / (profile + '-' + label)
                    with patch.object(local_context, 'dcf_root', return_value=None), \
                            patch.object(local_context, 'compile_context', return_value=compiled), \
                            patch.object(caller, 'preflight', return_value={'status': 'READY'}):
                        full_stack.prepare_request(draft_path, action_path, None, output)
                    request = caller.load_request(output / 'request.json')
                    self.assertEqual(request.timeout_seconds, timeout)
                    self.assertEqual(request.model, draft['model'])
                    self.assertEqual(request.execution_profile, profile)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin signal scope')
    def test_real_null_supervision_and_replay_never_mint_a_total_budget(self):
        f = self.fixture()
        request = replace(f._request(), timeout_seconds=None)
        with patch.dict(os.environ, {core.BUDGET_ENV: 'foreign'}):
            terminal = core.execute_full_core(request)
        self.assertEqual(terminal['status'], 'RESULT_AVAILABLE', terminal)
        self.assertTrue(terminal['cleanup_pass'])
        budget = request.artifact_root / request.request_id / 'execution-state/execution-budget.json'
        self.assertFalse(budget.exists())
        with patch.object(core, '_supervisor_environment', side_effect=AssertionError('replayed')):
            self.assertEqual(core.execute_full_core(request), terminal)

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin signal scope')
    def test_old_runtime_rejected_before_any_worker_effect(self):
        f = self.fixture()
        binary = f.f.runtime / 'tura_runtime'
        binary.write_text(binary.read_text().replace(core.BUDGET_SCHEMA, 'old_runtime'))
        f._image()
        request = f._request()
        with self.assertRaises(caller.EmbeddedNokiyError) as error:
            core.execute_full_core(request)
        self.assertEqual(error.exception.code, 'NOKIY_FULL_CORE_EXECUTION_BUDGET_REQUIRED')
        self.assertFalse((request.artifact_root / request.request_id).exists())

    @unittest.skipUnless(sys.platform == 'darwin', 'Darwin signal scope')
    def test_real_supervision_passes_same_binding_and_replay_does_not_remint(self):
        f = self.fixture()
        request = f._request()
        start = time.time_ns() // 1_000_000
        with patch.dict(os.environ, {core.BUDGET_ENV: 'untrusted-inherited-value'}):
            terminal = core.execute_full_core(request)
        self.assertEqual(terminal['status'], 'RESULT_AVAILABLE')
        self.assertTrue(terminal['cleanup_pass'])
        path = request.artifact_root / request.request_id / 'execution-state/execution-budget.json'
        value = json.loads(path.read_text())
        self.assertEqual(value['request_sha256'], request.request_sha256)
        self.assertEqual(value['timeout_ms'], request.timeout_seconds * 1000)
        self.assertGreaterEqual(value['deadline_unix_ms'], start + value['timeout_ms'])
        self.assertLessEqual(value['deadline_unix_ms'], time.time_ns() // 1_000_000 + value['timeout_ms'])
        before = path.read_bytes()
        with patch.object(core, '_supervisor_environment', side_effect=AssertionError('replayed')):
            self.assertEqual(core.execute_full_core(request), terminal)
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
