"""Exact files outside the worker workspace become inlined evidence, not unsafe-path refusals.

Live 2026-10-22 (i6, Nokiy Ultra): coordinators listed the frozen tests, rubric and validator that
the task names, all outside the worker workspace, in read_scopes; every dispatch was refused with
action.read_scopes unsafe_path or came back as a capability gap, and no source was changed.
"""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'src'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('external_evidence_host', 'nokiy-native-host.py')


class ExternalEvidenceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='nokiy-external-evidence-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        self.workspace = root / 'workspace'
        (self.workspace / 'pkg').mkdir(parents=True)
        (self.workspace / 'pkg/module.py').write_text('x = 1\n')
        self.frozen = root / 'frozen'
        self.frozen.mkdir()
        self.rubric = self.frozen / 'rubric.json'
        self.rubric.write_text('{"A": "exact"}\n')
        self.tests = self.frozen / 'test_contract.py'
        self.tests.write_text('import unittest\n')
        self.binary = self.frozen / 'blob.bin'
        self.binary.write_bytes(b'\xff\xfe\x00')
        self.link = root / 'rubric-link.json'
        self.link.symlink_to(self.rubric)
        self.draft = {'workspace': str(self.workspace), 'prompt': 'Implement pkg/module.py.'}

    def action(self, reads, **extra):
        return {'operations': ['read', 'modify', 'command'], 'source_read': True, 'read_scopes': reads,
                'write_scopes': ['pkg/module.py'], 'target_paths': ['pkg/module.py'], **extra}

    def test_external_files_are_inlined_with_sha_and_leave_read_scopes(self):
        action, draft, repairs = host.inline_external_reads(
            self.action(['pkg/module.py', str(self.rubric), str(self.tests)]), self.draft, self.workspace)
        self.assertEqual(action['read_scopes'], ['pkg/module.py'])
        self.assertIs(action['source_read'], True)
        self.assertEqual(action['operations'], ['read', 'modify', 'command'])
        for path in (self.rubric, self.tests):
            self.assertIn(str(path) + '\nSHA-256: ' + hashlib.sha256(path.read_bytes()).hexdigest(), draft['prompt'])
            self.assertIn('BEGIN EVIDENCE\n' + path.read_text() + '\nEND EVIDENCE', draft['prompt'])
        self.assertTrue(draft['prompt'].startswith('Implement pkg/module.py.'))
        self.assertEqual(repairs, [{'path': 'action.read_scopes', 'rule': 'external_read_to_inline_evidence', 'count': 2}])
        self.assertEqual(self.draft['prompt'], 'Implement pkg/module.py.', 'input draft is not mutated')

    def test_explicit_small_verifier_pin_is_context_without_changing_authority(self):
        data = '{"label": "\u96ea"}\r\n'.encode('utf-8')
        self.rubric.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        for pin_digest in (digest, None):
            with self.subTest(pin_digest=pin_digest):
                pin = {'path': str(self.rubric), 'scope': 'verifier_only'}
                if pin_digest is not None:
                    pin['sha256'] = pin_digest
                commands = [{'argv': ['/usr/bin/python3', str(self.rubric)], 'pinned_files': [pin]}]
                source = self.action(['pkg/module.py', str(self.rubric)], verifier_commands=commands,
                                     forbidden_effects=['deployment', 'broker', 'order'])
                action, draft, repairs = host.inline_external_reads(source, self.draft, self.workspace)
                self.assertEqual(action, {**source, 'read_scopes': ['pkg/module.py']})
                self.assertEqual(action['verifier_commands'], commands)
                self.assertEqual(source['read_scopes'], ['pkg/module.py', str(self.rubric)])
                self.assertIn(host.INLINE_EVIDENCE_NOTE, draft['prompt'])
                self.assertIn(str(self.rubric) + '\nSHA-256: ' + digest, draft['prompt'])
                self.assertIn('BEGIN EVIDENCE\n' + data.decode('utf-8') + '\nEND EVIDENCE',
                              draft['prompt'])
                self.assertEqual(self.draft['prompt'], 'Implement pkg/module.py.')
                self.assertEqual(repairs, [{'path': 'action.read_scopes',
                    'rule': 'external_read_to_inline_evidence', 'count': 1}])

    def test_a_pin_without_an_explicit_read_is_not_worker_context(self):
        commands = [{'pinned_files': [{'path': str(self.rubric), 'scope': 'verifier_only',
                                      'sha256': host.sha(self.rubric)}]}]
        source = self.action(['pkg/module.py'], verifier_commands=commands)
        self.assertEqual(host.inline_external_reads(source, self.draft, self.workspace),
                         (source, self.draft, []))

    def test_identity_projection_and_context_use_the_same_observed_bytes(self):
        data = self.rubric.read_bytes()
        commands = [{'pinned_files': [{'path': str(self.rubric), 'scope': 'verifier_only',
                                      'sha256': hashlib.sha256(data).hexdigest()}]}]
        source = self.action(['pkg/module.py', str(self.rubric)], verifier_commands=commands)
        observations = {}
        identity, identity_draft, _ = host.inline_external_reads(
            source, self.draft, self.workspace, verifier_context=False, observations=observations)
        self.rubric.write_text('changed after the shared observation\n')
        action, draft, _ = host.inline_external_reads(
            source, self.draft, self.workspace, observations=observations)
        self.assertEqual(identity, action)
        self.assertEqual(identity_draft, self.draft)
        self.assertIn('BEGIN EVIDENCE\n' + data.decode() + '\nEND EVIDENCE', draft['prompt'])
        self.assertIn('SHA-256: ' + hashlib.sha256(data).hexdigest(), draft['prompt'])
        self.assertNotIn(self.rubric.read_text(), draft['prompt'])
        self.assertEqual(action['verifier_commands'], commands)

    def test_binary_verifier_pin_keeps_its_dependency_fallback(self):
        commands = [{'pinned_files': [{'path': str(self.binary), 'scope': 'verifier_only',
                                      'sha256': host.sha(self.binary)}]}]
        source = self.action(['pkg/module.py', str(self.binary)], verifier_commands=commands)
        action, draft, repairs = host.inline_external_reads(source, self.draft, self.workspace)
        self.assertEqual(action, {**source, 'read_scopes': ['pkg/module.py']})
        self.assertEqual(draft, self.draft)
        self.assertEqual(repairs, [{'path': 'action.read_scopes',
            'rule': 'external_read_to_verifier_dependency', 'count': 1}])

    def test_large_exact_verifier_dependency_is_not_worker_context(self):
        self.rubric.write_text('x' * 10000)
        commands = [{'pinned_files': [{'path': str(self.rubric), 'scope': 'verifier_only',
                                      'sha256': host.sha(self.rubric)}]}]
        source = self.action(['pkg/module.py', str(self.rubric)], verifier_commands=commands)
        action, draft, repairs = host.inline_external_reads(source, self.draft, self.workspace, limit=100)
        self.assertEqual(action['read_scopes'], ['pkg/module.py'])
        self.assertEqual(draft, self.draft)
        self.assertEqual(action['verifier_commands'], commands)
        self.assertEqual(source['read_scopes'], ['pkg/module.py', str(self.rubric)])
        self.assertEqual(repairs[0]['rule'], 'external_read_to_verifier_dependency')

    def test_a_pin_without_a_digest_still_marks_its_external_read_a_verifier_dependency(self):
        # 2026-10-26 (R19 pair, Nokiy Efficient): a gate pinned validate.py and the manifest by path
        # only; the manifest was too large to inline, stayed in read_scopes and was refused.
        self.rubric.write_text('x' * 10000)
        commands = [{'argv': ['/usr/bin/python3', 'tools/validate.py', '--manifest', 'tools/manifest.json'],
                     'pinned_files': [{'path': str(self.rubric), 'scope': 'verifier_only'}]}]
        source = self.action(['pkg/module.py', str(self.rubric)], verifier_commands=commands)
        action, _, repairs = host.inline_external_reads(source, self.draft, self.workspace, limit=100)
        self.assertEqual(action['read_scopes'], ['pkg/module.py'])
        self.assertEqual(repairs[0]['rule'], 'external_read_to_verifier_dependency')
        wrong = [{'pinned_files': [{'path': str(self.rubric), 'scope': 'verifier_only', 'sha256': '0' * 64}]}]
        mismatched = self.action(['pkg/module.py', str(self.rubric)], verifier_commands=wrong)
        with self.assertRaises(host.ExternalEvidenceBudgetError):
            host.inline_external_reads(mismatched, self.draft, self.workspace, limit=100)
        self.assertIn(str(self.rubric), mismatched['read_scopes'])

    def test_a_relative_operand_under_the_command_cwd_is_its_dependency(self):
        self.rubric.write_text('x' * 10000)
        argv = ['/usr/bin/python3', 'validate.py', '--manifest', self.rubric.name]
        source = self.action(['pkg/module.py', str(self.rubric)],
                             verifier_commands=[{'argv': argv, 'cwd': str(self.frozen), 'pinned_files': []}])
        action, _, repairs = host.inline_external_reads(source, self.draft, self.workspace, limit=100)
        self.assertEqual(action['read_scopes'], ['pkg/module.py'])
        self.assertEqual(repairs[0]['rule'], 'external_read_to_verifier_dependency')

    def test_a_verifier_operand_too_large_or_binary_to_inline_is_its_dependency(self):
        # V430 (2026-10-25): a 216 KiB manifest the validator reads stayed in read_scopes, where a
        # path outside the workspace refused the whole preparation.
        self.rubric.write_text('x' * 10000)
        validator = self.frozen / 'validate.py'
        validator.write_text('pass\n')
        argv = ['/usr/bin/python3', str(validator), '--manifest', str(self.rubric), '--blob', str(self.binary)]
        own = {'path': str(validator), 'scope': 'verifier_only', 'sha256': host.sha(validator)}
        source = self.action(['pkg/module.py', str(self.rubric), str(self.binary)],
                             verifier_commands=[{'argv': argv, 'pinned_files': [own]}])
        action, draft, repairs = host.inline_external_reads(source, self.draft, self.workspace, limit=100)
        self.assertEqual(action['read_scopes'], ['pkg/module.py'])
        self.assertEqual(draft, self.draft)
        self.assertEqual(action['verifier_commands'][0]['pinned_files'], [own] + [
            {'path': str(path), 'sha256': host.sha(path), 'scope': 'verifier_only'}
            for path in (self.rubric, self.binary)])
        self.assertEqual(repairs, [{'path': 'action.read_scopes', 'rule': 'external_read_to_verifier_dependency',
                                    'count': 2}])
        self.assertEqual(source['verifier_commands'][0]['pinned_files'], [own], 'input action is not mutated')
        # A command without a pin list still reads it: verifiers read globally; only the read moves.
        unpinned = self.action(['pkg/module.py', str(self.rubric)], verifier_commands=[{'argv': argv}])
        action, _, _ = host.inline_external_reads(unpinned, self.draft, self.workspace, limit=100)
        self.assertEqual(action['read_scopes'], ['pkg/module.py'])
        self.assertEqual(action['verifier_commands'], [{'argv': argv}])
        # A large file no verifier names receives the native budget refusal.
        plain = self.action(['pkg/module.py', str(self.rubric)])
        with self.assertRaises(host.ExternalEvidenceBudgetError):
            host.inline_external_reads(plain, self.draft, self.workspace, limit=100)
        self.assertIn(str(self.rubric), plain['read_scopes'])

    def test_unmatched_or_conflicting_verifier_pins_do_not_remove_read(self):
        for scope, digests in [('source', [host.sha(self.rubric)]),
                               ('verifier_only', ['0' * 64]),
                               ('verifier_only', [host.sha(self.rubric), '0' * 64])]:
            with self.subTest(scope=scope, digests=digests):
                commands = [{'pinned_files': [{'path': str(self.rubric), 'scope': scope, 'sha256': d}
                                             for d in digests]}]
                source = self.action([str(self.rubric)], verifier_commands=commands)
                with self.assertRaises(host.ExternalEvidenceBudgetError):
                    host.inline_external_reads(source, self.draft, self.workspace, limit=1)
                self.assertEqual(source['read_scopes'], [str(self.rubric)])
                self.assertEqual(source['verifier_commands'], commands)

    def test_globs_directories_binary_relative_missing_and_workspace_paths_stay(self):
        reads = ['pkg/module.py', str(self.frozen), str(self.frozen) + '/*.json',
                 str(self.binary), str(self.workspace / 'pkg/module.py'), str(self.frozen / 'absent.txt')]
        action, draft, repairs = host.inline_external_reads(self.action(reads), self.draft, self.workspace)
        self.assertEqual(action['read_scopes'], reads)
        self.assertEqual(draft['prompt'], self.draft['prompt'])
        self.assertEqual(repairs, [])

    def test_a_symlinked_spelling_is_inlined_under_its_canonical_file(self):
        # Same rule as draft paths: one file, one spelling (canonical_spelling), normalized not refused.
        action, draft, repairs = host.inline_external_reads(
            self.action(['pkg/module.py', str(self.link)]), self.draft, self.workspace)
        self.assertEqual(action['read_scopes'], ['pkg/module.py'])
        self.assertIn(str(self.rubric) + '\nSHA-256: ', draft['prompt'])
        self.assertNotIn(str(self.link), draft['prompt'])
        self.assertEqual(repairs[0]['count'], 1)

    def test_oversized_external_json_is_typed_before_host_preparation(self):
        report = self.frozen / 'report.json'
        prefix, suffix = b'{"report":"', b'"}\n'
        report.write_bytes(prefix + b'x' * (147028 - len(prefix) - len(suffix)) + suffix)
        self.assertEqual(len(report.read_bytes()), 147028)
        self.assertIsInstance(json.loads(report.read_text(encoding='utf-8')), dict)
        self.assertEqual(report.resolve(strict=True), report)
        artifact = self.workspace.parent / 'artifacts'
        artifact.mkdir()
        source = self.action(['pkg/module.py', str(self.rubric), str(self.tests), str(report)],
                             forbidden_effects=['deployment', 'broker', 'order'])
        payload = {'operation': 'run', 'action': source,
                   'draft': {**self.draft, 'artifact_root': str(artifact), 'authority_effect': 'workspace'}}
        draft, _, _ = host.validate_action(payload)
        envelope = {'schema_version': 'nokiy_native_tool_v2', 'worker': 'fixture',
                    'reasoning_effort': 'high', 'service_tier': 'priority', 'payload': payload}
        before = host.canonical(envelope)
        # This sentinel entry is never opened: the real host must refuse before caller preparation.
        with patch.object(host.subprocess, 'run') as caller, patch.object(host.os, 'execv') as execute:
            with self.assertRaises(host.ExternalEvidenceBudgetError) as caught:
                host.prepare_action(envelope, SRC / 'unused-caller', 'fixture-thread', 'fixture',
                                    'high', 'priority', payload, draft)
            caller.assert_not_called()
            execute.assert_not_called()
        receipt = caught.exception.receipt()
        self.assertEqual(receipt['code'], 'NOKIY_NATIVE_INPUT_INVALID')
        self.assertEqual(receipt['status'], 'BLOCKED_BEFORE_EXECUTION')
        self.assertEqual(receipt['phase'], 'validate')
        self.assertIs(receipt['provider_execution_started'], False)
        self.assertIs(receipt['safe_to_correct_input'], True)
        self.assertEqual(receipt['issues'], [{'path': 'action.read_scopes[3]',
                         'reason': 'external_evidence_prompt_budget_exceeded'}])
        _, small_draft, _ = host.inline_external_reads(
            self.action(source['read_scopes'][:-1]), draft, self.workspace)
        block = ('\n\n%s\nSHA-256: %s\nBEGIN EVIDENCE\n%s\nEND EVIDENCE'
                 % (report, host.sha(report), report.read_text(encoding='utf-8')))
        used, size = len(small_draft['prompt'].encode('utf-8')), len(block.encode('utf-8'))
        self.assertEqual(receipt['external_evidence_budget'], {
            'read_scope_index': 3, 'file_bytes': 147028, 'evidence_block_bytes': size,
            'prompt_bytes_before_evidence': used, 'required_prompt_bytes': used + size,
            'prompt_limit_bytes': 65536, 'available_evidence_bytes': max(0, 65536 - used)})
        self.assertIn('action.read_scopes', receipt['continuation'])
        self.assertNotIn(str(report), host.canonical(receipt))
        self.assertEqual(host.canonical(envelope), before)
        self.assertEqual(list(artifact.iterdir()), [], 'no preparation or recovery identity is published')

    def test_cumulative_evidence_budget_refuses_instead_of_partially_inlining(self):
        self.rubric.write_bytes('{"label":"\u96ea"}\r\n'.encode('utf-8'))
        source = self.action(['pkg/module.py', str(self.rubric), str(self.tests)])
        _, exact, _ = host.inline_external_reads(source, self.draft, self.workspace)
        limit = len(exact['prompt'].encode('utf-8'))
        self.assertEqual(host.inline_external_reads(source, self.draft, self.workspace, limit=limit)[1], exact)
        with self.assertRaises(host.ExternalEvidenceBudgetError) as caught:
            host.inline_external_reads(source, self.draft, self.workspace, limit=limit - 1)
        receipt = caught.exception.receipt()
        self.assertEqual(receipt['issues'], [{'path': 'action.read_scopes[2]',
                         'reason': 'external_evidence_prompt_budget_exceeded'}])
        budget = receipt['external_evidence_budget']
        self.assertEqual(budget['file_bytes'], len(self.tests.read_bytes()))
        self.assertLess(budget['file_bytes'], budget['prompt_limit_bytes'])
        self.assertEqual(budget['required_prompt_bytes'], limit)
        self.assertEqual(budget['prompt_limit_bytes'], limit - 1)
        self.assertEqual(budget['evidence_block_bytes'] - budget['available_evidence_bytes'], 1)
        self.assertEqual(source['read_scopes'], ['pkg/module.py', str(self.rubric), str(self.tests)])
        self.assertEqual(self.draft['prompt'], 'Implement pkg/module.py.')

    def test_when_every_read_was_external_the_reader_grants_are_dropped(self):
        action, draft, repairs = host.inline_external_reads(
            self.action([str(self.rubric)]), self.draft, self.workspace)
        self.assertEqual(action['read_scopes'], [])
        self.assertNotIn('source_read', action)
        self.assertEqual(action['operations'], ['modify'])
        kept, _, _ = host.inline_external_reads(
            self.action([str(self.rubric)], command_templates=[{'argv': ['pwd'], 'effects': ['read'], 'targets': []}]),
            self.draft, self.workspace)
        self.assertEqual(kept['operations'], ['modify', 'command'])

    def test_a_read_only_member_with_only_external_reads_keeps_the_typed_refusal(self):
        action = {'operations': ['read', 'command'], 'source_read': True, 'read_scopes': [str(self.rubric)]}
        same, draft, repairs = host.inline_external_reads(action, self.draft, self.workspace)
        self.assertIs(same, action)
        self.assertEqual(draft['prompt'], self.draft['prompt'])
        self.assertEqual(repairs, [])


if __name__ == '__main__':
    unittest.main()

