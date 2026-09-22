# SPDX-License-Identifier: MIT
"""Actual Darwin checks on synthetic, bounded processes, never production jobs."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from codex_collaboration_harness import graph_process
from codex_collaboration_harness.graph_entry import sandbox_profile
from test_graph_entry import GraphFixture


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
