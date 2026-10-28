"""Mock-only exact-URL HTTP evidence and no-replay recovery tests."""

from contextlib import ExitStack
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error


SOURCE = Path(__file__).resolve().parents[1] / 'src' / 'nokiy-http-evidence.py'
SPEC = importlib.util.spec_from_file_location('nokiy_http_evidence', SOURCE)
evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(evidence)


class Response(io.BytesIO):
    def __init__(self, body=b'complete evidence\n', status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = {} if headers is None else headers

    def getcode(self):
        return self.status


class HttpEvidenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='nokiy-http-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.thread = '01234567-89ab-cdef-0123-456789abcdef'
        self.opener = mock.Mock()
        self.opener.open.side_effect = AssertionError('Unexpected network request')
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.build_opener = stack.enter_context(mock.patch.object(
            evidence.urllib.request, 'build_opener', return_value=self.opener))
        self.proxies = stack.enter_context(mock.patch.object(
            evidence.urllib.request, 'getproxies', return_value={}))

    def payload(self, **changes):
        return {
            'operation': 'read_http', 'artifact_root': str(self.root),
            'urls': ['https://example.invalid/exact?q=1'], 'observation_id': 'observation-1',
            **changes,
        }

    def snapshot(self, payload=None, thread=None):
        payload = self.payload() if payload is None else payload
        thread = self.thread if thread is None else thread
        digest = hashlib.sha256(evidence.canonical({'thread': thread, 'payload': payload}).encode()).hexdigest()
        return self.root / ('native-http-' + digest)

    def read(self, payload=None):
        return evidence.read_http_evidence(self.payload() if payload is None else payload, self.thread)

    def test_get_is_exact_bounded_and_has_no_caller_headers(self):
        body = 'A complete UTF-8 document: \u96ea\n'.encode()
        self.opener.open.side_effect = None
        self.opener.open.return_value = Response(body, headers={'Content-Length': str(len(body))})
        result = self.read()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.payload()['urls'][0])
        self.assertEqual(request.get_method(), 'GET')
        self.assertIsNone(request.data)
        self.assertEqual(request.header_items(), [])
        self.assertEqual(self.opener.open.call_args.kwargs, {'timeout': 30})
        self.assertEqual(result['status'], 'EVIDENCE_AVAILABLE')
        self.assertIs(result['cached'], False)
        self.assertEqual(Path(result['snapshot_dir']), self.snapshot())
        item = result['observations'][0]
        self.assertEqual(Path(item['body_path']).read_bytes(), body)
        self.assertEqual(item['body_sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(item['body_bytes'], len(body))
        self.assertIs(item['complete'], True)
        self.assertIs(item['utf8'], True)
        self.assertEqual(result['source_context'], {
            'workspace': result['snapshot_dir'], 'read_scopes': ['body-01.txt'],
            'source_read': True, 'command_templates': [], 'operations': ['read', 'command'],
        })
        self.assertNotIn(body.decode(), json.dumps(result))
        receipt = json.loads(Path(result['receipt_path']).read_text())
        self.assertEqual(receipt, {key: value for key, value in result.items() if key != 'cached'})

    def test_payload_and_thread_validation_precedes_files_and_network(self):
        invalid = [
            {'headers': {'Authorization': 'secret'}}, {'auth': 'secret'}, {'cookies': 'secret'},
            {'operation': 'post'}, {'urls': []}, {'urls': ['https://a.invalid'] * 2},
            {'urls': [f'https://{index}.invalid' for index in range(5)]},
            {'urls': 'https://example.invalid'}, {'urls': [1]},
            {'observation_id': '../escape'}, {'observation_id': ''}, {'observation_id': 'a' * 129},
            {'artifact_root': 'relative'}, {'artifact_root': str(self.root) + '/..'},
        ]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(self.payload(**changes))
        for thread in ('', 'not-a-uuid', 'a' * 36, None):
            with self.subTest(thread=thread), self.assertRaises(ValueError):
                evidence.read_http_evidence(self.payload(), thread)
        self.assertEqual(list(self.root.iterdir()), [])
        self.build_opener.assert_not_called()

    def test_forbidden_urls_are_rejected_without_network(self):
        for url in (
                'http://example.invalid/', 'https://user@example.invalid/',
                'https://user:secret@example.invalid/', 'https://example.invalid/#fragment',
                'https://example.invalid/#', 'https://*.invalid/', 'https://example.invalid/*',
                'https://example.invalid/\nsecret', 'https://example.invalid/\x00secret',
                'https://example.invalid/\x7fsecret', ' https://example.invalid/',
                'https://example.invalid/with space', 'https://example.invalid:99999/',
                'https:///missing-host', 'file:///tmp/file', 'https://example.invalid\\other'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.read(self.payload(urls=[url]))
        self.assertEqual(list(self.root.iterdir()), [])
        self.build_opener.assert_not_called()

    def test_every_standard_redirect_is_forbidden_by_the_actual_handler(self):
        handler = evidence._NoRedirect()
        request = evidence.urllib.request.Request('https://example.invalid/exact', method='GET')
        for code in (301, 302, 303, 307, 308):
            with self.subTest(code=code), self.assertRaises(urllib.error.HTTPError) as caught:
                handler.redirect_request(request, io.BytesIO(b'redirect'), code, 'redirect',
                                         {'Location': 'https://other.invalid/'}, 'https://other.invalid/')
            self.assertEqual(caught.exception.code, code)
            caught.exception.close()

    def test_redirect_and_http_error_bodies_are_partial_not_live_success(self):
        for status, issue in ((302, 'redirect_forbidden'), (404, 'http_error'), (500, 'http_error')):
            with self.subTest(status=status):
                self.opener.open.side_effect = urllib.error.HTTPError(
                    self.payload()['urls'][0], status, 'error', {}, io.BytesIO(b'error evidence'))
                result = self.read(self.payload(observation_id=f'http-{status}'))
                self.assertEqual(result['status'], 'EVIDENCE_PARTIAL')
                self.assertEqual(result['observations'][0]['http_status'], status)
                self.assertEqual(result['observations'][0]['issue'], issue)
                self.assertEqual(result['source_context']['read_scopes'], ['body-01.txt'])
                self.assertTrue(result['observations'][0]['complete'])

    def test_authenticated_environment_proxy_is_not_used(self):
        self.proxies.return_value = {'https': 'https://user:secret@proxy.invalid:443'}
        result = self.read()
        self.assertEqual(result['status'], 'EVIDENCE_PARTIAL')
        self.assertEqual(result['observations'][0]['issue'], 'authenticated_proxy_forbidden')
        self.assertEqual(result['source_context']['read_scopes'], [])
        self.build_opener.assert_not_called()
        self.assertNotIn('secret', json.dumps(result))

    def test_completed_cache_is_verified_and_never_retrieved_again(self):
        self.opener.open.side_effect = None
        self.opener.open.return_value = Response()
        first = self.read()
        self.opener.open.assert_called_once()
        self.opener.open.side_effect = AssertionError('Cached evidence must not fetch')
        self.build_opener.reset_mock()
        second = self.read()
        self.assertEqual(second, {**first, 'cached': True})
        self.opener.open.assert_not_called()
        self.build_opener.assert_not_called()

    def test_partial_cache_also_never_retries(self):
        self.opener.open.side_effect = urllib.error.URLError('secret transport detail')
        first = self.read()
        self.assertEqual(first['status'], 'EVIDENCE_PARTIAL')
        self.assertEqual(first['observations'][0]['issue'], 'transport_error')
        self.assertEqual(first['source_context']['read_scopes'], [])
        self.assertNotIn('secret', json.dumps(first))
        self.opener.open.side_effect = AssertionError('Do not retry partial observation')
        second = self.read()
        self.assertEqual(second, {**first, 'cached': True})
        self.opener.open.assert_called_once()

    def test_missing_receipt_existing_file_or_symlink_fails_closed(self):
        for kind in ('empty_directory', 'file', 'symlink'):
            with self.subTest(kind=kind):
                payload = self.payload(observation_id=kind)
                path = self.snapshot(payload)
                if kind == 'empty_directory':
                    path.mkdir()
                elif kind == 'file':
                    path.write_bytes(b'existing unrelated file')
                else:
                    path.symlink_to(self.root, target_is_directory=True)
                result = self.read(payload)
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertTrue(result['cached'])
                self.assertEqual(result['source_context']['read_scopes'], [])
        self.build_opener.assert_not_called()

    def test_artifact_root_and_parent_symlinks_are_rejected(self):
        directory = self.root / 'real'
        directory.mkdir()
        (directory / 'nested').mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(directory, target_is_directory=True)
        for root in (alias, alias / 'nested', self.root / 'missing'):
            with self.subTest(root=root), self.assertRaises(ValueError):
                self.read(self.payload(artifact_root=str(root)))
        self.build_opener.assert_not_called()
        self.assertEqual(list((directory / 'nested').iterdir()), [])

    def test_cached_tamper_invalid_receipt_or_extra_file_never_refetches(self):
        def tamper_body(result):
            Path(result['observations'][0]['body_path']).write_bytes(b'tampered')

        def tamper_identity(result):
            path = Path(result['receipt_path'])
            receipt = json.loads(path.read_text())
            receipt['native_thread_id'] = 'ffffffff-ffff-ffff-ffff-ffffffffffff'
            path.write_text(json.dumps(receipt))

        def tamper_payload(result):
            path = Path(result['receipt_path'])
            receipt = json.loads(path.read_text())
            receipt['payload']['urls'] = ['https://other.invalid/']
            path.write_text(json.dumps(receipt))

        def invalid_json(result):
            Path(result['receipt_path']).write_text('{')

        def extra_file(result):
            (Path(result['snapshot_dir']) / 'unexpected.txt').write_text('unexpected')

        for mutate in (tamper_body, tamper_identity, tamper_payload, invalid_json, extra_file):
            with self.subTest(mutate=mutate.__name__):
                payload = self.payload(observation_id=mutate.__name__)
                self.opener.open.side_effect = None
                self.opener.open.return_value = Response()
                first = self.read(payload)
                mutate(first)
                self.opener.open.reset_mock()
                self.opener.open.side_effect = AssertionError('Tampered cache must not refetch')
                result = self.read(payload)
                self.assertEqual(result['status'], 'UNKNOWN')
                self.assertTrue(result['cached'])
                self.assertEqual(result['source_context']['read_scopes'], [])
                self.opener.open.assert_not_called()

    def test_receipt_and_body_symlinks_and_hardlinks_fail_closed(self):
        for filename, kind in (('receipt.json', 'symlink'), ('body-01.txt', 'symlink'),
                               ('body-01.txt', 'hardlink')):
            with self.subTest(filename=filename, kind=kind):
                payload = self.payload(observation_id=filename + '-' + kind)
                self.opener.open.side_effect = None
                self.opener.open.return_value = Response()
                first = self.read(payload)
                path = Path(first['snapshot_dir']) / filename
                original = self.root / (filename + '-' + kind)
                path.rename(original)
                if kind == 'symlink':
                    path.symlink_to(original)
                else:
                    os.link(original, path)
                self.opener.open.reset_mock()
                self.opener.open.side_effect = AssertionError('Unsafe cache must not refetch')
                result = self.read(payload)
                self.assertEqual(result['status'], 'UNKNOWN')
                self.opener.open.assert_not_called()

    def test_non_utf8_and_split_utf8_sequences(self):
        class TinyResponse(Response):
            def read(self, count=-1):
                return super().read(min(count, 1))

        for body, status, issue in ((b'valid\xffinvalid', 'EVIDENCE_PARTIAL', 'non_utf8'),
                                    (b'truncated\xe9', 'EVIDENCE_PARTIAL', 'non_utf8'),
                                    ('\u96ea'.encode(), 'EVIDENCE_AVAILABLE', None)):
            with self.subTest(body=body):
                self.opener.open.side_effect = None
                self.opener.open.return_value = TinyResponse(body)
                result = self.read(self.payload(observation_id='utf8-' + body.hex()))
                self.assertEqual(result['status'], status)
                self.assertEqual(result['observations'][0]['issue'], issue)
                self.assertEqual(result['source_context']['read_scopes'],
                                 ['body-01.txt'] if issue is None else [])

    def test_oversize_is_truncated_to_one_mebibyte_and_not_readable_source(self):
        self.opener.open.side_effect = None
        self.opener.open.return_value = Response(b'x' * (evidence.MAX_BODY_BYTES + 1))
        first = self.read()
        item = first['observations'][0]
        self.assertEqual(first['status'], 'EVIDENCE_PARTIAL')
        self.assertEqual(item['issue'], 'oversize')
        self.assertFalse(item['complete'])
        self.assertEqual(item['body_bytes'], evidence.MAX_BODY_BYTES)
        self.assertEqual(Path(item['body_path']).stat().st_size, evidence.MAX_BODY_BYTES)
        self.assertEqual(first['source_context']['read_scopes'], [])
        self.opener.open.side_effect = AssertionError('Oversize cache must not refetch')
        self.assertEqual(self.read(), {**first, 'cached': True})

    def test_exact_limit_and_empty_success_bodies_are_complete(self):
        for body in (b'x' * evidence.MAX_BODY_BYTES, b''):
            with self.subTest(size=len(body)):
                self.opener.open.side_effect = None
                self.opener.open.return_value = Response(body)
                result = self.read(self.payload(observation_id='size-' + str(len(body))))
                self.assertEqual(result['status'], 'EVIDENCE_AVAILABLE')
                self.assertEqual(result['observations'][0]['body_bytes'], len(body))
                self.assertEqual(result['source_context']['read_scopes'], ['body-01.txt'])

    def test_incomplete_stream_and_content_length_mismatch_are_partial(self):
        class BrokenResponse(Response):
            def read(self, count=-1):
                if self.tell():
                    raise http.client.IncompleteRead(b'not stored')
                return super().read(3)

        for response, issue in (
                (BrokenResponse(b'abcdef'), 'transport_error'),
                (Response(b'abc', headers={'Content-Length': '6'}), 'content_length_mismatch'),
                (Response(b'abc', headers={'Content-Length': '-1'}), 'invalid_content_length')):
            with self.subTest(issue=issue):
                self.opener.open.side_effect = None
                self.opener.open.return_value = response
                result = self.read(self.payload(observation_id=issue))
                self.assertEqual(result['status'], 'EVIDENCE_PARTIAL')
                self.assertEqual(result['observations'][0]['issue'], issue)
                self.assertFalse(result['observations'][0]['complete'])
                self.assertEqual(result['source_context']['read_scopes'], [])

    def test_multiple_urls_have_only_actual_complete_utf8_scopes(self):
        urls = [f'https://{index}.invalid/exact' for index in range(4)]
        self.opener.open.side_effect = [
            Response(b'ok'), urllib.error.URLError('offline'), Response(b'\xff'), Response(b'error', 403),
        ]
        result = self.read(self.payload(urls=urls))
        self.assertEqual(result['status'], 'EVIDENCE_PARTIAL')
        self.assertEqual(result['source_context']['read_scopes'], ['body-01.txt', 'body-04.txt'])
        self.assertEqual([item['url'] for item in result['observations']], urls)
        self.assertEqual([call.args[0].full_url for call in self.opener.open.call_args_list], urls)

    def test_concurrent_existing_snapshot_is_unknown_without_network(self):
        path = self.snapshot()
        path.mkdir()
        with mock.patch.object(evidence.fcntl, 'flock', side_effect=BlockingIOError):
            result = self.read()
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['reason'], 'observation_in_progress')
        self.build_opener.assert_not_called()

    def test_reentrant_request_cannot_start_a_second_observation(self):
        nested = []

        def respond(request, timeout):
            nested.append(self.read())
            return Response()

        self.opener.open.side_effect = respond
        result = self.read()
        self.assertEqual(result['status'], 'EVIDENCE_AVAILABLE')
        self.assertEqual(len(nested), 1)
        self.assertEqual(nested[0]['status'], 'UNKNOWN')
        self.assertEqual(nested[0]['reason'], 'observation_in_progress')
        self.opener.open.assert_called_once()

    def test_fresh_observation_id_gets_a_distinct_snapshot(self):
        self.opener.open.side_effect = [Response(b'first'), Response(b'second')]
        first = self.read()
        second = self.read(self.payload(observation_id='observation-2'))
        self.assertNotEqual(first['snapshot_dir'], second['snapshot_dir'])
        self.assertEqual(self.opener.open.call_count, 2)

    def test_file_replacement_during_cached_read_is_not_admitted(self):
        self.opener.open.side_effect = None
        self.opener.open.return_value = Response()
        first = self.read()
        original = Path(first['observations'][0]['body_path'])
        fdopen = os.fdopen

        def replace_while_open(fd, *args, **kwargs):
            stream = fdopen(fd, *args, **kwargs)
            if os.fstat(fd).st_ino == original.stat().st_ino:
                destination = self.root / 'replaced.txt'
                original.rename(destination)
                original.symlink_to(destination)
            return stream

        self.opener.open.reset_mock()
        self.opener.open.side_effect = AssertionError('Replacement must not refetch')
        with mock.patch.object(evidence.os, 'fdopen', side_effect=replace_while_open):
            result = self.read()
        self.assertEqual(result['status'], 'UNKNOWN')
        self.assertEqual(result['source_context']['read_scopes'], [])
        self.opener.open.assert_not_called()

    def test_inconsistent_http_metadata_is_unknown_without_replay(self):
        self.opener.open.side_effect = None
        self.opener.open.return_value = Response(b'error', 404)
        first = self.read()
        path = Path(first['receipt_path'])
        receipt = json.loads(path.read_text())
        receipt['observations'][0]['http_status'] = 200
        path.write_text(json.dumps(receipt))
        self.opener.open.reset_mock()
        self.opener.open.side_effect = AssertionError('Inconsistent metadata must not refetch')
        self.assertEqual(self.read()['status'], 'UNKNOWN')
        self.opener.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
