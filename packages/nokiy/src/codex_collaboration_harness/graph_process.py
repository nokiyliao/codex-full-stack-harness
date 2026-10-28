# SPDX-License-Identifier: MIT
"""Per-call supervisor within an exclusive kernel-enforced signal scope.

The kernel's same-sandbox signal rule, not a PID/session snapshot, bounds every
signal. A child may change session or process group without escaping this rule.
No daemon, saved PID recovery, or inference about command effects lives here.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar
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


_batch_member: ContextVar[Any] = ContextVar("native_batch_process_member", default=None)


def current_process_is_sandboxed() -> bool:
    """Read kernel membership without claiming a batch member or signal scope."""
    if sys.platform != "darwin":
        return False
    library = ctypes.CDLL("/usr/lib/libsandbox.dylib", use_errno=True)
    library.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    library.sandbox_check.restype = ctypes.c_int
    sandboxed = library.sandbox_check(os.getpid(), None, 0)
    if sandboxed < 0:
        raise ProcessScopeError("GRAPH_SANDBOX_MEMBERSHIP_UNKNOWN")
    return bool(sandboxed)


def inherited_root_parent() -> int | None:
    """Reuse an existing fence only from its otherwise empty command root."""
    member = _batch_member.get()
    if member is not None:
        return member.scope.claim(member)
    if not current_process_is_sandboxed():
        return None
    if threading.current_thread() is not threading.main_thread() or threading.active_count() != 1:
        raise ProcessScopeError("GRAPH_SUPERVISOR_ROOT_THREAD_REQUIRED")
    parent_pid = os.getppid()
    processes = DarwinProcesses()
    processes.verify_isolation(parent_pid)
    if processes.members():
        raise ProcessScopeError("GRAPH_SUPERVISOR_SCOPE_NOT_EXCLUSIVE")
    return parent_pid


@dataclass
class _BatchMember:
    scope: Any
    identity: str
    bound: bool = False
    claimed: bool = False
    started: bool = False
    slot: bool = False
    finished: bool = False


def _cleanup_scope(processes, timeout, killed, poll=lambda: None):
    deadline = time.monotonic() + timeout
    while True:
        poll()
        members = processes.members()
        if not members:
            return killed
        for pid, identity in members.items():
            if processes.kill_member(pid, identity):
                killed.add(identity)
        if time.monotonic() >= deadline:
            raise ProcessScopeError("GRAPH_PROCESS_SCOPE_NOT_EMPTY")
        time.sleep(.025)


class _NativeBatchScope:
    """One command-root fence, with deferred cleanup after every member settles.

    This is invocation-local state, never a persisted grant or a new process.
    Workers release execution slots before awaiting the shared cleanup barrier.
    """

    def __init__(self, parent_pid, identities, parallel):
        self.pid = os.getpid()
        self.parent_pid = parent_pid
        self.condition = threading.Condition()
        self.cancelled = threading.Event()
        self.slots = threading.BoundedSemaphore(parallel)
        self.members = {key: _BatchMember(self, key) for key in identities}
        self.processes = DarwinProcesses()
        self.processes.verify_isolation(parent_pid)
        self.result = None
        self.cleaning = False

    def _check(self, member):
        if (os.getpid() != self.pid or os.getppid() != self.parent_pid
                or self.members.get(member.identity) is not member):
            raise ProcessScopeError("GRAPH_BATCH_SCOPE_IDENTITY_CHANGED")

    def claim(self, member):
        with self.condition:
            self._check(member)
            if not member.bound or member.claimed or member.finished:
                raise ProcessScopeError("GRAPH_BATCH_ROOT_CLAIM_INVALID")
            member.claimed = True
            return self.parent_pid

    @contextmanager
    def member(self, identity, *, cancelled=None):
        with self.condition:
            member = self.members.get(identity)
            if member is None:
                raise ProcessScopeError("GRAPH_BATCH_MEMBER_UNKNOWN")
            self._check(member)
            if member.bound or member.finished:
                raise ProcessScopeError("GRAPH_BATCH_MEMBER_ALREADY_CONSUMED")
            member.bound = True
        token = _batch_member.set(member)
        try:
            if cancelled is not None:
                self._acquire(member, cancelled)
            yield
        except BaseException:
            self.abort()
            raise
        finally:
            self.release(member)
            self.finish(member, wait=False)
            _batch_member.reset(token)

    def enter(self, member, cancelled):
        with self.condition:
            self._check(member)
            if not member.claimed or member.started or member.finished:
                raise ProcessScopeError("GRAPH_BATCH_SUPERVISION_INVALID")
            member.started = True
        self._acquire(member, cancelled.is_set)

    def _acquire(self, member, cancelled):
        while not member.slot and not self.slots.acquire(timeout=.025):
            self._check(member)
            if cancelled() or self.cancelled.is_set():
                raise ProcessScopeError("GRAPH_BATCH_CANCELLED_BEFORE_SPAWN")
        member.slot = True
        self._check(member)
        if cancelled() or self.cancelled.is_set():
            self.release(member)
            raise ProcessScopeError("GRAPH_BATCH_CANCELLED_BEFORE_SPAWN")

    def release(self, member):
        with self.condition:
            if member.slot:
                member.slot = False
                self.slots.release()

    def abort(self):
        with self.condition:
            self.cancelled.set()
            # A submit failure must not strand already running members at the
            # barrier. Unbound jobs cannot acquire a claim after this point.
            for member in self.members.values():
                if not member.bound:
                    member.finished = True
            self.condition.notify_all()

    def finish(self, member, *, wait, failed=False, cleanup_timeout=5):
        with self.condition:
            member.finished = True
            if failed:
                self.abort()
            clean = (not self.cleaning and self.result is None
                     and all(item.finished for item in self.members.values()))
            if clean:
                self.cleaning = True
        if clean:
            killed = set()
            try:
                _cleanup_scope(self.processes, cleanup_timeout, killed)
                result = (killed, True, None)
            except Exception as error:
                result = (killed, False, f"{type(error).__name__}:{error}")
            with self.condition:
                self.result = result
                self.condition.notify_all()
        with self.condition:
            while wait and self.result is None:
                self.condition.wait()
            return self.result

    def close(self):
        with self.condition:
            if not all(member.finished for member in self.members.values()):
                raise ProcessScopeError("GRAPH_BATCH_MEMBERS_UNSETTLED")


@contextmanager
def native_batch_scope(identities: list[str], parallel: int):
    """Admit one batch at the exclusive native root, before starting threads."""
    if (_batch_member.get() is not None or not identities
            or any(not isinstance(key, str) or not key for key in identities)
            or len(set(identities)) != len(identities)
            or type(parallel) is not int or not 1 <= parallel <= len(identities)):
        raise ProcessScopeError("GRAPH_BATCH_SCOPE_INVALID")
    parent_pid = inherited_root_parent()
    if parent_pid is None:
        yield None  # Unsandboxed callers keep their existing per-member fences.
        return
    scope = _NativeBatchScope(parent_pid, identities, parallel)
    try:
        yield scope
    finally:
        scope.close()


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
        # Refuse an unsandboxed supervisor or a child inside its caller's scope.
        # Native command-root reuse additionally requires an initially empty scope.
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


# UTF-8 replacement/control characters cost at most six ASCII JSON bytes per
# retained byte. Reserve room in both 262144-byte envelopes for framing/proof.
_VERIFIER_DIAGNOSTIC_BYTES = 8192


class _DiagnosticCapture:
    """Bounded excerpt plus original bytes on a private parent-drained pipe."""

    def __init__(self, limit: int, descriptor: int | None = None, *,
                 cancelled: threading.Event | None = None):
        self.limit = limit
        self.buffer = bytearray()
        self.total = 0
        self.digest = hashlib.sha256()
        self.descriptor = descriptor
        self.forwarded = descriptor is not None
        self.cancelled = cancelled
        self.eof = False
        if self.forwarded:
            try:
                os.set_blocking(descriptor, False)
            except OSError:
                self.forwarded = False

    def append(self, chunk: bytes, *, forward_timeout: float = .025) -> None:
        self.total += len(chunk)
        self.digest.update(chunk)
        if self.forwarded:
            try:
                remaining = memoryview(chunk)
                stop_at = time.monotonic() + forward_timeout
                while remaining:
                    try:
                        count = os.write(self.descriptor, remaining)
                    except BlockingIOError:
                        delay = min(.001, stop_at - time.monotonic())
                        if delay <= 0 or (self.cancelled is not None and self.cancelled.is_set()):
                            raise
                        # Give an actively draining parent a bounded chance to
                        # catch up; SIGTERM's event interrupts this wait too.
                        if self.cancelled is None:
                            time.sleep(delay)
                        else:
                            self.cancelled.wait(delay)
                        continue
                    if count <= 0:
                        raise OSError("diagnostic pipe write failed")
                    remaining = remaining[count:]
                    if remaining and (time.monotonic() >= stop_at
                            or (self.cancelled is not None and self.cancelled.is_set())):
                        raise OSError("diagnostic pipe forwarding interrupted")
            except OSError:
                # Retention failure is separate from workload/cleanup status.
                self.forwarded = False
        self.buffer.extend(chunk)
        if len(self.buffer) > self.limit:
            head = self.limit // 2
            del self.buffer[head:len(self.buffer) - (self.limit - head)]

    def observation(self) -> dict[str, Any]:
        # This is a producer observation, never a persistence receipt.
        return {"bytes": self.total, "sha256": self.digest.hexdigest(),
                "eof": self.eof, "forwarded": self.forwarded}

    def __bytes__(self) -> bytes:
        if self.total <= self.limit:
            return bytes(self.buffer)
        head = self.limit // 2
        marker = f"\n[... {self.total - self.limit} diagnostic bytes truncated ...]\n".encode("ascii")
        return bytes(self.buffer[:head]) + marker + bytes(self.buffer[head:])


def supervise(
    argv: list[str], *, cwd: str, env: dict[str, str], input_bytes: bytes,
    timeout: float | None, cancelled: threading.Event, max_stdout: int, max_stderr: int,
    parent_pid: int, terminate_grace: float = 10, cleanup_timeout: float = 5,
    pass_fds: tuple[int, ...] = (), diagnostic_output: bool = False,
    diagnostic_fds: tuple[int, ...] = (),
) -> EngineExit:
    """Run one engine within the already applied per-call Seatbelt instance."""
    processes = DarwinProcesses()
    processes.verify_isolation(parent_pid)
    stdout, stderr = ((_DiagnosticCapture(max_stdout, diagnostic_fds[0] if diagnostic_fds else None,
                                         cancelled=cancelled),
                       _DiagnosticCapture(max_stderr, diagnostic_fds[1] if diagnostic_fds else None,
                                          cancelled=cancelled))
                      if diagnostic_output else (bytearray(), bytearray()))
    failure = cleanup_error = None
    killed: set[tuple[int, int, int, int]] = set()
    no_live_descendants = False
    member = _batch_member.get()
    if member is not None:
        member.scope.enter(member, cancelled)
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, pass_fds=pass_fds)
    except BaseException:
        if member is not None:
            member.scope.release(member)
            member.scope.abort()
        raise
    for fd in pass_fds:
        os.close(fd)
    terminating_at: float | None = None
    deadline = None if timeout is None else time.monotonic() + timeout

    def append(buffer: bytearray | _DiagnosticCapture, chunk: bytes, limit: int) -> None:
        nonlocal failure
        if isinstance(buffer, _DiagnosticCapture):
            # Never wait for retention during termination or past the workload's
            # deadline, including the final drain after cleanup/reaping.
            budget = .025 if terminating_at is None else 0
            if deadline is not None:
                budget = min(budget, max(0, deadline - time.monotonic()))
            buffer.append(chunk, forward_timeout=budget)
            return
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
                batch_cancelled = member is not None and member.scope.cancelled.is_set()
                if terminating_at is None and (cancelled.is_set() or batch_cancelled
                        or (deadline is not None and now >= deadline) or failure):
                    failure = failure or ("GRAPH_CANCELLED" if cancelled.is_set()
                                          else "GRAPH_BATCH_PEER_FAILED" if batch_cancelled
                                          else "GRAPH_ENGINE_DEADLINE")
                    if member is not None:
                        member.scope.abort()
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
                            buffer = stdout if key.data == "stdout" else stderr
                            if isinstance(buffer, _DiagnosticCapture):
                                buffer.eof = True
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
            if member is None:
                _cleanup_scope(processes, cleanup_timeout, killed, process.poll)
                no_live_descendants = True
            else:
                # Reap/release before the barrier so queued members can start.
                # No member may sweep the shared fence while a peer is active.
                try:
                    returncode = process.wait(timeout=5)
                finally:
                    member.scope.release(member)
                killed, no_live_descendants, cleanup_error = member.scope.finish(
                    member, wait=True, failed=bool(failure or returncode != 0),
                    cleanup_timeout=cleanup_timeout)
                if member.scope.cancelled.is_set() and returncode == 0:
                    failure = failure or "GRAPH_BATCH_PEER_FAILED"
                if cleanup_error:
                    failure = failure or "GRAPH_PROCESS_CLEANUP_UNPROVEN"
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
                    if isinstance(buffer, _DiagnosticCapture):
                        buffer.eof = True
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
        **({"diagnostic_output": {"stdout": stdout.observation(), "stderr": stderr.observation()}}
           if diagnostic_output else {}),
        **({"cleanup_scope": "whole_native_batch", "batch_member_count": len(member.scope.members),
            "batch_member_id": member.identity} if member is not None else {}),
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
    if sys.argv[1:2] == ["--focused-verifier"]:
        return focused_verifier_main()
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


def focused_verifier_main() -> int:
    """The same scope supervisor with a caller-bound argv, no graph state."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--focused-verifier", action="store_true", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--diagnostic-fds", type=int, nargs=2)
    args = parser.parse_args()
    if not 0 < args.timeout <= 300:
        parser.error("invalid verifier budget")
    cancelled = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda _sig, _frame: cancelled.set())
    diagnostic_fds = tuple(args.diagnostic_fds or ())
    try:
        if diagnostic_fds:
            if len(set(diagnostic_fds)) != 2 or min(diagnostic_fds) < 3:
                raise ValueError("VERIFIER_DIAGNOSTIC_FDS_INVALID")
            for descriptor in diagnostic_fds:
                # Popen below closes these, including across an exec in the workload.
                os.set_inheritable(descriptor, False)
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("VERIFIER_INPUT_LIMIT")
        request = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        expected = {"argv", "cwd"} | ({"diagnostic_identity"} if diagnostic_fds else set())
        if not isinstance(request, dict) or set(request) != expected:
            raise ValueError("VERIFIER_INPUT_INVALID")
        argv = request["argv"]
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or any(not isinstance(arg, str) or not arg or len(arg) > 1024 for arg in argv)
                or not os.path.isabs(argv[0]) or request["cwd"] != os.getcwd()):
            raise ValueError("VERIFIER_ARGV_INVALID")
        result = supervise(argv, cwd=request["cwd"], env=dict(os.environ), input_bytes=b"",
                           timeout=args.timeout, cancelled=cancelled,
                           max_stdout=_VERIFIER_DIAGNOSTIC_BYTES, max_stderr=_VERIFIER_DIAGNOSTIC_BYTES,
                           parent_pid=args.parent_pid, terminate_grace=2, diagnostic_output=True,
                           diagnostic_fds=diagnostic_fds)
        clean = (result.scope.get("engine_reaped") is True
                 and result.scope.get("no_live_descendants") is True
                 and not result.scope.get("cleanup_error"))
        observation = {"success":result.returncode == 0 and not result.failure and clean,
                          "exit_code":result.returncode,
                          "stdout":result.stdout.decode("utf-8", "replace"),
                          "stderr":result.stderr.decode("utf-8", "replace"),
                          "process_reaped":result.scope.get("engine_reaped") is True,
                          "process_group_empty":clean,
                       "outcome":"known" if clean and not result.failure else "unknown"}
        if diagnostic_fds:
            observation["diagnostic_capture"] = {"identity": request["diagnostic_identity"],
                                                 "streams": result.scope.get("diagnostic_output")}
        print(json.dumps(observation, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception as error:
        print(json.dumps({"failure":str(error)}))
        return 2
    finally:
        for descriptor in diagnostic_fds:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
