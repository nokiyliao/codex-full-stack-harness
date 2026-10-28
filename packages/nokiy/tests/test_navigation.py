import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_collaboration_harness import navigation as nav
from codex_collaboration_harness import embedded_nokiy as caller


class NavigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / 'project'
        self.workspace.mkdir()
        self.cache = self.root / 'cache'
        (self.workspace / 'a.py').write_text('from b import beta\ndef alpha():\n    return beta()\n')
        (self.workspace / 'b.py').write_text('def beta():\n    return 1\n')

    def query(self, paths=None, **kw):
        return nav.navigate(self.workspace, paths or ['.'], cache_root=self.cache, **kw)

    def test_explicit_reference_query_finds_missing_import_without_scope_expansion(self):
        (self.workspace / 'a.py').write_text('def check():\n    import missing_fixture as fixture\n    return fixture.verify()\n')
        self.assertEqual(self.query(['a.py'], query='missing_fixture')['matched_symbols'], 0)
        result = self.query(['a.py'], query='ref:missing_fixture')
        self.assertEqual(result['matched_symbols'], 1)
        self.assertEqual(result['symbols'][0]['symbol'], 'a.check')
        self.assertIn('missing_fixture', result['symbols'][0]['references'])
        self.assertFalse(result['selection_grants_read_write'])
        with patch.object(nav.extractor, 'extract_python', side_effect=AssertionError('no reparse')):
            warm = self.query(['a.py'], query='ref:missing_fixture', projection='locations')
        self.assertEqual(warm['matched_symbols'], 1)
        self.assertNotIn('references', warm['symbols'][0])
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.query(query='ref:')

    def test_cold_warm_incremental_and_source_hashes(self):
        cold = self.query()
        with patch.object(nav.extractor, 'extract_python', wraps=nav.extractor.extract_python) as parse:
            warm = self.query()
            parse.assert_not_called()
            (self.workspace / 'b.py').write_text('def beta():\n    return 2\n')
            changed = self.query()
            self.assertEqual(parse.call_count, 1)
        self.assertEqual(cold['metrics']['file_cache_misses'], 2)
        self.assertEqual(warm['metrics']['file_cache_hits'], 2)
        self.assertEqual(cold['symbols'], warm['symbols'])
        self.assertEqual(cold['map_sha256'], warm['map_sha256'])
        self.assertNotEqual(warm['map_sha256'], changed['map_sha256'])
        self.assertEqual(changed['metrics']['cache_state'], 'incremental')
        for row in changed['symbols']:
            self.assertEqual(row['source_sha256'], hashlib.sha256((self.workspace / row['path']).read_bytes()).hexdigest())

    def test_unconfigured_project_expands_and_removes_corpus_edges(self):
        first = self.query(['a.py'])
        self.assertEqual(first['symbols'][0]['calls'], [])
        expanded = self.query()
        self.assertEqual(expanded['metrics']['file_cache_hits'], 1)
        self.assertEqual(expanded['symbols'][0]['calls'][0]['targets'], ['symbol:b.beta'])
        self.assertEqual(expanded['symbols'][0]['calls'][0]['confidence'], 'exact')
        (self.workspace / 'b.py').unlink()
        self.assertEqual(self.query()['symbols'][0]['calls'], [])

    def test_repeated_calls_preserve_relationships_and_multiplicity(self):
        (self.workspace / 'a.py').write_text(
            'from b import beta\ndef alpha():\n    beta()\n    return beta()\n')
        result = self.query()
        self.assertEqual(result['symbols'][0]['calls'], [
            {'name': 'beta', 'confidence': 'exact', 'targets': ['symbol:b.beta'],
             'occurrences': 2}])
        calls = [
            {'text': 'f', 'confidence': 'exact', 'candidate_ids': ['symbol:a.f']},
            {'text': 'f', 'confidence': 'weak', 'candidate_ids': ['symbol:a.f']},
            {'text': 'f', 'confidence': 'ambiguous', 'candidate_ids': ['symbol:a.f', 'symbol:b.f']},
            {'text': 'alias', 'confidence': 'exact', 'candidate_ids': ['symbol:a.f']},
            {'text': 'unresolved', 'confidence': 'unresolved', 'candidate_ids': []},
        ]
        self.assertEqual(len(nav.call_relations(calls)), 4)
        self.assertTrue(all('occurrences' not in row for row in nav.call_relations(calls)))

    def test_locations_skip_relation_resolution(self):
        with patch.object(nav.resolver, 'resolve_calls', side_effect=AssertionError('not needed')):
            self.assertEqual(self.query(projection='locations')['matched_symbols'], 2)

    def test_projection_revision_invalidates_cursor_without_reparsing(self):
        first = self.query()
        with patch.object(nav, 'PROJECTION_REVISION', nav.PROJECTION_REVISION + 1), \
                patch.object(nav.extractor, 'extract_python', wraps=nav.extractor.extract_python) as parse:
            changed = self.query()
            self.assertNotEqual(first['map_sha256'], changed['map_sha256'])
            self.assertEqual(first['symbols'], changed['symbols'])
            parse.assert_not_called()
            with self.assertRaises(caller.EmbeddedNokiyError):
                self.query(offset=1, expected_map_sha256=first['map_sha256'])

    def test_duplicate_relations_fit_one_page_without_losing_edges(self):
        source = ['from b import beta']
        for index in range(30):
            source.extend([f'def alpha_{index}():', *['    beta()'] * 12])
        (self.workspace / 'a.py').write_text('\n'.join(source) + '\n')
        result = self.query(queries=['alpha'])
        self.assertIsNone(result['next_offset'])
        self.assertEqual(result['matched_symbols'], 30)
        for symbol in result['symbols']:
            self.assertEqual(symbol['calls'], [
                {'name': 'beta', 'confidence': 'exact', 'targets': ['symbol:b.beta'],
                 'occurrences': 12}])

    def test_same_content_different_project_is_not_shared_authority(self):
        self.query()
        other = self.root / 'other'
        other.mkdir()
        (other / 'a.py').write_bytes((self.workspace / 'a.py').read_bytes())
        result = nav.navigate(other, ['a.py'], cache_root=self.cache)
        self.assertEqual(result['metrics']['file_cache_misses'], 1)
        self.assertEqual(result['workspace'], str(other))
        self.assertFalse(result['selection_grants_read_write'])
        self.assertFalse(result['provider_execution_started'])

    def test_invalid_scopes_and_links_are_not_followed(self):
        (self.workspace / 'link.py').symlink_to(self.workspace / 'a.py')
        for paths in (['../a.py'], ['.git'], ['/tmp'], ['a.py', 'a.py'], ['*.py'], ['link.py'], ['missing']):
            with self.subTest(paths=paths), self.assertRaises(caller.EmbeddedNokiyError):
                self.query(paths)
        self.assertEqual(self.query()['coverage']['indexed_files'], 2)

    def test_corrupt_cache_and_unavailable_cache_recover_without_claiming_hit(self):
        self.query()
        for p in self.cache.rglob('*.json'):
            p.write_text('[]')
        recovered = self.query()
        self.assertEqual(recovered['metrics']['file_cache_misses'], 2)
        result = nav.navigate(self.workspace, ['a.py'], cache_root=self.root / 'absent-parent' / 'cache')
        self.assertFalse(result['metrics']['cache_available'])
        self.assertFalse((self.root / 'absent-parent').exists())

    def test_parser_revision_invalidates_parse_cache(self):
        self.query()
        with patch.object(nav.extractor, '__file__', __file__):
            result = self.query()
        self.assertEqual(result['metrics']['file_cache_misses'], 2)

    def test_partial_scope_explicit_for_parse_errors_and_unsupported_languages(self):
        (self.workspace / 'broken.py').write_text('def broken(\n')
        (self.workspace / 'x.ts').write_text('export const x=1;')
        result = self.query()
        self.assertFalse(result['coverage']['complete_for_supported_scope'])
        self.assertEqual(result['coverage']['parse_failure_count'], 1)
        self.assertEqual(result['coverage']['unsupported_extensions'], {'.ts': 1})
        self.assertEqual(result['coverage']['indexed_files'], 2)

    def test_pagination_binds_source_and_query_and_never_hides_continuation(self):
        (self.workspace / 'many.py').write_text('\n'.join(f'def function_{i}(): return {i}' for i in range(150)))
        result = self.query(['many.py'])
        self.assertIsNotNone(result['next_offset'])
        second = self.query(['many.py'], offset=result['next_offset'], expected_map_sha256=result['map_sha256'])
        self.assertFalse({v['symbol'] for v in result['symbols']} & {v['symbol'] for v in second['symbols']})
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.query(['many.py'], offset=1)
        (self.workspace / 'many.py').write_text('def replaced(): pass')
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.query(['many.py'], offset=1, expected_map_sha256=result['map_sha256'])

    def test_multi_query_normalizes_literal_or_without_expanding_scope(self):
        result = self.query(queries=['B.PY', 'ALPHA', 'alpha'])
        reordered = self.query(query='', queries=['alpha', 'b.py'])
        self.assertEqual(result['symbols'], self.query()['symbols'])
        self.assertEqual(result['map_sha256'], reordered['map_sha256'])
        self.assertEqual(result['matched_symbols'], 2)
        scalar = self.query(query='ALPHA')
        single = self.query(queries=['alpha'])
        self.assertEqual(scalar['symbols'], single['symbols'])
        self.assertEqual(scalar['map_sha256'], single['map_sha256'])
        scoped = self.query(['a.py'], queries=['alpha', 'beta'])
        self.assertEqual([row['symbol'] for row in scoped['symbols']], ['a.alpha'])
        self.assertEqual(scoped['coverage']['indexed_files'], 1)
        self.assertEqual(self.query(['a.py'], queries=['beta'])['matched_symbols'], 0)
        self.assertEqual(self.query(queries=['a.*'])['matched_symbols'], 0)
        self.assertEqual(self.query(queries=[' '])['matched_symbols'], 2)

    def test_invalid_selectors_fail_before_filesystem_work(self):
        cases = [
            {'queries': []}, {'queries': 'alpha'}, {'queries': ('alpha',)},
            {'queries': {'alpha': True}}, {'queries': 1}, {'queries': ['alpha'] * 33},
            {'queries': ['']}, {'queries': [None]}, {'queries': [1]},
            {'queries': ['alpha', False]}, {'queries': ['é' * 129]},
            {'queries': ['\ud800']}, {'query': 'alpha', 'queries': ['beta']},
            {'query': ' ', 'queries': ['alpha']}, {'query': None},
            {'query': 'x' * 513}, {'projection': 'summary'},
            {'projection': None}, {'projection': []},
        ]
        with patch.object(caller, '_plain_path') as workspace_check, patch.object(nav, 'inventory') as discover:
            for fields in cases:
                with self.subTest(fields=fields), self.assertRaises(caller.EmbeddedNokiyError) as error:
                    self.query(**fields)
                self.assertEqual(error.exception.code, 'NOKIY_NAVIGATION_INVALID')
            workspace_check.assert_not_called()
            discover.assert_not_called()

    def test_query_utf8_boundaries_and_legacy_scalar_limit(self):
        self.assertEqual(self.query(queries=['é' * 128])['matched_symbols'], 0)
        self.assertEqual(self.query(queries=['x' * 256])['matched_symbols'], 0)
        self.assertEqual(self.query(queries=['alpha'] * 32)['matched_symbols'], 1)
        self.assertEqual(self.query(query='é' * 512)['matched_symbols'], 0)
        (self.workspace / 'unicode.py').write_text('def naïve():\n    return 1\n', encoding='utf-8')
        scalar = self.query(['unicode.py'], query='NAÏVE')
        batched = self.query(['unicode.py'], queries=['naïve', 'NAÏVE'])
        self.assertEqual(scalar['symbols'], batched['symbols'])
        self.assertEqual(batched['matched_symbols'], 1)
        self.assertEqual(scalar['map_sha256'], batched['map_sha256'])

    def test_locations_preserve_exact_positions_coverage_and_provenance(self):
        (self.workspace / 'broken.py').write_text('def broken(\n')
        (self.workspace / 'x.ts').write_text('export const x=1;')
        full = self.query()
        locations = self.query(projection='locations')
        keys = {'path', 'line', 'end_line', 'kind', 'symbol', 'source_sha256'}
        self.assertEqual(locations['symbols'], [{key: row[key] for key in keys} for row in full['symbols']])
        self.assertEqual([(row['path'], row['symbol'], row['line'], row['end_line'])
                          for row in locations['symbols']],
                         [('a.py', 'a.alpha', 2, 3), ('b.py', 'b.beta', 1, 2)])
        for row in locations['symbols']:
            self.assertEqual(set(row), keys)
            self.assertEqual(row['source_sha256'], hashlib.sha256((self.workspace / row['path']).read_bytes()).hexdigest())
        self.assertEqual(set(full), set(locations))
        for key in full.keys() - {'symbols', 'map_sha256', 'metrics'}:
            self.assertEqual(full[key], locations[key], key)
        self.assertNotEqual(full['map_sha256'], locations['map_sha256'])
        self.assertFalse(locations['coverage']['complete_for_supported_scope'])
        self.assertLess(len(nav.encoded(locations['symbols'])), len(nav.encoded(full['symbols'])))

    def test_three_unrelated_late_symbols_fit_one_bounded_response(self):
        (self.workspace / 'many.py').write_text('\n'.join(f'def filler_{i}(): return {i}' for i in range(250)))
        names = ['orchid', 'quartz', 'zenith']
        for name in names:
            (self.workspace / f'z_{name}.py').write_text(f'# late fixture\ndef {name}():\n    return 1\n')
        first = self.query()
        self.assertIsNotNone(first['next_offset'])
        expected = [f'z_{name}.{name}' for name in names]
        self.assertFalse(set(expected) & {row['symbol'] for row in first['symbols']})
        locations = self.query(queries=names, projection='locations')
        full = self.query(queries=names)
        self.assertEqual([row['symbol'] for row in locations['symbols']], expected)
        self.assertEqual(locations['matched_symbols'], 3)
        self.assertIsNone(locations['next_offset'])
        self.assertIsNone(full['next_offset'])
        self.assertLessEqual(len(nav.encoded(locations)), 24000)
        self.assertEqual([(row['path'], row['line'], row['end_line'], row['source_sha256'])
                          for row in locations['symbols']],
                         [(row['path'], row['line'], row['end_line'], row['source_sha256'])
                          for row in full['symbols']])
        self.assertEqual([(row['line'], row['end_line']) for row in locations['symbols']], [(2, 3)] * 3)

    def test_locations_cursor_binds_normalized_selection_projection_and_source(self):
        source = self.workspace / 'many.py'
        source.write_text('\n'.join(f'def {"alpha" if i % 2 else "beta"}_{i}(): return {i}' for i in range(220)))
        selection = ['alpha_', 'beta_']
        first = self.query(['many.py'], queries=selection, projection='locations')
        self.assertIsNotNone(first['next_offset'])
        seen = list(first['symbols'])
        page = first
        while page['next_offset'] is not None:
            page = self.query(['many.py'], queries=['BETA_', 'alpha_', 'ALPHA_'], projection='locations',
                              offset=page['next_offset'], expected_map_sha256=first['map_sha256'])
            self.assertEqual(page['map_sha256'], first['map_sha256'])
            self.assertLessEqual(len(nav.encoded(page['symbols'])), 24000)
            seen.extend(page['symbols'])
        self.assertEqual(len(seen), 220)
        self.assertEqual(len({row['symbol'] for row in seen}), 220)
        self.assertEqual({(row['line'], row['end_line']) for row in seen}, {(i, i) for i in range(1, 221)})
        for fields in ({'queries': ['alpha_'], 'projection': 'locations'},
                       {'queries': selection, 'projection': 'full'}):
            with self.subTest(fields=fields), self.assertRaises(caller.EmbeddedNokiyError):
                self.query(['many.py'], offset=1, expected_map_sha256=first['map_sha256'], **fields)
        source.write_text(source.read_text() + '\n# source changed\n')
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.query(['many.py'], queries=selection, projection='locations',
                       offset=1, expected_map_sha256=first['map_sha256'])
        changed = self.query(['many.py'], queries=selection, projection='locations')
        self.assertNotEqual(first['map_sha256'], changed['map_sha256'])
        self.assertNotEqual(first['symbols'][0]['source_sha256'], changed['symbols'][0]['source_sha256'])

    def test_frozen_parser_is_exact_canonical_snapshot(self):
        package = Path(nav.__file__).parent
        provenance = json.loads((package / 'protocol/dcf_navigation_sources.json').read_text())
        for name, record in provenance['sources'].items():
            self.assertEqual(hashlib.sha256((package / name).read_bytes()).hexdigest(), record['sha256'])

    def test_source_snapshot_reuses_locations_for_changed_view_not_changed_cursor(self):
        first = self.query(queries=['alpha', 'beta'], projection='locations')
        expected = first['source_snapshot_sha256']
        full = self.query(queries=['alpha'], projection='full', expected_source_snapshot_sha256=expected)
        self.assertEqual(full['source_snapshot_sha256'], expected)
        self.assertNotEqual(first['map_sha256'], full['map_sha256'])
        self.assertEqual(full['symbols'][0]['calls'][0]['targets'], ['symbol:b.beta'])
        self.assertEqual(self.query(projection='locations')['source_snapshot_sha256'], expected)
        for fields in (
            {'queries': ['alpha'], 'expected_map_sha256': first['map_sha256']},
            {'offset': 1, 'expected_source_snapshot_sha256': expected},
            {'queries': ['alpha'], 'expected_source_snapshot_sha256': expected,
             'expected_map_sha256': first['map_sha256']},
        ):
            with self.subTest(fields=fields), self.assertRaises(caller.EmbeddedNokiyError):
                self.query(**fields)
        second = self.query(queries=['alpha', 'beta'], projection='locations', offset=1,
                            expected_map_sha256=first['map_sha256'], expected_source_snapshot_sha256=expected)
        self.assertEqual(second['symbols'], first['symbols'][1:])

    def test_source_snapshot_binds_workspace_scope_sources_and_parser_not_projection_revision(self):
        first = self.query()
        expected = first['source_snapshot_sha256']
        with patch.object(nav, 'PROJECTION_REVISION', nav.PROJECTION_REVISION + 1):
            revised = self.query(expected_source_snapshot_sha256=expected)
            self.assertEqual(revised['source_snapshot_sha256'], expected)
            self.assertNotEqual(revised['map_sha256'], first['map_sha256'])
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.query(['a.py', 'b.py'], expected_source_snapshot_sha256=expected)
        other = self.root / 'other'
        other.mkdir()
        for name in ('a.py', 'b.py'):
            (other / name).write_bytes((self.workspace / name).read_bytes())
        with self.assertRaises(caller.EmbeddedNokiyError):
            nav.navigate(other, ['.'], cache_root=self.cache, expected_source_snapshot_sha256=expected)
        with patch.object(nav.extractor, '__file__', __file__), self.assertRaises(caller.EmbeddedNokiyError):
            self.query(expected_source_snapshot_sha256=expected)
        (self.workspace / 'b.py').write_text('def beta():\n    return 2\n')
        with patch.object(nav.resolver, 'resolve_calls') as resolve, self.assertRaises(caller.EmbeddedNokiyError):
            self.query(expected_source_snapshot_sha256=expected)
        resolve.assert_not_called()
        self.assertNotEqual(self.query()['source_snapshot_sha256'], expected)

    def test_source_snapshot_hashes_invalid_source_bytes_and_coverage(self):
        for before, after in ((b'def broken(\n', b'def other(\n'), (b'\xff', b'\xfe')):
            with self.subTest(before=before):
                source = self.workspace / 'broken.py'
                source.write_bytes(before)
                first = self.query()
                self.assertIsNotNone(first['source_snapshot_sha256'])
                self.assertEqual(first['coverage']['indexed_files'], 2)
                source.write_bytes(after)
                with self.assertRaises(caller.EmbeddedNokiyError):
                    self.query(expected_source_snapshot_sha256=first['source_snapshot_sha256'])
        first = self.query()
        (self.workspace / 'unknown.ts').write_text('unsupported source')
        with self.assertRaises(caller.EmbeddedNokiyError):
            self.query(expected_source_snapshot_sha256=first['source_snapshot_sha256'])

    def test_source_snapshot_unavailable_for_unreadable_deferred_or_over_budget_sources(self):
        expected = self.query()['source_snapshot_sha256']
        original = nav._read_exact
        def unreadable(workspace, name, **options):
            if name == 'b.py':
                raise caller.EmbeddedNokiyError('READ_DENIED', 'fixture')
            return original(workspace, name, **options)
        for context in (patch.object(nav, '_read_exact', side_effect=unreadable),
                        patch.object(nav, 'MAX_FILES', 1), patch.object(nav, 'MAX_BYTES', 1)):
            with context:
                self.assertIsNone(self.query()['source_snapshot_sha256'])
                with self.assertRaises(caller.EmbeddedNokiyError):
                    self.query(expected_source_snapshot_sha256=expected)

    def test_source_snapshot_identity_shape_fails_before_filesystem_reads(self):
        with patch.object(caller, '_plain_path') as workspace_check:
            for key in ('expected_map_sha256', 'expected_source_snapshot_sha256'):
                for value in ('bad', 'A' * 64, 1, [], True):
                    with self.subTest(key=key, value=value), self.assertRaises(caller.EmbeddedNokiyError):
                        self.query(**{key: value})
            workspace_check.assert_not_called()

    def test_cli_source_snapshot_reuse_and_static_recovery_diagnostics(self):
        from contextlib import redirect_stdout
        import io
        first = self.query(queries=['alpha', 'beta'], projection='locations')
        base = ['navigate', '--workspace', str(self.workspace), '--read-path', '.',
                '--query-any', 'alpha', '--projection', 'full']
        for option, identity, success in (
            ('--expected-source-snapshot-sha256', first['source_snapshot_sha256'], True),
            ('--expected-map-sha256', first['map_sha256'], False),
        ):
            output = io.StringIO()
            with patch.object(nav, 'CACHE_ROOT', self.cache), redirect_stdout(output):
                code = caller.main([*base, option, identity])
            result = json.loads(output.getvalue())
            self.assertEqual(code, 0 if success else 2)
            if success:
                self.assertEqual(result['source_snapshot_sha256'], first['source_snapshot_sha256'])
            else:
                self.assertIn('expected_source_snapshot_sha256', result['navigation_recovery'])
                self.assertEqual(result['first_typed_blocker'], 'NOKIY_NAVIGATION_INVALID')
        failure = caller._failure(caller.EmbeddedNokiyError('NOKIY_NAVIGATION_INVALID', 'private fixture detail'))
        self.assertNotIn('navigation_recovery', failure)
        self.assertNotIn('private fixture detail', json.dumps(failure))
        self.assertNotIn('navigation_detail', failure)
        static = caller._failure(caller.EmbeddedNokiyError(
            'NOKIY_NAVIGATION_INVALID', 'read_paths must stay within explicit non-hidden source scopes'))
        self.assertEqual(static['navigation_detail'], 'read_paths must stay within explicit non-hidden source scopes')

    def test_static_navigation_details_cover_every_invalid_literal(self):
        import re
        source = Path(nav.__file__).read_text(encoding='utf-8')
        literals = set(re.findall(r"invalid\('([^']*)'\)", source))
        self.assertTrue(literals)
        self.assertEqual(literals, set(caller._NAVIGATION_STATIC_DETAILS))
        self.assertNotIn('invalid(f', source)

    def test_modules_larger_than_the_excerpt_limit_are_indexed(self):
        from codex_collaboration_harness import source_excerpt
        body = ''.join(f'def helper_{i}():\n    return {i}\n' for i in range(40000))
        self.assertGreater(len(body.encode()), source_excerpt.MAX_SOURCE_BYTES)
        (self.workspace / 'large.py').write_text(body + 'def large_target():\n    return 0\n')
        result = self.query(queries=['large_target'], projection='locations')
        self.assertEqual(result['coverage']['parse_failures'], [])
        self.assertTrue(result['coverage']['complete_for_supported_scope'])

    def test_cli_success_uses_same_map_without_provider(self):
        from contextlib import redirect_stdout
        import io
        output = io.StringIO()
        with patch.object(nav, 'CACHE_ROOT', self.cache), redirect_stdout(output):
            code = caller.main(['navigate', '--workspace', str(self.workspace), '--read-path', '.', '--query', 'alpha'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['matched_symbols'], 1)
        self.assertEqual(result['symbols'][0]['symbol'], 'a.alpha')

    def test_cli_repeatable_query_any_and_locations_use_same_map(self):
        from contextlib import redirect_stdout
        import io
        output = io.StringIO()
        with patch.object(nav, 'CACHE_ROOT', self.cache), redirect_stdout(output):
            code = caller.main(['navigate', '--workspace', str(self.workspace), '--read-path', '.',
                                '--query', '', '--query-any', 'BETA', '--query-any', 'alpha',
                                '--query-any', 'ALPHA', '--projection', 'locations'])
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        expected = self.query(queries=['alpha', 'beta'], projection='locations')
        self.assertEqual(result['map_sha256'], expected['map_sha256'])
        self.assertEqual(result['symbols'], expected['symbols'])
        self.assertEqual(result['coverage'], expected['coverage'])
        self.assertFalse(result['provider_execution_started'])
        self.assertFalse(result['selection_grants_read_write'])

    def test_cli_rejects_mixed_and_malformed_selectors_before_filesystem(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        base = ['navigate', '--workspace', str(self.workspace), '--read-path', '.']
        with patch.object(caller, '_plain_path') as workspace_check:
            for flags in (['--query', 'alpha', '--query-any', 'beta'],
                          ['--query-any', ''], ['--query-any', 'é' * 129]):
                output = io.StringIO()
                with self.subTest(flags=flags), redirect_stdout(output):
                    self.assertEqual(caller.main(base + flags), 2)
                self.assertIn('NOKIY_NAVIGATION_INVALID', output.getvalue())
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                caller.main(base + ['--projection', 'summary'])
            self.assertEqual(error.exception.code, 2)
            workspace_check.assert_not_called()
