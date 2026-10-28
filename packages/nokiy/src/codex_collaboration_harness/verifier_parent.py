# SPDX-License-Identifier: MIT
"""Per-run verifier execution owned by the existing unfenced full-core caller."""
from __future__ import annotations

import copy
import hashlib
import json
import logging
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
from . import local_context


VERIFICATION_EVIDENCE_SCHEMA = "nokiy_focused_verifier_evidence_v1"


class _VerifierOutput:
    """One call's parent-owned files; only pipe writers cross the sandbox."""

    @staticmethod
    def _directory(path: Path) -> int:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("VERIFIER_OUTPUT_PATH_REJECTED")
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(path.anchor, flags)
        try:
            for part in path.parts[1:]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def __init__(self, root: Path, scratch: Path, identity: dict):
        self.root, self.identity = root, copy.deepcopy(identity)
        self.directory = None
        self.streams, self.readers, self.writers = {}, {}, []
        path_error = None
        try:
            if root == scratch or root.is_relative_to(scratch):
                raise ValueError("VERIFIER_OUTPUT_PATH_REJECTED")
            self.directory = self._directory(root)
        except (OSError, ValueError):
            path_error = "VERIFIER_OUTPUT_PATH_REJECTED"
        try:
            name = "verifier-" + caller._canonical_sha256(identity)
            for stream in ("stdout", "stderr"):
                row = {"fd": None, "leaf": name + "." + stream + ".bin", "bytes": 0,
                       "digest": hashlib.sha256(), "observed_bytes": 0,
                       "observed_digest": hashlib.sha256(), "eof": False, "error": path_error}
                self.streams[stream] = row
                if self.directory is not None:
                    try:
                        row["fd"] = os.open(row["leaf"], os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                            0o600, dir_fd=self.directory)
                    except OSError:
                        row["error"] = "VERIFIER_OUTPUT_CREATE_FAILED"
                reader, writer = os.pipe()
                self.readers[reader] = stream
                self.writers.append(writer)
        except BaseException:
            self.close()
            raise

    def release_writers(self):
        for descriptor in self.writers:
            os.close(descriptor)
        self.writers.clear()

    def append(self, descriptor: int, data: bytes):
        row = self.streams[self.readers[descriptor]]
        if not data:
            row["eof"] = True
            return
        row["observed_bytes"] += len(data)
        row["observed_digest"].update(data)
        if row["fd"] is None or row["error"]:
            return  # Still drain the producer after a disk failure.
        try:
            remaining = memoryview(data)
            while remaining:
                count = os.write(row["fd"], remaining)
                if count <= 0:
                    raise OSError("diagnostic file write failed")
                row["bytes"] += count
                row["digest"].update(remaining[:count])
                remaining = remaining[count:]
        except OSError:
            row["error"] = "VERIFIER_OUTPUT_WRITE_FAILED"

    def _seal(self, row: dict) -> dict:
        reference = {"path": None, "sha256": None, "bytes": 0, "durable": False,
                     "complete": False, "error": row["error"]}
        if row["fd"] is None:
            return reference
        try:
            os.fsync(row["fd"])
            os.fsync(self.directory)
            # Reopen every component without following links and bind the public
            # path to the directory/file descriptors used for the actual writes.
            current = self._directory(self.root)
            try:
                before = os.fstat(row["fd"])
                same = lambda a, b: (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
                if (not same(os.fstat(current), os.fstat(self.directory))
                        or not same(before, os.stat(row["leaf"], dir_fd=current, follow_symlinks=False))
                        or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
                    raise ValueError("VERIFIER_OUTPUT_PATH_REJECTED")
                digest, size = hashlib.sha256(), 0
                while size < before.st_size:
                    data = os.pread(row["fd"], min(65536, before.st_size - size), size)
                    if not data:
                        break
                    digest.update(data)
                    size += len(data)
                after = os.fstat(row["fd"])
                identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mode,
                                         info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)
                if (identity(before) != identity(after) or size != row["bytes"] or size != after.st_size
                        or digest.hexdigest() != row["digest"].hexdigest()):
                    raise ValueError("VERIFIER_OUTPUT_CONTENT_CHANGED")
                final_directory = self._directory(self.root)
                try:
                    if (not same(os.fstat(current), os.fstat(final_directory))
                            or identity(after) != identity(os.stat(row["leaf"], dir_fd=final_directory,
                                                                   follow_symlinks=False))):
                        raise ValueError("VERIFIER_OUTPUT_PATH_REJECTED")
                finally:
                    os.close(final_directory)
                reference.update(path=str(self.root / row["leaf"]), sha256=digest.hexdigest(),
                                 bytes=size, durable=True)
            finally:
                os.close(current)
        except (OSError, ValueError):
            reference["error"] = reference["error"] or "VERIFIER_OUTPUT_PERSISTENCE_UNPROVEN"
        return reference

    def finish(self, observation) -> dict:
        bound = (isinstance(observation, dict) and observation.get("identity") == self.identity
                 and isinstance(observation.get("streams"), dict))
        references = {}
        for name, row in self.streams.items():
            reference = self._seal(row)
            producer = observation["streams"].get(name) if bound else None
            matches = (isinstance(producer, dict) and type(producer.get("bytes")) is int
                       and producer["bytes"] == row["observed_bytes"]
                       and producer.get("sha256") == row["observed_digest"].hexdigest()
                       and producer.get("eof") is True and producer.get("forwarded") is True
                       and row["eof"])
            reference["complete"] = reference["durable"] and not row["error"] and bool(matches)
            if not reference["complete"] and not reference["error"]:
                reference["error"] = "VERIFIER_OUTPUT_CAPTURE_INCOMPLETE"
            references[name] = reference
        return {"schema_version": "nokiy_verifier_output_v1", **self.identity, "streams": references}

    def close(self):
        self.release_writers()
        for descriptor in self.readers:
            os.close(descriptor)
        self.readers.clear()
        for row in self.streams.values():
            if row["fd"] is not None:
                os.close(row["fd"])
                row["fd"] = None
        if self.directory is not None:
            os.close(self.directory)
            self.directory = None


def _admitted_absent_creation(contract: dict, name: str) -> bool:
    generation = contract.get("dcf_generation")
    snapshot = generation.get("source_snapshot") if isinstance(generation, dict) else None
    return (isinstance(snapshot, dict) and name in snapshot and snapshot[name] is None
            and name in contract.get("write_scopes", ())
            and name in contract.get("declared_targets", ())
            and "create" in contract.get("allowed_operations", ())
            and "create" not in contract.get("denied_operations", ()))


def _verification_targets(workspace: Path, contract: dict, *, source_targets=None) -> list[str] | None:
    """Exact readable subjects; an explicit subject set grants no source writes."""
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
            or not isinstance(reads, list)
            or not isinstance(allowed, list) or not isinstance(denied, list)
            or not all(op in allowed and op not in denied for op in ("command", "read"))):
        return None
    subjects = writes if source_targets is None else source_targets
    if (not isinstance(subjects, (list, tuple))
            or len(subjects) > file_change_evidence.MAX_TARGETS
            or any(full_core._exact_presentation_path(name, absolute=False) is None for name in subjects)
            or len(set(subjects)) != len(subjects)):
        return None
    directory_reads = [name for name in subjects if name not in reads]
    if directory_reads:
        policy = full_core._directory_read_presentation(contract)
        generation = contract.get("dcf_generation")
        snapshot = generation.get("directory_snapshot") if isinstance(generation, dict) else None
        if (policy is None or not isinstance(snapshot, dict)
                or any(any(part.startswith(".") for part in name.split("/"))
                       or not any(name.startswith(root + "/") for root in policy["roots"])
                       for name in directory_reads)):
            return None
        try:
            # Reuse the admitted directory identities and no-symlink path resolver.
            # This binds named targets only; it does not expand the read/write grant.
            if local_context._directories(workspace, policy["roots"]) != snapshot:
                return None
            if source_targets is None:
                for name in directory_reads:
                    local_context._file(workspace, name)
        except (caller.EmbeddedNokiyError, OSError, ValueError):
            return None
    return sorted(subjects)


def _capture_verification_sources(workspace: Path, contract: dict, *, source_targets=None) -> dict | None:
    """Private identities bracket the existing bounded, no-follow postimage reader."""
    names = _verification_targets(workspace, contract, source_targets=source_targets)
    if names is None:
        return None
    captured, total = {}, 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                          s.st_ctime_ns, s.st_mode)
    directory_bindings = ({workspace / root: row for root, row in
                           contract["dcf_generation"]["directory_snapshot"].items()}
                          if any(name not in contract["read_scopes"] for name in names) else {})
    for name in names:
        descriptor = os.open(workspace.anchor, flags)
        directories = []
        try:
            directory_path = Path(workspace.anchor)
            for part in workspace.parts[1:] + tuple(name.split("/"))[:-1]:
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                directory_path /= part
                info = os.fstat(descriptor)
                directories.append((info.st_dev, info.st_ino))
                if name not in contract["read_scopes"] and directory_path in directory_bindings:
                    if directory_bindings[directory_path] != {
                            "device": info.st_dev, "inode": info.st_ino,
                            "mode": stat.S_IMODE(info.st_mode)}:
                        return None
            leaf = name.split("/")[-1]
            parent_before = os.fstat(descriptor)
            try:
                before = os.stat(leaf, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                if source_targets is None and not _admitted_absent_creation(contract, name):
                    return None
                parent_after = os.fstat(descriptor)
                if identity(parent_before) != identity(parent_after):
                    return None
                # Explicit readable subjects may prove absence without a create grant.
                # Implicit mutation subjects still require admitted creation.
                # Parent metadata also fences create/remove ABA during verification.
                captured[name] = (None, identity(parent_after), tuple(directories))
                continue
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
                 validated_dcf_generation: dict | None = None, verification_source_targets=None,
                 diagnostic_root: Path | None = None, request_id: str | None = None,
                 request_sha256: str | None = None):
        self.workspace, self.contract, self.router = workspace, contract, router
        self.deadline = deadline
        self._diagnostic_root = diagnostic_root
        self._request_id, self._request_sha256 = request_id, request_sha256
        if diagnostic_root is not None and (not request_id or not request_sha256):
            raise ValueError("VERIFIER_OUTPUT_REQUEST_IDENTITY_REQUIRED")
        self.binding = contract["authorization_semantic_sha256"]
        self.calls = 0
        self._unproven = set()
        self._emitted_source_postimages = set()
        self._verification_source_targets = copy.deepcopy(verification_source_targets)
        try:
            self._source_preimages = postimage_context.capture_preimages(
                workspace, contract, validated_dcf_generation=validated_dcf_generation)
        except Exception:
            self._source_preimages = None

    @property
    def cleanup_pass(self):
        return not self._unproven

    def handle_request(self, index: int, call_id: str, cancelled: threading.Event) -> dict:
        """Keep a proven preparation rejection on the existing result channel.

        __call__ increments calls and marks cleanup unproven BEFORE Popen. Never
        convert an exception once that dispatch boundary has been reached, even
        if Popen itself failed or no child PID was observed. The channel still
        owns authentication, single-use call claims and reply publication.
        """
        grant_sha256 = caller._canonical_sha256(self.contract["verifier_commands"][index])
        dispatched = self.calls
        try:
            return self(index, call_id, cancelled)
        except (OSError, ValueError, subprocess.SubprocessError, caller.EmbeddedNokiyError) as error:
            if (self.calls != dispatched or call_id in self._unproven or cancelled.is_set()
                    or self.contract.get("authorization_semantic_sha256") != self.binding
                    or caller._canonical_sha256(self.contract["verifier_commands"][index]) != grant_sha256):
                raise  # Dispatch, identity or publication continuity is unproven.
            code = "VERIFIER_PREPARATION_FAILED"
            diagnostics = {}
            if isinstance(error, (subprocess.CalledProcessError, subprocess.TimeoutExpired)):
                code = ("VERIFIER_PLAN_COMMAND_REJECTED" if isinstance(error, subprocess.CalledProcessError)
                        else "VERIFIER_PLAN_COMMAND_TIMEOUT")
                diagnostics["stage"] = "focused-verifier-plan"
                if isinstance(error, subprocess.CalledProcessError):
                    diagnostics["returncode"] = error.returncode
                for name, raw in (("stdout", error.output), ("stderr", error.stderr)):
                    raw = raw.encode("utf-8") if isinstance(raw, str) else raw
                    raw = raw if isinstance(raw, bytes) else b""
                    diagnostics[name + "_bytes"] = len(raw)
                    diagnostics[name + "_sha256"] = hashlib.sha256(raw).hexdigest()
                    if name == "stderr":
                        # Only the existing typed-code vocabulary is public;
                        # never include paths, messages, environment or raw output.
                        prefix = raw[:256].removeprefix(b"Error: ").partition(b":")[0].strip()
                        child_code = prefix.decode("ascii", "replace")
                        if caller._PREPARATION_CHILD_CODE.fullmatch(child_code):
                            diagnostics["child_error_code"] = child_code
            elif isinstance(error, (json.JSONDecodeError, UnicodeError)):
                code = "VERIFIER_PLAN_INVALID"
            elif type(error) is ValueError and str(error) in {
                    "VERIFIER_PLAN_TOO_LARGE", "VERIFIER_PLAN_INVALID", "VERIFIER_PLAN_IDENTITY_MISMATCH",
                    "VERIFIER_PARENT_CANCELLED", "VERIFIER_NODE_PATH_INVALID", "VERIFIER_SUPERVISOR_PYTHON_UNSUPPORTED",
                    "VERIFIER_SUPERVISOR_FRAMEWORK_INVALID"}:
                code = str(error)
            elif isinstance(error, caller.EmbeddedNokiyError) and error.code == "NOKIY_VERIFIER_ROUTER_DRIFT":
                code = error.code
            rejection = {"error_type": "VERIFIER_PREPARATION_REJECTED", "message": code,
                         "authorization_semantic_sha256": self.binding, "verifier_index": index,
                         "verifier_sha256": grant_sha256, "call_id": call_id, "process_started": False,
                         "retry_safe": False, "auto_retry_allowed": False, "diagnostics": diagnostics}
            # Use the existing seven-field result envelope, with safe error JSON
            # in stderr. A negative non-workload exit cannot enter red/green
            # resolution. Receipt durability is still the router's responsibility;
            # losing this reply remains UNKNOWN, never a no-start inference.
            return {"success": False, "exit_code": -1, "stdout": "",
                    "stderr": json.dumps(rejection, sort_keys=True, separators=(",", ":")),
                    "process_reaped": True, "process_group_empty": True, "outcome": "known"}

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
        if not isinstance(plan, dict):
            raise ValueError("VERIFIER_PLAN_INVALID")
        # Freeze the entire planned grant, including pins/import roots, before
        # source capture or subprocess execution can yield to another thread.
        grant = copy.deepcopy(self.contract["verifier_commands"][index])
        if (plan.get("binding") != self.binding or type(plan.get("verifier_index")) is not int
                or plan.get("verifier_index") != index
                or plan.get("argv") != grant["argv"]
                or plan.get("scratch_root") != grant["scratch_root"]
                or plan.get("timeout_seconds") != grant["timeout_seconds"]
                or ("python_import_roots" in plan) != ("python_import_roots" in grant)
                or plan.get("python_import_roots") != grant.get("python_import_roots")
                or ("cwd" in plan) != ("cwd" in grant) or plan.get("cwd") != grant.get("cwd")):
            raise ValueError("VERIFIER_PLAN_IDENTITY_MISMATCH")
        if not isinstance(plan.get("profile"), str) or not plan["profile"]:
            raise ValueError("VERIFIER_PLAN_INVALID")
        # Only the frozen, plan-bound external Node grant supplies a child search path.
        argv = grant["argv"]
        if (Path(argv[0]).name == "node" and len(argv) > 1
                and Path(argv[1]).suffix in {".js", ".cjs", ".mjs"}
                and any(entry.get("scope") == "verifier_only" and entry["path"] == argv[1]
                        and not Path(entry["path"]).is_relative_to(self.workspace)
                        for entry in grant.get("pinned_files", []))):
            node_directory = str(Path(argv[0]).parent)
            if os.pathsep in node_directory:
                raise ValueError("VERIFIER_NODE_PATH_INVALID")
            env["PATH"] = os.pathsep.join((node_directory, env["PATH"]))
        # The contract's pinned discovery executables supply their own directories as well: code
        # under test may look them up by name. J-Space directory capabilities call shutil.which("rg"),
        # and without it a final gate failed eight tests the parent environment passes (2026-10-26).
        # Only a pin whose bytes still match joins; any other pin leaves the minimal search path.
        for tool in ("rg", "cat"):
            pinned = (self.contract.get("read_commands") or {}).get(tool)
            if not isinstance(pinned, dict):
                continue
            path = Path(str(pinned.get("path")))
            directory = str(path.parent)
            if (not path.is_absolute() or os.pathsep in directory or not path.is_file()
                    or caller._file_sha256(path) != pinned.get("sha256")):
                continue
            if directory not in env["PATH"].split(os.pathsep):
                env["PATH"] = os.pathsep.join((directory, env["PATH"]))
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
        source_changes = []
        proof_omission = "PREIMAGE_UNAVAILABLE"
        try:
            if type(index) is int and index >= 0 and isinstance(call_id, str) and call_id:
                verifier_sha256 = caller._canonical_sha256(grant)
                verification_preimages = _capture_verification_sources(self.workspace, self.contract,
                    source_targets=self._verification_source_targets)
        except Exception:
            proof_omission = "PREIMAGE_CAPTURE_ERROR"
        capture = None
        if self._diagnostic_root is not None:
            capture = _VerifierOutput(self._diagnostic_root, Path(grant["scratch_root"]), {
                "request_id": self._request_id, "request_sha256": self._request_sha256,
                "authorization_semantic_sha256": self.binding, "verifier_index": index,
                "verifier_sha256": caller._canonical_sha256(grant), "call_id": call_id})
            command += ["--diagnostic-fds", *(str(fd) for fd in capture.writers)]
        self.calls += 1
        self._unproven.add(call_id)
        # V318: a granted cwd moves only the starting directory; the OS profile is unchanged.
        start = grant.get("cwd", str(self.workspace))
        try:
            process = subprocess.Popen(command, cwd=start, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                       **({"pass_fds": tuple(capture.writers)} if capture else {}))
        except BaseException:
            if capture:
                capture.close()
            raise
        # The verifier and its cleanup remain bounded even without a worker deadline.
        command_deadline = time.monotonic() + timeout + 20
        if self.deadline is not None:
            command_deadline = min(command_deadline, self.deadline)
        try:
            if capture:
                capture.release_writers()
            assert process.stdin and process.stdout and process.stderr
            process.stdin.write(json.dumps({"argv":grant["argv"],"cwd":start,
                **({"diagnostic_identity": capture.identity} if capture else {})}).encode())
            process.stdin.close()
            output, errors = bytearray(), bytearray()
            stop_at = None
            with selectors.DefaultSelector() as selector:
                for stream, buffer in ((process.stdout, output), (process.stderr, errors)):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, buffer)
                if capture:
                    for descriptor in capture.readers:
                        os.set_blocking(descriptor, False)
                        selector.register(descriptor, selectors.EVENT_READ, descriptor)
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
                        if isinstance(key.data, int):
                            capture.append(key.fd, data)
                            if not data:
                                selector.unregister(key.fileobj)
                            continue
                        if not data:
                            selector.unregister(key.fileobj)
                        elif len(key.data) < 262145:
                            key.data.extend(data[:262145-len(key.data)])
            if process.wait() != 0 or len(output) > 262144:
                raise ValueError("VERIFIER_SUPERVISOR_FAILED:" + errors[:1024].decode("utf-8", "replace"))
            result = json.loads(output, object_pairs_hook=caller._unique_object,
                                parse_constant=caller._invalid_constant)
            expected = {"success","exit_code","stdout","stderr","process_reaped","process_group_empty","outcome"}
            if (not isinstance(result, dict)
                    or set(result) not in (expected, expected | ({"diagnostic_capture"} if capture else set()))
                    or type(result["exit_code"]) is not int
                    or any(type(result[key]) is not bool for key in
                           ("success", "process_reaped", "process_group_empty"))
                    or any(not isinstance(result[key], str) for key in ("stdout", "stderr", "outcome"))):
                raise ValueError("VERIFIER_OBSERVATION_INVALID")
            if result["process_reaped"] is True and result["process_group_empty"] is True:
                self._unproven.discard(call_id)
            if capture:
                result["verifier_output"] = capture.finish(result.pop("diagnostic_capture", None))
        finally:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                        # A forced supervisor exit is never a proof about its child.
                        self._unproven.add(call_id)
            finally:
                try:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream and not stream.closed:
                            stream.close()
                finally:
                    if capture:
                        capture.close()
        # Optional disclosure and dedup happen only after supervisor cleanup and
        # stream finalization have returned successfully. A failed return must not
        # suppress the same evidence on a later successful verifier call.
        if postimage_context.known_success(result) and call_id not in self._unproven:
            try:
                if verification_preimages is not None:
                    proof_omission = "LIFECYCLE_OR_GRANT_CHANGED"
                if (verification_preimages is not None and stop_at is None
                        and not cancelled.is_set()
                        and (self.deadline is None or time.monotonic() < self.deadline)
                        and self.contract.get("authorization_semantic_sha256") == self.binding
                        and caller._canonical_sha256(self.contract["verifier_commands"][index]) == verifier_sha256):
                    postimages = _capture_verification_sources(self.workspace, self.contract,
                        source_targets=self._verification_source_targets)
                    proof_omission = ("POSTIMAGE_UNAVAILABLE" if postimages is None
                                     else "SOURCE_IDENTITY_CHANGED")
                    if postimages is not None:
                        for name in sorted(verification_preimages.keys() | postimages.keys()):
                            before, after = verification_preimages.get(name), postimages.get(name)
                            if before == after:
                                continue
                            kind = ("TARGET_SET_CHANGED" if before is None or after is None
                                    else "SOURCE_POSTIMAGE_CHANGED" if before[0] != after[0]
                                    else "ANCESTOR_IDENTITY_CHANGED" if before[2] != after[2]
                                    else "ABSENCE_PARENT_METADATA_CHANGED" if before[0] is None
                                    else "SOURCE_METADATA_CHANGED")
                            source_changes.append({"path": name, "kind": kind})
                    if postimages == verification_preimages:
                        result = {**result, "verification_evidence": {
                            "schema_version": VERIFICATION_EVIDENCE_SCHEMA,
                            "authorization_semantic_sha256": self.binding,
                            "verifier_index": index, "verifier_sha256": verifier_sha256,
                            "call_id": call_id,
                            "source_postimages": {name: row[0] for name, row in postimages.items()},
                        }}
            except Exception:
                proof_omission = "POSTIMAGE_CAPTURE_ERROR"
            if "verification_evidence" not in result:
                logging.getLogger(__name__).warning(
                    "VERIFICATION_EVIDENCE_OMITTED %s", json.dumps({
                        "reason": proof_omission, "call_id": call_id,
                        "verifier_index": index, "request_id": self._request_id,
                        "source_changes": source_changes,
                    }, ensure_ascii=True))
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
