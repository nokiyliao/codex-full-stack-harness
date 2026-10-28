"""Prompt-only identity validation; disposable files, no deployment effects."""
import json
import os
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path

from test_caller_release_preparation import adapter


class AgentPromptIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.old = root / 'old'
        self.prior = self.old / 'runtime/agents/src/direct/prompt.md'
        self.source = root / 'source/agents/src/direct/prompt.md'
        for path in (self.prior, self.source):
            path.parent.mkdir(parents=True)
            path.write_text('Preserve authority and focused verification.\n')
        self.image = {'artifacts': {'direct_prompt': adapter.identity(self.prior)}}
        self.save_image()
        self.updates = {'direct_prompt': {**adapter.identity(self.source),
                                        'previous_sha256': adapter.sha(self.prior)}}

    def save_image(self):
        (self.old / 'runtime-image.json').write_text(json.dumps(self.image))

    def test_exact_identity_preserves_original_inode(self):
        before = self.prior.stat().st_ino, self.prior.read_bytes()
        self.assertEqual(adapter.validate_agent_prompt_updates(self.old, self.updates), self.updates)
        self.assertEqual((self.prior.stat().st_ino, self.prior.read_bytes()), before)

    def test_prompt_only_stage_and_readback_keep_binary_and_old_inode(self):
        self.source.write_text('Use small exact patches; preserve verification.\n')
        self.updates['direct_prompt']['sha256'] = adapter.sha(self.source)
        binary = self.old / 'runtime/tura_runtime'
        binary.write_bytes(b'unchanged compiled runtime')
        self.image['artifacts']['tura_runtime'] = adapter.identity(binary)
        self.save_image()
        new = self.old.with_name('new')
        staging = new.with_name('.new.staging')
        staged = staging / self.prior.relative_to(self.old)
        staged.parent.mkdir(parents=True)
        os.link(self.prior, staged)
        image = json.loads(json.dumps(self.image))
        for name, entry in image['artifacts'].items():
            prior = Path(entry['path'])
            target = staging / prior.relative_to(self.old)
            if name != 'direct_prompt':
                os.link(prior, target)
            entry['path'] = str(new / prior.relative_to(self.old))
        task = SimpleNamespace(old=self.old, new=new, runtime_build=lambda: None,
                               prompt_updates=lambda: self.updates,
                               artifact_updates=lambda build: adapter.validate_agent_prompt_updates(self.old, self.updates),
                               hashes=lambda: None)
        phase = adapter.Adapter.__new__(adapter.Adapter)
        phase.task, phase.catalog_staged = task, False
        phase.package = SimpleNamespace(RUNTIME_BINS=())
        prior_identity = self.prior.stat().st_ino, self.prior.read_bytes()
        phase.stage_save(adapter.save, staging / 'runtime-image.json', image)
        phase.stage_save(adapter.save, staging / 'release-provenance.json', {'changed_runtime_artifacts': []})
        staging.rename(new)
        self.assertTrue(phase.catalog_staged)
        self.assertEqual(phase.runtime_artifacts()['tura_runtime']['sha256'], adapter.sha(binary))
        self.assertEqual((self.prior.stat().st_ino, self.prior.read_bytes()), prior_identity)
        self.assertEqual((new / self.prior.relative_to(self.old)).read_bytes(), self.source.read_bytes())
        self.assertEqual(adapter.document(new / 'release-provenance.json')['changed_runtime_artifacts'], ['direct_prompt'])
        (new / 'runtime/tura_runtime').unlink()
        (new / 'runtime/tura_runtime').write_bytes(b'drift')
        with self.assertRaises(adapter.PreparationError):
            phase.runtime_artifacts()

    def test_rejects_drift(self):
        for path in (self.source, self.prior):
            original = path.read_bytes()
            path.write_text('Changed\n')
            with self.assertRaises(adapter.PreparationError):
                adapter.validate_agent_prompt_updates(self.old, self.updates)
            path.write_bytes(original)

    def test_rejects_config_destination(self):
        config = self.prior.with_name('agent_config.json')
        config.write_text('{}')
        self.image['artifacts']['direct_prompt'] = adapter.identity(config)
        self.save_image()
        with self.assertRaises(adapter.PreparationError):
            adapter.validate_agent_prompt_updates(self.old, self.updates)

    def test_rejects_invalid_content(self):
        for content in (b'', b'  \n', b'bad\x00text', b'\xff'):
            self.source.write_bytes(content)
            self.updates['direct_prompt']['sha256'] = adapter.sha(self.source)
            with self.assertRaises(adapter.PreparationError):
                adapter.validate_agent_prompt_updates(self.old, self.updates)

    def test_rejects_source_symlink(self):
        target = self.source.with_name('target.md')
        self.source.rename(target)
        self.source.symlink_to(target)
        with self.assertRaises(adapter.PreparationError):
            adapter.validate_agent_prompt_updates(self.old, self.updates)

    def test_rejects_duplicate_destination(self):
        self.image['artifacts']['alias'] = self.image['artifacts']['direct_prompt']
        self.save_image()
        self.updates['alias'] = dict(self.updates['direct_prompt'])
        with self.assertRaises(adapter.PreparationError):
            adapter.validate_agent_prompt_updates(self.old, self.updates)
