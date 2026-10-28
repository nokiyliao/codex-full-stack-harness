"""One directory, one spelling: symlinked paths and URL fragments are normalized, not refused.

Live 2026-10-19: a coordinator's draft.artifact_root "/tmp" (canonical /private/tmp) and a
read_http URL ending in "#issuecomment-..." were both refused before any work started.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / 'src'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('path_spelling_host', 'nokiy-native-host.py')
http = load('path_spelling_http', 'nokiy-http-evidence.py')
THREAD = '00000000-0000-0000-0000-000000000001'


class PathSpellingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='nokiy-path-spelling-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.real = self.root / 'real'
        (self.real / 'workspace').mkdir(parents=True)
        (self.real / 'artifacts').mkdir()
        self.link = self.root / 'link'
        self.link.symlink_to(self.real, target_is_directory=True)
        (self.root / 'file').write_text('x')

    def test_symlinked_directories_get_their_canonical_spelling(self):
        self.assertEqual(host.canonical_spelling(str(self.link / 'workspace')), str(self.real / 'workspace'))
        self.assertEqual(host.canonical_spelling(str(self.link) + '/'), str(self.real))
        self.assertEqual(host.canonical_spelling(str(self.link / 'batch' / 'm1')), str(self.real / 'batch' / 'm1'))

    def test_canonical_and_unresolvable_spellings_come_back_unchanged(self):
        canonical = str(self.real / 'workspace')
        dangling = self.root / 'dangling'
        dangling.symlink_to(self.root / 'absent')
        for value in (canonical, canonical + '/', '/', 'relative/path', str(self.link) + '/../real',
                      str(self.link) + '/./workspace', str(self.root / 'file' / 'below'),
                      '//' + canonical.lstrip('/'), canonical + '\n', str(dangling / 'x'), 7, None):
            self.assertEqual(host.canonical_spelling(value), value)

    def test_directory_accepts_a_symlinked_spelling_and_returns_the_canonical_path(self):
        self.assertEqual(host.directory(str(self.link / 'artifacts'), 'draft.artifact_root'),
                         self.real / 'artifacts')
        for value in ('relative', str(self.root / 'absent'), str(self.root / 'file')):
            with self.assertRaisesRegex(ValueError, '^Existing absolute directory required for draft.workspace$'):
                host.directory(value, 'draft.workspace')

    def test_draft_paths_are_canonical_and_accepted_spellings_stay_byte_identical(self):
        draft = {'workspace': str(self.link / 'workspace'), 'artifact_root': str(self.link / 'artifacts'),
                 'prompt': 'p', 'authority_effect': 'none'}
        normalized = host.normalize_draft(draft)
        self.assertEqual((normalized['workspace'], normalized['artifact_root']),
                         (str(self.real / 'workspace'), str(self.real / 'artifacts')))
        accepted = {**draft, 'workspace': str(self.real / 'workspace') + '/',
                    'artifact_root': str(self.real / 'artifacts')}
        normalized = host.normalize_draft(accepted)
        # Request identity hashes the normalized draft: earlier accepted spellings keep it.
        self.assertEqual((normalized['workspace'], normalized['artifact_root']),
                         (accepted['workspace'], accepted['artifact_root']))

    def test_new_batch_member_under_a_symlinked_batch_root_keeps_the_create_relation(self):
        root, create = host.batch_directory(str(self.link / 'batch'))
        self.assertEqual((root, create), (self.real / 'batch', True))
        member = Path(host.canonical_spelling(str(self.link / 'batch' / 'm1')))
        self.assertEqual(member.parent, root)

    def test_live_read_http_with_a_fragment_is_fetched_without_it(self):
        payload = {'operation': 'read_http', 'artifact_root': str(self.link / 'artifacts') + '/',
                   'observation_id': 'app-signed-repair-public-check-20261019',
                   'urls': ['https://updates.oaistatic.com/codex/app/appcast',
                            'https://developers.openai.com/codex/changelog',
                            'https://developers.openai.com/codex/app/troubleshooting',
                            'https://github.com/openai/codex/issues/55141#issuecomment-6258199096']}
        with self.assertRaises(ValueError):
            http._validate(payload, THREAD)
        normalized = host.normalize_read_http(payload)
        self.assertEqual(normalized['urls'][3], 'https://github.com/openai/codex/issues/55141')
        self.assertEqual(normalized['urls'][:3], payload['urls'][:3])
        self.assertEqual(normalized['artifact_root'], str(self.real / 'artifacts'))
        self.assertEqual(http._validate(normalized, THREAD)['urls'], normalized['urls'])
        self.assertEqual(payload['urls'][3][-7:], '6258199096'[-7:])

    def test_fragment_only_differences_collapse_and_other_refusals_stay(self):
        payload = {'operation': 'read_http', 'artifact_root': str(self.real), 'observation_id': 'o',
                   'urls': ['https://example.invalid/a#x', 'https://example.invalid/a', 'https://example.invalid/b#']}
        self.assertEqual(host.normalize_read_http(payload)['urls'],
                         ['https://example.invalid/a', 'https://example.invalid/b'])
        for url in ('http://example.invalid/#x', 'https://user@example.invalid/#x', 'https://*.invalid/#x'):
            with self.assertRaises(ValueError):
                http._validate(host.normalize_read_http({**payload, 'urls': [url]}), THREAD)
        for odd in ('not-a-dict', {**payload, 'urls': 'https://example.invalid/#x'}, {**payload, 'urls': [1]}):
            self.assertEqual(host.normalize_read_http(odd), odd)


if __name__ == '__main__':
    unittest.main()

