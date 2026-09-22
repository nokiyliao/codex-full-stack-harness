# SPDX-License-Identifier: MIT
"""Per-call supervisor, launched INSIDE graph_entry's restrictive Seatbelt.

The kernel's same-sandbox signal rule, not a PID/session snapshot, bounds every
signal. A child may change session or process group without escaping this rule.
No daemon, saved PID recovery, or inference about command effects lives here.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any


class ProcessScopeError(RuntimeError):
    pass


class _BsdInfo(ctypes.Structure):
    # Darwin SDK sys/proc_info.h, PROC_PIDTBSDINFO (3).
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid",
        "rgid", "svuid", "svgid", "reserved",
    )] + [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)] + [
        (name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")
    ] + [("nice", ctypes.c_int32), ("started_sec", ctypes.c_uint64),
         ("started_usec", ctypes.c_uint64)]


class DarwinProcesses:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        self.lib.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32,
                                         ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_listpids.restype = ctypes.c_int
        self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                         ctypes.c_void_p, ctypes.c_int]
        self.lib.proc_pidinfo.restype = ctypes.c_int
        self._admitted = False

    def info(self, pid: int) -> _BsdInfo | None:
        info = _BsdInfo()
        ctypes.set_errno(0)
        size = self.lib.proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
        if size == 0 and ctypes.get_errno() == errno.ESRCH:
            return None
        if size != ctypes.sizeof(info) or info.pid != pid:
            raise ProcessScopeError("GRAPH_PROCESS_IDENTITY_UNAVAILABLE")
        return info

    def verify_isolation(self, parent_pid: int) -> None:
        # Refuse direct/unsandboxed use. graph_entry alone constructs the fixed
        # policy: deny default; allow signal only to target same-sandbox.
        if parent_pid <= 1 or os.getppid() != parent_pid:
            raise ProcessScopeError("GRAPH_SUPERVISOR_PARENT_MISMATCH")
        parent = self.info(parent_pid)
        if parent is None or parent.uid != os.getuid():
            raise ProcessScopeError("GRAPH_SUPERVISOR_PARENT_IDENTITY_UNAVAILABLE")
        try:
            os.kill(parent_pid, 0)
        except PermissionError:
            self._admitted = True
            return
        raise ProcessScopeError("GRAPH_SUPERVISOR_SIGNAL_ISOLATION_REQUIRED")

    def members(self) -> dict[int, tuple[int, int, int, int]]:
        if not self._admitted:
            raise ProcessScopeError("GRAPH_PROCESS_SCOPE_NOT_ADMITTED")
        # Process metadata only, not argv, environment or file content.
        pids = (ctypes.c_int * 32768)()
        count = self.lib.proc_listpids(1, 0, pids, ctypes.sizeof(pids))
        if count <= 0 or count >= ctypes.sizeof(pids):
            raise ProcessScopeError("GRAPH_PROCESS_SNAPSHOT_INCOMPLETE")
        members = {}
        for pid in pids[:count // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 1 or pid == os.getpid():
                continue
            try:
                os.kill(pid, 0)
            except (PermissionError, ProcessLookupError):
                continue
            info = self.info(pid)
            if info is not None and info.status != 5:  # SZOMB: exited, not a live writer.
                if info.uid != os.getuid():
                    raise ProcessScopeError("GRAPH_PROCESS_SCOPE_UID_CHANGED")
                members[pid] = (info.pid, info.uid, info.started_sec, info.started_usec)
        return members

    def kill_member(self, pid: int, expected: tuple[int, int, int, int]) -> bool:
        if not self._admitted or pid <= 1 or pid == os.getpid():
            raise ProcessScopeError("GRAPH_PROCESS_SCOPE_NOT_ADMITTED")
        info = self.info(pid)
        if info is None or (info.pid, info.uid, info.started_sec, info.started_usec) != expected:
            return False
        try:
            # PID reuse after the metadata check is still kernel-fenced: an
            # unrelated replacement PID is outside this inherited sandbox.
            os.kill(pid, signal.SIGKILL)
            return True
        except (PermissionError, ProcessLookupError):
            return False


@dataclass(frozen=True)
class EngineExit:
    stdout: bytes
    stderr: bytes
    returncode: int
    scope: dict[str, Any]
    failure: str | None


def supervise(
    argv: list[str], *, cwd: str, env: dict[str, str], input_bytes: bytes,
    timeout: float, cancelled: threading.Event, max_stdout: int, max_stderr: int,
    parent_pid: int, terminate_grace: float = 10, cleanup_timeout: float = 5,
) -> EngineExit:
    """Run one engine within the already applied per-call Seatbelt instance."""
    processes = DarwinProcesses()
    processes.verify_isolation(parent_pid)
    stdout, stderr = bytearray(), bytearray()
    failure = cleanup_error = None
    killed: set[tuple[int, int, int, int]] = set()
    no_live_descendants = False
    process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    terminating_at: float | None = None
    deadline = time.monotonic() + timeout

    def append(buffer: bytearray, chunk: bytes, limit: int) -> None:
        nonlocal failure
        if len(buffer) + len(chunk) > limit:
            failure = failure or "GRAPH_ENGINE_OUTPUT_LIMIT_EFFECT_UNSETTLED"
        buffer.extend(chunk[:max(0, limit - len(buffer))])

    try:
        with selectors.DefaultSelector() as streams:
            assert process.stdin and process.stdout and process.stderr
            for stream, event, name in (
                (process.stdin, selectors.EVENT_WRITE, "input"),
                (process.stdout, selectors.EVENT_READ, "stdout"),
                (process.stderr, selectors.EVENT_READ, "stderr"),
            ):
                os.set_blocking(stream.fileno(), False)
                streams.register(stream, event, name)
            remaining = memoryview(input_bytes)
            while process.poll() is None:
                now = time.monotonic()
                if os.getppid() != parent_pid:
                    failure = failure or "GRAPH_SUPERVISOR_CALLER_EXITED"
                if terminating_at is None and (cancelled.is_set() or now >= deadline or failure):
                    failure = failure or ("GRAPH_CANCELLED" if cancelled.is_set()
                                          else "GRAPH_ENGINE_DEADLINE")
                    process.send_signal(signal.SIGTERM)
                    terminating_at = now
                if terminating_at is not None and now - terminating_at >= terminate_grace:
                    process.kill()
                for key, _ in streams.select(0.025):
                    if key.data == "input":
                        try:
                            count = os.write(key.fd, remaining[:65536])
                            remaining = remaining[count:]
                        except BrokenPipeError:
                            remaining = remaining[:0]
                        except BlockingIOError:
                            continue
                        if not remaining:
                            streams.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            streams.unregister(key.fileobj)
                        elif key.data == "stdout":
                            append(stdout, chunk, max_stdout)
                        else:
                            append(stderr, chunk, max_stderr)
    except Exception as exc:
        failure = failure or f"GRAPH_PROCESS_SUPERVISION_FAILED:{type(exc).__name__}:{exc}"
    finally:
        if process.poll() is None:
            process.kill()
        try:
            cleanup_deadline = time.monotonic() + cleanup_timeout
            while True:
                process.poll()
                members = processes.members()
                if not members:
                    no_live_descendants = True
                    break
                for pid, identity in members.items():
                    if processes.kill_member(pid, identity):
                        killed.add(identity)
                if time.monotonic() >= cleanup_deadline:
                    raise ProcessScopeError("GRAPH_PROCESS_SCOPE_NOT_EMPTY")
                time.sleep(0.025)
        except Exception as exc:
            cleanup_error = f"{type(exc).__name__}:{exc}"
            failure = failure or "GRAPH_PROCESS_CLEANUP_UNPROVEN"
        returncode = process.wait(timeout=5)
        for stream, buffer, limit in ((process.stdout, stdout, max_stdout),
                                      (process.stderr, stderr, max_stderr)):
            if stream and not stream.closed:
                try:
                    while chunk := os.read(stream.fileno(), 65536):
                        append(buffer, chunk, limit)
                except BlockingIOError:
                    failure = failure or "GRAPH_PROCESS_PIPE_NOT_CLOSED"
                finally:
                    stream.close()
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    if killed:
        failure = failure or "GRAPH_DESCENDANTS_OUTLIVED_ENGINE"
    return EngineExit(bytes(stdout), bytes(stderr), returncode, {
        "scope": "per-call-inherited-seatbelt-signal-boundary",
        "supervisor_pid": os.getpid(), "engine_pid": process.pid,
        "engine_exit_code": returncode,
        "engine_reaped": True, "no_live_descendants": no_live_descendants,
        "descendant_signals": len(killed), "cleanup_error": cleanup_error,
        "effect_outcome_inferred_from_process_exit": False,
    }, failure)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("GRAPH_ENGINE_DUPLICATE_JSON_KEY")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"GRAPH_ENGINE_NONFINITE_JSON:{value}")


def _result(data: bytes) -> dict[str, Any] | None:
    result = json.loads(data, object_pairs_hook=_unique_object,
                        parse_constant=_invalid_constant) if data else None
    if result is not None and not isinstance(result, dict):
        raise ValueError("object required")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--max-stdout", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-stderr", type=int, default=65536)
    args = parser.parse_args()
    if not 0 < args.timeout <= 135 or not 1 <= args.max_stdout <= 64 * 1024 * 1024:
        parser.error("invalid supervisor budget")
    if not 1 <= args.max_stderr <= 65536:
        parser.error("invalid stderr budget")
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: cancelled.set())
    request = sys.stdin.buffer.read(262145)
    if len(request) > 262144:
        parser.error("input limit")
    try:
        outcome = supervise(
            [args.engine, "--state-dir", args.state_dir, "--supervised"], cwd=os.getcwd(),
            env=dict(os.environ), input_bytes=request, timeout=args.timeout,
            cancelled=cancelled, max_stdout=args.max_stdout, max_stderr=args.max_stderr,
            parent_pid=args.parent_pid,
        )
        failure = outcome.failure
        try:
            result = _result(outcome.stdout)
            if (not failure and outcome.returncode == 0 and result is not None
                    and result.get("process_closeout_required") is True):
                finalized = supervise(
                    [args.engine, "--finalize", "--state-dir", args.state_dir],
                    cwd=os.getcwd(), env=dict(os.environ),
                    input_bytes=json.dumps({"request": json.loads(request),
                                            "process_scope": outcome.scope}).encode(),
                    timeout=5, cancelled=cancelled, max_stdout=args.max_stdout,
                    max_stderr=args.max_stderr, parent_pid=args.parent_pid,
                )
                scope = {**outcome.scope, "finalization_process_scope": finalized.scope}
                scope["no_live_descendants"] = (
                    outcome.scope["no_live_descendants"] and finalized.scope["no_live_descendants"]
                )
                scope["cleanup_error"] = finalized.scope["cleanup_error"]
                outcome = EngineExit(finalized.stdout, finalized.stderr, finalized.returncode,
                                     scope, finalized.failure)
                failure = finalized.failure
                result = _result(finalized.stdout)
        except (ValueError, UnicodeError):
            result = None
            failure = failure or "GRAPH_ENGINE_RESULT_INVALID_EFFECT_UNSETTLED"
        print(json.dumps({
            "engine_result": result, "exit_code": outcome.returncode,
            "process_scope": outcome.scope, "failure": failure,
            "stdout_sha256": hashlib.sha256(outcome.stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(outcome.stderr).hexdigest(),
        }, ensure_ascii=False, allow_nan=False))
        return 0
    except Exception as exc:
        print(json.dumps({"failure": str(exc), "engine_result": None,
                          "process_scope": {"no_live_descendants": False}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
