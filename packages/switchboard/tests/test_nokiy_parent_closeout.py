"""Offline parent closeout routing; no DCF, worker, or actual lease mutation."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(os.environ.get('NOKIY_PARENT_CLOSEOUT_HOST',
                            str(Path(__file__).resolve().parents[1] / 'src/nokiy-native-host.py')))
SPEC = importlib.util.spec_from_file_location('nokiy_parent_closeout_host', SOURCE)
host = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host)


class ParentCloseoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.entry = self.root / '.local/bin/nokiy-embedded-run'
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text('not executed')
        self.plan = self.root / 'plan.json'
        self.plan.write_text('{}')
        env = mock.patch.dict(os.environ, {'HOME': str(self.root), 'CODEX_THREAD_ID':
                                         '019fd4f0-1079-77e2-8be7-cbf75ca28df5'})
        env.start()
        self.addCleanup(env.stop)

    def envelope(self, payload, version='nokiy_native_tool_v2'):
        return {'schema_version': version, 'worker': 'irrelevant', 'reasoning_effort': 'irrelevant',
                'service_tier': 'irrelevant', 'payload': payload,
                **({'workflow': 'nokiy'} if version.endswith('v3') else {})}

    def test_routes_before_model_preparation_and_without_serial_worker_lock(self):
        for version in ('nokiy_native_tool_v2', 'nokiy_native_tool_v3'):
            for operation, command in (('check_closeout', 'check-closeout'), ('closeout', 'closeout')):
                payload = {'operation': operation, 'plan_path': str(self.plan), 'plan_sha256': 'a' * 64}
                with mock.patch.object(host, 'prepare_action', side_effect=AssertionError('no preparation')), \
                     mock.patch.object(host.subprocess, 'call', return_value=0) as call, \
                     mock.patch.object(host.subprocess, 'run', side_effect=AssertionError('no worker')):
                    self.assertEqual(host.run(self.envelope(payload, version)), 0)
                    call.assert_called_once_with([str(self.entry), command, '--plan', str(self.plan),
                                                  '--approved-plan-sha256', 'a' * 64])
        self.assertFalse((self.root / 'nokiy-native-locks').exists())

    def test_historical_read_uses_original_reference_not_worker_or_plan(self):
        payload = {'operation': 'read_closeout', 'artifact_root': str(self.root), 'action_id': 'original'}
        with mock.patch.object(host.subprocess, 'call', return_value=0) as call:
            self.assertEqual(host.run(self.envelope(payload)), 0)
            call.assert_called_once_with([str(self.entry), 'read-closeout-result', '--artifact-root',
                                         str(self.root), '--action-id', 'original'])

    def test_extra_fields_owner_absence_digest_and_symlink_rejected(self):
        payload = {'operation': 'closeout', 'plan_path': str(self.plan), 'plan_sha256': 'a' * 64}
        invalid = [{**payload, key: 'not allowed'} for key in ('draft', 'action', 'native_thread_id', 'surface_id')]
        invalid += [{**payload, 'plan_sha256': True}, {**payload, 'plan_sha256': 'A' * 64}]
        alias = self.root / 'alias.json'
        alias.symlink_to(self.plan)
        invalid.append({**payload, 'plan_path': str(alias)})
        with mock.patch.object(host.subprocess, 'call') as call:
            for value in invalid:
                with self.subTest(value=value), self.assertRaises((ValueError, TypeError, OSError)):
                    host.run(self.envelope(value))
            with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': ''}), self.assertRaises(ValueError):
                host.run(self.envelope(payload))
            call.assert_not_called()


if __name__ == '__main__':
    unittest.main()
