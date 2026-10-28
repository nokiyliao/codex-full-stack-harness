# SPDX-License-Identifier: MIT
"""Parent-executed read-only shell lines for worker actions (J-Space ``parent_shell``).

A writing worker's engine stays inside its signal-only fence, and Seatbelt cannot nest, so no
per-shell profile can be applied inside it. The router therefore sends each shell line over the
inherited verifier channel and this parent runs it under a caller-written profile that the
contract binds by digest: reads anywhere except credential stores, writes only under the action's
parent-shell root and temporary directories, signals only inside its own sandbox. Repository
edits stay apply_patch-only, so file-change evidence keeps a single source.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from . import embedded_nokiy as caller

POLICY_SCHEMA = "nokiy_parent_shell_v1"
CODE = "NOKIY_PARENT_SHELL_INVALID"
MAX_LINE_BYTES = 60_000
MAX_TIMEOUT_SECONDS = 3600
_HEAD_BYTES = 64 * 1024
_TAIL_BYTES = 192 * 1024
_POLICY_KEYS = {"schema_version", "evidence_root", "network", "profile_sha256"}
_CREDENTIAL_DIRECTORIES = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
                           ".config/gcloud", ".config/gh", "Library/Keychains")
_CREDENTIAL_FILES = (".netrc", ".npmrc", ".pypirc", ".git-credentials", ".codex/auth.json")
NOTICE = ("\nParent shell: command_run shell lines of any form (pipelines, heredocs, rg, git, python3, "
          "test runners) run in the parent process under an OS sandbox: reads anywhere except credential "
          "stores, writes only under the shell evidence root and $TMPDIR. Repository files are read-only to "
          "shell lines: edit them with apply_patch. Put shell lines in their own command_run call, separate "
          "from apply_patch and source_read.")


def _sbpl(text: str) -> str:
    return json.dumps(text)


def _fail(message: str, path: str = "artifact_root", reason: str = "invalid_shape"):
    raise caller.EmbeddedNokiyError(CODE, message, issues=[{"path": path, "reason": reason}])


def layout(root: Path) -> tuple[Path, Path, Path]:
    return root / "evidence", root / "scratch", root / "profile.sb"


def shell_root(artifact_root: Path, action: dict) -> Path:
    # artifact_root is shared by a parent's requests; each action gets its own shell root.
    return artifact_root / "parent-shell" / caller._canonical_sha256(action)[:16]


def profile_text(root: Path, workspace: Path) -> str:
    """Signal confinement as in the engine fence, credential reads denied, writes confined.

    Temporary directories stay writable: zsh here-documents (TMPPREFIX) and many tools use /tmp or
    the per-user /var/folders directories regardless of TMPDIR. The workspace is denied after
    them, so a repository under a temporary directory stays read-only too.
    """
    home = Path.home().resolve()
    secrets = " ".join([f"(subpath {_sbpl(str(home / name))})" for name in _CREDENTIAL_DIRECTORIES]
                       + [f"(literal {_sbpl(str(home / name))})" for name in _CREDENTIAL_FILES])
    return "\n".join([
        "(version 1)", "(allow default)",
        "(deny signal) (allow signal (target same-sandbox))",
        f"(deny file-read* {secrets})",
        "(deny file-write*)",
        f"(allow file-write* (subpath {_sbpl(str(root))}) (subpath \"/private/tmp\") "
        '(subpath "/private/var/folders") (literal "/dev/null") '
        '(literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/fd/") (regex #"^/dev/ttys[0-9]+$"))',
        f"(deny file-write* (subpath {_sbpl(str(workspace))}))",
        f"(allow file-write* (subpath {_sbpl(str(root))}))",
    ]) + "\n"


def _write_profile(path: Path, text: str) -> None:
    data = text.encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        # A reused artifact root keeps one profile; a different policy needs a new root.
        if path.is_symlink() or path.read_bytes() != data:
            _fail("artifact root already holds a different parent shell profile", reason="profile_conflict")
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def compile_policy(artifact_root: Path, action: dict, workspace: Path) -> dict:
    """Create the action's shell root and bound profile; return the contract policy."""
    root = shell_root(artifact_root, action)
    for directory in layout(root)[:2]:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            _fail("parent shell directories must be real directories", reason="unsafe_path")
    root = root.resolve()
    evidence, _, profile = layout(root)
    _write_profile(profile, profile_text(root, Path(workspace).resolve()))
    return {"schema_version": POLICY_SCHEMA, "evidence_root": str(evidence), "network": "inherit",
            "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest()}


def verify_policy(contract: dict, artifact_root: Path | None) -> Path:
    """Recheck the bound policy against the request artifact root; return the profile path."""
    policy = contract.get("parent_shell") if isinstance(contract, dict) else None
    if (artifact_root is None or not isinstance(policy, dict) or set(policy) != _POLICY_KEYS
            or policy.get("schema_version") != POLICY_SCHEMA or policy.get("network") != "inherit"
            or not isinstance(policy.get("evidence_root"), str)
            or not {"read", "command"}.issubset(contract.get("allowed_operations") or [])):
        raise caller.EmbeddedNokiyError(CODE, "parent shell policy is not the caller's bound policy")
    evidence = Path(policy["evidence_root"])
    root = evidence.parent
    _, scratch, profile = layout(root)
    if (not evidence.is_absolute() or root.parent != (artifact_root / "parent-shell").resolve()
            or evidence != evidence.resolve() or not evidence.is_dir() or not scratch.is_dir()):
        raise caller.EmbeddedNokiyError(CODE, "parent shell root differs from the request artifact root")
    workspace = contract.get("repo_root")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise caller.EmbeddedNokiyError(CODE, "parent shell policy needs the contract workspace")
    expected = profile_text(root, Path(workspace).resolve()).encode("utf-8")
    if (profile.is_symlink() or not profile.is_file() or profile.read_bytes() != expected
            or hashlib.sha256(expected).hexdigest() != policy.get("profile_sha256")):
        raise caller.EmbeddedNokiyError("NOKIY_PARENT_SHELL_PROFILE_CHANGED",
                                        "parent shell profile is not the bound policy")
    return profile


class _Capture:
    """Head and tail of one output stream; the middle is counted, never silently dropped."""

    def __init__(self):
        self.head, self.tail, self.total = bytearray(), bytearray(), 0

    def feed(self, data: bytes) -> None:
        self.total += len(data)
        room = _HEAD_BYTES - len(self.head)
        if room > 0:
            self.head += data[:room]
            data = data[room:]
        if data:
            self.tail += data
            if len(self.tail) > _TAIL_BYTES:
                del self.tail[:len(self.tail) - _TAIL_BYTES]

    def text(self) -> str:
        omitted = self.total - len(self.head) - len(self.tail)
        body = bytes(self.head)
        if omitted > 0:
            body += f"\n[... {omitted} bytes omitted ...]\n".encode()
        return (body + bytes(self.tail)).decode("utf-8", errors="replace")


def _pump(stream, capture: _Capture) -> None:
    try:
        while chunk := os.read(stream.fileno(), 65536):
            capture.feed(chunk)
    except OSError:
        pass  # The descriptor closed under the reader; what was read is kept.
    finally:
        stream.close()


def _group_empty(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _settle_group(process: subprocess.Popen, grace: float = 2.0) -> bool:
    """Stop what the line left in its process group: TERM, then KILL. True when empty.

    The leader is reaped while polling, so its own zombie never counts as a live member."""
    pgid = process.pid
    for sig in (signal.SIGTERM, signal.SIGKILL):
        process.poll()
        if _group_empty(pgid):
            return True
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if _group_empty(pgid):
                return True
            time.sleep(0.05)
    process.poll()
    return _group_empty(pgid)


def _not_run(code: int, message: str) -> dict:
    return {"success": False, "exit_code": code, "stdout": "", "stderr": message,
            "process_reaped": True, "process_group_empty": True, "outcome": "known"}


def run_line(line: str, *, cwd: Path, env: dict[str, str], profile: Path, timeout: int,
             cancelled: threading.Event) -> dict:
    argv = ["/usr/bin/sandbox-exec", "-f", str(profile), "/bin/zsh", "-c", line]
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, start_new_session=True)
    except OSError as error:
        return _not_run(127, f"PARENT_SHELL_SPAWN_FAILED: {error}")
    stdout, stderr = _Capture(), _Capture()
    readers = [threading.Thread(target=_pump, args=(process.stdout, stdout), daemon=True),
               threading.Thread(target=_pump, args=(process.stderr, stderr), daemon=True)]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    note = None
    while True:
        try:
            code = process.wait(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            if cancelled.is_set():
                note = "PARENT_SHELL_CANCELLED"
            elif time.monotonic() >= deadline:
                note = f"PARENT_SHELL_TIMEOUT: stopped after {timeout} s"
            else:
                continue
            _settle_group(process)
            code = process.wait()
            break
    # Background jobs the line started share its process group; they end with the line.
    empty = _settle_group(process)
    for reader in readers:
        reader.join(5)
    empty = empty and not any(reader.is_alive() for reader in readers)
    error_text = stderr.text()
    if note is not None:
        error_text = (error_text + "\n" if error_text else "") + note
        code = 130 if note == "PARENT_SHELL_CANCELLED" else 124
    elif code < 0:
        code = 128 - code
    return {"success": code == 0, "exit_code": code, "stdout": stdout.text(), "stderr": error_text,
            "process_reaped": True, "process_group_empty": empty, "outcome": "known" if empty else "unknown"}


class ParentShell:
    """Serves admitted shell lines for one run over the inherited verifier channel."""

    def __init__(self, workspace: Path, contract: dict, artifact_root: Path | None):
        self.profile = verify_policy(contract, artifact_root)
        self.policy = contract["parent_shell"]
        self.binding = contract["authorization_semantic_sha256"]
        self.workspace = Path(workspace).resolve()
        self.root = self.profile.parent
        self.evidence, self.scratch, _ = layout(self.root)
        self.calls = 0
        self._broker = None
        self._broker_env: dict[str, str] = {}

    def _process_broker(self) -> dict[str, str]:
        """Seatbelt refuses setuid /bin/ps; serve ps from this unsandboxed parent (as for inspect)."""
        if self._broker is None:
            from .process_broker import ProcessBroker, shell_env
            try:
                self._broker_env = shell_env(self.root, sys.executable, "")
                self._broker = ProcessBroker.for_root(self.root).__enter__()
            except OSError:
                self._broker, self._broker_env = False, {}
        return self._broker_env

    def close(self) -> None:
        broker, self._broker = self._broker, None
        if broker:
            broker.__exit__(None, None, None)

    def _profile_bound(self) -> bool:
        try:
            data = None if self.profile.is_symlink() else self.profile.read_bytes()
        except OSError:
            return False
        return data is not None and hashlib.sha256(data).hexdigest() == self.policy["profile_sha256"]

    def _cwd(self, workdir) -> Path | None:
        if workdir is None:
            return self.workspace
        path = Path(workdir).resolve()
        if not path.is_dir() or not (path.is_relative_to(self.workspace) or path.is_relative_to(self.root)):
            return None
        return path

    def environment(self) -> dict[str, str]:
        temporary, cache = self.scratch / "tmp", self.scratch / "cache"
        for directory in (temporary, cache):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        parts = []
        broker = self._process_broker()
        if broker.get("PATH"):
            parts.append(broker["PATH"].rstrip(":"))
        venv = self.workspace / ".venv" / "bin"
        if venv.is_dir():
            parts.append(str(venv))
        parts += [part for part in os.environ.get("PATH", "").split(":") if part]
        parts += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        values = {
            "PATH": ":".join(dict.fromkeys(parts)), "HOME": str(Path.home()),
            "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
            "SHELL": "/bin/zsh", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "TERM": "dumb",
            "NO_COLOR": "1", "PAGER": "cat", "GIT_PAGER": "cat", "GIT_TERMINAL_PROMPT": "0",
            # Read-only shells must not take git's optional index lock.
            "GIT_OPTIONAL_LOCKS": "0",
            "TMPDIR": str(temporary) + "/", "TMPPREFIX": str(temporary / "zsh"),
            "XDG_CACHE_HOME": str(cache), "PIP_CACHE_DIR": str(cache / "pip"), "UV_CACHE_DIR": str(cache / "uv"),
            "npm_config_cache": str(cache / "npm"), "npm_config_update_notifier": "false",
            "CARGO_TARGET_DIR": str(self.scratch / "cargo-target"),
            # The repository is read-only here: no bytecode or pytest cache writes into it.
            "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "NOKIY_PARENT_SHELL_EVIDENCE": str(self.evidence),
        }
        values.update({key: value for key, value in broker.items() if key != "PATH"})
        return {key: value for key, value in values.items() if value}

    def handle_request(self, shell: dict, call_id: str, cancelled: threading.Event) -> dict:
        self.calls += 1
        cwd = self._cwd(shell.get("workdir"))
        if cwd is None:
            return _not_run(126, "PARENT_SHELL_WORKDIR_INVALID: use an existing directory inside the workspace")
        if not self._profile_bound():
            return _not_run(126, "PARENT_SHELL_PROFILE_CHANGED: the bound sandbox profile is unavailable")
        timeout = max(1, min(int(shell["timeout_seconds"]), MAX_TIMEOUT_SECONDS))
        return run_line(shell["line"], cwd=cwd, env=self.environment(), profile=self.profile,
                        timeout=timeout, cancelled=cancelled)

