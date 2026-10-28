# SPDX-License-Identifier: MIT
"""Deterministic concurrent preflight and byte-identical bounded summaries."""
from copy import deepcopy
import json
from pathlib import Path
import random
import threading
import time
import unittest
from unittest.mock import patch

from codex_collaboration_harness import batch
from codex_collaboration_harness.embedded_nokiy import EmbeddedNokiyError
import test_nokiy_batch as fixtures
from test_batch_verifier_delivery import verified_evidence
from test_summary_compaction import fixture as result_fixture


def reference_fit(summary):
    """Original v173 truncation algorithm, retained only as differential oracle."""
    def encoded(value):
        return json.dumps(value, ensure_ascii=True, sort_keys=True).encode('ascii')
    rows = summary['members']
    for row in rows:
        row['result_truncated'] = bool(row['result_truncated'])
    if len(encoded(summary)) > 8000:
        compact = batch._compact_summary(summary)
        if len(encoded(compact)) < len(encoded(summary)):
            summary, rows = compact, compact['members']
    while len(encoded(summary)) > 8000 and any(row['result_text'] for row in rows):
        largest = max(rows, key=lambda row: len(encoded(row['result_text'])) if row['result_text'] else 0)
        text = largest['result_text']
        largest['result_text'] = text[:max(0, len(text) // 2)]
        largest['result_truncated'] = True
    if len(encoded(summary)) > 8000:
        return {'schema_version': 'nokiy_direct_batch_summary_v1', 'status': 'COMPACT_METADATA_TOO_LARGE',
            'first_typed_blocker': 'NOKIY_BATCH_SUMMARY_TOO_LARGE',
            'recovery': 'Use the printed original plan path and SHA with read-batch; use read-result for original terminals.'}
    return summary


def summary_fixture(texts, *, metadata_size=0):
    rows = []
    for index, text in enumerate(texts):
        row = {key: None for key in batch._SHARED_SUMMARY_FIELDS}
        row.update(request_id='tura_embedded_' + f'{index:064x}', artifact_root='/fixture',
            terminal_path=f'/fixture/tura_embedded_{index:064x}/terminal.json',
            result_text=text, result_truncated=False, usage={'coverage': {'complete': True}},
            evidence_status='EVIDENCE_VERIFIED', metadata='x' * metadata_size)
        rows.append(row)
    return {'schema_version': 'nokiy_direct_batch_summary_v1', 'status': 'RESULT_AVAILABLE',
            'first_typed_blocker': None, 'members': rows, 'continuation_owner': 'codex',
            'mission_acceptance': 'parent_owned'}


class SummaryDifferentialTests(unittest.TestCase):
    def test_randomized_unicode_ties_metadata_and_empty_results(self):
        rng = random.Random(737)
        alphabet = ['A', '\n', '"', '\\', '中', '🚀', '\t', '\u0001']
        for case in range(240):
            texts = [''.join(rng.choices(alphabet, k=rng.randrange(0, 5000)))
                     if rng.randrange(5) else None for _ in range(rng.randrange(1, 7))]
            if case % 9 == 0:
                texts = ['中' * 2000] * len(texts)
            source = summary_fixture(texts, metadata_size=2000 if case % 13 == 0 else 0)
            source['members'][0]['result_truncated'] = bool(case % 2)
            with self.subTest(case=case):
                expected = reference_fit(deepcopy(source))
                actual = batch._fit_summary(deepcopy(source))
                self.assertEqual(actual, expected)
                self.assertEqual(json.dumps(actual, sort_keys=True), json.dumps(expected, sort_keys=True))
                if actual['status'] == 'RESULT_AVAILABLE':
                    self.assertLessEqual(len(json.dumps(actual, ensure_ascii=True, sort_keys=True).encode()), 8000)

    def test_full_summary_encoded_at_most_twice(self):
        source = summary_fixture(['🚀' * 30000] * 5)
        original = json.dumps
        full_encodes = []
        def counted(value, *args, **kwargs):
            if isinstance(value, dict) and 'members' in value:
                full_encodes.append(1)
            return original(value, *args, **kwargs)
        with patch.object(batch.json, 'dumps', side_effect=counted):
            result = batch._fit_summary(deepcopy(source))
        self.assertEqual(len(full_encodes), 2)
        self.assertEqual(result, reference_fit(deepcopy(source)))


class SummaryBindingTests(unittest.TestCase):
    def result(self):
        members = []
        for index in range(2):
            digest = f'{index + 1:064x}'
            terminal = {
                'request_id': 'tura_embedded_' + digest,
                'request_sha256': digest, 'native_thread_id': fixtures.THREAD,
                'status': 'RESULT_AVAILABLE', 'cleanup_pass': True,
                'result_text': f'answer {index}', 'result_truncated': False,
                'result_inspection': {'status': 'EVIDENCE_VERIFIED',
                                      'terminal_sha256': f'{index + 3:064x}'},
            }
            members.append({
                'request_id': terminal['request_id'], 'artifact_root': f'/fixture/{index}',
                'status': terminal['status'], 'cleanup_pass': True,
                'first_typed_blocker': None, 'terminal': terminal,
            })
        return {'status': 'RESULT_AVAILABLE', 'first_typed_blocker': None, 'members': members}

    def test_first_return_preserves_binding_without_reinspection_or_execution(self):
        result = self.result()
        before = deepcopy(result)
        with patch.object(batch, '_project', side_effect=AssertionError('second inspection')), \
                patch.object(batch.caller, 'execute', side_effect=AssertionError('execution')), \
                patch.object(Path, 'open', side_effect=AssertionError('artifact reread')):
            summary = batch.expand_summary(batch.summarize(result))
        for row, original in zip(summary['members'], result['members']):
            for key in ('request_id', 'request_sha256', 'native_thread_id', 'result_text'):
                self.assertEqual(row[key], original['terminal'][key])
            self.assertEqual(row['terminal_sha256'], original['terminal']['result_inspection']['terminal_sha256'])
        self.assertEqual(summary['mission_acceptance'], 'parent_owned')
        self.assertEqual(result, before)

    def test_compaction_keeps_identity_per_member_without_rebinding(self):
        for same_thread in (True, False):
            with self.subTest(same_thread=same_thread):
                result = self.result()
                if not same_thread:
                    result['members'][1]['terminal']['native_thread_id'] = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
                # Projection must preserve the inspected value, not derive it from request_id.
                result['members'][1]['terminal']['request_sha256'] = 'f' * 64
                summary = batch.summarize(result)
                compact = batch._compact_summary(summary)
                for key in ('request_sha256', 'native_thread_id'):
                    self.assertNotIn(key, compact['member_defaults'])
                    self.assertEqual([row[key] for row in compact['members']],
                                     [row['terminal'][key] for row in result['members']])
                self.assertEqual(batch.expand_summary(compact), summary)

    def test_truncation_preserves_binding_and_evidence(self):
        result = self.result()
        for row in result['members']:
            row['terminal']['result_text'] = 'x' * 80000
        compact = batch.summarize(result)
        self.assertLessEqual(len(json.dumps(compact, ensure_ascii=True, sort_keys=True).encode()),
                             batch.MAX_PROJECTED_BYTES)
        summary = batch.expand_summary(compact)
        for row, original in zip(summary['members'], result['members']):
            self.assertTrue(row['result_truncated'])
            self.assertEqual(row['native_thread_id'], original['terminal']['native_thread_id'])
            self.assertEqual(row['request_sha256'], original['terminal']['request_sha256'])
            self.assertEqual(row['evidence_status'], 'EVIDENCE_VERIFIED')

    def test_unknown_binding_and_failure_are_not_inferred_from_peers(self):
        result = self.result()
        for terminal in ({'result_inspection': {'status': 'EVIDENCE_REJECTED'}}, None):
            with self.subTest(terminal=terminal):
                result['status'] = 'BLOCKED'
                result['first_typed_blocker'] = 'TEST_EVIDENCE_UNAVAILABLE'
                result['members'][1].update(terminal=terminal, status='BLOCKED',
                                           cleanup_pass=False, first_typed_blocker='TEST_EVIDENCE_UNAVAILABLE')
                summary = batch.expand_summary(batch._compact_summary(batch.summarize(result)))
                row = summary['members'][1]
                self.assertIsNone(row['native_thread_id'])
                self.assertIsNone(row['request_sha256'])
                self.assertIsNone(row['result_text'])
                self.assertNotEqual(row['evidence_status'], 'EVIDENCE_VERIFIED')
                self.assertFalse(row['cleanup_pass'])
                self.assertEqual(summary['status'], 'BLOCKED')
                self.assertEqual(row['first_typed_blocker'], result['first_typed_blocker'])


class OptionalVerifierContextBudgetTests(unittest.TestCase):
    def test_fit_drops_context_before_receipts_results_or_previews(self):
        source = summary_fixture(['unique result'])
        source['members'][0]['result_artifact_preview'] = {'text': 'preview'}
        source['members'][0]['verifier_delivery'] = batch._verifier_delivery(
            verified_evidence('def support():\n    return "' + 'x' * 12000 + '"\n'))
        expected = deepcopy(source)
        expected['members'][0]['verifier_delivery']['receipts'][0].pop('source_postimage_context', None)
        actual = batch._fit_summary(source)
        self.assertEqual(batch.expand_summary(actual), batch.expand_summary(batch._fit_summary(expected)))
        self.assertEqual(actual['members'][0]['result_text'], 'unique result')
        self.assertIn('verifier_delivery', actual['members'][0])
        self.assertIn('result_artifact_preview', actual['members'][0])

    def test_twenty_members_drop_context_before_unique_diffs_results_or_receipts(self):
        source = summary_fixture([f'unique result {index}' for index in range(20)])
        for index, row in enumerate(source['members']):
            row['changes'] = {'diff': f'unique diff {index}'}
            row['verifier_delivery'] = batch._verifier_delivery(verified_evidence(
                'def support():\n    return "' + str(index) + 'x' * 12000 + '"\n'))
        expected = deepcopy(source)
        for row in expected['members']:
            row['verifier_delivery']['receipts'][0].pop('source_postimage_context')
        actual = batch.expand_summary(batch._fit_summary(source, max_bytes=batch.MAX_BATCH_SUMMARY_BYTES))
        self.assertEqual(actual, batch.expand_summary(batch._fit_summary(
            expected, max_bytes=batch.MAX_BATCH_SUMMARY_BYTES)))
        for index, row in enumerate(actual['members']):
            self.assertEqual(row['changes']['diff'], f'unique diff {index}')
            self.assertEqual(row['result_text'], f'unique result {index}')
            self.assertIn('verifier_delivery', row)

    def test_mutation_context_drops_before_original_diff_allocation_or_receipts(self):
        for count in (2, 6):
            with self.subTest(count=count):
                self.check_mutation_context_budget(count)

    def check_mutation_context_budget(self, count):
        source = result_fixture(count)
        source.update(status='RESULT_AVAILABLE', first_typed_blocker=None)
        for index, member in enumerate(source['members']):
            member.update(status='RESULT_AVAILABLE', first_typed_blocker=None, cleanup_pass=True,
                          artifact_root=f'/fixture/{index}')
            member['terminal'] = {'result_text': 'bounded answer', 'result_inspection': verified_evidence(
                'def support():\n    return "' + str(index) + 'x' * 24000 + '"\n')}
            diff = f'--- a/file{index}.py\n+++ b/file{index}.py\n@@ -1 +1 @@\n-old\n+unique-{index}\n'
            member['changes'] = {'status': 'COMPLETE', 'diff': diff, 'diff_bytes': len(diff),
                                 'diff_path': f'/fixture/{index}/changes.diff',
                                 'files': [{'path': f'file{index}.py', 'added': 1, 'removed': 1}]}
        baseline = deepcopy(source)
        for member in baseline['members']:
            for context in member['terminal']['result_inspection']['parent_review_context']['verifier_contexts']:
                context['source_postimages'].pop('schema_version')
        budgets = []
        inline = batch._inline_diffs
        def record_inline(rows, budget):
            budgets.append(budget)
            inline(rows, budget)
        with patch.object(batch, '_diff_text', side_effect=lambda changes: changes['diff']), \
                patch.object(batch, '_inline_diffs', side_effect=record_inline), \
                patch.object(Path, 'open', side_effect=AssertionError('new I/O')):
            expected = batch.expand_summary(batch.summarize(baseline))
            actual = batch.expand_summary(batch.summarize(source))
        self.assertEqual(budgets[0], budgets[1])
        without_context = deepcopy(actual)
        for row in without_context['members']:
            row['verifier_delivery']['receipts'][0].pop('source_postimage_context', None)
        self.assertEqual(without_context, expected)
        for original, row in zip(source['members'], actual['members']):
            self.assertEqual(row['changes']['diff'], original['changes']['diff'])
            self.assertFalse(row['changes']['diff_inline_truncated'])
            self.assertEqual(row['result_text'], 'bounded answer')
            self.assertIn('verifier_delivery', row)
            self.assertEqual('source_postimage_context' in row['verifier_delivery']['receipts'][0], count == 2)
        self.assertLessEqual(len(json.dumps(actual, ensure_ascii=True, sort_keys=True).encode()),
                             batch.MAX_BATCH_SUMMARY_BYTES)


class ParallelPreflightTests(unittest.TestCase):
    setUp = fixtures.BatchFixture.setUp
    write_scopes = fixtures.BatchFixture.write_scopes
    save_plan = fixtures.BatchFixture.save_plan
    mocks = fixtures.BatchFixture.mocks
    terminal = staticmethod(fixtures.BatchFixture.terminal)

    def test_preflight_overlaps_but_all_complete_before_execution(self):
        barrier = threading.Barrier(2, timeout=3)
        finished = set()
        lock = threading.Lock()
        def preflight(request):
            barrier.wait()
            with lock:
                finished.add(request.request_id)
            return {'status': 'READY'}
        def execute(request):
            self.assertEqual(finished, {r.request_id for r in self.requests})
            return self.terminal(request)
        pre, exe = self.mocks(preflight=preflight, execute=execute)
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result['status'], 'RESULT_AVAILABLE')
        self.assertEqual((pre.call_count, exe.call_count), (2, 2))

    def test_first_member_failure_wins_even_when_it_finishes_last(self):
        barrier = threading.Barrier(2, timeout=3)
        def preflight(request):
            barrier.wait()
            if request is self.requests[0]:
                time.sleep(0.02)
                raise EmbeddedNokiyError('FIRST', 'first ordered failure')
            raise EmbeddedNokiyError('SECOND', 'later failure')
        _, exe = self.mocks(preflight=preflight)
        with self.assertRaises(EmbeddedNokiyError) as error:
            batch.run_batch(self.plan, self.digest)
        self.assertEqual(error.exception.code, 'FIRST')
        exe.assert_not_called()

    def test_prior_preflight_failure_precedes_later_identity_error(self):
        self.members[1]['request_file_sha256'] = '0' * 64
        self.save_plan()
        def preflight(request):
            raise EmbeddedNokiyError('FIRST', 'original precedence')
        _, exe = self.mocks(preflight=preflight)
        with self.assertRaises(EmbeddedNokiyError) as error:
            batch.run_batch(self.plan, self.digest)
        self.assertEqual(error.exception.code, 'FIRST')
        exe.assert_not_called()

    def test_new_claim_during_preflight_remains_recovery_only(self):
        def preflight(request):
            if request is self.requests[0]:
                (request.artifact_root / request.request_id).mkdir()
            return {'status': 'READY'}
        _, exe = self.mocks(preflight=preflight)
        result = batch.run_batch(self.plan, self.digest)
        self.assertEqual(result['status'], 'BLOCKED')
        exe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
