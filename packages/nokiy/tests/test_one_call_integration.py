# SPDX-License-Identifier: MIT
"""Real prepare/caller/service-process acceptance; requires host Seatbelt admission.

No provider calls. Do not substitute these tests with mocks or call a capability
failure a pass. DevSpace may reject nested sandbox_apply; run in the genuine
native parent execution plane with its original HOME and CODEX_HOME.
"""
from contextlib import redirect_stdout
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from codex_collaboration_harness import embedded_nokiy as caller
import test_full_stack as fixtures


class OneCallIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullStackTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f
        self.fixture.action.write_text(json.dumps({
            'mission': {'mission_id': 'one-call-integration', 'task_id': 'edit-and-recover', 'mode': 'DELIVERY',
                'objective': 'Edit the fixture and verify read-only result recovery',
                'current_predicate': 'the edit and its execution evidence are verified'},
            'context_summary': 'Use the synthetic full-core runtime to write answer.txt and recover its evidence.',
            'operations': ['create', 'modify'], 'write_scopes': ['answer.txt'], 'target_paths': ['answer.txt']}))
        jspace = self.f.workspace / 'scripts/ops/dcf/jspace.py'
        jspace.parent.mkdir(parents=True, exist_ok=True)
        jspace.write_text('# synthetic J-Space compiler\n')
        self.fixture.draft.update(model='gpt-6-astra', reasoning_effort='max')
        self.f.request_path.write_text(json.dumps(self.fixture.draft))
        self.argv = ['prepare', '--request', str(self.f.request_path),
            '--action', str(self.fixture.action), '--surface-id', 'research_execution_engine',
            '--output-dir', str(self.fixture.output)]

    def cli(self, args):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = caller.main(args)
        return code, json.loads(stream.getvalue())

    def assert_completed(self, code, result):
        request = caller.load_request(self.fixture.output / 'request.json')
        errors = request.artifact_root / request.request_id / 'supervision.stderr'
        diagnostic = errors.read_text()[:2048] if errors.is_file() else None
        self.assertEqual(code, 0, {'terminal': result, 'supervision_stderr': diagnostic})
        self.assertEqual(result['result_inspection']['status'], 'EVIDENCE_VERIFIED')

    def test_real_combined_process_evidence_and_readonly_recovery(self):
        with self.fixture.compiler():
            code, result = self.cli(self.argv + ['--run', '--require-verified-evidence'])
        self.assert_completed(code, result)
        self.assertEqual((result['model'], result['reasoning_effort']), ('gpt-6-astra', 'max'))
        request = caller.load_request(self.fixture.output / 'request.json')
        run = request.artifact_root / request.request_id
        terminal = run / 'terminal.json'
        raw = terminal.read_bytes()
        self.assertEqual((self.f.workspace / 'answer.txt').read_text(), 'full-core result\n')
        self.assertEqual(result['prepared_request']['sha256'],
                         hashlib.sha256((self.fixture.output / 'request.json').read_bytes()).hexdigest())
        with patch.object(caller, 'execute', side_effect=AssertionError('recovery executed')):
            recovered_code, recovered = self.cli(['read-result', '--artifact-root', str(request.artifact_root),
                '--request-id', request.request_id, '--require-verified-evidence'])
        self.assertEqual(recovered_code, 0, recovered)
        self.assertEqual(recovered['result_text'], result['result_text'])
        self.assertEqual(recovered['usage'], result['usage'])
        self.assertEqual(terminal.read_bytes(), raw)
        with self.fixture.compiler(), patch.object(caller, 'execute', side_effect=AssertionError('replayed')):
            retry_code, retry = self.cli(self.argv + ['--run'])
        self.assertEqual(retry_code, 2)
        self.assertEqual(terminal.read_bytes(), raw)
        self.assertEqual(retry['status'], 'BLOCKED_PREEXECUTION')

    def test_split_path_retains_same_semantic_result_and_readback(self):
        with self.fixture.compiler():
            code, prepared = self.cli(self.argv)
        self.assertEqual(code, 0)
        self.assertEqual(prepared['status'], 'PREPARED')
        code, result = self.cli(['run', '--request', prepared['request']['path'], '--require-verified-evidence'])
        self.assert_completed(code, result)
        self.assertEqual((self.f.workspace / 'answer.txt').read_text(), 'full-core result\n')
        self.assertNotIn('prepared_request', result)


if __name__ == '__main__':
    unittest.main()
