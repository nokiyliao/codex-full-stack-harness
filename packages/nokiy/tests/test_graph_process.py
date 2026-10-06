# SPDX-License-Identifier: MIT
"""Actual Darwin checks on synthetic, bounded processes, never production jobs."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
import threading
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from codex_collaboration_harness import graph_process
from codex_collaboration_harness.graph_entry import sandbox_profile
from test_graph_entry import GraphFixture


class OptionalTimeoutTests(unittest.TestCase):
    def test_nullable_supervision_keeps_cancel_parent_exit_output_and_cleanup(self):
        cases = (("complete", None, None), ("finite", 1, "GRAPH_ENGINE_DEADLINE"),
                 ("cancel", None, "GRAPH_CANCELLED"),
                 ("parent", None, "GRAPH_SUPERVISOR_CALLER_EXITED"),
                 ("output", None, "GRAPH_ENGINE_OUTPUT_LIMIT_EFFECT_UNSETTLED"))
        for name, timeout, expected in cases:
            with self.subTest(name=name):
                process, processes, streams = Mock(), Mock(), MagicMock()
                process.pid = 123
                process.poll.side_effect = [None, None, 0, 0, 0]
                process.wait.return_value = 0
                for fd, stream in enumerate((process.stdin, process.stdout, process.stderr), 10):
                    stream.fileno.return_value = fd
                    stream.closed = False
                processes.members.return_value = {}
                streams.__enter__.return_value = streams
                streams.select.return_value = []
                if name == "output":
                    streams.select.side_effect = [[(SimpleNamespace(data="stdout", fd=11), 1)], []]
                cancelled = threading.Event()
                if name == "cancel":
                    cancelled.set()
                with patch.object(graph_process, "DarwinProcesses", return_value=processes), \
                        patch.object(graph_process.subprocess, "Popen", return_value=process), \
                        patch.object(graph_process.selectors, "DefaultSelector", return_value=streams), \
                        patch.object(graph_process.os, "set_blocking"), \
                        patch.object(graph_process.os, "getppid", return_value=18 if name == "parent" else 17), \
                        patch.object(graph_process.os, "read", side_effect=([b"xxx", b"", b""]
                                                                            if name == "output" else [b"", b""])), \
                        patch.object(graph_process.time, "monotonic", side_effect=count(step=10000)):
                    result = graph_process.supervise(["unused"], cwd="/", env={}, input_bytes=b"",
                        timeout=timeout, cancelled=cancelled, max_stdout=1, max_stderr=1, parent_pid=17)
                self.assertEqual(result.failure, expected)
                self.assertTrue(result.scope["engine_reaped"])
                self.assertTrue(result.scope["no_live_descendants"])
                self.assertIsNone(result.scope["cleanup_error"])
                processes.verify_isolation.assert_called_once_with(17)
                if name == "complete":
                    process.send_signal.assert_not_called()
                else:
                    process.send_signal.assert_called_once_with(signal.SIGTERM)


class FocusedVerifierDiagnosticTests(unittest.TestCase):
    def supervise_logs(self, stdout, stderr, exit_code=0, *, timeout=300,
                       cancelled=False, parent_exited=False, cleanup_failed=False, drain_only=False):
        process, processes, streams = Mock(), Mock(), MagicMock()
        process.pid = 123
        process.wait.return_value = exit_code
        for fd, stream in enumerate((process.stdin, process.stdout, process.stderr), 10):
            stream.fileno.return_value = fd
            stream.closed = False
        chunks = {fd: [data[i:i + 65536] for i in range(0, len(data), 65536)]
                  for fd, data in ((11, stdout), (12, stderr))}
        events = [[(SimpleNamespace(data=name, fd=fd), 1)]
                  for fd, name in ((11, "stdout"), (12, "stderr")) for _ in chunks[fd]]
        process.poll.side_effect = ([exit_code] * 3 if drain_only else
                                    [None] * (len(events) + 1) + [exit_code] * 3)
        processes.members.return_value = {}
        if cleanup_failed:
            processes.members.side_effect = graph_process.ProcessScopeError("synthetic cleanup failure")
        streams.__enter__.return_value = streams
        streams.select.side_effect = events + [[]]
        cancellation = threading.Event()
        if cancelled:
            cancellation.set()
        with patch.object(graph_process, "DarwinProcesses", return_value=processes), \
                patch.object(graph_process.subprocess, "Popen", return_value=process), \
                patch.object(graph_process.selectors, "DefaultSelector", return_value=streams), \
                patch.object(graph_process.os, "set_blocking"), \
                patch.object(graph_process.os, "getppid", return_value=18 if parent_exited else 17), \
                patch.object(graph_process.os, "read", side_effect=lambda fd, size:
                             chunks[fd].pop(0) if chunks[fd] else b""), \
                patch.object(graph_process.time, "monotonic", side_effect=count(step=.01)):
            result = graph_process.supervise(["unused"], cwd="/", env={}, input_bytes=b"",
                timeout=timeout, cancelled=cancellation,
                max_stdout=graph_process._VERIFIER_DIAGNOSTIC_BYTES,
                max_stderr=graph_process._VERIFIER_DIAGNOSTIC_BYTES,
                parent_pid=17, diagnostic_output=True)
        processes.verify_isolation.assert_called_once_with(17)
        self.assertTrue(result.scope["engine_reaped"])
        return result, process

    def observe(self, result):
        output = io.StringIO()
        request = json.dumps({"argv": ["/verifier"], "cwd": "/fixture"}).encode()
        with patch.object(graph_process.sys, "argv", ["supervisor", "--focused-verifier",
                "--parent-pid", "17", "--timeout", "300"]), \
                patch.object(graph_process.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request))), \
                patch.object(graph_process.sys, "stdout", output), \
                patch.object(graph_process.os, "getcwd", return_value="/fixture"), \
                patch.object(graph_process.signal, "signal"), \
                patch.object(graph_process, "supervise", return_value=result) as supervise:
            self.assertEqual(graph_process.focused_verifier_main(), 0)
        self.assertTrue(supervise.call_args.kwargs["diagnostic_output"])
        for key in ("max_stdout", "max_stderr"):
            self.assertEqual(supervise.call_args.kwargs[key], graph_process._VERIFIER_DIAGNOSTIC_BYTES)
        encoded = output.getvalue().encode("ascii")
        observation = json.loads(encoded)
        self.assertEqual(set(observation), {"success", "exit_code", "stdout", "stderr",
                                           "process_reaped", "process_group_empty", "outcome"})
        return observation, encoded

    def test_noisy_success_and_failure_keep_actual_exit_head_and_tail(self):
        logs = {name: name.encode() + b"-head\n" + b"x" * 180224 + b"\n" + name.encode() + b"-tail\n"
                for name in ("stdout", "stderr")}
        for exit_code in (0, 1):
            with self.subTest(exit_code=exit_code):
                result, process = self.supervise_logs(logs["stdout"], logs["stderr"], exit_code)
                self.assertIsNone(result.failure)
                self.assertEqual(result.returncode, exit_code)
                process.send_signal.assert_not_called()
                process.kill.assert_not_called()
                observation, _ = self.observe(result)
                self.assertEqual(observation["exit_code"], exit_code)
                self.assertEqual(observation["outcome"], "known")
                self.assertEqual(observation["success"], exit_code == 0)
                self.assertTrue(observation["process_reaped"] and observation["process_group_empty"])
                for name, data in logs.items():
                    captured = getattr(result, name)
                    self.assertTrue(captured.startswith(name.encode() + b"-head\n"))
                    self.assertTrue(captured.endswith(b"\n" + name.encode() + b"-tail\n"))
                    omitted = len(data) - graph_process._VERIFIER_DIAGNOSTIC_BYTES
                    self.assertIn(f"{omitted} diagnostic bytes truncated".encode(), captured)
                    self.assertLessEqual(len(captured), graph_process._VERIFIER_DIAGNOSTIC_BYTES + 128)

    def test_streamed_capture_is_bounded_and_small_logs_are_unchanged(self):
        for length in (0, 15, 16, 17, 200):
            with self.subTest(length=length):
                data = bytes(range(200))[:length]
                capture = graph_process._DiagnosticCapture(16)
                for offset in range(0, length, 7):
                    capture.append(data[offset:offset + 7])
                    self.assertLessEqual(len(capture.buffer), 16)
                expected = data if length <= 16 else (data[:8] +
                    f"\n[... {length - 16} diagnostic bytes truncated ...]\n".encode() + data[-8:])
                self.assertEqual(bytes(capture), expected)

    def test_fast_exit_drains_large_diagnostics_after_reaping(self):
        data = b"head\n" + b"x" * 180224 + b"\ntail\n"
        result, process = self.supervise_logs(data, data, 1, drain_only=True)
        self.assertIsNone(result.failure)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.endswith(b"\ntail\n"))
        self.assertTrue(result.stderr.endswith(b"\ntail\n"))
        process.send_signal.assert_not_called()

    def test_difficult_diagnostics_fit_both_json_envelopes(self):
        cases = (("controls", b"\x00" * 180224),
                 ("escaping", b'\x01"\\\n\t' * 45056),
                 ("unicode", "\U0001f600\u2028é".encode() * 32768),
                 ("invalid_utf8", b"\xff\xfe\xc0\x80" * 45056))
        for name, noise in cases:
            with self.subTest(name=name):
                data = b"head\n" + noise + b"\ntail\n"
                result, _ = self.supervise_logs(data, data)
                observation, inner = self.observe(result)
                self.assertTrue(observation["success"])
                self.assertEqual(observation["outcome"], "known")
                self.assertLessEqual(len(inner), 262144)
                reply = {"version": 1, "binding": "a" * 64, "call_id": "x" * 256,
                         "verifier_index": 0, "result": observation}
                outer = (json.dumps(reply, ensure_ascii=True, allow_nan=False,
                                    sort_keys=True, separators=(",", ":")) + "\n").encode()
                self.assertLessEqual(len(outer), 262144)
                self.assertEqual(json.loads(outer)["result"], observation)
                for stream in ("stdout", "stderr"):
                    self.assertIn("diagnostic bytes truncated", observation[stream])
                    self.assertTrue(observation[stream].endswith("\ntail\n"))
                if name == "invalid_utf8":
                    self.assertIn("\ufffd", observation["stderr"])

    def test_noise_does_not_mask_cancellation_deadline_parent_exit_or_cleanup_failure(self):
        cases = (("cancel", {"cancelled": True}, "GRAPH_CANCELLED"),
                 ("deadline", {"timeout": .005}, "GRAPH_ENGINE_DEADLINE"),
                 ("parent", {"parent_exited": True}, "GRAPH_SUPERVISOR_CALLER_EXITED"),
                 ("cleanup", {"cleanup_failed": True}, "GRAPH_PROCESS_CLEANUP_UNPROVEN"))
        for name, options, expected in cases:
            with self.subTest(name=name):
                result, process = self.supervise_logs(b"x" * 180224, b"y" * 180224, **options)
                self.assertEqual(result.failure, expected)
                observation, _ = self.observe(result)
                self.assertFalse(observation["success"])
                self.assertEqual(observation["outcome"], "unknown")
                self.assertEqual(observation["process_group_empty"], name != "cleanup")
                if name == "cleanup":
                    self.assertIn("synthetic cleanup failure", result.scope["cleanup_error"])
                    process.send_signal.assert_not_called()
                else:
                    process.send_signal.assert_called_once_with(signal.SIGTERM)


@unittest.skipUnless(sys.platform == "darwin", "requires actual macOS Seatbelt")
class SandboxSupervisorTests(GraphFixture):
    def setUp(self) -> None:
        super().setUp()
        self.supervisor = Path(graph_process.__file__).resolve()
        self.engine = self.workspace / "src/engine.py"
        self.profile = sandbox_profile(self.payload["request"], self.settings, self.artifacts)
        self.profile += f'\n(allow file-read* (literal {json.dumps(str(self.supervisor))}))'

    def program(self, body: str) -> None:
        self.engine.write_text(f"#!{sys.executable}\n" + body)
        self.engine.chmod(0o700)

    def launch(self, *, sandbox: bool = True, timeout: float = 2, extra: tuple = ()):
        argv = (["/usr/bin/sandbox-exec", "-p", self.profile] if sandbox else []) + [
            sys.executable, "-B", str(self.supervisor), "--engine", str(self.engine),
            "--state-dir", str(self.artifacts), "--parent-pid", str(os.getpid()),
            "--timeout", str(timeout), *extra,
        ]
        return subprocess.Popen(argv, cwd=self.workspace,
                                env={"PATH": "/usr/bin:/bin", "HOME": str(self.artifacts)},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

    def result(self, process, timeout: float = 8) -> dict:
        try:
            stdout, stderr = process.communicate(b"{}" if process.stdin else None, timeout=timeout)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=18)
        self.assertFalse(stderr, stderr.decode())
        return json.loads(stdout)

    def assert_clean(self, result: dict) -> None:
        scope = result["process_scope"]
        self.assertTrue(scope["engine_reaped"], result)
        self.assertTrue(scope["no_live_descendants"], result)
        self.assertIsNone(scope["cleanup_error"], result)
        self.assertFalse(scope["effect_outcome_inferred_from_process_exit"])

    def test_direct_unsandboxed_use_is_rejected_before_engine_launch(self) -> None:
        self.program("from pathlib import Path\nPath('src/ran').touch()\n")
        result = self.result(self.launch(sandbox=False))
        self.assertEqual(result["failure"], "GRAPH_SUPERVISOR_SIGNAL_ISOLATION_REQUIRED")
        self.assertFalse((self.workspace / "src/ran").exists())

    def test_fast_exit_drains_output_and_reaps_engine(self) -> None:
        self.program("print('{\"status\":\"completed\"}')\n")
        result = self.result(self.launch())
        self.assertEqual(result["engine_result"], {"status": "completed"}, result)
        self.assertIsNone(result["failure"], result)
        self.assert_clean(result)

    def test_sigkill_with_session_escape_and_open_pipe_is_cleaned(self) -> None:
        self.program(
            "import json, os, signal, subprocess, sys, time\n"
            "from pathlib import Path\n"
            "child = subprocess.Popen([sys.executable, '-B', '-c', 'import time;time.sleep(5)'],"
            " start_new_session=True)\n"
            "Path('src/child.json').write_text(json.dumps({'pid':child.pid,'sid':os.getsid(child.pid)}))\n"
            "time.sleep(.05)\n"
            "os.kill(os.getpid(), signal.SIGKILL)\n"
        )
        start = time.monotonic()
        result = self.result(self.launch())
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(result["exit_code"], -signal.SIGKILL, result)
        self.assertIsNone(result["engine_result"])
        self.assert_clean(result)
        self.assertGreaterEqual(result["process_scope"]["descendant_signals"], 1)
        child = json.loads((self.workspace / "src/child.json").read_text())
        self.assertEqual(child["pid"], child["sid"])
        info = graph_process.DarwinProcesses().info(child["pid"])
        self.assertTrue(info is None or info.status == 5)

    def test_identical_sibling_sandbox_and_outside_process_survive(self) -> None:
        self.program("print('{\"status\":\"completed\"}')\n")
        outside = subprocess.Popen(["/bin/sleep", "5"])
        sibling = subprocess.Popen(["/usr/bin/sandbox-exec", "-p", self.profile,
                                    "/bin/sleep", "5"])
        try:
            result = self.result(self.launch())
            self.assert_clean(result)
            self.assertIsNone(outside.poll())
            self.assertIsNone(sibling.poll())
        finally:
            outside.kill()
            outside.wait()
            sibling.kill()
            sibling.wait()

    def test_deadline_stops_engine(self) -> None:
        self.program("import time\ntime.sleep(5)\n")
        result = self.result(self.launch(timeout=.15))
        self.assertEqual(result["failure"], "GRAPH_ENGINE_DEADLINE", result)
        self.assert_clean(result)

    def test_output_limit_stops_engine_and_preserves_failure(self) -> None:
        self.program("import time\nprint('x' * 4096, flush=True)\ntime.sleep(5)\n")
        result = self.result(self.launch(extra=("--max-stdout", "1024")))
        self.assertEqual(result["failure"], "GRAPH_ENGINE_OUTPUT_LIMIT_EFFECT_UNSETTLED", result)
        self.assert_clean(result)

    def test_duplicate_engine_json_is_not_normalized_into_success(self) -> None:
        self.program("print('{\"status\":\"blocked\",\"status\":\"completed\"}')\n")
        result = self.result(self.launch())
        self.assertIsNone(result["engine_result"])
        self.assertEqual(result["failure"], "GRAPH_ENGINE_RESULT_INVALID_EFFECT_UNSETTLED")
        self.assert_clean(result)

    def test_cancellation_finishes_cleanup_before_supervisor_exit(self) -> None:
        self.program("from pathlib import Path\nimport time\nPath('src/ready').touch()\ntime.sleep(5)\n")
        process = self.launch()
        process.stdin.write(b"{}")
        process.stdin.close()
        process.stdin = None
        deadline = time.monotonic() + 2
        while not (self.workspace / "src/ready").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        process.terminate()
        result = self.result(process)
        self.assertEqual(result["failure"], "GRAPH_CANCELLED", result)
        self.assert_clean(result)

    def test_caller_death_triggers_finite_supervisor_cleanup(self) -> None:
        self.program("from pathlib import Path\nimport time\nPath('src/ready').touch()\ntime.sleep(5)\n")
        argv = ["/usr/bin/sandbox-exec", "-p", self.profile, sys.executable, "-B",
                str(self.supervisor), "--engine", str(self.engine),
                "--state-dir", str(self.artifacts), "--timeout", "3"]
        relay = subprocess.Popen([
            sys.executable, "-B", "-c",
            "import json,os,subprocess,sys,time\n"
            "p=subprocess.Popen(json.loads(sys.argv[1])+['--parent-pid',str(os.getpid())],"
            "stdin=subprocess.PIPE)\np.stdin.write(b'{}');p.stdin.close()\ntime.sleep(5)",
            json.dumps(argv),
        ], cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.artifacts)})
        try:
            deadline = time.monotonic() + 2
            while not (self.workspace / "src/ready").exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue((self.workspace / "src/ready").exists())
            relay.kill()
            stdout, stderr = relay.communicate(timeout=8)
            self.assertFalse(stderr, stderr.decode())
            result = json.loads(stdout)
            self.assertEqual(result["failure"], "GRAPH_SUPERVISOR_CALLER_EXITED", result)
            self.assert_clean(result)
        finally:
            if relay.poll() is None:
                relay.kill()
            relay.communicate(timeout=8)


if __name__ == "__main__":
    unittest.main()
