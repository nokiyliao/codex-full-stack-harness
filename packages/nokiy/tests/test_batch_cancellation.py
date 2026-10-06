"""Offline invocation-local cancellation regressions; never call a provider."""
from __future__ import annotations

import os
import signal
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from unittest.mock import patch

from codex_collaboration_harness import batch, embedded_nokiy as caller
import test_nokiy_batch as fixtures


class CancellationTests(unittest.TestCase):
    # Reuse only the fixture methods, not its test cases.
    setUp = fixtures.BatchFixture.setUp
    write_scopes = fixtures.BatchFixture.write_scopes
    save_plan = fixtures.BatchFixture.save_plan
    mocks = fixtures.BatchFixture.mocks
    terminal = staticmethod(fixtures.BatchFixture.terminal)

    def run_sleep(self, label, seconds=30, on_spawn=None):
        prefix = self.workspace / label
        stdin = prefix.with_suffix(".in")
        stdin.write_bytes(b"")
        return caller._run_process(
            [sys.executable, "-c", f"import time; time.sleep({seconds})"],
            cwd=self.workspace, env=os.environ, stdin_path=stdin,
            stdout_path=prefix.with_suffix(".out"),
            stderr_path=prefix.with_suffix(".err"), timeout=4,
            output_limit=1024, on_spawn=on_spawn)

    def assert_handlers_restored(self, previous):
        for signum, handler in previous.items():
            self.assertIs(signal.getsignal(signum), handler)

    def test_both_batch_workers_cancel_and_next_batch_is_clean(self):
        self.mocks()
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        for signum in (signal.SIGINT, signal.SIGTERM):
            spawned = [threading.Event(), threading.Event()]
            sent = threading.Event()
            results = {}
            outsider_ready = threading.Event()
            outsider_release = threading.Event()
            outsider_cancelled = []

            def unrelated():
                with caller._cancellation_signals() as cancelled:
                    outsider_ready.set()
                    outsider_release.wait(6)
                    outsider_cancelled.extend(cancelled)

            outsider = threading.Thread(target=unrelated)
            outsider.start()
            self.assertTrue(outsider_ready.wait(2))

            def execute(request):
                index = self.requests.index(request)
                result = self.run_sleep(f"batch-{signum}-{index}", on_spawn=spawned[index].set)
                results[index] = result
                return self.terminal(request, status="BLOCKED")

            def interrupt():
                if all(event.wait(2) for event in spawned):
                    os.kill(os.getpid(), signum)
                    sent.set()

            sender = threading.Thread(target=interrupt)
            sender.start()
            try:
                with patch.object(batch.caller, "execute", side_effect=execute):
                    response = batch.run_batch(self.plan, self.digest)
                sender.join(timeout=3)
                self.assertTrue(sent.is_set(), "both subprocesses must start before signal")
                self.assertEqual(response["status"], "BLOCKED")
                self.assertEqual(set(results), {0, 1})
                for result in results.values():
                    self.assertEqual(result[2], "NOKIY_EMBEDDED_CANCELLED")
                    self.assertTrue(result[3]["process_stopped"])
            finally:
                sender.join(timeout=3)
                outsider_release.set()
                outsider.join(timeout=3)
            self.assertFalse(sender.is_alive())
            self.assertFalse(outsider.is_alive())
            self.assertEqual(outsider_cancelled, [])
            self.assert_handlers_restored(previous)

        # No create-only claim in the mock: reentry runs again, without old
        # cancellation leaking into either new worker.
        completed = []

        def execute_again(request):
            index = self.requests.index(request)
            result = self.run_sleep(f"next-{index}", seconds=0.03)
            completed.append(result)
            return self.terminal(request)

        with patch.object(batch.caller, "execute", side_effect=execute_again):
            response = batch.run_batch(self.plan, self.digest)
        self.assertEqual(response["status"], "RESULT_AVAILABLE")
        self.assertEqual(len(completed), 2)
        self.assertTrue(all(result[2] is None for result in completed))
        self.assert_handlers_restored(previous)

    def test_before_spawn_inherited_cancel_never_calls_popen(self):
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        with caller._cancellation_signals():
            signal.raise_signal(signal.SIGINT)
            with ThreadPoolExecutor(max_workers=1) as pool, patch.object(
                    caller.subprocess, "Popen", side_effect=AssertionError("spawned after cancel")) as popen:
                future = pool.submit(copy_context().run, self.run_sleep, "never-start")
                with self.assertRaisesRegex(caller.EmbeddedNokiyError, "cancelled before spawn"):
                    future.result(timeout=3)
                popen.assert_not_called()
        self.assert_handlers_restored(previous)

    def test_single_run_signal_and_handler_restoration(self):
        previous = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        spawned = threading.Event()
        sent = threading.Event()

        def interrupt():
            if spawned.wait(2):
                os.kill(os.getpid(), signal.SIGTERM)
                sent.set()

        sender = threading.Thread(target=interrupt)
        sender.start()
        try:
            result = self.run_sleep("single", on_spawn=spawned.set)
            sender.join(timeout=3)
            self.assertTrue(sent.is_set())
            self.assertEqual(result[2], "NOKIY_EMBEDDED_CANCELLED")
            self.assertTrue(result[3]["process_stopped"])
        finally:
            sender.join(timeout=3)
        self.assertFalse(sender.is_alive())
        self.assert_handlers_restored(previous)
