"""Failure barrier and original-plan recovery; no subprocess/provider allowed."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load('parallel_host', ROOT.parent / 'src/nokiy-native-host.py')
existing = load('existing_reference_tests', ROOT / 'test_nokiy_native_reference.py')
existing.host = host


class ParallelPreparationTests(existing.NativeReferenceTests):
    def check_barrier(self, failure=None, exception=False, parallel=3):
        payload = self.missing_batch_payload()
        payload['max_parallel_workers'] = parallel
        barrier = threading.Barrier(parallel, timeout=5)
        completed = []
        active = 0
        peak = 0
        lock = threading.Lock()

        def prepare(*args):
            nonlocal active, peak
            draft = args[-1]
            index = int(Path(draft['artifact_root']).name.rsplit('-', 1)[1])
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait()
            try:
                if index == failure:
                    if exception:
                        raise ValueError('fixture preparation exception')
                    return None, {'status': 'REJECTED_PREPARATION', 'exit_code': 7,
                                  'provider_execution_started': False}
                return self.prepared_batch_member(*args)
            finally:
                with lock:
                    active -= 1
                    completed.append(index)

        def execute(_entry, argv):
            self.assertEqual(sorted(completed), [1, 2, 3])
            self.assertEqual(active, 0)
            self.assertIsNone(failure)
            self.assertEqual(argv[1], 'run-batch')
            raise SystemExit('captured, never executed')

        self.execv.side_effect = execute
        with mock.patch.object(host, 'worker_capacity', return_value=3), \
                mock.patch.object(host, 'prepare_action', side_effect=prepare), \
                redirect_stdout(io.StringIO()) as output:
            if exception:
                with self.assertRaisesRegex(ValueError, 'fixture preparation'):
                    host.run(self.envelope(payload))
            elif failure:
                self.assertEqual(host.run(self.envelope(payload)), 7)
                self.assertFalse(json.loads(output.getvalue())['provider_execution_started'])
            else:
                with self.assertRaises(SystemExit):
                    host.run(self.envelope(payload))
        self.assertEqual(sorted(completed), [1, 2, 3])
        self.assertEqual(peak, parallel)
        self.assertEqual(active, 0)
        plans = list(Path(payload['artifact_root']).glob('native-batch-plan-*.json'))
        if failure:
            self.execv.assert_not_called()
            self.assertEqual(plans, [])
        else:
            self.assertEqual(len(plans), 1)
            members = json.loads(plans[0].read_bytes())['members']
            self.assertEqual([Path(m['artifact_root']).name for m in members],
                             ['member-1', 'member-2', 'member-3'])
        self.prepare_process.assert_not_called()

    def test_parallel_barrier_before_plan_and_execution(self):
        self.check_barrier()

    def test_serial_setting_is_preserved(self):
        self.check_barrier(parallel=1)

    def test_first_rejection_leaves_all_preparations_terminal_no_plan(self):
        self.check_barrier(failure=1)

    def test_last_rejection_cannot_launch_prepared_peers(self):
        self.check_barrier(failure=3)

    def test_exception_waits_for_running_preparation_no_plan(self):
        self.check_barrier(failure=2, exception=True)


def load_tests(loader, tests, pattern):
    # The reference suite is discovered separately; only add the new barriers.
    return unittest.TestSuite(ParallelPreparationTests(name)
                              for name in ParallelPreparationTests.__dict__
                              if name.startswith('test_'))


if __name__ == '__main__':
    names = [name for name in unittest.defaultTestLoader.getTestCaseNames(existing.NativeReferenceTests)
             if 'batch' in name]
    suite = unittest.TestSuite(existing.NativeReferenceTests(name) for name in names)
    suite.addTests(ParallelPreparationTests(name) for name in ParallelPreparationTests.__dict__
                   if name.startswith('test_'))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
