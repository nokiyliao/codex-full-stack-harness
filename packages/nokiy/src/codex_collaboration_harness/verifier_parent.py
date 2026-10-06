# SPDX-License-Identifier: MIT
"""Per-run verifier execution owned by the existing unfenced full-core caller."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import threading
import time

from . import embedded_nokiy as caller
from . import graph_process
from . import postimage_context
from . import file_change_evidence
from . import full_core


VERIFICATION_EVIDENCE_SCHEMA = "nokiy_focused_verifier_evidence_v1"


def _verification_targets(workspace: Path, contract: dict) -> list[str] | None:
    """Exact, fully readable write targets only; never select a supported prefix."""
    writes, declared = contract.get("write_scopes"), contract.get("declared_targets")
    reads = contract.get("read_scopes")
    allowed, denied = contract.get("allowed_operations", ()), contract.get("denied_operations", ())
    if (full_core._exact_presentation_path(str(workspace), absolute=True) is None
            or contract.get("repo_root") != str(workspace)
            or not isinstance(writes, list) or not isinstance(declared, list)
            or len(writes) > file_change_evidence.MAX_TARGETS
            or len(declared) > file_change_evidence.MAX_TARGETS
            or any(full_core._exact_presentation_path(name, absolute=False) is None
                   for name in writes + declared)
            or len(set(writes)) != len(writes) or len(set(declared)) != len(declared)
            or set(writes) != set(declared)
            or not isinstance(reads, list) or any(name not in reads for name in writes)
            or not isinstance(allowed, list) or not isinstance(denied, list)
            or not all(op in allowed and op not in denied for op in ("command", "read"))):
        return None
    return sorted(writes)


def _capture_verification_sources(workspace: Path, contract: dict) -> dict | None:
    """Private identities bracket the existing bounded, no-follow postimage reader."""
    names = _verification_targets(workspace, contract)
    if names is None:
        return None
    captured, total = {}, 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                          s.st_ctime_ns, s.st_mode)
    for name in names:
        descriptor = os.open(workspace.anchor, flags)
        directories = []
        try:
            for part in workspace.parts[1:] + tuple(name.split("/"))[:-1]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                info = os.fstat(descriptor)
                directories.append((info.st_dev, info.st_ino))
            leaf = name.split("/")[-1]
            before = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            total += before.st_size
            if not stat.S_ISREG(before.st_mode) or total > file_change_evidence.MAX_FILE_BYTES:
                return None
            postimage = file_change_evidence._postimage(workspace, name)
            after = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            if identity(before) != identity(after):
                return None
            captured[name] = (postimage, identity(after), tuple(directories))
        finally:
            os.close(descriptor)
    return captured


def supervisor_policy(profile: str) -> tuple[str, Path, Path]:
    python = Path(sys.executable).resolve(strict=True)
    script = Path(graph_process.__file__).resolve(strict=True)
    libraries = python.parent.parent / "lib"
    if python.parent.name != "bin" or not libraries.is_dir():
        raise ValueError("VERIFIER_SUPERVISOR_PYTHON_UNSUPPORTED")
    quote = lambda value: json.dumps(str(value), ensure_ascii=True)
    # These are trusted supervisor code/runtime reads, not user-data scope.
    profile += (f"\n(allow process-exec (literal {quote(python)}))\n"
                f"(allow file-read* (literal {quote(python)}) (literal {quote(script)}) "
                f"(subpath {quote(libraries)}))\n"
                "(allow process-info-pidinfo)\n(allow process-info-listpids)\n")
    prefix = python.parent.parent
    if prefix.parent.name == "Versions" and prefix.parent.parent.name == "Python.framework":
        framework = prefix / "Python"
        if not framework.is_file() or framework.resolve(strict=True) != framework:
            raise ValueError("VERIFIER_SUPERVISOR_FRAMEWORK_INVALID")
        profile += f"(allow file-read* (literal {quote(framework)}))\n"
        app = prefix / "Resources/Python.app/Contents/MacOS/Python"
        if not app.is_file() or app.resolve(strict=True) != app:
            raise ValueError("VERIFIER_SUPERVISOR_FRAMEWORK_INVALID")
        profile += f"(allow file-read* (literal {quote(app)}))\n(allow process-exec (literal {quote(app)}))\n"
    return profile, python, script


class ParentVerifier:
    def __init__(self, workspace: Path, contract: dict, router, deadline: float | None, *,
                 validated_dcf_generation: dict | None = None):
        self.workspace, self.contract, self.router = workspace, contract, router
        self.deadline = deadline
        self.binding = contract["authorization_semantic_sha256"]
        self.calls = 0
        self._unproven = set()
        self._emitted_source_postimages = set()
        try:
            self._source_preimages = postimage_context.capture_preimages(
                workspace, contract, validated_dcf_generation=validated_dcf_generation)
        except Exception:
            self._source_preimages = None

    @property
    def cleanup_pass(self):
        return not self._unproven

    def __call__(self, index: int, call_id: str, cancelled: threading.Event) -> dict:
        if cancelled.is_set() or (self.deadline is not None and time.monotonic() >= self.deadline):
            raise ValueError("VERIFIER_PARENT_CANCELLED")
        self.router.verify(code="NOKIY_VERIFIER_ROUTER_DRIFT")
        env = {key: os.environ[key] for key in ("HOME", "CODEX_HOME") if key in os.environ}
        env.update(PATH="/usr/bin:/bin", PYTHONDONTWRITEBYTECODE="1",
                   PYTHONNOUSERSITE="1", LANG="C.UTF-8")
        # This existing binary mode only checks pins and builds the OS policy.
        planned = subprocess.run([str(self.router.path), "focused-verifier-plan"],
            input=json.dumps({"workspace":str(self.workspace),"contract":self.contract,
                              "binding":self.binding,"verifier_index":index}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.workspace, env=env,
            timeout=10 if self.deadline is None else min(10, max(.01, self.deadline-time.monotonic())), check=True)
        if len(planned.stdout) > 131072:
            raise ValueError("VERIFIER_PLAN_TOO_LARGE")
        plan = json.loads(planned.stdout, object_pairs_hook=caller._unique_object,
                          parse_constant=caller._invalid_constant)
        # Freeze the entire planned grant, including pins/import roots, before
        # source capture or subprocess execution can yield to another thread.
        grant = copy.deepcopy(self.contract["verifier_commands"][index])
        if (plan.get("binding") != self.binding or plan.get("verifier_index") != index
                or plan.get("argv") != grant["argv"]
                or plan.get("scratch_root") != grant["scratch_root"]
                or plan.get("timeout_seconds") != grant["timeout_seconds"]
                or ("python_import_roots" in plan) != ("python_import_roots" in grant)
                or plan.get("python_import_roots") != grant.get("python_import_roots")):
            raise ValueError("VERIFIER_PLAN_IDENTITY_MISMATCH")
        profile, python, script = supervisor_policy(plan["profile"])
        timeout = (grant["timeout_seconds"] if self.deadline is None
                   else min(grant["timeout_seconds"], self.deadline-time.monotonic()))
        if cancelled.is_set() or timeout <= 0:
            raise ValueError("VERIFIER_PARENT_CANCELLED")
        env["TMPDIR"] = grant["scratch_root"]
        if "python_import_roots" in grant:
            env["PYTHONPATH"] = os.pathsep.join(grant["python_import_roots"])
        command = ["/usr/bin/sandbox-exec", "-p", profile, str(python), "-B", str(script),
                   "--focused-verifier", "--parent-pid", str(os.getpid()), "--timeout", str(timeout)]
        verification_preimages, verifier_sha256 = None, None
        try:
            if type(index) is int and index >= 0 and isinstance(call_id, str) and call_id:
                verifier_sha256 = caller._canonical_sha256(grant)
                verification_preimages = _capture_verification_sources(self.workspace, self.contract)
        except Exception:
            pass  # Optional proof must not change planning, execution or the raw outcome.
        self.calls += 1
        self._unproven.add(call_id)
        process = subprocess.Popen(command, cwd=self.workspace, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        # The verifier and its cleanup remain bounded even without a worker deadline.
        command_deadline = time.monotonic() + timeout + 20
        if self.deadline is not None:
            command_deadline = min(command_deadline, self.deadline)
        try:
            assert process.stdin and process.stdout and process.stderr
            process.stdin.write(json.dumps({"argv":grant["argv"],"cwd":str(self.workspace)}).encode())
            process.stdin.close()
            output, errors = bytearray(), bytearray()
            stop_at = None
            with selectors.DefaultSelector() as selector:
                for stream, buffer in ((process.stdout, output), (process.stderr, errors)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, buffer)
                while selector.get_map() or process.poll() is None:
                    now = time.monotonic()
                    if stop_at is None and (cancelled.is_set() or now >= command_deadline
                                             or len(output) > 262144 or len(errors) > 65536):
                        process.send_signal(signal.SIGTERM)
                        stop_at = now
                    if stop_at is not None and now-stop_at > 10:
                        raise ValueError("VERIFIER_SUPERVISOR_CLEANUP_UNPROVEN")
                    for key, _ in selector.select(.05):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                        elif len(key.data) < 262145:
                            key.data.extend(data[:262145-len(key.data)])
            if process.wait() != 0 or len(output) > 262144:
                raise ValueError("VERIFIER_SUPERVISOR_FAILED:" + errors[:1024].decode("utf-8", "replace"))
            result = json.loads(output, object_pairs_hook=caller._unique_object,
                                parse_constant=caller._invalid_constant)
            expected = {"success","exit_code","stdout","stderr","process_reaped","process_group_empty","outcome"}
            if (not isinstance(result, dict) or set(result) != expected
                    or type(result["exit_code"]) is not int
                    or any(type(result[key]) is not bool for key in
                           ("success", "process_reaped", "process_group_empty"))
                    or any(not isinstance(result[key], str) for key in ("stdout", "stderr", "outcome"))):
                raise ValueError("VERIFIER_OBSERVATION_INVALID")
            if result["process_reaped"] is True and result["process_group_empty"] is True:
                self._unproven.discard(call_id)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    # A forced supervisor exit is never a proof about its child.
                    self._unproven.add(call_id)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
        # Optional disclosure and dedup happen only after supervisor cleanup and
        # stream finalization have returned successfully. A failed return must not
        # suppress the same evidence on a later successful verifier call.
        if postimage_context.known_success(result) and call_id not in self._unproven:
            try:
                if (verification_preimages is not None and stop_at is None
                        and not cancelled.is_set()
                        and (self.deadline is None or time.monotonic() < self.deadline)
                        and self.contract.get("authorization_semantic_sha256") == self.binding
                        and caller._canonical_sha256(self.contract["verifier_commands"][index]) == verifier_sha256):
                    postimages = _capture_verification_sources(self.workspace, self.contract)
                    if postimages == verification_preimages:
                        result = {**result, "verification_evidence": {
                            "schema_version": VERIFICATION_EVIDENCE_SCHEMA,
                            "authorization_semantic_sha256": self.binding,
                            "verifier_index": index, "verifier_sha256": verifier_sha256,
                            "call_id": call_id,
                            "source_postimages": {name: row[0] for name, row in postimages.items()},
                        }}
            except Exception:
                pass  # Advisory source context below remains separate from this proof.
            try:
                context = postimage_context.project(self.workspace, self.contract,
                    self._source_preimages, result, self._emitted_source_postimages)
                if context is not None:
                    keys = {(context["jspace_semantic_sha256"], row["path"], row["postimage_sha256"])
                            for row in context["files"]}
                    result = {**result, "source_postimages": context}
                    self._emitted_source_postimages.update(keys)
            except Exception:
                pass  # Context failure must never change the raw verifier result.
        return result
