# SPDX-License-Identifier: MIT
"""Opt-in native sandbox transport to the existing invocation-owned verifier.

This tests a host-established channel, not permission for a sandboxed caller to
create an external executor. No worker, provider, daemon or credentials involved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_verifier_parent
from codex_collaboration_harness.verifier_channel import VerifierChannel


NATIVE = os.environ.get("NOKIY_NATIVE_SANDBOX_TEST_BIN")
ROUTER = os.environ.get("NOKIY_VERIFIER_TEST_ROUTER")
CLIENT = r'''
import ctypes,json,os,socket,sys
library=ctypes.CDLL('/usr/lib/libsandbox.dylib')
library.sandbox_check.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int]
library.sandbox_check.restype=ctypes.c_int
assert library.sandbox_check(os.getpid(),None,0)==1
channel=socket.socket(fileno=int(sys.argv[1]))
channel.settimeout(15)
request={'version':1,'binding':sys.argv[2],'call_id':'native-scoped-test','verifier_index':0}
channel.sendall((json.dumps(request)+'\n').encode())
with channel.makefile('rb') as stream:
    line=stream.readline(262145)
    assert len(line)<=262144
channel.close()
print(json.dumps({'sandboxed':True,'reply':json.loads(line) if line else None}))
'''


@unittest.skipUnless(sys.platform == "darwin" and NATIVE and ROUTER,
                     "requires explicit native binary and compiled router")
class NativeVerifierChannelTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_verifier_parent.VerifierParentTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.prefix = [str(Path(NATIVE).resolve(strict=True)), "sandbox",
            "--permission-profile", "nokiy_benchmark", "--include-managed-config",
            "-c", 'default_permissions="nokiy_benchmark"',
            "-c", 'permissions.nokiy_benchmark.extends=":workspace"',
            "-c", "permissions.nokiy_benchmark.network.enabled=true", "--"]

    def invoke(self, runner, binding=None):
        identity = {key: os.environ.get(key)
                    for key in ("HOME", "CODEX_HOME", "CODEX_THREAD_ID")}
        with VerifierChannel(runner.binding, 1, runner, timeout_seconds=15) as channel:
            process = subprocess.Popen([
                *self.prefix, sys.executable, "-I", "-B", "-c", CLIENT,
                str(channel.router_fd), binding or runner.binding,
            ], cwd=self.fixture.workspace, env=dict(os.environ),
                pass_fds=(channel.router_fd,), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            channel.release_router_copy()
            try:
                stdout, stderr = process.communicate(timeout=20)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            observed = json.loads(stdout)
        self.assertEqual(identity, {key: os.environ.get(key) for key in identity})
        self.assertTrue(observed["sandboxed"])
        return observed["reply"], channel.failure

    def test_native_channel_runs_scoped_verifier_without_nested_sandbox(self):
        f = self.fixture
        outside = f.root / "outside.txt"
        outside.write_text("must remain private")
        marker = f.scratch / "allowed.txt"
        body = (
            "from pathlib import Path\nimport json,socket\n"
            "assert Path('solution.txt').read_text()=='broken'\n"
            f"Path({str(marker)!r}).write_text('verified')\n"
            "denied=[]\n"
            f"for label,action in [('outside_read',lambda:Path({str(outside)!r}).read_text()),\n"
            "('source_write',lambda:Path('solution.txt').write_text('unauthorized')),\n"
            "('network',lambda:socket.create_connection(('127.0.0.1',9),timeout=.2))]:\n"
            "    try: action()\n"
            "    except PermissionError: denied.append(label)\n"
            "    else: raise AssertionError(label+' was not denied')\n"
            "print(json.dumps({'denied':denied}))\n"
        )
        runner = f.runner(body)
        reply, failure = self.invoke(runner)
        self.assertIsNone(failure)
        self.assertEqual(reply["binding"], runner.binding)
        self.assertEqual(reply["call_id"], "native-scoped-test")
        self.assertEqual(reply["verifier_index"], 0)
        result = reply["result"]
        self.assertTrue(result["success"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["outcome"], "known")
        self.assertTrue(result["process_reaped"])
        self.assertTrue(result["process_group_empty"])
        self.assertEqual(json.loads(result["stdout"])["denied"],
                         ["outside_read", "source_write", "network"])
        self.assertEqual(marker.read_text(), "verified")
        self.assertEqual(f.solution.read_text(), "broken")
        self.assertEqual(outside.read_text(), "must remain private")
        self.assertEqual(runner.calls, 1)
        self.assertTrue(runner.cleanup_pass)

    def test_native_channel_wrong_binding_never_executes_verifier(self):
        f = self.fixture
        marker = f.scratch / "must-not-exist"
        runner = f.runner(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        wrong = ("0" if runner.binding[0] != "0" else "1") + runner.binding[1:]
        reply, failure = self.invoke(runner, wrong)
        self.assertIsNone(reply)
        self.assertIn("wrong binding", failure)
        self.assertFalse(marker.exists())
        self.assertEqual(runner.calls, 0)
        self.assertTrue(runner.cleanup_pass)
