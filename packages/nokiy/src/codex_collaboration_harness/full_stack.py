# SPDX-License-Identifier: MIT
"""Prepare bounded context through canonical DCF or explicit local inputs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess

from . import embedded_nokiy as caller


_NAVIGATION_TARGET = re.compile(r"^symbol:[A-Za-z_][A-Za-z0-9_.]{0,254}$")
_NAVIGATION_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
_SURFACE_UNVERIFIED = "NOKIY_FULL_STACK_SURFACE_UNVERIFIED"
_GIT = "/usr/bin/git"
# Action fields that, when present, take 1..N items; an empty one states the field's absence.
_NONEMPTY_WHEN_PRESENT = frozenset({"verifier_commands", "source_sections", "navigation_targets"})


def _normalize_action_input(action: dict) -> tuple[dict, list[dict]]:
    """Adapt legacy syntax, leaving all authority decisions to the compiler."""
    action = dict(action)
    repairs = []
    if action.get("inspect") == {}:
        from .inspect_context import adapt_action
        action, repairs = adapt_action(action)
    # A null, blank or (for a field that takes 1..N items) empty value states nothing, which is
    # what omission states. A required field so emptied stays refused as missing; an optional
    # one takes its default instead of costing the coordinator a round (2026-10: context_summary
    # null, verifier_commands []). Inspect reports its own empty grants first.
    for key in sorted(action):
        value = action[key]
        if (value is None or (isinstance(value, str) and not value.strip())
                or (value == [] and key in _NONEMPTY_WHEN_PRESENT)):
            del action[key]
            repairs.append({"path": "action." + key, "rule": "empty_to_omission"})
    declined_source_read = action.get("source_read") is False
    if action.get("source_read") is False:
        action.pop("source_read")
        repairs.append({"path": "action.source_read", "rule": "false_to_omission"})
    action, read_repairs = _modified_targets_readable(action)
    repairs += read_repairs
    action, reader_repairs = _command_reader(action, declined_source_read)
    repairs += reader_repairs
    templates = action.get("command_templates")
    if not isinstance(templates, list) or len(templates) > 32:
        return action, repairs
    normalized = []
    for index, template in enumerate(templates):
        if isinstance(template, str):
            try:
                argv = shlex.split(template)
            except ValueError:
                argv = []
            reads = action.get("read_scopes", [])
            if argv == ["pwd"]:
                template = {"argv": argv, "effects": ["read"], "targets": []}
            elif (isinstance(reads, list) and len(argv) > 1 and argv[0] == "cat"
                  and all(name in reads and not name.startswith("-")
                          and not any(c in name for c in "*?[]\x00\r\n") for name in argv[1:])):
                template = {"argv": argv, "effects": ["read"], "targets": [
                    {"operation": "read", "path": name, "argv_index": i}
                    for i, name in enumerate(argv[1:], 1)]}
            else:
                raise caller.EmbeddedNokiyError(
                    "NOKIY_FULL_STACK_PREPARATION_FAILED",
                    "legacy command requires exact scoped cat/pwd; no shell inference",
                    issues=[{"path": "action.command_templates", "reason": "unsupported_command"}])
            repairs.append({"path": f"action.command_templates[{index}]",
                            "rule": "exact_read_string_to_typed_template"})
        normalized.append(template)
    if isinstance(templates, list):
        action["command_templates"] = normalized
    return action, repairs


def _command_reader(action: dict, declined_source_read: bool) -> tuple[dict, list[dict]]:
    """Give a command grant its reader, or drop a command grant that nothing would use.

    The command operation is consumed only by command templates, verifiers, discovery
    directories or the source_read tool; both compilers refuse it alone. With exact reads the
    request is to read them, which is source_read; without reads the grant is empty. An explicit
    source_read false keeps that choice and drops the unused grant instead."""
    operations, reads = action.get("operations"), action.get("read_scopes")
    if (not isinstance(operations, list) or "command" not in operations
            or any(not isinstance(op, str) for op in operations) or "inspect" in action
            or "source_read" in action
            or any(action.get(key) for key in ("command_templates", "verifier_commands", "read_directories"))):
        return action, []
    if (not declined_source_read and isinstance(reads, list) and reads
            and all(isinstance(name, str) and name and not any(c in name for c in "*?[]") for name in reads)):
        return dict(action, source_read=True), [
            {"path": "action.source_read", "rule": "command_without_reader_to_source_read"}]
    if isinstance(reads, list) and any(not isinstance(name, str) or any(c in name for c in "*?[]")
                                       for name in reads):
        return action, []
    return dict(action, operations=[op for op in operations if op != "command"]), [
        {"path": "action.operations", "rule": "command_without_reader_omitted"}]


def _modified_targets_readable(action: dict) -> tuple[dict, list[dict]]:
    """A modify-only action that declares no reads reads the files it modifies.

    Both compilers refuse a read grant without read scopes, and changing a file needs it read."""
    operations, writes = action.get("operations"), action.get("write_scopes")
    if (not isinstance(operations, list) or "read" not in operations or "modify" not in operations
            or "create" in operations or "inspect" in action
            or action.get("read_scopes") or action.get("read_directories")
            or not isinstance(writes, list) or not writes
            or any(not isinstance(name, str) or not name or any(c in name for c in "*?[]") for name in writes)):
        return action, []
    return dict(action, read_scopes=list(dict.fromkeys(writes))), [
        {"path": "action.read_scopes", "rule": "read_scopes_from_modified_targets"}]


def _new_create_targets_modifiable(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """A create grant whose exact targets are all absent at preparation also lets the worker revise them.

    Both compilers confine mutations to the exact declared targets, and create already authorizes
    their full content. Without modify a worker that finds a fault in its own new file hands the
    fix back and the parent spends another dispatch. Absence is checked when the request is
    prepared, not when the worker runs: a path another writer creates in between can then be
    revised within the same exact-target confinement, as any declared modify target can. An
    existing target, a wildcard, or an explicit modify denial keeps the grant as declared."""
    operations, targets = action.get("operations"), action.get("target_paths")
    denials = [*(action.get("forbidden_effects") or []), *(action.get("denied_operations") or [])]
    if (not isinstance(operations, list) or "create" not in operations or "modify" in operations
            or "modify" in denials or "inspect" in action
            or any(not isinstance(op, str) for op in operations)
            or not isinstance(targets, list) or not targets
            or any(not isinstance(name, str) or not name or name.endswith("/") or Path(name).is_absolute()
                   or ".." in Path(name).parts or any(c in name for c in "*?[]") for name in targets)):
        return action, []
    if any(os.path.lexists(workspace / name) for name in targets):
        return action, []
    return dict(action, operations=[*operations, "modify"]), [
        {"path": "action.operations", "rule": "new_create_targets_modifiable"}]


def _summary_from_mission(action: dict) -> str | None:
    from .local_context import summary_from_mission
    return summary_from_mission(action)


def _declared_writes_operable(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """Exact declared writes with no write operation take modify/create by current existence.

    Write scopes name what the action changes; operations without create/modify/delete then
    contradict them and cost the coordinator a round (2026-10-22). Modify covers files that
    exist, create those that do not; an explicit denial, inspect or any non-exact scope keeps
    the refusal."""
    operations, writes = action.get("operations"), action.get("write_scopes")
    denials = [*(action.get("forbidden_effects") or []), *(action.get("denied_operations") or [])]
    if (not isinstance(operations, list) or any(not isinstance(op, str) for op in operations)
            or {"create", "modify", "delete"}.intersection(operations) or "inspect" in action
            or not isinstance(writes, list) or not writes
            or any(not isinstance(name, str) or not name or name.endswith("/") or Path(name).is_absolute()
                   or ".." in Path(name).parts or any(c in name for c in "*?[]") for name in writes)):
        return action, []
    present = [os.path.lexists(workspace / name) for name in writes]
    added = (["modify"] if any(present) else []) + (["create"] if not all(present) else [])
    if any(op in denials for op in added) or "write" in denials:
        return action, []
    return dict(action, operations=[*operations, *added]), [
        {"path": "action.operations", "rule": "write_scopes_to_" + "_".join(added)}]


_DCF_EXECUTABLES = {"cat": "/bin/cat", "git": "/usr/bin/git", "mkdir": "/bin/mkdir", "pwd": "/bin/pwd",
                    "rm": "/bin/rm", "touch": "/usr/bin/touch"}


def _dcf_models(template: dict, argv: list[str], reads: list[str]) -> bool:
    """Conservative mirror of DCF's trusted argv effect model (scripts/ops/dcf/jspace.py)."""
    name = next((key for key, path in _DCF_EXECUTABLES.items() if argv[0] in (key, path)), None)
    rest = argv[1:]
    if name == "pwd":
        return not rest
    if name == "cat":
        return bool(rest) and not any(token.startswith("-") or any(c in token for c in "*?[]") for token in rest)
    if name == "git":
        return bool(rest) and rest[0] in {"status", "rev-parse", "ls-files"}
    if name in {"rm", "mkdir", "touch"}:
        targets, ended = [], False
        for token in rest:
            if token == "--" and not ended:
                ended = True
                continue
            if not ended and token.startswith("-"):
                return False
            targets.append(token)
        return bool(targets)
    return False


def _source_sections_spelled(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """One spelling per source section, without changing which lines exist to read.

    A workspace file named by its absolute path takes its workspace-relative name, and an end
    line past the end of the file stops at the file's last line (2026-10-21: 188..224 of a
    204-line test and absolute paths each cost an Efficient coordinator round). Other locator
    faults keep their refusal."""
    from .source_excerpt import _read_exact

    sections = action.get("source_sections")
    if not isinstance(sections, list):
        return action, []
    spelled, fields, counts = [], set(), {}
    for section in sections:
        if isinstance(section, dict) and isinstance(section.get("path"), str):
            path = Path(section["path"])
            if path.is_absolute() and ".." not in path.parts and path.is_relative_to(workspace):
                section = dict(section, path=path.relative_to(workspace).as_posix())
                fields.add("path")
            start, end = section.get("start_line"), section.get("end_line")
            if type(start) is int and type(end) is int and 1 <= start <= end:
                name = section["path"]
                if name not in counts:
                    try:
                        text = _read_exact(workspace, name, python_only=False).decode("utf-8")
                        lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", text)
                        counts[name] = len(lines) - (lines[-1] == "")
                    except (caller.EmbeddedNokiyError, OSError, UnicodeError, ValueError):
                        counts[name] = None
                count = counts[name]
                if count is not None and start <= count < end:
                    section = dict(section, end_line=count)
                    fields.add("end_line")
        spelled.append(section)
    if not fields:
        return action, []
    return dict(action, source_sections=spelled), [
        {"path": "action.source_sections", "rule": "source_sections_spelled", "fields": sorted(fields)}]


def _dcf_compile_repair(action: dict, error: caller.EmbeddedNokiyError,
                        workspace: Path) -> tuple[dict, dict | None]:
    """One mechanical repair for a DCF compile refusal, or the action unchanged with None.

    Without a summary DCF projects the task from its declared targets, and targets that resolve
    to different subject surfaces conflict: the complete mission states the task. Commands outside
    DCF's trusted effect model in a writing action go to the parent, and its reads of files that
    do not exist are dropped with a note, as in local context."""
    diagnostics = error.diagnostics if isinstance(error.diagnostics, dict) else {}
    child = diagnostics.get("child_error_code")
    if child == "TASK_CONTEXT_PROJECTION_CONFLICT":
        summary = _summary_from_mission(action)
        stated = action.get("context_summary")
        if (summary is None or "task_projection" in action
                or not (stated is None or (isinstance(stated, str) and not stated.strip()))):
            return action, None
        return dict(action, context_summary=summary), {"path": "action.context_summary",
                                                       "rule": "projection_conflict_to_mission_summary"}
    if child == "JSPACE_COMMAND_EFFECT_UNVERIFIED":
        from .local_context import unadmitted_commands_to_parent
        adapted, repairs = unadmitted_commands_to_parent(action, _dcf_models)
        return (adapted, repairs[0]) if repairs else (action, None)
    if child in {"JSPACE_READ_SCOPE_MISSING", "JSPACE_SOURCE_READ_FILE_INVALID"}:
        # A source_read of a file that does not exist reads nothing; links and unreadable
        # files are not absent, find nothing to drop and keep the refusal.
        from .local_context import absent_reads_to_note
        adapted, repairs = absent_reads_to_note(action, workspace)
        return (adapted, repairs[0]) if repairs else (action, None)
    if isinstance(child, str) and child.startswith("MAPPED_SOURCE_READ_"):
        from .local_context import linked_reads_to_note
        adapted, repairs = linked_reads_to_note(action, workspace)
        return (adapted, repairs[0]) if repairs else (action, None)
    return action, None


def _dcf_unavailable(detail: str):
    return caller.EmbeddedNokiyError("NOKIY_FULL_STACK_DCF_UNAVAILABLE", detail)


def _absent_gate_reads(action: dict, workspace: Path, post_batch_verifier: bool) -> tuple[dict, list[dict]]:
    """Drop absent declared reads of a verifier gate, which grant nothing, with a note.

    The host prepares a batch's final gate with prepare-post-batch-verifier only when it binds a
    separate scratch root and with plain prepare otherwise; either way the gate is prepared before
    its batch runs, so files the batch creates are absent (2026-10-26: a DCF two-branch final gate
    prepared with plain prepare was refused twice). A gate only runs pinned verifiers and writes
    nothing; exploratory read-only actions keep the refusal and prepare as inspect."""
    from .local_context import absent_post_batch_reads_to_note
    operations = action.get("operations")
    gate = post_batch_verifier or (isinstance(action.get("verifier_commands"), list) and bool(action["verifier_commands"])
                                   and not action.get("write_scopes") and isinstance(operations, list)
                                   and not {"create", "modify", "delete"}.intersection(operations))
    return absent_post_batch_reads_to_note(action, workspace) if gate else (action, [])


def pair_discovery(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """Pair recursive read scopes with their discovery roots, which J-Space requires both ways.

    J-Space reads a recursive scope only through its discovery root (read_directories) and
    admits a root only beside its recursive scope, so a coordinator naming one implies the other.
    Only existing canonical non-hidden directories pair; at most eight roots, as the compiler."""
    reads, roots, operations = action.get("read_scopes"), action.get("read_directories", []), action.get("operations")
    if (not isinstance(reads, list) or not isinstance(roots, list) or not isinstance(operations, list)
            or any(not isinstance(value, str) for value in reads + roots + operations)
            or not ({"read", "command"} <= set(operations) or action.get("source_read") is True)):
        return action, []

    def canonical_root(name):
        path = workspace / name
        return (name not in ("", ".") and not Path(name).is_absolute() and Path(name).as_posix() == name
                and not any(part.startswith(".") for part in Path(name).parts)
                and not any(char in name for char in "*?[]{}\\") and path.is_dir() and path.resolve() == path)

    added_roots = [scope[:-3] for scope in reads
                   if scope.endswith("/**") and scope[:-3] not in roots and canonical_root(scope[:-3])]
    added_scopes = [root + "/**" for root in roots if root + "/**" not in reads and canonical_root(root)]
    paired = list(dict.fromkeys(roots + added_roots))
    if len(paired) > 8 or not (added_roots or added_scopes):
        return action, []
    adapted = dict(action, read_directories=paired, read_scopes=list(dict.fromkeys(reads + added_scopes)),
                   operations=list(dict.fromkeys(operations + ["read", "command"])))
    repairs = []
    if added_roots:
        repairs.append({"path": "action.read_directories", "rule": "recursive_reads_to_discovery"})
    if added_scopes:
        repairs.append({"path": "action.read_scopes", "rule": "discovery_roots_to_recursive_reads"})
    return adapted, repairs


def _git_identity(root: Path) -> tuple[Path, Path, Path]:
    result = subprocess.run(
        [_GIT, "rev-parse", "--path-format=absolute", "--show-toplevel",
         "--git-common-dir", "--absolute-git-dir"], cwd=root,
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode or len(result.stdout.encode()) > 16384:
        raise _dcf_unavailable("registered worktree Git identity unavailable")
    rows = result.stdout.splitlines()
    if len(rows) != 3:
        raise _dcf_unavailable("registered worktree Git identity malformed")
    paths = tuple(Path(row) for row in rows)
    if any(not path.is_absolute() or path.resolve(strict=True) != path for path in paths):
        raise _dcf_unavailable("registered worktree Git paths are not canonical")
    return paths


def _git_pointer(path: Path) -> str:
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise _dcf_unavailable("registered worktree pointer is not a bounded regular file")
        raw = source.read(4097)
    if len(raw) > 4096:
        raise _dcf_unavailable("registered worktree pointer exceeded byte limit")
    return raw.decode("utf-8")


def _registered_primary_root(workspace: Path) -> Path:
    try:
        top, common, git_dir = _git_identity(workspace)
        primary = common.parent
        if (top != workspace or common.name != ".git" or workspace == primary
                or _git_identity(primary) != (primary, common, common)
                or git_dir.parent != common / "worktrees"
                or _git_pointer(workspace / ".git") != f"gitdir: {git_dir}\n"
                or _git_pointer(git_dir / "gitdir") != f"{workspace / '.git'}\n"
                or (git_dir / _git_pointer(git_dir / "commondir").strip()).resolve(strict=True) != common):
            raise _dcf_unavailable("shared interpreter requires exact primary/worktree backpointers")
        result = subprocess.run([_GIT, "worktree", "list", "--porcelain", "-z"],
                                cwd=primary, capture_output=True, timeout=10)
        if result.returncode or len(result.stdout) > 262144:
            raise _dcf_unavailable("registered worktree list unavailable")
        records = result.stdout.split(b"\0\0")
        listed = {}
        for record in records:
            fields = record.strip(b"\0").split(b"\0")
            if fields == [b""]:
                continue
            if not fields[0].startswith(b"worktree "):
                raise _dcf_unavailable("registered worktree list malformed")
            name = os.fsdecode(fields[0][9:])
            if name in listed:
                raise _dcf_unavailable("registered worktree list ambiguous")
            listed[name] = fields[1:]
        if (str(workspace) not in listed or str(primary) not in listed
                or any(field.startswith(b"prunable") for field in listed[str(workspace)])):
            raise _dcf_unavailable("shared interpreter requires a currently registered worktree")
        return primary
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as error:
        raise _dcf_unavailable("registered worktree identity could not be verified") from error


def _dcf_interpreter(workspace: Path) -> Path:
    """Share only an existing primary environment with its registered thin worktree."""
    local = workspace / ".venv"
    python = local / "bin/python"
    if local.exists() or local.is_symlink():
        if not python.is_file():
            raise _dcf_unavailable("local DCF interpreter missing or broken; no shared fallback")
        return python
    primary = _registered_primary_root(workspace)
    python = primary / ".venv/bin/python"
    if not python.is_file():
        raise _dcf_unavailable("registered primary DCF interpreter unavailable")
    return python


def _dcf_interpreter_identity(workspace: Path, python: Path) -> tuple:
    try:
        if _dcf_interpreter(workspace) != python:
            raise _dcf_unavailable("DCF interpreter binding changed")
        resolved = python.resolve(strict=True)
        info = resolved.stat()
        environment = python.parent.parent.resolve(strict=True)
        env_info = environment.stat()
        return (resolved, info.st_dev, info.st_ino, hashlib.sha256(resolved.read_bytes()).hexdigest(),
                environment, env_info.st_dev, env_info.st_ino)
    except OSError as error:
        raise _dcf_unavailable("DCF interpreter identity unavailable") from error


def _run_dcf_python(workspace: Path, python: Path, arguments: list[str], **kwargs):
    before = _dcf_interpreter_identity(workspace, python)
    completed = subprocess.run([str(python), *arguments], cwd=workspace, **kwargs)
    if _dcf_interpreter_identity(workspace, python) != before:
        raise _dcf_unavailable("DCF interpreter identity changed during preparation")
    return completed


_DCF_CODE_PATHS = ("scripts/ops/dcf", "scripts/ops/dcf.py", "scripts/ops/codex_task_harness_common.py")


def _dcf_code_root(workspace: Path) -> Path:
    """The DCF implementation a workspace's own data is compiled with.

    A registered thin worktree already runs its primary's interpreter, but its copy of the DCF
    code froze when it was created, so most worktrees compile actions with an older grammar than
    the primary and the runtime. Such a worktree uses its primary's committed DCF code; its
    contracts, sources and generation stay its own, as the DCF CLI's --root already separates
    code from data. Uncommitted DCF edits in the primary keep the worktree's own code, so an edit
    in progress never reaches every worktree. A worktree whose .venv links to its primary's
    environment shares it exactly as one without a .venv; a real .venv is its own project."""
    venv = workspace / ".venv"
    if venv.exists() and not venv.is_symlink():
        return workspace
    from .local_context import jspace_root
    try:
        primary = _registered_primary_root(workspace)
        if venv.is_symlink() and venv.resolve() != (primary / ".venv").resolve():
            return workspace
        if jspace_root(primary) != primary:
            return workspace
        status = subprocess.run([_GIT, "status", "--porcelain", "--untracked-files=all", "--",
                                 *_DCF_CODE_PATHS], cwd=primary, capture_output=True, text=True, timeout=10)
    except (caller.EmbeddedNokiyError, OSError, subprocess.TimeoutExpired):
        return workspace
    return workspace if status.returncode or status.stdout.strip() else primary


def _dcf_env(code_root: Path) -> dict:
    """Environment naming the DCF implementation root for _DCF_PATH and the bridge."""
    return dict(os.environ, NOKIY_DCF_CODE_ROOT=str(code_root))


def _with_parent_shell(action: dict, workspace: Path, draft: dict) -> dict:
    """V432: a DCF reading action also gets the read-only parent shell, as a local one does.

    Only DCF code that binds the policy receives it (an older worktree copy would drop it and
    leave a bare command operation); an action that forbids commands keeps none."""
    operations = action.get("operations", ["read"])
    forbidden = action.get("forbidden_effects") or []
    if ("parent_shell" in action or "inspect" in action or draft.get("artifact_root") is None
            or not isinstance(operations, list) or "read" not in operations
            or not isinstance(forbidden, list) or "command" in forbidden):
        return action
    try:
        supported = "nokiy_parent_shell_v1" in (_dcf_code_root(workspace) / "scripts/ops/dcf/jspace.py").read_text()
    except OSError:
        return action
    if not supported:
        return action
    from .parent_shell import compile_policy
    artifact_root = caller._plain_path(draft.get("artifact_root"), name="artifact_root", directory=True)
    shaped = dict(action, parent_shell=compile_policy(artifact_root, action, workspace))
    if "command" not in operations:
        shaped["operations"] = sorted(set(operations) | {"command"})
    return shaped


# Every project snippet imports DCF from NOKIY_DCF_CODE_ROOT ahead of the working directory,
# whose data (contracts, sources, generation) it still reads.
_DCF_PATH = "import os,sys;sys.path.insert(0,os.environ.get('NOKIY_DCF_CODE_ROOT') or '')\n"


# The only path matching and catalog read happen inside the project's own DCF
# interpreter. Do not route paths via sourcegraph or a partial public preview.
_SURFACE_QUERY = _DCF_PATH + """
import hashlib, inspect, json, sys
from pathlib import Path
from scripts.ops.dcf.collectors import path_matches_surface
from scripts.ops.dcf.query import query_capability
from scripts.ops.dcf.runtime import DcfRuntime
from scripts.ops.dcf.storage import GenerationUnavailable

paths = json.load(sys.stdin)
options = {"capability_id": "surface-map"}
if "full_output" in inspect.signature(query_capability).parameters:
    options["full_output"] = True
try:
    response, _ = query_capability(DcfRuntime(Path.cwd()), **options)
except GenerationUnavailable:
    sys.stdout.write(json.dumps({"schema_version": "nokiy_surface_query_error_v1",
        "error_code": "DCF_GENERATION_UNAVAILABLE", "authority_effect": "none",
        "no_apply": True}, sort_keys=True))
    sys.exit(2)
wire = response.model_dump(mode="json")
if (response.freshness_status != "current" or response.projection_status != "pass"
        or response.domain_verdict != "pass"):
    raise ValueError("surface-map is not current and passing")
rows = wire["result"]["surfaces"]
if not isinstance(rows, list) or len(rows) > 4096:
    raise ValueError("invalid surface-map catalog")
ids = [row["surface_id"] for row in rows]
if any(not isinstance(sid, str) or not sid or len(sid) > 255 for sid in ids) or len(set(ids)) != len(ids):
    raise ValueError("invalid surface IDs")
matches = [[sid for sid, row in zip(ids, rows) if path_matches_surface(path, row)] for path in paths]
# Membership is evidence; a write needs the surface's allowed_write_scopes, the rule the
# J-Space compiler applies, so report the surfaces that would admit each path as a write.
def write_admits(path, row):
    if row.get("read_only") is True:
        return False
    for scope in row.get("allowed_write_scopes") or []:
        if not isinstance(scope, str):
            continue
        if path == scope:
            return True
        if scope.endswith("/**"):
            root = scope[:-3].rstrip("/")
            if root and path.startswith(root + "/"):
                return True
    return False
write_matches = [[sid for sid, row in zip(ids, rows) if write_admits(path, row)] for path in paths]
# Membership in a writable surface: the context an operator-admitted write can use (dcf_prepare).
writable_matches = [[sid for sid, row in zip(ids, rows)
                     if row.get("read_only") is not True and path_matches_surface(path, row)] for path in paths]
digest = hashlib.sha256(json.dumps(wire["result"], ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8")).hexdigest()
wire["result"] = {"surface_count": len(rows), "full_result_sha256": digest,
                  "paths": paths, "matches": matches, "write_matches": write_matches,
                  "writable_matches": writable_matches, "complete": True}
payload = json.dumps(wire, sort_keys=True)
if len(payload.encode("utf-8")) > 65536:
    raise ValueError("surface-map evidence exceeds response budget")
sys.stdout.write(payload)
"""


def _surface_file_identity(workspace: Path, name: str) -> tuple[int, int, int]:
    code = "NOKIY_FULL_STACK_SURFACE_TARGET_INVALID"
    if (not isinstance(name, str) or not name or len(name) > 512
            or Path(name).is_absolute() or Path(name).as_posix() != name
            or any(c in name for c in "*?[]{}\\\x00\r\n")
            or any(part in {"", ".", "..", ".git"} for part in name.split("/"))):
        raise caller.EmbeddedNokiyError(code, "surface_targets require exact normalized workspace-relative files")
    node = workspace
    try:
        parts = name.split("/")
        for i, part in enumerate(parts):
            node = node / part
            info = node.lstat()
            if not (stat.S_ISREG(info.st_mode) if i == len(parts) - 1 else stat.S_ISDIR(info.st_mode)):
                raise ValueError("non-regular file or symlink component")
    except (OSError, ValueError) as error:
        raise caller.EmbeddedNokiyError(code, "surface target is not a physical regular file under workspace") from error
    return info.st_dev, info.st_ino, info.st_mode


def _surface_files_still_current(workspace: Path, identities: dict) -> bool:
    try:
        return all(_surface_file_identity(workspace, path) == identity
                   for path, identity in identities.items())
    except caller.EmbeddedNokiyError:
        return False


def _surface_target_identities(workspace: Path, targets: list) -> dict:
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 4
            or len(set(t for t in targets if isinstance(t, str))) != len(targets)):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID",
                                         "surface_targets require 1..4 distinct paths")
    identities = {}
    for target in targets:
        identity = _surface_file_identity(workspace, target)
        identities[target] = identity
    if len(set(identities.values())) != len(targets):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID",
                                         "surface_targets must identify distinct physical files")
    return identities


def _surface_candidates(workspace: Path, python: Path, targets: list) -> tuple[dict, dict]:
    identities = _surface_target_identities(workspace, targets)
    try:
        completed = _run_dcf_python(workspace, python, ["-B", "-c", _SURFACE_QUERY],
                                    input=json.dumps(targets), capture_output=True,
                                    text=True, timeout=30, env=_dcf_env(_dcf_code_root(workspace)))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED, "surface-map query did not finish") from error
    if (completed.returncode or len(completed.stdout.encode("utf-8")) > 65536
            or len(completed.stderr.encode("utf-8")) > 4096):
        diagnostics = _dcf_failure_diagnostics(completed, "dcf_surface_query")
        code = _SURFACE_UNVERIFIED
        if completed.returncode == 2 and len(completed.stdout.encode("utf-8")) <= 65536:
            try:
                error = json.loads(completed.stdout, object_pairs_hook=caller._unique_object,
                                   parse_constant=caller._invalid_constant)
                if (error == {"schema_version": "nokiy_surface_query_error_v1",
                              "error_code": "DCF_GENERATION_UNAVAILABLE",
                              "authority_effect": "none", "no_apply": True}
                        and error["no_apply"] is True):
                    code = "NOKIY_FULL_STACK_DCF_GENERATION_UNAVAILABLE"
            except (ValueError, TypeError, caller.EmbeddedNokiyError):
                pass
        raise caller.EmbeddedNokiyError(code, "surface-map query failed or exceeded budget",
                                        diagnostics=diagnostics)
    try:
        wire = json.loads(completed.stdout)
        result = wire["result"]
        matches = result["matches"]
        count = result["surface_count"]
        if (wire["schema_version"] != "dcf_query_response_v2"
                or wire["capability_id"] != "surface-map" or wire["target"] is not None
                or wire["depth"] != 2 or wire["freshness_status"] != "current"
                or wire["projection_status"] != "pass" or wire["domain_verdict"] != "pass"
                or wire["safety"]["authority_effect"] != "none"
                or wire["safety"]["no_apply"] is not True
                or wire["safety"]["protected_mutation_authorized"] is not False
                or wire["safety"]["command_queue_authority"] != "proposal_only"
                or not isinstance(wire["generation_id"], str) or not wire["generation_id"]
                or set(result) - {"write_matches", "writable_matches"} != {
                    "surface_count", "full_result_sha256", "paths", "matches", "complete"}
                or result["complete"] is not True or result["paths"] != targets
                or type(count) is not int or not 0 <= count <= 4096
                or not re.fullmatch(r"[0-9a-f]{64}", result["full_result_sha256"])
                or not isinstance(matches, list) or len(matches) != len(targets)):
            raise ValueError("incomplete or stale surface-map response")
        # A DCF query that predates write admission reports membership only.
        write_matches = result.get("write_matches")
        writable_matches = result.get("writable_matches")
        for optional in (write_matches, writable_matches):
            if optional is not None and (not isinstance(optional, list) or len(optional) != len(targets)):
                raise ValueError("invalid surface write candidates")
        for row in matches + (write_matches or []) + (writable_matches or []):
            if (not isinstance(row, list) or len(row) > count
                    or any(not isinstance(sid, str) or not sid or len(sid) > 255 for sid in row)
                    or len(set(row)) != len(row)):
                raise ValueError("invalid surface candidates")
        if not _surface_files_still_current(workspace, identities):
            raise ValueError("target changed during surface lookup")
    except (KeyError, TypeError, ValueError, caller.EmbeddedNokiyError) as error:
        raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED, "surface-map evidence is not current and complete") from error
    evidence = {"generation_id": wire["generation_id"], "surface_map_sha256": result["full_result_sha256"],
                "surface_count": count, "targets": targets,
                "candidates": [sorted(row) for row in matches],
                "common_surface_ids": sorted(set.intersection(*(set(row) for row in matches)))}
    if write_matches is not None:
        evidence["write_candidates"] = [sorted(row) for row in write_matches]
    if writable_matches is not None:
        evidence["writable_candidates"] = [sorted(row) for row in writable_matches]
    return evidence, identities


def query_surface_candidates(workspace: Path, targets: list) -> dict:
    """Return current metadata candidates; never prepare a request or grant access."""
    from .local_context import MODE, dcf_root

    workspace = caller._plain_path(workspace, name="workspace", directory=True)
    managed_root = dcf_root(workspace)
    program = workspace / "scripts/ops/dcf.py"
    data_only = managed_root == workspace and not (program.exists() or program.is_symlink())
    if managed_root is None or data_only:
        _surface_target_identities(workspace, targets)
        # Absence is a route observation, not stale DCF evidence or a scope grant.
        return {"status": "CONTEXT_ROUTE_AVAILABLE", "workspace": str(workspace),
                "context_mode": MODE, "dcf_state": "data_without_program" if data_only else "not_configured",
                "map_cache_state": "not_applicable", "targets": targets,
                "surface_required": False,
                "next_action": "Use existing prepare with exact authorized read/write scopes; omit DCF surface_id, surface_targets and navigation_targets. This route does not provide DCF navigation.",
                "authority_effect": "none", "selection_grants_read_write": False,
                "provider_execution_started": False, "native_mutation_count": 0}
    if managed_root != workspace or not (workspace / "scripts/ops/dcf.py").is_file():
        raise caller.EmbeddedNokiyError(
            "NOKIY_FULL_STACK_DCF_UNAVAILABLE", "use the DCF root and its working interpreter; no local downgrade")
    python = _dcf_interpreter(workspace)
    evidence, _ = _surface_candidates(workspace, python, targets)
    return {"status": "SURFACE_CANDIDATES", "workspace": str(workspace), **evidence,
            "authority_effect": "none", "selection_grants_read_write": False,
            "provider_execution_started": False, "native_mutation_count": 0}


def _resolve_surface_targets(workspace: Path, python: Path, action: dict,
                             surface_id: str | None) -> tuple[dict, str, dict, dict]:
    evidence, identities = _surface_candidates(workspace, python, action["surface_targets"])
    common = evidence["common_surface_ids"]
    if not common:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_UNRESOLVED",
                                         "no common registered surface for all targets")
    if surface_id is None:
        if len(common) != 1:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_AMBIGUOUS",
                                             "multiple common surfaces; supply an exact surface ID")
        surface_id = common[0]
    elif surface_id not in common:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_MISMATCH",
                                         "explicit surface ID does not match every target")
    compiled_action = dict(action)
    compiled_action.pop("surface_targets")
    evidence = {**evidence, "selected_surface_id": surface_id}
    return compiled_action, surface_id, evidence, identities


def _identity(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _with_verifier_grammar(script: str) -> str:
    """Run a project DCF snippet with the runtime's verifier grammar bound, as the bridge does:
    contract validation inside these snippets re-checks verifier grants (dcf_prepare)."""
    bridge = Path(__file__).with_name("dcf_prepare.py")
    return (_DCF_PATH + "import importlib.util as _u;"
            f"_s=_u.spec_from_file_location('nokiy_dcf_bridge',{str(bridge)!r});"
            "_m=_u.module_from_spec(_s);_s.loader.exec_module(_m);_m.install_verifier_grammar();"
            + script)


def verify_action_freshness(workspace: Path, contract: dict) -> None:
    """Use DCF's existing current-domain verifier, not generation age as a proxy."""
    python = _dcf_interpreter(workspace)
    script = _with_verifier_grammar(
        "import json,sys;from pathlib import Path;"
        "from scripts.ops.dcf.jspace import verify_contract_freshness;"
        "from scripts.ops.dcf.runtime import DcfRuntime;"
        "verify_contract_freshness(DcfRuntime(Path.cwd()),json.load(sys.stdin))"
    )
    try:
        result = _run_dcf_python(workspace, python, ["-B", "-c", script], input=json.dumps(contract),
                                 capture_output=True, text=True, timeout=30,
                                 env=_dcf_env(_dcf_code_root(workspace)))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_FRESHNESS_UNVERIFIED", "DCF verifier unavailable") from error
    if result.returncode:
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_FRESHNESS_UNVERIFIED", "required DCF domains failed verification")


def _query_navigation(workspace: Path, argv: list[str], target: str) -> dict:
    code = "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED"
    try:
        response = _run_dcf_python(workspace, Path(argv[0]), argv[1:],
                                  capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(code, "DCF navigation did not finish") from error
    if response.returncode or len(response.stdout.encode()) > 65536:
        raise caller.EmbeddedNokiyError(code, "DCF navigation failed or exceeded its response budget")
    payload = json.loads(response.stdout)
    if (payload["capability_id"] != "source-navigation"
            or payload["target"] != target
            or payload["freshness_status"] != "current"
            or payload["domain_verdict"] != "pass"
            or payload["projection_status"] != "pass"
            or not isinstance(payload["result"], dict)
            or not isinstance(payload["result"]["_projection"], dict)):
        raise ValueError("navigation result is not current and passing")
    return payload


def _navigate_known_symbols(workspace: Path, compiler: Path, python: Path,
                            action: dict) -> tuple[dict, list[dict]]:
    targets = action.get("navigation_targets")
    if targets is None:
        return action, []
    code = "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED"
    if (not isinstance(targets, list) or not 1 <= len(targets) <= 4
            or any(not isinstance(target, str) or not _NAVIGATION_TARGET.fullmatch(target)
                   for target in targets) or len(set(targets)) != len(targets)
            or not isinstance(action.get("context_summary"), str)
            or not action["context_summary"].strip() or action.get("task_projection") is not None):
        raise caller.EmbeddedNokiyError(code, "navigation requires 1..4 distinct exact symbols and a context summary")
    locators: list[dict] = []
    generation = None
    for target in targets:
        # The CLI reads this workspace's data even when its code is the primary's.
        root = [] if compiler.parents[2] == workspace else ["--root", str(workspace)]
        argv = [str(python), "-B", str(compiler), *root, "query", "--capability", "source-navigation",
                "--target", target, "--depth", "1", "--json"]
        try:
            payload = _query_navigation(workspace, argv, target)
            current_generation = payload["generation_id"]
            if not isinstance(current_generation, str) or not current_generation:
                raise ValueError("navigation generation is missing")
            if generation is not None and current_generation != generation:
                raise ValueError("navigation generations differ")
            result = payload["result"]
            projection = result["_projection"]
            if projection["complete"] is True:
                resolved = result["resolved"]
            elif projection["complete"] is False:
                digest = projection["full_result_sha256"]
                if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,254}", current_generation)
                        or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                    raise ValueError("navigation continuation binding is malformed")
                # Graph pagination need not block one exact locator. Select only
                # /resolved, bound to the original full result, once (no page loop).
                payload = _query_navigation(workspace, argv + [
                    "--result-pointer", "/resolved", "--expected-generation-id", current_generation,
                    "--expected-full-result-sha256", digest,
                ], target)
                result = payload["result"]
                projection = result["_projection"]
                if (payload["generation_id"] != current_generation
                        or result["result_pointer"] != "/resolved"
                        or type(result["item_count"]) is not int or result["item_count"] != 1
                        or projection["complete"] is not True
                        or projection["full_result_sha256"] != digest
                        or type(projection["page_offset"]) is not int or projection["page_offset"] != 0
                        or type(projection["returned_item_count"]) is not int
                        or projection["returned_item_count"] != 1
                        or projection["next_offset"] is not None
                        or any(projection.get(key, []) != []
                               for key in ("deferred_item_refs", "deferred_value_refs"))
                        or any(type(projection.get(key, 0)) is not int or projection.get(key, 0) != 0
                               for key in ("omitted_item_count", "deferred_item_count"))):
                    raise ValueError("navigation selection is not bound and complete")
                resolved = result["items"]
            else:
                raise ValueError("navigation completeness is malformed")
            if not isinstance(resolved, list) or len(resolved) != 1:
                raise ValueError("navigation result is not current and complete")
            symbol = resolved[0]
            path = symbol["payload"]["path"]
            line = symbol["payload"]["line"]
            if (symbol["entity_id"] != target or not isinstance(path, str)
                    or not _NAVIGATION_PATH.fullmatch(path)
                    or path.startswith("/") or ".." in Path(path).parts
                    or type(line) is not int or line < 1):
                raise ValueError("navigation did not resolve an exact source location")
            generation = current_generation
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise caller.EmbeddedNokiyError(code, "DCF navigation result is not an exact current locator") from error
        locators.append({"target": target, "path": path, "line": line,
                         "generation_id": generation})
    compiled_action = dict(action)
    compiled_action.pop("navigation_targets")
    lines = [f"{row['target']} -> {row['path']}:{row['line']}" for row in locators]
    compiled_action["context_summary"] = (
        action["context_summary"].strip()
        + "\nDCF source-navigation locators (context only; no read/write grant):\n"
        + "\n".join(lines)
    )
    return compiled_action, locators


def _auto_excerpt_eligible(draft: dict, contract: dict, locators: list) -> bool:
    """Optimize an exact read, never a discovery, verification or mutation task."""
    operations = contract.get("allowed_operations", [])
    return (
        draft.get("authority_effect") == "none"
        and draft.get("require_tool_call") is False
        and len(locators) == 1 and locators[0]["path"].endswith(".py")
        and contract.get("read_scopes") == [locators[0]["path"]]
        and contract.get("write_scopes") == []
        and isinstance(operations, list) and "read" in operations
        and all(operation in ("read", "command") for operation in operations)
        and ("command" not in operations or contract.get("source_read") is True)
        and "read" not in contract.get("denied_operations", [])
        and not contract.get("command_templates")
        and not any(contract.get(key) for key in
                    ("verifier_commands", "verifier_grants", "verifier_command_templates"))
    )


def _auto_edit_excerpt_eligible(draft: dict, contract: dict, locators: list) -> bool:
    """Select supported edit shapes without rejecting other valid task shapes."""
    operations = contract.get("allowed_operations", [])
    return (
        draft.get("authority_effect") == "workspace"
        and draft.get("require_tool_call") is True
        and 1 <= len(locators) <= 4
        and isinstance(operations, list)
        and "read" in operations and "modify" in operations
        and all(op in ("read", "modify", "command") for op in operations)
        and contract.get("source_read") is True
        and all(row["path"].endswith(".py")
                and row["path"] in contract.get("read_scopes", [])
                and row["path"] in contract.get("write_scopes", []) for row in locators)
    )


# Read-only commands the workspace context cannot admit still run inside the inspect fence,
# which allows no repository writes: local context admits only cat/pwd templates, and DCF's
# trusted argv model refuses executables it does not model (JSPACE_COMMAND_EFFECT_UNVERIFIED).
_INSPECT_ROUTABLE_CHILD_CODES = frozenset({"JSPACE_COMMAND_EFFECT_UNVERIFIED"})
INSPECT_ROUTE_RULE = "unadmitted_read_commands_to_inspect"
# Any other refusal of a read-only action (oversized or missing reads, an absent or refused DCF
# surface, denied read commands) is a mismatch between exact grants and the task, not a reason
# to stop it: the fence admits every read and no repository write. Only a refusal showing the
# action is not read-only keeps it, so new refusal kinds need no enumeration.
READ_ONLY_ROUTE_RULE = "read_only_refusal_to_inspect"
_NOT_READ_ONLY_CHILD_CODES = frozenset({"JSPACE_COMMAND_EFFECT_UNDERDECLARED"})


def prepare_request(draft_path: Path, action_path: Path, surface_id: str | None,
                    output: Path, *, topology_model: str | None = None,
                    worker_family: str | None = None) -> dict:
    """Compile once, validate through the real executor, and publish request last.

    A read-only action whose exact preparation is refused is prepared once more as the
    equivalent inspect action; if that also fails, the exact refusal is reported."""
    try:
        return _prepare_request(draft_path, action_path, surface_id, output,
                                topology_model=topology_model, worker_family=worker_family)
    except caller.EmbeddedNokiyError as error:
        refusal = error
    if output.exists() or output.is_symlink():
        raise refusal
    if not _read_only_routable(refusal):
        raise refusal
    try:
        original = caller._load_json(action_path, limit=caller.MAX_REQUEST_BYTES,
                                     code="NOKIY_FULL_STACK_PREPARATION_FAILED")
        try:
            action = _normalize_action_input(original)[0]
        except caller.EmbeddedNokiyError:
            # A legacy command string outside cat/pwd is still a request to read.
            action = original
        if _inspect_routable(refusal):
            routed = _read_commands_as_inspect(action)
        else:
            routed = _read_commands_as_inspect(action, rule=READ_ONLY_ROUTE_RULE,
                                               refused=_refusal_label(refusal))
    except (caller.EmbeddedNokiyError, TypeError, ValueError):
        routed = None
    if routed is None:
        raise refusal
    try:
        return _prepare_request(draft_path, action_path, None, output, topology_model=topology_model,
                                worker_family=worker_family, routed_action=routed)
    except caller.EmbeddedNokiyError:
        # Before publication the exact refusal stays the reported cause.
        if output.exists() or output.is_symlink():
            raise
        raise refusal from None


def _inspect_routable(error: caller.EmbeddedNokiyError) -> bool:
    issues = error.issues if isinstance(error.issues, list) else []
    if any(isinstance(issue, dict) and issue.get("path") == "action.command_templates"
           and issue.get("reason") == "unsupported_command" for issue in issues):
        return True
    diagnostics = error.diagnostics if isinstance(error.diagnostics, dict) else {}
    return diagnostics.get("child_error_code") in _INSPECT_ROUTABLE_CHILD_CODES


def _read_only_routable(error: caller.EmbeddedNokiyError) -> bool:
    diagnostics = error.diagnostics if isinstance(error.diagnostics, dict) else {}
    return diagnostics.get("child_error_code") not in _NOT_READ_ONLY_CHILD_CODES


def _refusal_label(error: caller.EmbeddedNokiyError) -> str:
    diagnostics = error.diagnostics if isinstance(error.diagnostics, dict) else {}
    issues = error.issues if isinstance(error.issues, list) else []
    parts = [str(error.code)]
    if isinstance(diagnostics.get("child_error_code"), str):
        parts.append(diagnostics["child_error_code"])
    issue = next((item for item in issues if isinstance(item, dict)), None)
    if issue is not None:
        parts.append(f"{issue.get('path')}:{issue.get('reason')}")
    return "/".join(parts)[:200]


def _read_commands_as_inspect(action: dict, *, rule: str = INSPECT_ROUTE_RULE,
                              refused: str | None = None) -> tuple[dict, list[dict]] | None:
    """The inspect form of a read-only action, or None when the action may write.

    The requested commands, read paths, directories and sections move into the task context;
    the fence, not exact grants, then keeps every write inside the evidence root and temporary
    directories. The unadmitted-command route requires commands; a refused read-only action
    may have none."""
    from .inspect_context import NOTICE, _MAX_SUMMARY_CHARS
    templates, mission = action.get("command_templates", []), action.get("mission")
    operations = action.get("operations", []) + action.get("allowed_operations", [])
    if (not isinstance(templates, list) or (rule == INSPECT_ROUTE_RULE and not templates)
            or "inspect" in action
            # An explicit excerpt asks for an answer from exact source, not a shell session.
            or (rule == READ_ONLY_ROUTE_RULE and action.get("include_source_excerpt") is True)
            or not isinstance(mission, dict)
            or any(not isinstance(mission.get(key), str) or not mission[key].strip()
                   for key in ("mission_id", "task_id", "mode", "objective", "current_predicate"))
            or not operations or not set(operations) <= {"read", "command"}
            or any(action.get(key) for key in ("write_scopes", "target_paths", "verifier_commands",
                                               "verifier_artifact_root"))):
        return None
    commands = []
    for template in templates:
        if isinstance(template, str) and template.strip():
            commands.append(template.strip())
        elif (isinstance(template, dict) and template.get("effects") == ["read"]
              and isinstance(template.get("argv"), list) and template["argv"]
              and all(isinstance(arg, str) for arg in template["argv"])):
            commands.append(shlex.join(template["argv"]))
        else:
            return None
    parts = [f"Exact preparation was refused ({refused}); this read-only task runs in the inspect shell"
             ] if refused else []
    if commands:
        parts.append("Requested read-only commands: " + "; ".join(commands))
    for key, label in (("read_scopes", "Requested read paths"), ("read_directories", "Requested read directories"),
                       ("surface_targets", "Requested surface targets"),
                       ("navigation_targets", "Requested navigation targets")):
        names = [name for name in action.get(key) or [] if isinstance(name, str)]
        if names:
            parts.append(label + ": " + ", ".join(names))
    sections = [f"{section['path']}:{section.get('start_line')}-{section.get('end_line')}"
                for section in action.get("source_sections") or []
                if isinstance(section, dict) and isinstance(section.get("path"), str)]
    if sections:
        parts.append("Requested source sections: " + ", ".join(sections))
    summary = action.get("context_summary")
    summary = summary.strip() if isinstance(summary, str) else ""
    note = ". ".join(parts) + "."
    budget = _MAX_SUMMARY_CHARS - len(NOTICE) - len(summary) - 1
    if len(note) > budget:
        # Long path lists are guidance, not grants: keep their head rather than drop the route.
        note = note[:max(budget - 4, 0)] + " ..."
    if budget < 200:
        return None
    text = (summary + "\n" + note).strip()
    routed = {key: action[key] for key in ("mission", "forbidden_effects", "initial_task_state",
                                           "terminal_delivery") if key in action}
    routed.update(context_summary=text, operations=["read", "command"], inspect={})
    adaptation = {"path": "action", "rule": rule}
    if refused:
        adaptation["refused"] = refused
    return routed, [adaptation]


def _prepare_request(draft_path: Path, action_path: Path, surface_id: str | None,
                     output: Path, *, topology_model: str | None = None,
                     worker_family: str | None = None,
                     routed_action: tuple[dict, list[dict]] | None = None,
                     post_batch_verifier: bool = False) -> dict:
    """Compile once, validate through the real executor, and publish request last."""
    code = "NOKIY_FULL_STACK_PREPARATION_FAILED"
    draft = dict(caller._load_json(draft_path, limit=caller.MAX_REQUEST_BYTES, code=code))
    selection = None
    if topology_model is not None:
        from .model_topology import project_worker_draft
        draft, selection = project_worker_draft(draft, topology_model, worker_family)
    elif worker_family is not None:
        raise caller.EmbeddedNokiyError("NOKIY_TOPOLOGY_MODE_REQUIRED",
                                         "worker-family requires topology-model")
    action = caller._load_json(action_path, limit=caller.MAX_REQUEST_BYTES, code=code)
    original_action_sha256 = caller._canonical_sha256(action)
    action, input_adaptations = _normalize_action_input(action)
    if routed_action is not None:
        action = dict(routed_action[0])
        input_adaptations = input_adaptations + list(routed_action[1])
    if "focused_verifiers" in action:
        raise caller.EmbeddedNokiyError(
            code, "action.focused_verifiers is unsupported; use action.verifier_commands")
    if "terminal_delivery" in draft:
        raise caller.EmbeddedNokiyError(code, "terminal_delivery belongs in action, not draft")
    if "initial_task_state" in draft:
        raise caller.EmbeddedNokiyError(code, "initial_task_state belongs in action, not draft")
    initial_task_state = None
    if "initial_task_state" in action:
        initial_task_state = caller._normalize_initial_task_state(action.pop("initial_task_state"), code=code)
    terminal_delivery = action.pop("terminal_delivery", "assistant_reply")
    if (not isinstance(terminal_delivery, str)
            or terminal_delivery not in {"assistant_reply", "evidence_only"}):
        raise caller.EmbeddedNokiyError(code, "terminal_delivery must be assistant_reply or evidence_only")
    auto_source_excerpt = "include_source_excerpt" not in action
    include_source_excerpt = action.pop("include_source_excerpt", False)
    excerpt = None
    excerpt_decision = "ineligible" if auto_source_excerpt else "disabled"
    if type(include_source_excerpt) is not bool:
        raise caller.EmbeddedNokiyError(code, "include_source_excerpt must be boolean")
    if surface_id is not None and (not isinstance(surface_id, str) or not surface_id.strip()):
        raise caller.EmbeddedNokiyError(code, "surface_id must be an exact nonempty ID")
    if {"context_capsule", "jspace_contract", "request_id", "request_sha256", "model_selection"} & draft.keys():
        raise caller.EmbeddedNokiyError(code, "prepare requires a new draft, not a submitted request")
    if not isinstance(action.get("mission"), dict) or not action["mission"].get("task_id"):
        raise caller.EmbeddedNokiyError(code, "explicit mission/task_id required")
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise caller.EmbeddedNokiyError(code, "new absolute output directory required")
    workspace = caller._plain_path(draft.get("workspace"), name="workspace", directory=True)
    action, operable = _declared_writes_operable(action, workspace)
    input_adaptations = input_adaptations + operable
    action, revisable = _new_create_targets_modifiable(action, workspace)
    input_adaptations = input_adaptations + revisable
    action, spelled = _source_sections_spelled(action, workspace)
    input_adaptations = input_adaptations + spelled
    compiler = workspace / "scripts/ops/dcf.py"
    draft.setdefault("schema_version", caller.REQUEST_SCHEMA_VERSION)
    draft.setdefault("execution_profile", "direct")
    draft.setdefault("timeout_seconds", None)
    draft.setdefault("max_trajectory_bytes", None)
    draft.setdefault("native_thread_id", os.environ.get("CODEX_THREAD_ID"))
    draft.setdefault("persistence_mode", "native_codex_thread_only")
    if (draft["schema_version"] != caller.REQUEST_SCHEMA_VERSION
            or draft["execution_profile"] not in {"direct", "balanced"}
            or not draft["native_thread_id"]
            or draft["native_thread_id"] != os.environ.get("CODEX_THREAD_ID")):
        raise caller.EmbeddedNokiyError(code, "current native thread and full-core profile required")
    if terminal_delivery == "evidence_only":
        draft["terminal_delivery"] = terminal_delivery
    if initial_task_state is not None:
        draft["initial_task_state"] = initial_task_state
    from .local_context import MODE, adapt_action as adapt_local_action, compile_context, dcf_root, jspace_root
    managed_root = dcf_root(workspace)
    if managed_root is not None and "inspect" not in action and jspace_root(workspace) is None:
        # This checkout's DCF predates J-Space (a branch forked before it) and compiles no
        # contract at all; exact local grants with snapshots are the authority that applies.
        # A J-Space checkout whose DCF fails keeps its refusal: that is no downgrade path.
        # DCF data copied without the DCF program (a frozen snapshot) compiles nothing either.
        jspace = managed_root / "scripts/ops/dcf/jspace.py"
        program = managed_root / "scripts/ops/dcf.py"
        data_only = ((jspace.exists() or jspace.is_symlink())
                     and not (program.exists() or program.is_symlink()))
        managed_root = None
        input_adaptations.append({"path": "workspace", "rule": "dcf_data_without_program_to_local_context"
                                  if data_only else "dcf_without_jspace_to_local_context"})
        if surface_id is not None:
            surface_id = None
            input_adaptations.append({"path": "surface_id", "rule": "local_context_needs_no_surface"})
        for key in ("surface_targets", "navigation_targets"):
            if key in action:
                action = {name: value for name, value in action.items() if name != key}
                input_adaptations.append({"path": "action." + key, "rule": "local_context_needs_no_surface"})
        if include_source_excerpt:
            include_source_excerpt = False
            input_adaptations.append({"path": "action.include_source_excerpt",
                                      "rule": "local_context_needs_no_surface"})
    # Checked after the local-context adaptations, which turn a DCF checkout that compiles no
    # J-Space contract into the unconfigured local request this preparation supports.
    if post_batch_verifier and (managed_root is not None or surface_id is not None
            or "inspect" in action or draft.get("authority_effect") != "none"
            or draft.get("execution_profile") != "direct" or "verifier_commands" not in action):
        raise caller.EmbeddedNokiyError("NOKIY_POST_BATCH_PREPARATION_UNSUPPORTED",
                                       "post-batch preparation requires a read-only unconfigured local direct request")
    if "inspect" in action:
        # Read-and-record runs inside the bound Seatbelt profile and grants no repository
        # writes, so it needs no DCF surface even in a DCF-managed workspace.
        from .inspect_context import MODE as INSPECT_MODE, compile_context as compile_inspect_context
        if surface_id is not None:
            # An explicit surface would only bound DCF reads; the fenced shell uses none.
            input_adaptations.append({"path": "surface_id", "rule": "inspect_needs_no_surface"})
            surface_id = None
        if include_source_excerpt or {"surface_targets", "navigation_targets"} & set(action):
            raise caller.EmbeddedNokiyError(code, "inspect actions take no DCF surface or source excerpt")
        artifact_root = caller._plain_path(draft.get("artifact_root"), name="artifact_root", directory=True)
        capsule, contract = compile_inspect_context(workspace, action, artifact_root=artifact_root)
        compiler_before = _identity(Path(__file__).with_name("inspect_context.py"))
        context_mode = INSPECT_MODE
    elif managed_root is None:
        if "surface_targets" in action:
            raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_SURFACE_TARGET_INVALID",
                                             "surface_targets require a DCF workspace")
        if include_source_excerpt:
            raise caller.EmbeddedNokiyError(code, "source excerpt requires a DCF workspace")
        if "navigation_targets" in action:
            raise caller.EmbeddedNokiyError(code, "source-navigation is available only in a DCF workspace")
        if surface_id is not None:
            raise caller.EmbeddedNokiyError(code, "DCF surface requested but workspace has no DCF; omit surface for local context")
        # The request artifact root also holds the V431 parent shell's evidence and bound profile.
        artifact_root = (caller._plain_path(draft.get("artifact_root"), name="artifact_root", directory=True)
                         if "verifier_commands" in action or draft.get("artifact_root") is not None else None)
        action, local_adaptations = adapt_local_action(action, workspace)
        input_adaptations.extend(local_adaptations)
        action, absent_reads = _absent_gate_reads(action, workspace, post_batch_verifier)
        input_adaptations.extend(absent_reads)
        capsule, contract = compile_context(workspace, action, artifact_root=artifact_root,
            **({"post_batch_verifier": True} if post_batch_verifier else {}))
        compiler_before = _identity(Path(__file__).with_name("local_context.py"))
        context_mode = MODE
    else:
        if managed_root != workspace or not compiler.is_file():
            raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_DCF_UNAVAILABLE", "use the DCF root and its working interpreter; no local downgrade")
        python = _dcf_interpreter(workspace)
        interpreter_before = _dcf_interpreter_identity(workspace, python)
        compiler = _dcf_code_root(workspace) / "scripts/ops/dcf.py"
        action, paired = pair_discovery(action, workspace)
        input_adaptations.extend(paired)
        action = _with_parent_shell(action, workspace, draft)
        if "verifier_commands" in action:
            artifact_root = caller._plain_path(draft.get("artifact_root"), name="artifact_root", directory=True)
            if action.get("verifier_artifact_root", str(artifact_root)) != str(artifact_root):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_FULL_STACK_VERIFIER_ARTIFACT_ROOT_MISMATCH",
                    "verifier artifact root differs from request",
                )
            action = dict(action, verifier_artifact_root=str(artifact_root))
        resolution = None
        target_identities = {}
        if "surface_targets" in action:
            action, surface_id, resolution, target_identities = _resolve_surface_targets(
                workspace, python, action, surface_id)
        if not isinstance(surface_id, str) or not surface_id.strip():
            raise caller.EmbeddedNokiyError(code, "DCF-managed workspace requires an exact surface",
                                             issues=[{"path": "surface_id", "reason": "surface_required"}])
        if "navigation_targets" in action:
            capsule, contract, compiler_before, action, locators = _compile_navigated_dcf(
                workspace, compiler, python, surface_id, action)
        else:
            locators = []
            applied = set()
            while True:
                try:
                    capsule, contract, compiler_before = _compile_dcf(workspace, compiler, python, surface_id, action)
                    break
                except caller.EmbeddedNokiyError as error:
                    # Each mechanical repair applies at most once, so this ends.
                    action, repair = _dcf_compile_repair(action, error, workspace)
                    if repair is None or repair["rule"] in applied:
                        raise
                    applied.add(repair["rule"])
                    input_adaptations.append(repair)
        if resolution is not None:
            if (contract.get("dcf_generation", {}).get("generation_id") != resolution["generation_id"]
                    or contract.get("matched_surface_ids") != [surface_id]
                    or not _surface_files_still_current(workspace, target_identities)):
                raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED,
                                                 "surface resolution drifted before J-Space compilation")
        if (locators and contract.get("dcf_generation", {}).get("generation_id")
                != locators[0]["generation_id"]):
            raise caller.EmbeddedNokiyError(
                "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED",
                "DCF generation changed between navigation and J-Space compilation",
            )
        auto_excerpt = auto_source_excerpt and _auto_excerpt_eligible(draft, contract, locators)
        # Explicit true retains its original single-symbol readonly semantics;
        # Only new automatic edit preparation opts into envelope-bounded allocation.
        edit_excerpt = auto_source_excerpt and _auto_edit_excerpt_eligible(draft, contract, locators)
        from .source_excerpt import (
            CONTEXT_MARKER, MAX_ENVELOPE_BYTES, OUTLINE_KIND, READONLY_PARTIAL_KIND,
            ExcerptBudgetExceeded, encode_context, extract, extract_edits, extract_outline,
            readonly_outline_eligible, still_current,
        )
        if include_source_excerpt or auto_excerpt or edit_excerpt:
            if not edit_excerpt and (draft.get("authority_effect") != "none"
                    or draft.get("require_tool_call") is not False or len(locators) != 1):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "one read-only, no-tool DCF symbol required",
                )
            if auto_excerpt or edit_excerpt:
                try:
                    excerpt = (extract_edits(workspace, contract, locators, per_target_budget=True,
                                             support_navigation=True, envelope_bounded=True)
                               if edit_excerpt else
                               extract(workspace, contract, locators[0], include_dependencies=True,
                                       allow_definitions_only=True))
                    if len(json.dumps(excerpt, ensure_ascii=True).encode()) > MAX_ENVELOPE_BYTES:
                        raise ExcerptBudgetExceeded(
                            "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "serialized excerpt exceeds context budget",
                        )
                except ExcerptBudgetExceeded:
                    # No complete target/envelope fits: leave navigation and all
                    # tools intact; use ordinary source_read without truncation.
                    excerpt = None
                    excerpt_decision = "capacity_exceeded"
            else:
                excerpt = extract(workspace, contract, locators[0])
            if excerpt is not None:
                excerpt_decision = ("auto_edit" if edit_excerpt else
                                    "auto_definitions_only" if auto_excerpt and
                                    excerpt.get("kind") == READONLY_PARTIAL_KIND else
                                    "auto" if auto_excerpt else "explicit")
        elif (auto_source_excerpt and not locators and draft.get("authority_effect") == "none"
                and isinstance(action.get("context_summary"), str)
                and action["context_summary"].strip() and readonly_outline_eligible(contract)
                and len(contract["read_scopes"]) > 1):
            try:
                excerpt = extract_outline(workspace, contract)
                outline_context = (action["context_summary"].strip() + CONTEXT_MARKER
                                   + encode_context(excerpt))
                if len(outline_context.encode()) > MAX_ENVELOPE_BYTES:
                    excerpt = None
                    excerpt_decision = "capacity_exceeded"
                else:
                    excerpt_decision = "auto_outline"
            except ExcerptBudgetExceeded:
                excerpt_decision = "capacity_exceeded"
            except caller.EmbeddedNokiyError:
                # Optional navigation must not block a valid ordinary read path.
                excerpt_decision = "outline_unavailable"
        if excerpt is not None:
            augmented = dict(action)
            augmented["context_summary"] = (
                action["context_summary"].strip()
                + CONTEXT_MARKER
                + encode_context(excerpt)
            )
            if not still_current(workspace, excerpt):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "source changed before capsule rebinding",
                )
            capsule = _rebind_dcf_capsule(workspace, compiler, python, compiler_before,
                                          contract, augmented)
            if ((locators and contract.get("dcf_generation", {}).get("generation_id")
                    != locators[0]["generation_id"])
                    or not still_current(workspace, excerpt)
                    or _identity(compiler) != compiler_before):
                raise caller.EmbeddedNokiyError(
                    "NOKIY_SOURCE_EXCERPT_UNVERIFIED", "source or DCF compiler changed",
                )
            action = augmented
        if resolution is not None and (contract.get("dcf_generation", {}).get("generation_id")
                                       != resolution["generation_id"]
                                       or not _surface_files_still_current(workspace, target_identities)):
            raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED, "surface resolution drifted during preparation")
        context_mode = "dcf_jspace_required"
    expected_verifier_root = draft.get("artifact_root")
    if post_batch_verifier:
        from .local_context import _post_batch_artifact_root
        expected_verifier_root = str(_post_batch_artifact_root(workspace, artifact_root,
            action.get("verifier_commands"), action.get("operations"),
            action.get("write_scopes", []), action.get("target_paths", [])))
    if "verifier_commands" in action and (
            contract.get("verifier_commands") != action["verifier_commands"]
            or contract.get("verifier_artifact_root") != expected_verifier_root):
        raise caller.EmbeddedNokiyError("NOKIY_FULL_STACK_VERIFIER_UNBOUND",
                                         "compiler did not bind the requested exact verifier grants")
    # A used directory is never reused, even after a failed prepare. Do not erase evidence.
    if (context_mode == "dcf_jspace_required"
            and _dcf_interpreter_identity(workspace, python) != interpreter_before):
        raise _dcf_unavailable("DCF interpreter binding changed before preparation publication")
    output.mkdir(mode=0o700)
    caller._write_create_only(output / "capsule.json", capsule)
    caller._write_create_only(output / "jspace.json", contract)
    draft.update(context_capsule=_identity(output / "capsule.json"),
                 jspace_contract=_identity(output / "jspace.json"))
    if selection is not None:
        from .model_topology import request_marker
        draft["model_selection"] = request_marker(selection)
    request = caller.decode_request(draft)
    if post_batch_verifier:
        from .full_core import preflight
        ready = preflight(request, post_batch_verifier=True)
    else:
        ready = caller.preflight(request)
    if context_mode == "dcf_jspace_required" and resolution is not None:
        if not _surface_files_still_current(workspace, target_identities):
            raise caller.EmbeddedNokiyError(_SURFACE_UNVERIFIED,
                                             "surface targets changed during preflight")
    preparation = {
        "status": "PREPARED", "execution_profile": request.execution_profile,
        "context_mode": context_mode, "request_id": request.request_id,
        "compiler": compiler_before, "action": action, "preflight": ready,
        "provider_execution_started": False,
    }
    if input_adaptations:
        preparation["input_adaptations"] = input_adaptations
        preparation["original_action_sha256"] = original_action_sha256
    if context_mode == "dcf_jspace_required":
        preparation["navigation_locators"] = locators
        if resolution is not None:
            preparation["surface_resolution"] = resolution
        preparation["source_excerpt_decision"] = excerpt_decision
        if excerpt is not None:
            if excerpt.get("kind") == OUTLINE_KIND:
                preparation["source_excerpt"] = {
                    "kind": OUTLINE_KIND,
                    "files": [{"path": row["path"], "source_sha256": row["source_sha256"],
                               "definition_count": len(row["definitions"])} for row in excerpt["files"]],
                }
            elif excerpt.get("kind") in ("edit_preimage", READONLY_PARTIAL_KIND):
                # Source bytes occur only in the capsule/action context, never
                # again in the public preparation receipt or per-symbol rows.
                preparation["source_excerpt"] = {
                    "kind": excerpt["kind"],
                    "files": [{"path": row["path"], "source_sha256": row["source_sha256"],
                               "spans": [{key: span[key] for key in ("start_line", "end_line")}
                                         for span in row["spans"]]} for row in excerpt["files"]],
                }
            else:
                preparation["source_excerpt"] = {
                    key: excerpt[key] for key in ("path", "start_line", "end_line", "source_sha256")
                }
    if selection is not None:
        from .model_topology import bind_prepared_request
        binding = bind_prepared_request(selection, request)
        caller._write_create_only(output / "model-selection.json", binding)
        preparation["model_selection"] = _identity(output / "model-selection.json")
        preparation["root_topology_verified"] = False
    caller._write_create_only(output / "preparation.json", preparation)
    caller._write_create_only(output / "request.json", request.to_wire(include_identity=False))
    public_preparation = {key: value for key, value in preparation.items() if key != "action"}
    action_bytes = caller._canonical_bytes(action)
    public_action = {"sha256": hashlib.sha256(action_bytes).hexdigest(), "bytes": len(action_bytes)}
    summary = action.get("context_summary")
    if isinstance(summary, str):
        public_action["context_summary_sha256"] = hashlib.sha256(summary.encode()).hexdigest()
        public_action["context_summary_chars"] = len(summary)
    public_preparation["action"] = public_action
    return {**public_preparation, "request": _identity(output / "request.json")}


def _dcf_failure_diagnostics(completed, stage):
    stdout = completed.stdout if isinstance(completed.stdout, str) else ""
    stderr = completed.stderr if isinstance(completed.stderr, str) else ""
    out, err = stdout.encode("utf-8"), stderr.encode("utf-8")
    diagnostics = {"stage": stage, "returncode": completed.returncode,
                   "stdout_bytes": len(out), "stderr_bytes": len(err),
                   "stdout_sha256": hashlib.sha256(out).hexdigest(),
                   "stderr_sha256": hashlib.sha256(err).hexdigest()}
    # Only the project's no-effect typed envelope can supply a public cause.
    if completed.returncode and len(out) <= caller.MAX_REQUEST_BYTES:
        try:
            payload = json.loads(stdout, object_pairs_hook=caller._unique_object,
                                 parse_constant=caller._invalid_constant)
            fields = {"schema_version", "status", "error_code", "authority_effect", "no_apply"}
            if (type(payload) is dict and fields <= set(payload)
                    and not set(payload) - fields - {"error", "capability_diagnostic"}
                    and payload["schema_version"] == "jspace_error_v1"
                    and payload["status"] in {"blocked", "invalid"}
                    and payload["authority_effect"] == "none" and payload["no_apply"] is True
                    and type(payload["error_code"]) is str
                    and caller._PREPARATION_CHILD_CODE.fullmatch(payload["error_code"])):
                diagnostics["child_error_code"] = payload["error_code"]
                from .dcf_prepare import safe_capability_diagnostic
                diagnostic = safe_capability_diagnostic(
                    payload.get("capability_diagnostic"), payload["error_code"])
                if diagnostic is not None:
                    diagnostics["capability_diagnostic"] = diagnostic
        except (ValueError, TypeError, caller.EmbeddedNokiyError):
            pass
    return caller._safe_preparation_diagnostics(diagnostics)


def _compile_navigated_dcf(workspace, compiler, python, surface_id, action):
    from .dcf_prepare import bind_locators, targets_for
    code = "NOKIY_FULL_STACK_NAVIGATION_UNVERIFIED"
    bridge = Path(__file__).with_name("dcf_prepare.py")
    compiler_before, bridge_before = _identity(compiler), _identity(bridge)
    completed = None
    try:
        targets_for(action)
        completed = _run_dcf_python(
            workspace, python, ["-B", str(bridge)], env=_dcf_env(compiler.parents[2]),
            input=json.dumps({"surface_id": surface_id, "action": action}),
            capture_output=True, text=True, timeout=60,
        )
        if completed.returncode or len(completed.stdout.encode()) > caller.MAX_REQUEST_BYTES:
            raise ValueError("DCF preparation failed or response exceeded budget")
        compiled = json.loads(completed.stdout)
        capsule, contract = compiled["task_context_capsule"], compiled["contract"]
        if not isinstance(capsule, dict) or not isinstance(contract, dict):
            raise ValueError("DCF did not provide canonical capsule/contract")
        locators = compiled["navigation_locators"]
        amended = bind_locators(action, locators, contract["dcf_generation"]["generation_id"])
        if (compiled["action"] != amended
                or capsule.get("context_summary") != amended["context_summary"]
                or capsule.get("dcf_generation") != contract["dcf_generation"]
                or _identity(compiler) != compiler_before or _identity(bridge) != bridge_before):
            raise ValueError("DCF context, generation or implementation changed")
        return capsule, contract, compiler_before, amended, locators
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError) as error:
        diagnostics = (_dcf_failure_diagnostics(completed, "dcf_navigated_prepare")
                       if completed is not None else None)
        raise caller.EmbeddedNokiyError(code, "single-pass DCF preparation did not verify",
                                        diagnostics=diagnostics) from error


def _compile_dcf(workspace, compiler, python, surface_id, action):
    code = "NOKIY_FULL_STACK_PREPARATION_FAILED"
    bridge = Path(__file__).with_name("dcf_prepare.py")
    compiler_before, bridge_before = _identity(compiler), _identity(bridge)
    try:
        completed = _run_dcf_python(
            workspace, python, ["-B", str(bridge)], env=_dcf_env(compiler.parents[2]),
            input=json.dumps({"surface_id": surface_id, "action": action}),
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(code, "DCF compiler did not finish") from error
    if completed.returncode or len(completed.stdout.encode()) > caller.MAX_REQUEST_BYTES:
        raise caller.EmbeddedNokiyError(code, "DCF compilation failed or response exceeded budget",
                                        diagnostics=_dcf_failure_diagnostics(completed, "dcf_compile"))
    try:
        compiled = json.loads(completed.stdout)
        capsule, contract = compiled["task_context_capsule"], compiled["contract"]
        if not isinstance(capsule, dict) or not isinstance(contract, dict):
            raise ValueError("invalid DCF result")
    except (ValueError, KeyError, TypeError) as error:
        raise caller.EmbeddedNokiyError(code, "DCF did not provide canonical capsule/contract",
                                        diagnostics=_dcf_failure_diagnostics(completed, "dcf_compile")) from error
    if compiler_before != _identity(compiler) or bridge_before != _identity(bridge):
        raise caller.EmbeddedNokiyError(code, "DCF compiler or bridge changed during preparation")
    return capsule, contract, compiler_before


def _rebind_dcf_capsule(workspace: Path, compiler: Path, python: Path,
                        compiler_before: dict, contract: dict, action: dict) -> dict:
    """Bind context through DCF's canonical compiler; never recompile authority."""
    code = "NOKIY_SOURCE_EXCERPT_UNVERIFIED"
    if _identity(compiler) != compiler_before:
        raise caller.EmbeddedNokiyError(code, "DCF compiler changed before rebinding")
    script = _with_verifier_grammar(
        "import json,sys;"
        "from scripts.ops.dcf.jspace import (canonical_contract_bytes,"
        "compile_task_context_capsule,render_task_context_capsule);"
        "data=json.load(sys.stdin);contract=data['contract'];"
        "canonical_contract_bytes(contract);"
        "capsule=compile_task_context_capsule(contract,action=data['action']);"
        "render_task_context_capsule(capsule,expected_task_id=data['action']['mission']['task_id'],"
        "expected_jspace_sha256=contract['authorization_semantic_sha256']);"
        "json.dump(capsule,sys.stdout,sort_keys=True)"
    )
    try:
        completed = _run_dcf_python(
            workspace, python, ["-B", "-c", script],
            input=json.dumps({"contract": contract, "action": action}),
            capture_output=True, text=True, timeout=30, env=_dcf_env(compiler.parents[2]),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise caller.EmbeddedNokiyError(code, "DCF capsule compiler did not finish") from error
    if completed.returncode or len(completed.stdout.encode()) > caller.MAX_REQUEST_BYTES:
        raise caller.EmbeddedNokiyError(code, "DCF capsule rebinding failed or exceeded budget")
    try:
        capsule = json.loads(completed.stdout)
        if (not isinstance(capsule, dict)
                or capsule.get("jspace_semantic_sha256")
                != contract["authorization_semantic_sha256"]
                or capsule.get("dcf_generation") != contract["dcf_generation"]
                or capsule.get("mission", {}).get("task_id") != action["mission"]["task_id"]
                or capsule.get("context_summary") != action["context_summary"].strip()
                or capsule.get("semantic_sha256") != caller._canonical_sha256(
                    {key: value for key, value in capsule.items() if key != "semantic_sha256"})):
            raise ValueError("capsule does not bind the original contract and action")
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise caller.EmbeddedNokiyError(code, "DCF capsule rebinding was not canonical") from error
    if _identity(compiler) != compiler_before:
        raise caller.EmbeddedNokiyError(code, "DCF compiler changed during rebinding")
    return capsule
