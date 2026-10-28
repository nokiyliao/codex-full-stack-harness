"""Focused real-binary delivery coverage, not a navigation or auth acceptance suite."""
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_native_verifier_channel as native
import test_responses_evidence_handoff as handoff
import test_runtime_patch_catalog as catalog


class BoundCatalogTests(catalog.RuntimePatchCatalogTests):
    @classmethod
    def setUpClass(cls):
        if handoff.IMAGE_ENV not in os.environ:
            raise unittest.SkipTest(f"{handoff.IMAGE_ENV} names no runtime image")
        image = json.loads(Path(os.environ[handoff.IMAGE_ENV]).read_text())
        artifacts = image['artifacts'].values()
        selected = {}
        for suffix, key in (
            ('/command_run/schema.json', 'NOKIY_TEST_COMMAND_SCHEMA'),
            ('/commands/apply_patch/prompt.md', 'NOKIY_TEST_PATCH_PROMPT'),
        ):
            matches = [entry for entry in artifacts if entry['path'].endswith(suffix)]
            assert len(matches) == 1
            entry = matches[0]
            assert handoff.file_sha256(Path(entry['path'])) == entry['sha256']
            selected[key] = entry['path']
        with patch.dict(os.environ, selected):
            super().setUpClass()


class OrderedFinalDeliveryTests(unittest.TestCase):
    def _run(self, reply, *, failing=False):
        fixture = handoff.RuntimeEvidenceHandoffTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original_load = handoff.caller.load_request
        original_message = handoff._tool_message
        original_provider = handoff._loopback_provider
        text = reply if isinstance(reply, str) else json.dumps(reply, sort_keys=True, separators=(',', ':'))

        def load_request(path):
            value = json.loads(Path(path).read_text())
            value['terminal_delivery'] = 'assistant_reply'
            Path(path).write_text(json.dumps(value))
            return original_load(path)

        def tool_message(commands, index):
            commands = deepcopy(commands)
            for command in commands:
                if command['command_type'] == 'task_status':
                    status = json.loads(command['command_line'])
                    if status.get('status') == 'done':
                        status['final_reply'] = reply
                        command['command_line'] = json.dumps(status)
            return original_message(commands, index)

        def provider(messages, **kwargs):
            fallback = {'id': 'msg-explicit-reply-control', 'type': 'message',
                        'role': 'assistant', 'phase': 'final_answer', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
            return original_provider([*messages, fallback], **kwargs)

        with patch.object(handoff.caller, 'load_request', side_effect=load_request), \
                patch.object(handoff, '_tool_message', side_effect=tool_message), \
                patch.object(handoff, '_loopback_provider', side_effect=provider):
            result = fixture._execute(failing=failing)
        return fixture, result, text

    def _persisted_reply_count(self, request, text):
        root = request.artifact_root / request.request_id / 'execution-state/session_log'
        index = root / 'index.sqlite3'
        self.assertTrue(index.is_file() and not index.is_symlink())
        session = 'full-' + request.request_sha256
        with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT DISTINCT workspace_db_path FROM runtime_locations '
                              'WHERE session_id = ?', (session,)).fetchall()
        self.assertEqual(len(rows), 1)
        path = Path(rows[0][0])
        self.assertTrue(path.is_relative_to(root) and path.resolve(strict=True) == path)
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            records = db.execute('SELECT record_json FROM session_context_records '
                                 'WHERE session_id = ? ORDER BY sequence', (session,)).fetchall()
            states = db.execute('SELECT lease_active, terminal FROM runtimes '
                                'WHERE session_id = ?', (session,)).fetchall()
        self.assertTrue(states and all(row == (0, 1) for row in states))
        parsed = [json.loads(row[0]) for row in records]
        return sum(record.get('role') == 'assistant' and record.get('content') == text
                   for record in parsed)

    def test_explicit_ordered_reply_uses_selected_binary_and_persists_once(self):
        for reply in ('Predetermined repair checked; parent acceptance pending.',
                      {'result': 'predetermined repair checked', 'parent_acceptance': 'pending'}):
            with self.subTest(reply=reply):
                fixture, result, text = self._run(reply)
                request, terminal, events, inspected, count = result
                build = fixture.source_manifest['runtime_build_identity']
                self.assertTrue(build)
                self.assertEqual(build, fixture.installed.build_identity)
                self.assertEqual(count, 2 if build == 'nokiy-v237-recovery-failed-status' else 1)
                self.assertEqual(terminal['status'], 'RESULT_AVAILABLE', fixture.diagnostics)
                self.assertEqual(inspected['status'], 'EVIDENCE_VERIFIED')
                self.assertEqual(terminal['mission_acceptance'], 'parent_owned')
                replies = [event['item'].get('text') for event in events
                           if event.get('item', {}).get('type') == 'assistant_message']
                self.assertEqual(replies.count(text), 1)
                self.assertEqual(self._persisted_reply_count(request, text), 1)

    def test_failed_verifier_never_delivers_the_pre_authored_success(self):
        fixture, result, text = self._run('Do not publish this failed repair.', failing=True)
        request, _, events, inspected, count = result
        self.assertEqual(count, 2, fixture.diagnostics)
        self.assertEqual(inspected['command_success'], 'known_failure')
        fixture._verifier_receipt(request, events, 1)
        self.assertFalse(any(event.get('item', {}).get('type') == 'assistant_message'
                             and event['item'].get('text') == text for event in events))
        self.assertEqual(self._persisted_reply_count(request, text), 0)


def load_tests(loader, _suite, _pattern):
    suite = unittest.TestSuite()
    for case in (BoundCatalogTests, OrderedFinalDeliveryTests,
                 handoff.OfflineIdentityTests, native.NativeVerifierChannelTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    for name in (
        'test_ordered_patch_verifier_done_needs_one_provider_response',
        'test_terminal_pressure_needs_one_provider_response',
        'test_failed_terminal_batch_repairs_and_finishes_in_same_session',
        'test_patch_executes_before_provider_response_completed',
        'test_separate_done_control_needs_one_extra_provider_response',
        'test_failed_verifier_fences_done_and_successful_terminal_evidence',
    ):
        suite.addTest(handoff.RuntimeEvidenceHandoffTests(name))
    return suite
