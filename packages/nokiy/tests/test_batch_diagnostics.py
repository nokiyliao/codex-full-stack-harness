"""Batch admission exposes bounded remediation, never arbitrary exception text."""
import unittest
from codex_collaboration_harness import embedded_nokiy as caller


class BatchDiagnosticsTests(unittest.TestCase):
    def test_scope_conflict_has_actionable_non_authorizing_reason(self):
        result = caller._failure(caller.EmbeddedNokiyError(
            'NOKIY_BATCH_INVALID', 'cross-member read/write or write/write overlap'))
        self.assertEqual(result['batch_recovery']['reason'], 'cross_member_scope_overlap')
        self.assertFalse(result['batch_recovery']['automatic_retry'])
        self.assertEqual(result['authority_effect'], 'none')
        self.assertEqual(result['status'], 'BLOCKED_PREEXECUTION')

    def test_unknown_details_are_not_exposed(self):
        for code in ('NOKIY_BATCH_INVALID', 'OTHER'):
            result = caller._failure(caller.EmbeddedNokiyError(code, '/private/secret'))
            self.assertNotIn('batch_recovery', result)
            self.assertNotIn('/private/secret', str(result))
