"""No-model producer acceptance; bindings preserve inspection, not task approval."""
import copy
import hashlib
import unittest

from codex_collaboration_harness import batch
from test_summary_compaction import fixture


def complete():
    result = fixture(1)
    terminal = result['members'][0]['terminal']
    terminal.update(status='RESULT_AVAILABLE', cleanup_pass=True, runtime_exit_code=0,
                    fallback_used=False, mission_acceptance='parent_owned', continuation_owner='codex',
                    result_truncated=False)
    terminal['result_inspection'].update(artifact_integrity='verified', command_success='known_success')
    terminal['result_inspection']['counts']['failed'] = 0
    return result


class BatchDeliveryBindingTests(unittest.TestCase):
    def test_missing_verifier_proof_reaches_parent_without_becoming_success(self):
        result = complete()
        inspection = result['members'][0]['terminal']['result_inspection']
        inspection.update(status='INCOMPLETE_EVIDENCE', first_blocker='COMMAND_FAILURE',
                          command_success='known_failure')
        observation = {'status': 'UNKNOWN', 'verifiers': [{
            'failed_call_id': 'original-failed', 'passing_call_id': 'original-passing',
            'passing_event_index': 24, 'verifier_index': 0,
            'reason': 'VERIFICATION_EVIDENCE_MISSING'}]}
        inspection['commands'] = [{'event_index': 21,
                                   'verifier_recovery_observation': observation}]
        inspection['pagination'] = {'next_offset': 16}
        settlement = {'schema_version': 'nokiy_post_batch_verifier_settlement_v1',
                      'terminal_sha256': inspection['terminal_sha256'],
                      'commands': 17, 'failed': 1, 'policy_denials': 0, 'unsettled_commands': 0}
        inspection['post_batch_verifier_settlement'] = settlement
        before = copy.deepcopy(result)
        row = batch.expand_summary(batch.summarize(result))['members'][0]
        self.assertEqual(row['verifier_recovery_observations'], {
            'commands': [{'event_index': 21, 'observation': observation}],
            'pagination': {'next_offset': 16},
            'parent_acceptance_required': True, 'retry_authorized': False})
        self.assertEqual(row['evidence_status'], 'INCOMPLETE_EVIDENCE')
        self.assertEqual(row['post_batch_verifier_settlement'], settlement)
        self.assertNotIn('delivery_binding', row)
        self.assertEqual(result, before)

    def test_complete_inline_answer_has_content_and_terminal_binding(self):
        result = complete()
        before = copy.deepcopy(result)
        row = batch.expand_summary(batch.summarize(result))['members'][0]
        self.assertEqual(row['delivery_binding'], {
            'schema_version': 'nokiy_batch_delivery_binding_v1',
            'terminal_sha256': row['terminal_sha256'],
            'result_sha256': hashlib.sha256(row['result_text'].encode()).hexdigest(),
            'result_bytes': len(row['result_text'].encode()),
        })
        self.assertEqual(result, before)

    def test_uncertain_or_effectful_terminals_have_no_delivery_binding(self):
        mutations = [
            ('status', 'BLOCKED'), ('cleanup_pass', False), ('runtime_exit_code', 1),
            ('runtime_exit_code', False), ('fallback_used', True), ('capability_gap', {}),
            ('first_typed_blocker', 'blocked'), ('first_blocker', 'blocked'),
            ('mission_acceptance', 'worker'), ('continuation_owner', 'other'),
            ('result_truncated', True), ('result_text', '\ud800'),
            ('result_artifact', {'sha256': 'a' * 64, 'bytes': 14}),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value):
                result = complete()
                result['members'][0]['terminal'][key] = value
                self.assertNotIn('delivery_binding', batch.summarize(result)['members'][0])

    def test_incomplete_inspection_has_no_delivery_binding(self):
        for key, value in [('status', 'UNKNOWN'), ('first_blocker', 'blocked'),
                           ('projection_partial', True), ('artifact_integrity', 'unknown'),
                           ('command_success', 'unknown'), ('counts', []),
                           ('counts', {'file_changes': 1, 'failed': 0, 'index_complete': True}),
                           ('counts', {'file_changes': 0, 'failed': 1, 'index_complete': True}),
                           ('counts', {'file_changes': 0, 'failed': 0, 'index_complete': False})]:
            with self.subTest(key=key, value=value):
                result = complete()
                result['members'][0]['terminal']['result_inspection'][key] = value
                self.assertNotIn('delivery_binding', batch.summarize(result)['members'][0])

    def test_truncation_preserves_original_digest_but_does_not_claim_complete(self):
        result = complete()
        result['members'][0]['terminal']['result_text'] = 'evidence' * 100000
        row = batch.expand_summary(batch.summarize(result))['members'][0]
        self.assertTrue(row['result_truncated'])
        self.assertNotEqual(len(row['result_text'].encode()), row['delivery_binding']['result_bytes'])

    def test_authenticated_non_attempts_bind_without_erasing_failures(self):
        result = complete()
        inspection = result['members'][0]['terminal']['result_inspection']
        inspection['counts']['failed'] = 1
        inspection['command_success'] = 'known_failure'
        inspection['source_read_rejection_reconciliation'] = {
            'schema_version': 'nokiy_source_read_rejection_reconciliation_v1',
            'not_attempted_count': 1, 'unresolved_count': 0,
            'effective_command_success': 'known_success'}
        before = copy.deepcopy(result)
        row = batch.expand_summary(batch.summarize(result))['members'][0]
        self.assertEqual(row['delivery_binding']['schema_version'], 'nokiy_batch_delivery_binding_v2')
        self.assertEqual(row['delivery_binding']['not_attempted_count'], 1)
        self.assertEqual(row['evidence_counts']['failed'], 1)
        self.assertEqual(result, before)
        for key, value in [('not_attempted_count', True), ('not_attempted_count', 0),
                           ('not_attempted_count', 2), ('unresolved_count', 1),
                           ('unresolved_count', False), ('effective_command_success', 'unproven'),
                           ('schema_version', 'unknown')]:
            with self.subTest(key=key, value=value):
                changed = copy.deepcopy(result)
                changed['members'][0]['terminal']['result_inspection']['source_read_rejection_reconciliation'][key] = value
                self.assertNotIn('delivery_binding', batch.summarize(changed)['members'][0])


if __name__ == '__main__':
    unittest.main()
