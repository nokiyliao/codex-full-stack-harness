# SPDX-License-Identifier: MIT
"""One project process for exact DCF locators and canonical J-Space compilation."""
from __future__ import annotations

import json
from contextlib import ExitStack, closing
import hashlib
import importlib
import os
from pathlib import Path
import re
import sqlite3
import sys

TARGET = re.compile(r"symbol:[A-Za-z_][A-Za-z0-9_.]{0,254}")
PATH = re.compile(r"[A-Za-z0-9_./-]+")
MAX_BYTES = 262144
# This bridge is also run as a standalone script in the project interpreter.
_PREPARATION_CHILD_CODE = re.compile(r"(?:JSPACE|TASK_CONTEXT|MAPPED_SOURCE_READ)_[A-Z][A-Z0-9_]{0,95}")
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh", "ksh", "env", "osascript"})
# Only a shell gives these meaning; argv is exec'd directly, so they mean shell was written.
_SHELL_SYNTAX = frozenset(";&|<>$\x60\\\"'\r\n\x00")
# Nothing expands these without a shell: in an operand they are data the program reads itself
# (unittest -p test_*.py, a pytest node ID test_x.py::t[a]). Only the program path refuses them.
_GLOB_SYNTAX = frozenset("*?[]{}()")
_MACHO = frozenset(bytes.fromhex(value) for value in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca"))
_PIN_FIELDS = ({"path", "sha256"}, {"path", "sha256", "scope"})
_COMMAND_FIELDS = {"argv", "executable_sha256", "pinned_files", "timeout_seconds", "scratch_root", "network"}
# A focused verifier's own kill bound. It was 300 s until 2026-10-26, when a worker's UTM suite
# ran past it, exited -15 with no summary and turned the run into a capability gap the parent
# redid natively; the grant still names the actual bound, now up to one hour (UTM J-Space alike).
VERIFIER_TIMEOUT_MAX_SECONDS = 3600
_CREDENTIAL_NAMES = frozenset({".git", ".ssh", ".gnupg", ".aws", ".docker", ".netrc"})
_CREDENTIAL_SUFFIXES = (Path(".config/gh"), Path("Library/Keychains"), Path(".codex/auth.json"))


def _credential_path(path):
    return any(node.name in _CREDENTIAL_NAMES or any(
        node.parts[-len(suffix.parts):] == suffix.parts for suffix in _CREDENTIAL_SUFFIXES)
        for node in (path, *path.parents))


def interpreter_reads_entries(executable_name):
    """CPython and Node open their entry operands as program or test source and treat a leading
    #! line as a comment; neither dispatches on it. Such a header is data, not an executable."""
    return executable_name == "node" or re.fullmatch(r"python[0-9.]*", executable_name) is not None


def argv_shell_syntax(argv):
    """Mirror of the runtime J-Space rule (V430): shell syntax anywhere, glob syntax in the program."""
    return any(any(char in _SHELL_SYNTAX for char in arg) for arg in argv) or any(
        char in _GLOB_SYNTAX for char in argv[0])


def _cpython_inline_code(argv):
    """CPython runs inline code only for a -c among its own options, which end at -m (the module
    owns every later operand), --, - or the script: python -m pytest -c pytest.ini is pytest's."""
    args = iter(argv[1:])
    for arg in args:
        if arg == "--check-hash-based-pycs":
            next(args, None)
            continue
        if arg in {"--", "-"} or not arg.startswith("-"):
            return False
        if arg.startswith("--"):
            continue
        flags = arg[1:]
        for offset, flag in enumerate(flags):
            if flag == "c":
                return True
            if flag == "m":
                return False
            if flag in "WX":
                # The option value is the rest of this token, or the next token.
                if offset + 1 == len(flags):
                    next(args, None)
                break
    return False


def inline_interpreter_code(executable_name, argv):
    """Mirror of the runtime J-Space rule (V430) for inline code in a verifier program."""
    if re.fullmatch(r"python[0-9.]*", executable_name):
        return _cpython_inline_code(argv)
    return executable_name.startswith(("python", "node", "ruby", "perl", "php")) and any(
        arg in {"-c", "-e", "--eval", "--execute"} for arg in argv[1:])


def read_scope_covers(relative, reads):
    """An exact read scope or a recursive dir/** scope admits the workspace file."""
    return relative in reads or any(
        isinstance(scope, str) and scope.endswith("/**") and relative.startswith(scope[:-2])
        for scope in reads)


def _module_test_runner(argv):
    """A CPython running unittest or pytest by module, with only -B before -m."""
    if not argv or not re.fullmatch(r"python[0-9.]+", Path(argv[0]).name) or "-m" not in argv[1:]:
        return False
    index = argv.index("-m", 1)
    return all(flag == "-B" for flag in argv[1:index]) and argv[index + 1:index + 2] in (["unittest"], ["pytest"])


def work_product_only(argv, workspace, write_scopes):
    """An operand this action writes is its work product: its bytes change by design, so it
    cannot be pinned. A verifier of work products, with no existing unwritten file operand inside
    or outside the workspace, has nothing to pin; its program is the pinned executable itself."""
    scopes = [scope for scope in write_scopes or () if isinstance(scope, str)]

    def written(path):
        if not path.is_relative_to(workspace):
            return False
        relative = path.relative_to(workspace).as_posix()
        return any(scope == relative or (scope.endswith("/**") and relative.startswith(scope[:-2]))
                   for scope in scopes)

    operands = [Path(arg) for arg in argv[1:] if isinstance(arg, str) and arg.startswith("/")]
    return any(written(path) for path in operands) and not any(
        path.is_file() and not written(path) for path in operands)


def runtime_verifier_grants(error_type):
    """The installed Nokiy runtime's verifier grammar, for any workspace DCF generation.

    DCF semantics (surfaces, scopes, generations) resolve inside the selected project. The
    verifier grant is not DCF semantics: it is the runtime's execution contract, which a
    project's DCF only mirrors, and a worktree's copy freezes when the worktree is created.
    This grammar ships with the runtime it mirrors (tura_path jspace verifier admission), so a
    worktree's DCF age never rejects a grant the runtime admits."""
    code = "JSPACE_VERIFIER_GRANT_INVALID"

    def reject(message):
        raise error_type(code, message, blocked=True)

    def canonical(raw, *, directory):
        if not isinstance(raw, str) or not raw or any(c in raw for c in "\x00\r\n"):
            reject("verifier paths must be absolute canonical paths")
        path = Path(raw)
        try:
            if (not path.is_absolute() or str(path) != raw
                    or any(node.is_symlink() for node in (path, *path.parents))
                    or path.resolve(strict=True) != path
                    or (not path.is_dir() if directory else not path.is_file())):
                reject("verifier paths cannot be missing, nonregular or symlinked")
        except OSError as error:
            raise error_type(code, "verifier path unavailable", blocked=True) from error
        return path

    def pinned(raw, digest):
        path = canonical(raw, directory=False)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            reject("verifier pins must be lowercase SHA-256")
        try:
            with path.open("rb") as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as error:
            raise error_type(code, "verifier pin unavailable", blocked=True) from error
        if actual != digest:
            reject("verifier pinned bytes changed")
        return path

    def import_roots(roots, argv):
        # Search-path context, not grants: PYTHONPATH for a CPython that keeps it.
        if not isinstance(roots, list) or not 1 <= len(roots) <= 4:
            reject("python_import_roots requires 1..4 directories")
        if not re.fullmatch(r"python[0-9.]*", Path(argv[0]).name):
            reject("python_import_roots require a CPython-named executable")
        for arg in argv[1:]:
            if arg.startswith("-") and not arg.startswith("--"):
                if {"I", "E"} & set(re.split(r"[WXmc]", arg[1:], maxsplit=1)[0]):
                    reject("python_import_roots cannot be combined with -I or -E")
        if any(not isinstance(root, str) or not root or ":" in root or len(root) > 1024
               or ".git" in Path(root).parts for root in roots) or len(set(roots)) != len(roots):
            reject("python_import_roots require distinct bounded absolute directories outside Git metadata")
        for root in roots:
            canonical(root, directory=True)

    def validate(payload):
        if not {"verifier_commands", "verifier_artifact_root"} & payload.keys():
            return
        workspace = canonical(payload.get("repo_root"), directory=True)
        artifacts = canonical(payload.get("verifier_artifact_root"), directory=True)
        allowed, denied = payload.get("allowed_operations", []), payload.get("denied_operations", [])
        if not {"read", "command"}.issubset(allowed) or {"read", "command"} & set(denied):
            reject("verifier requires admitted read and command operations")
        if "network" not in denied:
            reject("verifier requires denied network operations")
        reads = payload.get("read_scopes", [])
        commands = payload.get("verifier_commands")
        if not isinstance(commands, list) or not 1 <= len(commands) <= 8:
            reject("verifier requires 1..8 exact grants")
        seen_argv, scratches, external, node_programs = set(), [], [], []
        declared_scratch = [Path(c["scratch_root"]) for c in commands
                            if isinstance(c, dict) and isinstance(c.get("scratch_root"), str)]
        for command in commands:
            if not isinstance(command, dict) or set(command) - {"python_import_roots", "cwd"} != _COMMAND_FIELDS:
                reject("verifier fields differ from native typed grant")
            argv = command["argv"]
            if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                    or any(not isinstance(arg, str) or not arg or len(arg) > 1024
                           for arg in argv)
                    or argv_shell_syntax(argv)
                    or tuple(argv) in seen_argv):
                reject("verifier argv must be unique, exact and shell-free")
            seen_argv.add(tuple(argv))
            executable = pinned(argv[0], command["executable_sha256"])
            if (executable.name in _SHELLS or not executable.stat().st_mode & 0o111
                    or inline_interpreter_code(executable.name, argv)):
                reject("verifier cannot execute shells or inline interpreter code")
            with executable.open("rb") as stream:
                if stream.read(4) not in _MACHO:
                    reject("verifier requires a native Mach-O executable")
            files = command["pinned_files"]
            unpinned = _module_test_runner(argv) or work_product_only(
                argv, workspace, payload.get("write_scopes", []))
            if not isinstance(files, list) or not (0 if unpinned else 1) <= len(files) <= 8:
                reject("verifier requires 1..8 immutable entry files (0..8 for python -m unittest|pytest)")
            if "python_import_roots" in command:
                import_roots(command["python_import_roots"], argv)
            if "cwd" in command:
                cwd = command["cwd"]
                if not isinstance(cwd, str) or ":" in cwd or len(cwd) > 1024 or ".git" in Path(cwd).parts:
                    reject("verifier cwd requires a bounded absolute directory outside Git metadata")
                canonical(cwd, directory=True)
            header_is_data = (interpreter_reads_entries(executable.name)
                              and not any(executable.is_relative_to(s) for s in declared_scratch))
            seen_files = set()
            for entry in files:
                if (not isinstance(entry, dict) or set(entry) not in _PIN_FIELDS
                        or entry.get("scope", "verifier_only") != "verifier_only"):
                    reject("verifier entry fields invalid")
                path = pinned(entry["path"], entry["sha256"])
                occurrences = argv.count(str(path))
                if "scope" in entry:
                    # The runtime reads a verifier_only pin outside every source grant.
                    if path.is_relative_to(workspace) or ".git" in path.parts or occurrences != 1:
                        reject("verifier_only entries must be outside workspace and Git metadata, once in argv")
                    external.append(path)
                    if executable.name == "node":
                        # Node runs the pinned program directly: no flags, lookup or other entry first.
                        if (argv[1:2] != [str(path)] or path.suffix not in {".js", ".cjs", ".mjs"}
                                or _credential_path(path) or _credential_path(executable)):
                            reject("verifier_only Node entry requires a direct JavaScript program")
                        node_programs.append(executable)
                elif (not path.is_relative_to(workspace)
                        or not read_scope_covers(path.relative_to(workspace).as_posix(), reads)
                        or occurrences < 1):
                    reject("verifier entries must be exact argv operands in read scope")
                if path in seen_files:
                    reject("verifier entries must be unique")
                if not header_is_data:
                    # Another program may exec its operand, so the kernel would honor the header.
                    with path.open("rb") as stream:
                        header = stream.readline(256)
                    words = header[2:].decode("ascii", errors="ignore").split() if header.startswith(b"#!") else []
                    if words and Path(words[0]).name in _SHELLS:
                        reject("verifier entry cannot be a shell or env script")
                seen_files.add(path)
            scratch = canonical(command["scratch_root"], directory=True)
            if (scratch == artifacts or not scratch.is_relative_to(artifacts)
                    or scratch.is_relative_to(workspace) or workspace.is_relative_to(scratch)
                    or scratch in scratches):
                reject("verifier scratch must be unique, inside artifacts and outside workspace")
            scratches.append(scratch)
            if (type(command["timeout_seconds"]) is not int
                    or not 1 <= command["timeout_seconds"] <= VERIFIER_TIMEOUT_MAX_SECONDS
                    or command["network"] is not False):
                reject("verifier timeout or network policy invalid")
        # Every verifier writes its scratch root, so no external pin may live under one.
        if any(path.is_relative_to(scratch) for path in external for scratch in scratches):
            reject("verifier_only entry overlaps a verifier scratch root")
        if any(node.is_relative_to(scratch) for node in node_programs for scratch in scratches):
            reject("verifier_only Node entry requires a direct JavaScript program")

    return validate


def install_verifier_grammar(jspace=None):
    """Bind the runtime's verifier grammar into the project's J-Space module for this process."""
    if jspace is None:
        jspace = importlib.import_module("scripts.ops.dcf.jspace")
    jspace._validate_verifier_grants = runtime_verifier_grants(jspace.JSpaceCompileError)
    return jspace


def operator_writable(requested, root):
    """An exact repo file the operator's Nokiy authority may write: no glob, traversal, Git
    metadata or symlinked ancestor. Lanes and leases still govern ownership and adoption."""
    path = Path(requested)
    if (not isinstance(requested, str) or not requested or requested != requested.strip()
            or path.is_absolute() or path.as_posix() != requested or requested.endswith("/")
            or any(part in {"", ".", "..", ".git"} for part in requested.split("/"))
            or any(char in requested for char in "*?[]{}\\:\x00\r\n")):
        return False
    node = Path(root)
    for part in path.parts[:-1]:
        node = node / part
        if node.is_symlink() or (node.exists() and not node.is_dir()):
            return False
    target = Path(root) / path
    return not target.is_symlink() and (not target.exists() or target.is_file())


def install_operator_write_admission(jspace=None, root=None):
    """Admit exact declared writes outside the context surface's allowed_write_scopes.

    The operator granted Nokiy the write authority of a native Codex task in its workspace
    (2026-10: "maximum permissions", "fully deployable trading services"). DCF surfaces stay the
    context, provenance and freshness of the task; as a Nokiy write gate they forced a registry
    edit for every new file (28 of 89 refused preparations since 10-20; every file no surface
    admitted was under ui/dashboard). A write the surface admits is unchanged; any other must be
    an exact operator_writable file, and J-Space still enforces the exact write scopes."""
    if jspace is None:
        jspace = importlib.import_module("scripts.ops.dcf.jspace")
    root = Path.cwd() if root is None else Path(root)
    surface_check = getattr(jspace, "_validate_write_scopes", None)
    if surface_check is None:
        return jspace

    def validate(surface, write_scopes):
        for requested in write_scopes:
            try:
                surface_check(surface, [requested])
            except jspace.JSpaceCompileError as error:
                if (getattr(error, "error_code", None) != "JSPACE_WRITE_SCOPE_OUTSIDE_SURFACE"
                        or not operator_writable(requested, root)):
                    raise

    jspace._validate_write_scopes = validate
    surface_of = getattr(jspace, "_surface", None)
    if surface_of is not None:
        def context_surface(surface_data, surface_id):
            # A read-only surface is context for the operator's exact writes, as any other is: it
            # admits no write itself, so each one meets the exact operator_writable check above
            # (2026-10-20: dashboard UI files whose only surface was the read-only projection).
            surface = surface_of(surface_data, surface_id)
            if isinstance(surface, dict) and surface.get("read_only"):
                return dict(surface, read_only=False, allowed_write_scopes=[])
            return surface

        jspace._surface = context_surface
    return jspace


def safe_capability_diagnostic(value, error_code):
    fields = {"capability_id", "failed_field", "observed", "expected", "generation_id"}
    statuses = {
        "projection_status": ("pass", {"pass", "warn", "blocked"}),
        "domain_verdict": ("pass", {"pass", "warn", "blocked", "not_applicable"}),
        "freshness_status": ("current", {"current", "stale", "missing", "unverified"}),
    }
    if (error_code != "JSPACE_REQUIRED_CAPABILITY_NOT_CURRENT" or type(value) is not dict
            or set(value) != fields or any(type(v) is not str for v in value.values())):
        return None
    if value["capability_id"] not in {"surface-map", "verifier-gates", "authority-lineage"}:
        return None
    predicate = statuses.get(value["failed_field"])
    if (predicate is None or value["expected"] != predicate[0]
            or value["observed"] not in predicate[1] or value["observed"] == predicate[0]
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,254}", value["generation_id"])):
        return None
    return dict(value)


def targets_for(action):
    targets = action.get("navigation_targets")
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 4
            or any(not isinstance(t, str) or not TARGET.fullmatch(t) for t in targets)
            or len(set(targets)) != len(targets)
            or not isinstance(action.get("context_summary"), str)
            or not action["context_summary"].strip() or action.get("task_projection") is not None):
        raise ValueError("navigation requires 1..4 distinct exact symbols and a context summary")
    return targets


def bind_locators(action, locators, generation):
    targets = targets_for(action)
    if (not isinstance(generation, str) or not generation
            or not isinstance(locators, list) or len(locators) != len(targets)):
        raise ValueError("missing exact navigation evidence")
    for target, row in zip(targets, locators):
        if (not isinstance(row, dict) or row.get("target") != target
                or row.get("generation_id") != generation
                or not isinstance(row.get("path"), str) or not PATH.fullmatch(row["path"])
                or row["path"].startswith("/") or ".." in Path(row["path"]).parts
                or type(row.get("line")) is not int or row["line"] < 1):
            raise ValueError("navigation did not resolve an exact current locator")
    amended = dict(action)
    amended.pop("navigation_targets")
    amended["context_summary"] = (
        action["context_summary"].strip()
        + "\nDCF source-navigation locators (context only; no read/write grant):\n"
        + "\n".join(f"{row['target']} -> {row['path']}:{row['line']}" for row in locators)
    )
    return amended


def compile_bundle(runtime, surface_id, action):
    # Imports resolve inside the selected project, never a copied DCF implementation.
    from scripts.ops.dcf.graph import resolve_entities
    from scripts.ops.dcf.jspace import (
        compile_jspace_contract, compile_task_context_capsule, verify_contract_freshness,
    )

    targets = targets_for(action)
    snapshot, _, generation_dir = runtime.store.load_current(verify_file_hashes=True)
    _, capabilities, _ = runtime.capability_views(include_data=True, snapshot=snapshot)
    navigation = capabilities.get("source-navigation", {})
    if any(navigation.get(k) != v for k, v in (
            ("freshness_status", "current"), ("projection_status", "pass"), ("domain_verdict", "pass"))):
        raise ValueError("source-navigation is not current and passing")
    index, base_index = runtime.store.graph_paths(generation_dir, verify_file_hashes=True)
    locators = []
    with ExitStack() as opened:
        connection = opened.enter_context(closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)))
        base = (opened.enter_context(closing(sqlite3.connect(base_index.as_uri() + "?mode=ro", uri=True)))
                if base_index is not None else None)
        for target in targets:
            # Reuse DCF's overlay/base resolver, without traversing any edges.
            # A fuzzy match is not acceptable even when it has the right name.
            rows = resolve_entities(connection, target, base_connection=base, limit=2)
            if (not isinstance(rows, list) or len(rows) != 1 or rows[0]["entity_id"] != target
                    or rows[0]["entity_type"] != "symbol"):
                raise ValueError("symbol is absent, ambiguous or incomplete")
            locators.append({"target": target, "path": rows[0]["payload"]["path"],
                             "line": rows[0]["payload"]["line"], "generation_id": snapshot.generation_id})
    amended = bind_locators(action, locators, snapshot.generation_id)
    # Leave the canonical compiler's own snapshot and live-domain checks intact.
    # A generation change rejects the bundle instead of reusing stale grants.
    contract = compile_jspace_contract(runtime, surface_id=surface_id, action=amended)
    if contract["dcf_generation"]["generation_id"] != snapshot.generation_id:
        raise ValueError("DCF generation changed during compilation")
    verify_contract_freshness(runtime, contract)
    capsule = compile_task_context_capsule(contract, action=amended)
    return {"status": "compiled_not_admitted", "contract": contract,
            "task_context_capsule": capsule, "navigation_locators": locators,
            "action": amended, "authority_effect": "none", "no_apply": True}


def compile_plain_bundle(runtime, surface_id, action):
    from scripts.ops.dcf.jspace import (
        compile_jspace_contract, compile_task_context_capsule, verify_contract_freshness,
    )

    contract = compile_jspace_contract(runtime, surface_id=surface_id, action=action)
    # Both public APIs validate the canonical contract; freshness precedes context.
    verify_contract_freshness(runtime, contract)
    capsule = compile_task_context_capsule(contract, action=action)
    return {"status": "compiled_not_admitted", "contract": contract,
            "task_context_capsule": capsule, "navigation_locators": [],
            "action": action, "authority_effect": "none", "no_apply": True}


def main():
    # NOKIY_DCF_CODE_ROOT names the DCF implementation (the caller's _dcf_code_root); data stays cwd's.
    sys.path.insert(0, os.environ.get("NOKIY_DCF_CODE_ROOT") or str(Path.cwd()))
    try:
        jspace = importlib.import_module("scripts.ops.dcf.jspace")
        from scripts.ops.dcf.runtime import DcfRuntime
        JSpaceCompileError = jspace.JSpaceCompileError
    except (ImportError, AttributeError):
        # The workspace's DCF predates J-Space (e.g. a checkout of a branch forked before it):
        # a typed refusal instead of a traceback, so the parent can route around it. Nothing
        # was compiled and no input was read.
        sys.stdout.buffer.write(json.dumps({
            "schema_version": "jspace_error_v1", "status": "blocked", "error_code": "JSPACE_MODULE_UNAVAILABLE",
            "authority_effect": "none", "no_apply": True}, sort_keys=True, ensure_ascii=True).encode())
        return 2
    install_verifier_grammar(jspace)
    install_operator_write_admission(jspace)
    raw = sys.stdin.buffer.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("DCF preparation input exceeds budget")
    value = json.loads(raw)
    returncode = 0
    try:
        compile_action = compile_bundle if "navigation_targets" in value["action"] else compile_plain_bundle
        result = compile_action(DcfRuntime(Path.cwd()), value["surface_id"], value["action"])
    except JSpaceCompileError as error:
        result = {"schema_version": "jspace_error_v1",
                  "status": "blocked" if error.blocked else "invalid",
                  "authority_effect": "none", "no_apply": True}
        if type(error.error_code) is str and _PREPARATION_CHILD_CODE.fullmatch(error.error_code):
            result["error_code"] = error.error_code
            diagnostic = safe_capability_diagnostic(
                getattr(error, "capability_diagnostic", None), error.error_code)
            if diagnostic is not None:
                result["capability_diagnostic"] = diagnostic
        returncode = 2
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=True).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError("DCF preparation output exceeds budget")
    sys.stdout.buffer.write(encoded)
    return returncode


if __name__ == "__main__":
    sys.exit(main())
