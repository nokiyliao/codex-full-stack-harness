# SPDX-License-Identifier: MIT
"""Bounded workspace context for projects without a DCF integration."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import shutil
import shlex

from . import embedded_nokiy as caller
from .dcf_prepare import (VERIFIER_TIMEOUT_MAX_SECONDS, argv_shell_syntax, inline_interpreter_code,
                          interpreter_reads_entries, work_product_only)

MODE = "local_workspace_jspace"
CODE = "NOKIY_LOCAL_CONTEXT_INVALID"
_SHELLS = {"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh", "ksh", "env", "osascript"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MACHO = {bytes.fromhex(magic) for magic in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe",
    "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
)}
_SECTIONS_MARKER = "\nExact local source sections (context only; not a new grant):\n"
_MAX_SECTIONS = 16
_MAX_SECTION_LINES = 400
_MAX_SECTIONS_BYTES = 12_000
_MAX_SUMMARY_CHARS = 12_000
_SOURCE_READ_FILE_BYTES = 1_048_576
_MAX_CONTEXT_FILES = 32


class _InputTooLarge(caller.EmbeddedNokiyError):
    """A regular context file above its whole-file snapshot bound."""


def _invalid(detail: str, field: str, reason: str = "invalid_shape", *,
             code: str = CODE) -> caller.EmbeddedNokiyError:
    # Only static field/reason pairs survive the caller's diagnostic allowlist.
    # Keep detail intact for its existing hash; never publish it or input values.
    return caller.EmbeddedNokiyError(code, detail, issues=[{"path": field, "reason": reason}])


def dcf_root(workspace: Path) -> Path | None:
    # An absent interpreter, broken link or a subdirectory is not an opt-out.
    for root in (workspace, *workspace.parents):
        for name in ("scripts/ops/dcf.py", "config/contracts/dcf_v2_contract.json"):
            path = root / name
            if path.exists() or path.is_symlink():
                return root
    return None


def jspace_root(workspace: Path) -> Path | None:
    """The DCF root when its checkout can compile J-Space contracts, else None.

    A DCF that predates J-Space (a branch forked before it) never governed through J-Space,
    so exact local grants are the authority that applies there. The same holds for DCF data
    copied without the DCF program (no scripts/ops/dcf.py, such as a frozen snapshot): nothing
    there can compile a J-Space contract. A broken or uninterpretable program is still a
    program and keeps the no-downgrade refusal."""
    root = dcf_root(workspace)
    if root is None:
        return None
    present = [root / "scripts/ops/dcf/jspace.py", root / "scripts/ops/dcf.py"]
    return root if all(path.exists() or path.is_symlink() for path in present) else None


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64 or any(
        not isinstance(v, str) or not v.strip() for v in value
    ):
        raise caller.EmbeddedNokiyError(CODE, f"{field} requires a bounded string list",
            issues=[{"path": f"action.{field}", "reason": "invalid_string_list"}])
    return sorted(set(value))


def _file(workspace: Path, name: str) -> Path:
    path = Path(name)
    if (path.is_absolute() or path.as_posix() != name or any(c in name for c in "*?[]\x00\r\n")
            or any(p in {".", "..", ".git"} for p in path.parts) or not path.parts):
        raise caller.EmbeddedNokiyError(CODE, "local scopes require exact workspace-relative files")
    target = workspace / path
    if not target.resolve().is_relative_to(workspace):
        raise caller.EmbeddedNokiyError(CODE, "path escapes workspace")
    for node in (target, *target.parents):
        if node == workspace:
            break
        if node.is_symlink():
            raise caller.EmbeddedNokiyError(CODE, "local scopes cannot traverse symlinks")
    return target


def _snapshot_entry(workspace: Path, name: str, max_bytes: int) -> dict:
    flags = os.O_RDONLY | os.O_NOFOLLOW
    directory = os.open("/", flags | os.O_DIRECTORY)
    source = None
    try:
        for part in (*workspace.parts[1:], *Path(name).parts[:-1]):
            next_directory = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        source = os.open(Path(name).parts[-1], flags, dir_fd=directory)
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode):
            raise caller.EmbeddedNokiyError(CODE, "context inputs must be bounded regular files")
        if before.st_size > max_bytes:
            # Distinct from "unavailable": the file exists, and a smaller route can reach it.
            raise _InputTooLarge(CODE, "context input exceeds the whole-file snapshot bound")
        data = bytearray()
        while len(data) <= max_bytes:
            chunk = os.read(source, max_bytes + 1 - len(data))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(source)
        def identity(info):
            return (info.st_dev, info.st_ino, info.st_size, info.st_mode,
                    info.st_mtime_ns, info.st_ctime_ns)
        if len(data) != before.st_size or identity(before) != identity(after):
            raise caller.EmbeddedNokiyError(CODE, "context input changed during bounded read")
        return {"sha256": hashlib.sha256(data).hexdigest(), "mode": stat.S_IMODE(before.st_mode)}
    finally:
        if source is not None:
            os.close(source)
        os.close(directory)


def _snapshot(workspace: Path, names: list[str], *, max_bytes: int = caller.MAX_REQUEST_BYTES,
              issue_fields: dict[str, str] | None = None) -> dict:
    snapshots = {}
    for name in names:
        field = issue_fields.get(name) if issue_fields is not None else None
        try:
            _file(workspace, name)
        except caller.EmbeddedNokiyError as error:
            if field is None:
                raise
            raise caller.EmbeddedNokiyError(error.code, error.detail,
                issues=[{"path": field, "reason": "unsafe_path"}]) from error
        try:
            snapshots[name] = _snapshot_entry(workspace, name, max_bytes)
        except FileNotFoundError:
            snapshots[name] = None
        except caller.EmbeddedNokiyError as error:
            if field is None:
                raise
            reason = "exact_file_too_large" if isinstance(error, _InputTooLarge) else "exact_file_unavailable"
            raise caller.EmbeddedNokiyError(error.code, error.detail,
                issues=[{"path": field, "reason": reason}]) from error
        except OSError as error:
            raise caller.EmbeddedNokiyError(CODE, "context input unavailable",
                issues=([{"path": field, "reason": "exact_file_unavailable"}]
                        if field is not None else None)) from error
    return snapshots


def _source_sections(workspace: Path, raw: object, reads: list[str], snapshots: dict) -> list[dict]:
    from .source_excerpt import _read_exact

    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_SECTIONS:
        raise _invalid("source_sections requires 1..16 exact line ranges", "action.source_sections")
    sections = []
    seen = set()
    sources = {}
    total_bytes = 0
    for index, locator in enumerate(raw):
        if not isinstance(locator, dict) or set(locator) != {"path", "start_line", "end_line"}:
            raise _invalid("source section requires exact path/start_line/end_line", "action.source_sections")
        name, start, end = locator["path"], locator["start_line"], locator["end_line"]
        if (not isinstance(name, str) or name not in reads
                or type(start) is not int or type(end) is not int
                or start < 1 or end < start or end - start + 1 > _MAX_SECTION_LINES
                or (name, start, end) in seen):
            raise _invalid("source section must be a distinct bounded exact scoped read", "action.source_sections")
        seen.add((name, start, end))
        if name not in sources:
            _file(workspace, name)
            try:
                data = _read_exact(workspace, name, python_only=False)
                # Decode once, retaining bytes.splitlines' CR/LF boundaries and exact endings.
                lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", data.decode("utf-8"))
                if lines[-1] == "":
                    lines.pop()
            except (caller.EmbeddedNokiyError, UnicodeError) as error:
                raise _invalid("exact source section read failed", "action.source_sections",
                               "exact_file_unavailable") from error
            source_sha256 = hashlib.sha256(data).hexdigest()
            if snapshots.get(name, {}).get("sha256") != source_sha256:
                raise _invalid("source section exceeds budget or source snapshot changed", "action.source_sections")
            # Invocation-local only: compile and verify must each obtain a fresh source snapshot.
            sources[name] = lines, source_sha256
        lines, source_sha256 = sources[name]
        if end > len(lines):
            raise _invalid(f"source section action.source_sections[{index}] range {start}..{end} "
                           f"exceeds available line count {len(lines)}", "action.source_sections")
        excerpt = "".join(lines[start - 1:end])
        excerpt_bytes = excerpt.encode("utf-8")
        total_bytes += len(excerpt_bytes)
        if total_bytes > _MAX_SECTIONS_BYTES:
            raise _invalid("source sections exceed aggregate byte budget", "action.source_sections")
        sections.append({"path": name, "start_line": start, "end_line": end,
                         "source_sha256": source_sha256,
                         "section_sha256": hashlib.sha256(excerpt_bytes).hexdigest(), "text": excerpt})
    return sections


def _query_sections(workspace: Path, queries: object, reads: list[str], snapshots: dict) -> list[dict]:
    """Resolve literal references into existing source-bound section evidence."""
    from .source_excerpt import _read_exact

    if (not isinstance(queries, list) or not 1 <= len(queries) <= 16
            or any(not isinstance(q, str) or not q.strip() or len(q) > 256 for q in queries)):
        raise _invalid("source_queries requires 1..16 nonempty literal strings", "action.source_queries")
    ranges = []
    source_lines = {}
    for name in reads:
        if snapshots.get(name) is None:
            continue
        data = _read_exact(workspace, name, python_only=False)
        if hashlib.sha256(data).hexdigest() != snapshots[name]["sha256"]:
            raise _invalid("source query snapshot changed", "action.source_queries")
        try:
            lines = re.split(r"(?<=\n)|(?<=\r)(?!\n)", data.decode("utf-8"))
        except UnicodeError:
            continue
        if lines[-1] == "":
            lines.pop()
        source_lines[name] = lines
        for index, line in enumerate(lines):
            if not any(query in line for query in queries):
                continue
            start, end = max(1, index - 2), min(len(lines), index + 4)
            if ranges and ranges[-1]["path"] == name and start <= ranges[-1]["end_line"] + 1:
                ranges[-1]["end_line"] = end
            else:
                ranges.append({"path": name, "start_line": start, "end_line": end})
    # Optional discovery never truncates a result or prevents ordinary scoped reads.
    if (not ranges or len(ranges) > _MAX_SECTIONS
            or any(r["end_line"] - r["start_line"] + 1 > _MAX_SECTION_LINES for r in ranges)
            or sum(len("".join(source_lines[r["path"]][r["start_line"] - 1:r["end_line"]]).encode())
                   for r in ranges) > _MAX_SECTIONS_BYTES):
        return []
    return ranges


def adapt_action(action: dict, workspace: Path | None = None) -> tuple[dict, list[dict]]:
    """Resolve local scope mismatches instead of rejecting them; each change is reported.

    Directory write grants narrow to the exact targets they name, and a missing summary comes
    from the complete mission. With the workspace, exact reads of a writing action that the
    worker could never open become a bounded preview."""
    action, repairs = _narrow_directory_writes(action)
    summary = action.get("context_summary")
    if summary is None or (isinstance(summary, str) and not summary.strip()):
        stated = summary_from_mission(action)
        if stated is not None:
            action = dict(action, context_summary=stated)
            repairs = repairs + [{"path": "action.context_summary", "rule": "summary_from_mission"}]
    action, noted = unadmitted_commands_to_parent(action, _local_admits)
    repairs = repairs + noted
    if workspace is not None:
        action, absent = absent_reads_to_note(action, workspace)
        repairs = repairs + absent
        action, routed = _preview_unreadable_reads(action, workspace)
        repairs = repairs + routed
    action, sourced = uncovered_reads_to_source_read(action)
    repairs = repairs + sourced
    return action, repairs


def uncovered_reads_to_source_read(action: dict) -> tuple[dict, list[dict]]:
    """Exact reads with no reader become typed source reads.

    A read scope that no cat template, source section or discovery root covers could never be
    opened; the typed exact-file reader is the reader it implies, with the read and command
    operations source_read requires. Inspect actions use their own fenced shell."""
    reads, operations = action.get("read_scopes"), action.get("operations")
    if (action.get("source_read") is True or "inspect" in action
            or not isinstance(reads, list) or not isinstance(operations, list)
            or any(not isinstance(value, str) for value in reads + operations)):
        return action, []
    directories = [name for name in action.get("read_directories") or [] if isinstance(name, str)]
    command_reads = {name for template in action.get("command_templates") or []
                     if isinstance(template, dict) and isinstance(template.get("argv"), list)
                     and template["argv"][:1] == ["cat"] for name in template["argv"][1:]}
    sections = {section.get("path") for section in action.get("source_sections") or [] if isinstance(section, dict)}
    if not any(not name.endswith("/**") and name not in command_reads and name not in sections
               and not any(name.startswith(root + "/") for root in directories) for name in reads):
        return action, []
    adapted = dict(action, source_read=True, operations=list(dict.fromkeys(operations + ["read", "command"])))
    return adapted, [{"path": "action.source_read", "rule": "uncovered_reads_to_source_read"}]


def _local_admits(template: dict, argv: list[str], reads: list[str]) -> bool:
    return template.get("effects") == ["read"] and (
        argv == ["pwd"] or (len(argv) > 1 and argv[0] == "cat"
                            and all(name in reads and not name.startswith("-") for name in argv[1:])))


def unadmitted_commands_to_parent(action: dict, admits) -> tuple[dict, list[dict]]:
    """Move commands a writing action's context cannot admit into a note for the parent.

    Running what the worker writes (its tests or scripts) is parent verification: pinned
    verifier_commands cover files that exist now, and an unadmitted command must not stop the
    authoring itself. The parent sees each moved command in the adaptation and runs it after
    delivery. Read-only actions keep the refusal, which prepares them as inspect, where the
    fenced shell can run the command. Malformed templates are left to the compiler."""
    templates, reads = action.get("command_templates"), action.get("read_scopes") or []
    writes, operations = action.get("write_scopes") or [], action.get("operations")
    if (not isinstance(templates, list) or not templates or not isinstance(reads, list)
            or not isinstance(writes, list) or not isinstance(operations, list)
            or not (writes or {"create", "modify", "delete"}.intersection(
                op for op in operations if isinstance(op, str)))):
        return action, []
    kept, moved = [], []
    for template in templates:
        argv = template.get("argv") if isinstance(template, dict) else None
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
            return action, []
        if admits(template, argv, reads):
            kept.append(template)
        else:
            moved.append(shlex.join(argv)[:300])
    if not moved:
        return action, []
    adapted = dict(action, command_templates=kept)
    if (not kept and "command" in operations
            and not any(action.get(key) for key in ("verifier_commands", "read_directories", "source_read"))):
        # No command consumer remains: exact reads become source_read, otherwise the grant goes.
        if reads and all(isinstance(name, str) and not any(c in name for c in "*?[]") for name in reads):
            adapted["source_read"] = True
        else:
            adapted["operations"] = [op for op in operations if op != "command"]
    summary = action.get("context_summary")
    if isinstance(summary, str) and summary.strip():
        note = ("\nCommands not admitted in this context (reported to the parent, which can run them): "
                + "; ".join(moved))
        budget = _MAX_SUMMARY_CHARS - len(summary) - 2000
        if budget >= 200:
            adapted["context_summary"] = summary + (note if len(note) <= budget else note[:budget - 4] + " ...")
    return adapted, [{"path": "action.command_templates", "rule": "unadmitted_commands_to_parent",
                      "commands": moved[:8]}]


def summary_from_mission(action: dict) -> str | None:
    """The task as the complete mission states it, for an action that omitted its summary."""
    mission = action.get("mission")
    if not isinstance(mission, dict):
        return None
    objective, predicate = mission.get("objective"), mission.get("current_predicate")
    if not all(isinstance(value, str) and value.strip() for value in (objective, predicate)):
        return None
    return f"{objective.strip()} Current predicate: {predicate.strip()}."[:_MAX_SUMMARY_CHARS - 2000]


def _narrow_directory_writes(action: dict) -> tuple[dict, list[dict]]:
    """Narrow directory write grants to the exact files they name, instead of rejecting.

    Local preparation admits only exact write files, and write_scopes must be a subset of
    target_paths. A coordinator often declares write_scopes ["dir/**"] together with the exact
    target_paths it will produce there; replacing the directory with those named targets is
    strictly narrower. A directory grant that names no target is left to fail as before.
    """
    writes, targets = action.get("write_scopes"), action.get("target_paths")
    if not (isinstance(writes, list) and isinstance(targets, list)
            and all(isinstance(value, str) for value in writes + targets)):
        return action, []
    narrowed, rules = [], set()
    for scope in writes:
        root = scope[:-3] if scope.endswith("/**") else None
        named = ([target for target in targets if target.startswith(root + "/")
                  and not any(c in target for c in "*?[]")]
                 if root and not any(c in root for c in "*?[]") else [])
        if named:
            narrowed.extend(named)
            rules.add("directory_write_scope_to_named_targets")
            continue
        # A one-level pattern such as tests/nokiy-*.test.mjs narrows the same way to the exact
        # targets it names (2026-10-21: an Efficient write glob cost a coordinator round).
        named = [target for target in targets if _glob_names(scope, target)]
        if named:
            narrowed.extend(named)
            rules.add("glob_write_scope_to_named_targets")
        else:
            narrowed.append(scope)
    if not rules:
        return action, []
    return (dict(action, write_scopes=sorted(set(narrowed))),
            [{"path": "action.write_scopes", "rule": rule} for rule in sorted(rules)])


def _glob_names(scope: str, target: str) -> bool:
    """Segment-wise match of an exact target by a glob without recursive ** segments."""
    from fnmatch import fnmatchcase

    if (not any(c in scope for c in "*?[]") or "**" in scope or any(c in target for c in "*?[]")
            or Path(scope).is_absolute() or Path(target).is_absolute()):
        return False
    pattern, name = Path(scope).parts, Path(target).parts
    return (len(pattern) == len(name) and ".." not in name
            and all(fnmatchcase(part, glob) for part, glob in zip(name, pattern)))


_PREVIEW_FILES, _PREVIEW_BYTES, _PREVIEW_LINES, _PREVIEW_LINE_CHARS = 4, 4096, 16, 300


def _preview_unreadable_reads(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """Replace exact reads of a writing action that the worker cannot open with a preview.

    The runtime opens no file above 1 MiB (source_read, rg --max-filesize=1M and cat alike), so
    such a file, usually CSV or JSONL data, can be neither snapshot nor read. It leaves the exact
    list and its head moves into the task context, enough to write code that consumes it. Write
    targets keep their exact snapshot; operations no remaining grant needs are dropped. Read-only
    actions keep the refusal, which prepares them as inspect, where a shell can process the data."""
    reads, directories = action.get("read_scopes"), action.get("read_directories", [])
    operations = action.get("operations")
    write_scopes, targets = action.get("write_scopes") or [], action.get("target_paths") or []
    summary = action.get("context_summary")
    if (any(not isinstance(value, list) for value in (reads, directories, operations, write_scopes, targets))
            or any(not isinstance(value, str) for value in reads + directories + operations + write_scopes + targets)
            or not isinstance(summary, str)):
        return action, []
    writes = write_scopes + targets
    if not writes or "read" not in operations:
        return action, []
    kept, previews = [], []
    for name in reads:
        if name in writes or any(char in name for char in "*?[]"):
            kept.append(name)
            continue
        try:
            path = _file(workspace, name)
            info = path.lstat()
        except (caller.EmbeddedNokiyError, OSError):
            kept.append(name)
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size <= _SOURCE_READ_FILE_BYTES:
            kept.append(name)
            continue
        head = b""
        if len(previews) < _PREVIEW_FILES:
            try:
                with path.open("rb") as stream:
                    head = stream.read(_PREVIEW_BYTES)
            except OSError:
                head = b""
        lines = [line[:_PREVIEW_LINE_CHARS] for line in
                 head.decode("utf-8", "replace").splitlines()[:_PREVIEW_LINES]]
        previews.append(f"- {name}, {info.st_size} bytes, first {len(lines)} lines:\n" + "\n".join(lines))
    if not previews:
        return action, []
    adapted = _with_reads(action, kept, workspace)
    text = ("\nData preview (files above 1 MiB cannot be opened by the worker; context only, not a grant):\n"
            + "\n".join(previews)).replace(_SECTIONS_MARKER.strip(), "[reserved marker removed]")
    # Exact source sections may later fill the summary budget; then the preview yields to them.
    budget = 0 if action.get("source_sections") else _MAX_SUMMARY_CHARS - len(summary) - 1
    if budget >= 200:
        adapted["context_summary"] = summary + (text if len(text) <= budget else text[:budget - 4] + " ...")
    return adapted, [{"path": "action.read_scopes", "rule": "unreadable_read_to_preview"}]


def _with_reads(action: dict, kept: list[str], workspace: Path) -> dict:
    """The action reading only the kept files, with operations that still agree with its grants.

    A file being modified stays readable through its own exact snapshot; with no read grant
    left, what only reads needed (read, source_read, an otherwise unused command) goes."""
    operations = action["operations"]
    directories = action.get("read_directories") or []
    if not kept and not directories and "modify" in operations:
        kept = [name for name in dict.fromkeys(action.get("write_scopes") or []) if (workspace / name).is_file()]
    adapted = dict(action, read_scopes=kept)
    if not kept and not directories:
        adapted.pop("source_read", None)
        dropped = {"read"} | ({"command"} if not action.get("command_templates")
                              and not action.get("verifier_commands") else set())
        adapted["operations"] = [op for op in operations if op not in dropped]
    return adapted


def absent_reads_to_note(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """Drop declared reads of a writing action that do not exist, and say so in the context.

    A missing file grants nothing to read, and refusing would stop the authoring over a stale
    path. Reads that are also write targets keep the create path. Read-only actions keep the
    refusal, which prepares them as inspect, where the worker can look for the right path."""
    def absent(name):
        try:
            path = _file(workspace, name)
        except caller.EmbeddedNokiyError:
            return False
        return not (path.exists() or path.is_symlink())

    return _reads_to_note(action, workspace, absent, "absent_reads_to_note",
                          "\nDeclared reads that do not exist, so not available to this task: ")


def absent_post_batch_reads_to_note(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """Bind a verifier gate's declared reads that do not exist yet through their directory.

    A gate is prepared before its batch runs, so a file a member creates is absent at preparation,
    while the batch admission needs the gate to read every member target. The nearest existing
    parent becomes a discovery root with its recursive read scope, the form the admission accepted
    when a coordinator retried by hand (2026-10-26, DCF two-branch pair). An absent file directly
    under the workspace root, under a hidden directory or beyond the eight-root budget grants
    nothing yet and is dropped with a note; the verifier's pinned commands read the batch result
    when they run. Other read-only actions keep the refusal and prepare as inspect."""
    def absent(name):
        try:
            path = _file(workspace, name)
        except caller.EmbeddedNokiyError:
            return False
        return not (path.exists() or path.is_symlink())

    reads, roots = action.get("read_scopes"), action.get("read_directories") or []
    if (not isinstance(reads, list) or not isinstance(roots, list)
            or any(not isinstance(value, str) for value in reads + roots)):
        return action, []
    roots, scopes, converted = list(roots), list(reads), []
    for name in reads:
        if any(char in name for char in "*?[]") or not absent(name):
            continue
        parent = Path(name).parent
        while parent.parts and not (workspace / parent).is_dir():
            parent = parent.parent
        root = parent.as_posix()
        if (not parent.parts or any(part.startswith(".") for part in parent.parts)
                or (root not in roots and len(roots) >= 8)):
            continue
        if root not in roots:
            roots.append(root)
        scopes = [scope for scope in scopes if scope != name]
        if root + "/**" not in scopes:
            scopes.append(root + "/**")
        converted.append(name)
    adapted, repairs = action, []
    if converted:
        adapted = dict(action, read_scopes=scopes, read_directories=roots)
        repairs.append({"path": "action.read_scopes", "rule": "gate_absent_reads_to_directories",
                        "paths": converted[:16]})
        summary = action.get("context_summary")
        if isinstance(summary, str) and summary.strip():
            note = ("\nDeclared reads that do not exist before the batch runs, read through their directory: "
                    + ", ".join(converted))
            budget = _MAX_SUMMARY_CHARS - len(summary) - 2000
            if budget >= 200:
                adapted["context_summary"] = summary + (note if len(note) <= budget else note[:budget - 4] + " ...")
    adapted, dropped = _reads_to_note(adapted, workspace, absent, "post_batch_absent_reads_to_note",
                                      "\nDeclared reads that do not exist before the batch runs, so not bound here: ",
                                      require_writes=False)
    return adapted, repairs + dropped


def linked_reads_to_note(action: dict, workspace: Path) -> tuple[dict, list[dict]]:
    """Drop declared reads of a writing action that reach through a link DCF cannot map.

    DCF maps a read through a workspace link only under a current mapping contract; without one
    the read grants nothing (2026-10-23: output/exact598_reference linked to a sibling worktree
    stopped a one-file test edit). The authoring proceeds and the context names the dropped
    reads; read-only actions keep the refusal, as for absent reads."""
    def linked(name):
        parts = Path(name).parts
        if (Path(name).is_absolute() or not parts or ".." in parts
                or any(char in name for char in "*?[]\x00\r\n")):
            return False
        return any((workspace.joinpath(*parts[:index])).is_symlink()
                   for index in range(1, len(parts) + 1))

    return _reads_to_note(action, workspace, linked, "unmappable_reads_to_note",
                          "\nDeclared reads behind a link DCF cannot map, so not available to this task: ")


def _reads_to_note(action: dict, workspace: Path, drop, rule: str, label: str,
                   *, require_writes: bool = True) -> tuple[dict, list[dict]]:
    reads, operations = action.get("read_scopes"), action.get("operations")
    write_scopes, targets = action.get("write_scopes") or [], action.get("target_paths") or []
    if (any(not isinstance(value, list) for value in (reads, operations, write_scopes, targets))
            or any(not isinstance(value, str) for value in reads + operations + write_scopes + targets)
            or (require_writes and not (write_scopes or {"create", "modify", "delete"}.intersection(operations)))):
        return action, []
    writes = set(write_scopes + targets)
    kept, dropped = [], []
    for name in reads:
        if name in writes or any(char in name for char in "*?[]"):
            kept.append(name)
            continue
        (dropped if drop(name) else kept).append(name)
    if not dropped:
        return action, []
    adapted = _with_reads(action, kept, workspace)
    summary = action.get("context_summary")
    if isinstance(summary, str) and summary.strip():
        note = label + ", ".join(dropped)
        budget = _MAX_SUMMARY_CHARS - len(summary) - 2000
        if budget >= 200:
            adapted["context_summary"] = summary + (note if len(note) <= budget else note[:budget - 4] + " ...")
    return adapted, [{"path": "action.read_scopes", "rule": rule, "paths": dropped[:16]}]


def _directories(workspace: Path, names: list[str]) -> dict:
    if len(names) > 8:
        raise _invalid("at most 8 discovery directories", "action.read_directories")
    result = {}
    for name in names:
        if name in {".", "**"}:
            raise caller.EmbeddedNokiyError(CODE, "workspace-root discovery is not admitted; declare non-hidden descendant directories",
                issues=[{"path": "action.read_directories", "reason": "unsupported_root"}])
        try:
            path = _file(workspace, name)
        except caller.EmbeddedNokiyError as error:
            raise caller.EmbeddedNokiyError(CODE, error.detail,
                issues=[{"path": "action.read_directories", "reason": "unsafe_path"}]) from error
        if any(p.startswith(".") for p in Path(name).parts):
            raise caller.EmbeddedNokiyError(CODE, "discovery roots must be non-hidden directories",
                issues=[{"path": "action.read_directories", "reason": "unsafe_path"}])
        if not path.is_dir():
            raise caller.EmbeddedNokiyError(CODE, "discovery roots must be existing directories",
                issues=[{"path": "action.read_directories", "reason": "directory_missing"}])
        info = path.stat()
        result[name] = {"device": info.st_dev, "inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode)}
    return result


def _read_commands(roots: list[str]) -> dict:
    policy = {"roots": roots}
    for name in ("rg", "cat"):
        found = shutil.which(name)
        if not found:
            raise _invalid(f"required discovery executable missing: {name}",
                           "action.read_directories", "executable_unavailable")
        path = Path(found).resolve(strict=True)
        policy[name] = {"path": str(path), "sha256": caller._file_sha256(path)}
    return policy


def _verifier_file(raw: object, sha256: object, *, executable: bool, stale: bool,
                   header_is_data: bool = False) -> Path:
    field = "action.verifier_commands.argv" if executable else "action.verifier_commands.pinned_files"
    hash_field = "action.verifier_commands.executable_sha256" if executable else field
    if not isinstance(raw, str) or not raw:
        raise _invalid("verifier file requires an absolute path and lowercase SHA-256", field)
    if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256):
        raise _invalid("verifier file requires an absolute path and lowercase SHA-256", hash_field)
    path = Path(raw)
    if (not path.is_absolute() or ".." in path.parts or str(path) != raw
            or any(node.is_symlink() for node in (path, *path.parents))):
        raise _invalid("verifier files cannot traverse symlinks", field, "unsafe_path")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or (executable and not info.st_mode & 0o111):
            raise _invalid("verifier entry must be a regular executable file", field)
        with path.open("rb") as source:
            header = source.readline(256)
        if executable and header[:4] not in _MACHO:
            raise _invalid("verifier executable must be a pinned Mach-O binary", field, "unsupported_command")
        if header.startswith(b"#!") and not (header_is_data and not executable):
            # Only an operand another program may exec reaches the kernel's #! dispatch; an
            # entry CPython or Node opens as source keeps its header as a comment (dcf_prepare).
            words = header[2:].decode("ascii", errors="ignore").strip().split()
            if words and Path(words[0]).name in _SHELLS:
                raise _invalid("shell and env verifier entries are not admitted", field, "unsupported_command")
        actual = caller._file_sha256(path)
    except OSError as error:
        raise _invalid("verifier file unavailable", field, "exact_file_unavailable") from error
    if actual != sha256:
        code = "NOKIY_LOCAL_CONTEXT_STALE" if stale else CODE
        raise _invalid("verifier file SHA-256 changed", hash_field, "sha256_mismatch", code=code)
    return path


def _scratch_root(raw: object, workspace: Path, artifact_root: Path) -> str:
    field = "action.verifier_commands.scratch_root"
    if not isinstance(raw, str) or not raw:
        raise _invalid("verifier scratch_root must be absolute", field)
    path = Path(raw)
    if (not path.is_absolute() or ".." in path.parts or str(path) != raw
            or path == artifact_root or not path.is_relative_to(artifact_root)
            or path.is_relative_to(workspace)):
        raise _invalid("verifier scratch_root must be inside artifact_root and outside workspace", field, "unsafe_path")
    for node in (path, *path.parents):
        if node == artifact_root:
            break
        if node.is_symlink():
            raise _invalid("verifier scratch_root cannot traverse symlinks", field, "unsafe_path")
        try:
            info = node.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise _invalid("verifier scratch_root must be a directory", field, "directory_missing")
    if not path.is_dir():
        raise _invalid("verifier scratch_root must be allocated before preparation", field, "directory_missing")
    return raw


def _in_read_scope(path: Path, workspace: Path, read_scopes: list[str]) -> bool:
    try:
        relative = path.relative_to(workspace).as_posix()
    except ValueError:
        return False
    return relative in read_scopes or any(
        scope.endswith("/**") and relative.startswith(scope[:-3] + "/")
        for scope in read_scopes
    )


def _python_import_roots(raw: object, workspace: Path, read_scopes: list[str]) -> list[str]:
    """Mirror of the runtime J-Space rule (V312): focused verifiers read globally while writes stay
    in their scratch root and network stays denied, so an import root is search-path context, never
    a grant. The workspace root, a frozen acceptance directory outside it or a virtualenv
    site-packages may be named; Git metadata, symlinks and missing directories may not."""
    field = "action.verifier_commands.python_import_roots"
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        raise _invalid("python_import_roots requires 1..4 directories", field)
    result = []
    for root in raw:
        if (not isinstance(root, str) or not root
                or any(char in root for char in ":\x00\r\n")):
            raise _invalid("python_import_roots requires canonical absolute paths", field, "unsafe_path")
        path = Path(root)
        if (not path.is_absolute() or str(path) != root or ".." in path.parts
                or ".git" in path.parts or len(root) > 1024 or root in result):
            raise _invalid("python_import_roots requires distinct absolute directories outside Git metadata",
                           field, "unsafe_path")
        try:
            if (any(node.is_symlink() for node in (path, *path.parents))
                    or not path.is_dir() or path.resolve(strict=True) != path):
                raise _invalid("python_import_roots cannot traverse symlinks or missing directories", field, "unsafe_path")
        except (OSError, RuntimeError) as error:
            raise _invalid("python_import_roots directory unavailable", field, "exact_file_unavailable") from error
        result.append(root)
    return result


def _verifier_cwd(raw: object) -> str:
    """Mirror of the runtime J-Space rule (V318): a focused verifier reads globally and writes only
    its scratch root, so the directory it starts in grants nothing. A worker may then run tests
    from the directory its acceptance harness uses, inside or outside the workspace; Git metadata,
    symlinks and missing directories may not be named."""
    field = "action.verifier_commands.cwd"
    if not isinstance(raw, str) or not raw or any(char in raw for char in ":\x00\r\n") or len(raw) > 1024:
        raise _invalid("verifier cwd requires a canonical absolute directory", field)
    path = Path(raw)
    if not path.is_absolute() or str(path) != raw or ".." in path.parts or ".git" in path.parts:
        raise _invalid("verifier cwd requires a canonical absolute directory outside Git metadata",
                       field, "unsafe_path")
    try:
        if (any(node.is_symlink() for node in (path, *path.parents))
                or not path.is_dir() or path.resolve(strict=True) != path):
            raise _invalid("verifier cwd cannot traverse symlinks or name a missing directory", field, "unsafe_path")
    except (OSError, RuntimeError) as error:
        raise _invalid("verifier cwd directory unavailable", field, "exact_file_unavailable") from error
    return raw


def _module_test_runner(argv: list[str]) -> bool:
    """Mirror of the runtime J-Space rule: a pinned CPython running a test framework by module
    (python -B -m unittest|pytest ...) executes the framework plus test modules the worker names,
    whose bytes change during the action, so it may pin no entry file. Only -B may precede -m."""
    if not re.fullmatch(r"python[0-9.]+", Path(argv[0]).name):
        return False
    try:
        index = argv.index("-m", 1)
    except ValueError:
        return False
    return all(flag == "-B" for flag in argv[1:index]) and argv[index + 1:index + 2] in (["unittest"], ["pytest"])


def _verifier_commands(raw: object, workspace: Path, artifact_root: Path | None,
                       read_scopes: list[str], *, stale: bool = False,
                       write_scopes: object = ()) -> list[dict]:
    if (artifact_root is None or not artifact_root.is_absolute() or not artifact_root.is_dir()
            or artifact_root.is_symlink() or artifact_root.resolve(strict=True) != artifact_root):
        raise _invalid("verifier grant requires request artifact_root", "request.artifact_root")
    if (not isinstance(raw, list) or not 1 <= len(raw) <= 8
            or not isinstance(read_scopes, list) or any(not isinstance(scope, str) for scope in read_scopes)):
        raise _invalid("verifier_commands requires 1..8 typed commands", "action.verifier_commands")
    result = []
    argv_seen = set()
    scratch_seen = set()
    for command in raw:
        expected = {"argv", "executable_sha256", "pinned_files", "timeout_seconds", "scratch_root", "network"}
        optional = {"python_import_roots", "cwd"}
        if isinstance(command, dict) and set(command) - optional == expected - {"pinned_files"}:
            argv = command.get("argv")
            if (isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv)
                    and work_product_only(argv, workspace, write_scopes)):
                command = dict(command, pinned_files=[])
            else:
                # The host pins argv entry files inside the workspace read scope; a program other
                # than a python -m unittest|pytest runner whose entry file lies elsewhere has none.
                raise _invalid("verifier entry file could not be pinned",
                               "action.verifier_commands.pinned_files", "input_missing")
        if not isinstance(command, dict) or set(command) - optional != expected:
            raise _invalid("verifier command fields differ", "action.verifier_commands")
        argv = command["argv"]
        executable_name = Path(argv[0]).name if isinstance(argv, list) and argv and isinstance(argv[0], str) else ""
        if (isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv)
                and argv_shell_syntax(argv)):
            # Name the cause: argv runs without a shell, so ; | > $ or quotes cannot work.
            raise _invalid("verifier argv runs without a shell; shell metacharacters are refused",
                           "action.verifier_commands.argv", "shell_metacharacter")
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or any(not isinstance(arg, str) or not arg or len(arg) > 1024 for arg in argv)
                or not Path(argv[0]).is_absolute()
                or executable_name in _SHELLS
                or inline_interpreter_code(executable_name, argv)
                or tuple(argv) in argv_seen):
            raise _invalid("verifier argv must be unique, exact and shell-free", "action.verifier_commands.argv")
        argv_seen.add(tuple(argv))
        import_roots = None
        if "python_import_roots" in command:
            if (not re.fullmatch(r"python[0-9.]*", executable_name)
                    or any(re.match(r"-[bBdEhiIOqRsStuvVx]*[IE]", arg) for arg in argv[1:])):
                raise _invalid("python_import_roots requires CPython without -I/-E",
                               "action.verifier_commands.python_import_roots", "scope_mismatch")
            import_roots = _python_import_roots(command["python_import_roots"], workspace, read_scopes)
        cwd = _verifier_cwd(command["cwd"]) if "cwd" in command else None
        _verifier_file(argv[0], command["executable_sha256"], executable=True, stale=stale)
        pinned = command["pinned_files"]
        unpinned = _module_test_runner(argv) or work_product_only(argv, workspace, write_scopes)
        if not isinstance(pinned, list) or not (0 if unpinned else 1) <= len(pinned) <= 8:
            raise _invalid("verifier requires 1..8 pinned entry files (0..8 for python -B -m unittest|pytest)",
                           "action.verifier_commands.pinned_files")
        paths = set()
        for entry in pinned:
            if (not isinstance(entry, dict) or set(entry) not in (
                    {"path", "sha256"}, {"path", "sha256", "scope"})
                    or ("scope" in entry and entry["scope"] != "verifier_only")):
                raise _invalid("pinned verifier entry fields differ", "action.verifier_commands.pinned_files")
            raw_path = entry["path"]
            verifier_only = entry.get("scope") == "verifier_only"
            if verifier_only:
                if isinstance(raw_path, str) and ".git" in Path(raw_path).parts:
                    raise _invalid("verifier_only entry cannot name Git metadata",
                                   "action.verifier_commands.pinned_files", "unsafe_path")
                if not isinstance(raw_path, str) or Path(raw_path).is_relative_to(workspace):
                    raise _invalid("verifier_only entry must be outside workspace",
                                   "action.verifier_commands.pinned_files", "scope_mismatch")
            elif not isinstance(raw_path, str) or not _in_read_scope(Path(raw_path), workspace, read_scopes):
                raise _invalid("pinned verifier entry is outside admitted read scopes",
                               "action.verifier_commands.pinned_files", "scope_mismatch")
            node_program = verifier_only and executable_name == "node"
            if node_program and (
                    argv[1:2] != [raw_path] or Path(raw_path).suffix not in {".js", ".cjs", ".mjs"}):
                # Node runs the explicitly pinned program directly; no interpreter flags,
                # module lookup or alternate entry may stand between it and the program.
                raise _invalid("verifier_only Node entry requires a direct JavaScript program",
                               "action.verifier_commands.pinned_files", "unsupported_command")
            path = _verifier_file(raw_path, entry["sha256"], executable=False, stale=stale,
                                  header_is_data=interpreter_reads_entries(executable_name))
            if str(path) not in argv or str(path) in paths or (verifier_only and argv.count(str(path)) != 1):
                raise _invalid("pinned verifier entry must appear once in exact argv",
                               "action.verifier_commands.pinned_files", "invalid_bindings")
            paths.add(str(path))
        if (type(command["timeout_seconds"]) is not int
                or not 1 <= command["timeout_seconds"] <= VERIFIER_TIMEOUT_MAX_SECONDS):
            raise _invalid("verifier timeout/network policy invalid", "action.verifier_commands.timeout_seconds")
        if command["network"] is not False:
            raise _invalid("verifier timeout/network policy invalid", "action.verifier_commands.network")
        scratch = _scratch_root(command["scratch_root"], workspace, artifact_root)
        if scratch in scratch_seen:
            raise _invalid("verifier scratch_root duplicated", "action.verifier_commands.scratch_root", "invalid_bindings")
        scratch_seen.add(scratch)
        result.append({"argv": list(argv), "executable_sha256": command["executable_sha256"],
                       "pinned_files": [dict(entry) for entry in pinned],
                       "timeout_seconds": command["timeout_seconds"],
                       "scratch_root": scratch, "network": False})
        if import_roots is not None:
            result[-1]["python_import_roots"] = import_roots
        if cwd is not None:
            result[-1]["cwd"] = cwd
    # Every verifier can write its scratch root, so no external pin may live under any of them.
    for command in result:
        for entry in command["pinned_files"]:
            if entry.get("scope") == "verifier_only" and any(
                    Path(entry["path"]).is_relative_to(Path(scratch)) for scratch in scratch_seen):
                raise _invalid("verifier_only entry overlaps verifier scratch root",
                               "action.verifier_commands.pinned_files", "scope_mismatch")
    return result


def _post_batch_artifact_root(workspace: Path, artifact_root: Path, commands: object,
                              operations: object, writes: object, targets: object) -> Path:
    """Separate one read-only gate's scratch envelope from its receipt ownership."""
    if (not isinstance(commands, list) or len(commands) != 1
            or not isinstance(commands[0], dict)
            or not isinstance(operations, list) or set(operations) != {"read", "command"}
            or writes != [] or targets != []):
        raise _invalid("post-batch verifier requires one read/command-only grant", "action.verifier_commands")
    raw = commands[0].get("scratch_root")
    if not isinstance(raw, str) or not raw or artifact_root is None:
        raise _invalid("post-batch verifier requires explicit scratch and receipt roots", "action.verifier_commands.scratch_root")
    scratch = Path(raw)
    if scratch.is_relative_to(artifact_root) and scratch != artifact_root:
        return artifact_root
    # The envelope is metadata only. The unchanged native policy grants writes
    # to scratch, never to this parent directory or its workspace siblings.
    root = scratch.parent
    _scratch_root(raw, workspace, root)
    if (workspace.is_relative_to(scratch) or artifact_root.is_relative_to(scratch)
            or scratch.is_relative_to(artifact_root)):
        raise _invalid("post-batch scratch overlaps workspace or receipts", "action.verifier_commands.scratch_root", "unsafe_path")
    return root


def compile_context(workspace: Path, action: dict, *, artifact_root: Path | None = None,
                    post_batch_verifier: bool = False, parent_shell: bool = True) -> tuple[dict, dict]:
    if jspace_root(workspace) is not None:
        raise _invalid("DCF-managed workspace cannot use local preparation", "action", "scope_mismatch")
    if "read_search_scopes" in action:
        raise _invalid("exact-file search is not an admitted local capability",
                       "action.read_search_scopes", "unsupported_operation")
    mission = action.get("mission")
    if not isinstance(mission, dict):
        raise _invalid("complete parent mission required", "action.mission",
                       "input_missing" if "mission" not in action else "invalid_shape")
    for field in ("mission_id", "task_id", "mode", "objective", "current_predicate"):
        if not isinstance(mission.get(field), str) or not mission[field].strip():
            raise _invalid("complete parent mission required", "action.mission." + field,
                           "input_missing" if field not in mission else "invalid_shape")
    summary = action.get("context_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > _MAX_SUMMARY_CHARS:
        raise _invalid("bounded task context_summary required", "action.context_summary",
                       "input_missing" if "context_summary" not in action else "invalid_shape")
    if _SECTIONS_MARKER in summary:
        raise _invalid("source section marker is reserved for verified context", "action.context_summary")
    operations = _strings(action.get("operations"), "operations")
    if not operations or set(operations) - {"read", "create", "modify", "delete", "command"}:
        raise caller.EmbeddedNokiyError(CODE, "local preparation supports read/create/modify/delete/command only",
            issues=[{"path": "action.operations", "reason": "unsupported_operation"}])
    reads = _strings(action.get("read_scopes", []), "read_scopes")
    if "source_read" in action and action["source_read"] is not True:
        raise caller.EmbeddedNokiyError(CODE, "source_read requires an explicit true opt-in",
            issues=[{"path": "action.source_read", "reason": "invalid_shape"}])
    source_read = action.get("source_read") is True
    if source_read and any(name != name.strip() or "\\" in name for name in reads):
        raise caller.EmbeddedNokiyError(CODE, "source_read requires canonical exact file names",
            issues=[{"path": "action.read_scopes", "reason": "unsafe_path"}])
    directories = _strings(action.get("read_directories", []), "read_directories")
    if source_read and ((not reads and not directories) or not {"read", "command"}.issubset(operations)):
        raise caller.EmbeddedNokiyError(CODE, "source_read requires exact files and read/command operations",
            issues=[{"path": "action.source_read", "reason": "scope_mismatch"}])
    if directories and not {"read", "command"}.issubset(operations):
        raise caller.EmbeddedNokiyError(CODE, "discovery requires read and command operations",
            issues=[{"path": "action.operations", "reason": "scope_mismatch"}])
    directory_snapshot = _directories(workspace, directories)
    directory_scopes = {name + "/**" for name in directories}
    exact_reads = []
    for name in reads:
        if name in directory_scopes:
            continue
        if any(char in name for char in "*?[]"):
            reason = "directory_grant_required" if name.endswith("/**") else "unsupported_glob"
            raise caller.EmbeddedNokiyError(CODE, "local scopes require exact workspace-relative files or the same explicit directory grant; other globs are not admitted",
                issues=[{"path": "action.read_scopes", "reason": reason}])
        exact_reads.append(name)
    # Redundant directory scopes bind discovery, not a fabricated file snapshot.
    # Directory-only intake uses the existing pinned discovery tools.
    reads = exact_reads
    source_read = source_read and bool(reads)
    writes = _strings(action.get("write_scopes", []), "write_scopes")
    targets = _strings(action.get("target_paths", []), "target_paths")
    # Keep the five independent checks in legacy first-error order, within the
    # existing eight-issue serialization bound.
    scope_issues = []
    for broken, path, reason in (
            (not set(writes).issubset(targets), "action.write_scopes", "not_in_target_paths"),
            (writes and not {"create", "modify", "delete"}.intersection(operations),
             "action.operations", "write_operation_missing"),
            (reads and "read" not in operations, "action.operations", "read_operation_missing"),
            ("read" in operations and not reads and not directories, "action.read_scopes", "read_scope_missing"),
            ({"create", "modify", "delete"}.intersection(operations) and not writes,
             "action.write_scopes", "write_scope_missing")):
        if broken:
            scope_issues.append({"path": path, "reason": reason})
    if scope_issues:
        raise caller.EmbeddedNokiyError(CODE, "explicit scopes and operations disagree",
            issues=scope_issues)
    forbidden = _strings(action.get("forbidden_effects", []), "forbidden_effects")
    if set(forbidden).intersection(operations) or ("write" in forbidden and writes):
        raise caller.EmbeddedNokiyError(CODE, "action contradicts forbidden effects",
            issues=[{"path": "action.forbidden_effects", "reason": "scope_mismatch"}])
    # V431: a reading action also gets a read-only shell that its parent runs under a bound OS
    # profile; repository edits stay apply_patch. It needs the request artifact root for its
    # evidence and profile, and an action that forbids commands keeps none.
    parent_shell = (parent_shell and artifact_root is not None and "read" in operations
                    and "command" not in forbidden)
    if parent_shell and "command" not in operations:
        operations = sorted(set(operations) | {"command"})
    names = sorted(set(reads + writes + targets))
    if not names and not directories:
        raise _invalid("declare 1..32 exact context files", "action", "invalid_shape")
    if len(names) > _MAX_CONTEXT_FILES:
        raise caller.ContextFileLimitExceeded(actual=len(names), maximum=_MAX_CONTEXT_FILES)
    # Attribute shared inputs to reads first, then writes, then target-only paths.
    # Only static action field names enter diagnostics, never the input names.
    issue_fields = {name: "action." + field
                    for field, scopes in (("target_paths", targets), ("write_scopes", writes),
                                          ("read_scopes", reads)) for name in scopes}
    # Snapshots are hash-only CAS preimages, never inlined. Bound them by what the runtime can open
    # (1 MiB for source_read, rg and cat alike), not by the request size: a directory-only reader
    # turns source_read off, which left a 340 KB write target unpreparable (2026-10-22).
    snapshot = _snapshot(workspace, names, max_bytes=_SOURCE_READ_FILE_BYTES, issue_fields=issue_fields)
    create_reads = []
    if source_read and {"read", "command", "create"}.issubset(operations):
        # Bind exact readback authority, retaining the absence snapshot. Directory
        # authority remains sufficient, but explicit exact reads need no directory.
        create_reads = [name for name in targets if name in writes and snapshot[name] is None
                        and name == name.strip() and "\\" not in name
                        and not any(part.startswith(".") for part in Path(name).parts)
                        and (name in reads or any(name.startswith(root + "/") for root in directories))]
    if any(snapshot[n] is None and n not in create_reads for n in reads):
        raise caller.EmbeddedNokiyError(CODE, "read input missing",
            issues=[{"path": "action.read_scopes", "reason": "input_missing"}])
    # Absence requires create only for exact writable targets, not target-only
    # context. Reuse the snapshot and retain the existing missing-read precedence.
    if "create" not in operations and any(snapshot[name] is None for name in writes):
        raise caller.EmbeddedNokiyError(CODE, "new writable targets require create operation",
            issues=[{"path": "action.operations", "reason": "scope_mismatch"}])
    section_ranges = action.get("source_sections")
    original_summary = summary
    if "source_queries" in action:
        if section_ranges is not None:
            raise _invalid("choose source_queries or source_sections", "action.source_queries")
        section_ranges = _query_sections(workspace, action["source_queries"], reads, snapshot)
        summary += "\nLiteral source-query excerpts are partial context, not complete dependency evidence; ordinary scoped reads remain available."
    sections = (_source_sections(workspace, section_ranges, reads, snapshot)
                if "source_sections" in action or section_ranges else [])
    if "source_queries" in action:
        projected = summary + (_SECTIONS_MARKER + json.dumps(
            sections, sort_keys=True, ensure_ascii=True, separators=(",", ":")) if sections else "")
        if len(projected) > _MAX_SUMMARY_CHARS:
            sections = []
            summary = original_summary
    raw_templates = action.get("command_templates", [])
    if not isinstance(raw_templates, list) or len(raw_templates) > 32:
        raise caller.EmbeddedNokiyError(CODE, "bounded command_templates required",
            issues=[{"path": "action.command_templates", "reason": "invalid_shape"}])
    templates = []
    for raw in raw_templates:
        if not isinstance(raw, dict) or set(raw) != {"argv", "effects", "targets"}:
            raise caller.EmbeddedNokiyError(CODE, "commands require exact typed argv/effects/targets",
                issues=[{"path": "action.command_templates", "reason": "invalid_shape"}])
        argv = raw["argv"]
        if (not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv)
                or raw["effects"] != ["read"]):
            raise caller.EmbeddedNokiyError(CODE, "local commands require verified read semantics",
                issues=[{"path": "action.command_templates", "reason": "invalid_shape"}])
        expected = []
        if argv != ["pwd"]:
            if len(argv) < 2 or argv[0] != "cat" or any(n not in reads or n.startswith("-") for n in argv[1:]):
                raise caller.EmbeddedNokiyError(CODE, "unsupported command; use file tools or parent native validation",
                    issues=[{"path": "action.command_templates", "reason": "unsupported_command"}])
            expected = [{"operation": "read", "path": n, "argv_index": i} for i, n in enumerate(argv[1:], 1)]
        if raw["targets"] != expected or raw in templates:
            raise caller.EmbeddedNokiyError(CODE, "command operand bindings missing or duplicated",
                issues=[{"path": "action.command_templates", "reason": "invalid_bindings"}])
        templates.append(raw)
    verifiers = None
    verifier_root = artifact_root
    if "verifier_commands" in action:
        if post_batch_verifier:
            verifier_root = _post_batch_artifact_root(workspace, artifact_root, action["verifier_commands"],
                operations, writes, targets)
        verifier_reads = sorted(set(reads + [name + "/**" for name in directories]))
        verifiers = _verifier_commands(action["verifier_commands"], workspace, verifier_root,
                                       verifier_reads, write_scopes=list(writes))
    needs_command = bool(templates or directories or verifiers or source_read or parent_shell)
    if needs_command != ("command" in operations):
        raise caller.EmbeddedNokiyError(CODE, "command operation requires explicit templates",
            issues=[{"path": "action.operations",
                     "reason": "command_operation_missing" if needs_command else "command_without_templates"}])
    command_reads = {name for template in templates if template["argv"][0] == "cat"
                     for name in template["argv"][1:]}
    section_reads = {section["path"] for section in sections}
    if any(not source_read and name not in command_reads and name not in section_reads
           and not any(name.startswith(root + "/") for root in directories)
           for name in reads):
        raise caller.EmbeddedNokiyError(CODE, "exact read scope has no admitted read command",
            issues=[{"path": "action.command_templates", "reason": "invalid_bindings"}])
    # Existing wire schemas name this slot dcf_generation. Mark it explicitly
    # non-DCF; no generation ID, domain authority or discovered surface is invented.
    generation = {"context_mode": MODE, "dcf_available": False, "repo_root": str(workspace),
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  "required_domain_bindings": {}, "source_snapshot": snapshot}
    if directories:
        generation["directory_snapshot"] = directory_snapshot
    if sections:
        generation["source_sections"] = [{k: v for k, v in section.items() if k != "text"}
                                         for section in sections]
    reads = sorted(set(reads + create_reads + [name + "/**" for name in directories]))
    contract = {"schema_version": "jspace_contract_v2", "repo_root": str(workspace),
        "dcf_generation": generation, "provenance": {"context_mode": MODE, "authority": "parent_task"},
        "matched_surface_ids": [], "focused_verifiers": [], "declared_targets": targets,
        "read_scopes": reads, "write_scopes": writes, "allowed_operations": operations,
        "denied_operations": sorted({"read", "create", "modify", "delete", "command"} - set(operations)),
        "command_templates": templates, "command_effect_policy": "trusted_argv_effects_v1",
        "expansion": {"mode": "exact_target_only", "error_code": "JSPACE_EXPANSION_REQUIRED", "mutation_on_expansion": False}}
    if source_read:
        contract["source_read"] = True
    auth = {k: contract[k] for k in ("repo_root", "matched_surface_ids", "declared_targets", "read_scopes",
        "write_scopes", "allowed_operations", "denied_operations", "command_templates", "command_effect_policy", "expansion")}
    auth.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
    if source_read:
        auth["source_read"] = True
    if verifiers is not None:
        contract["denied_operations"] = sorted(set(contract["denied_operations"] + ["network"]))
        auth["denied_operations"] = contract["denied_operations"]
        contract["verifier_artifact_root"] = str(verifier_root)
        contract["verifier_commands"] = verifiers
        auth["verifier_artifact_root"] = contract["verifier_artifact_root"]
        auth["verifier_commands"] = verifiers
    if directories:
        contract["read_commands"] = _read_commands(directories)
        auth["read_commands"] = contract["read_commands"]
        rg = contract["read_commands"]["rg"]["path"]
        cat = contract["read_commands"]["cat"]["path"]
        summary += (f"\nDiscovery: use pinned executable {rg} --no-config --max-filesize=1M "
                    "--files -- <root> to list; or add -n with read-only match and output options "
                    "(-i -S -F -w -x -v, -o -c -l, -A/-B/-C/-m/-d N, -g GLOB) then -- <pattern> <root> "
                    f"to search. Read discovered files with {cat} -- <file>. "
                    f"Granted roots: {directories}. No hidden files, symlinks, traversal, pipelines, --pre or --follow. "
                    "Search/read grants do not grant any new write targets.")
    if parent_shell:
        from .parent_shell import compile_policy
        contract["parent_shell"] = compile_policy(artifact_root, action, workspace)
        auth["parent_shell"] = contract["parent_shell"]
    if sections:
        summary += _SECTIONS_MARKER + json.dumps(sections, sort_keys=True, ensure_ascii=True,
                                                 separators=(",", ":"))
    if len(summary) > _MAX_SUMMARY_CHARS:
        if "source_queries" in action:
            return compile_context(workspace, {k: v for k, v in action.items() if k != "source_queries"},
                                   artifact_root=artifact_root, post_batch_verifier=post_batch_verifier,
                                   parent_shell=parent_shell)
        raise _invalid("local context_summary exceeds existing budget", "action.context_summary")
    contract["authorization_semantic_sha256"] = caller._canonical_sha256(auth)
    contract["content_sha256"] = caller._canonical_sha256(contract)
    capsule = {"schema_version": "task_context_capsule_v1", "mission": mission,
        "context_summary": summary, "dcf_generation": generation,
        "surface": {"repo_root": str(workspace), "matched_surface_ids": [], "declared_targets": targets},
        "authority": {"source": "parent_task", "forbidden_effects": forbidden},
        "evidence_refs": [{"id": n, "kind": "local_source", "sha256": v["sha256"]}
                          for n, v in snapshot.items() if v is not None],
        "focused_verifiers": [], "jspace_semantic_sha256": contract["authorization_semantic_sha256"]}
    capsule["semantic_sha256"] = caller._canonical_sha256(capsule)
    if len(caller._canonical_bytes(capsule)) > caller.MAX_CONTEXT_BYTES:
        if "source_queries" in action:
            return compile_context(workspace, {k: v for k, v in action.items() if k != "source_queries"},
                                   artifact_root=artifact_root, post_batch_verifier=post_batch_verifier,
                                   parent_shell=parent_shell)
        raise _invalid("local capsule exceeds existing byte budget", "action", "invalid_shape")
    return capsule, contract


def verify_context(workspace: Path, context: dict, contract: dict,
                   *, artifact_root: Path | None = None, post_batch_verifier: bool = False) -> None:
    if jspace_root(workspace) is not None:
        raise caller.EmbeddedNokiyError(CODE, "workspace now has DCF; prepare through its canonical compiler")
    generation = context["dcf_generation"]
    if (generation != contract.get("dcf_generation") or generation.get("dcf_available") is not False
            or generation.get("required_domain_bindings") != {} or "action_freshness" in generation
            or contract.get("schema_version") != "jspace_contract_v2"
            or contract.get("command_effect_policy") != "trusted_argv_effects_v1"
            or contract.get("matched_surface_ids") != []):
        raise caller.EmbeddedNokiyError(CODE, "local context cannot claim DCF authority")
    snapshots = generation.get("source_snapshot")
    directory_snapshot = generation.get("directory_snapshot", {})
    directories = sorted(directory_snapshot)
    if directory_snapshot != _directories(workspace, directories):
        raise caller.EmbeddedNokiyError("NOKIY_LOCAL_CONTEXT_STALE", "discovery directory identity changed")
    if directories:
        policy = contract.get("read_commands", {})
        if policy != _read_commands(directories) or not {"read", "command"}.issubset(contract["allowed_operations"]):
            raise caller.EmbeddedNokiyError(CODE, "discovery policy/executable identity changed")
    elif "read_commands" in contract:
        raise caller.EmbeddedNokiyError(CODE, "discovery requires bound directories")
    source_read = contract.get("source_read") is True
    if "source_read" in contract and (not source_read
            or not {"read", "command"}.issubset(contract.get("allowed_operations", []))
            or not (set(contract.get("read_scopes", [])) - {name + "/**" for name in directories})):
        raise caller.EmbeddedNokiyError(CODE, "source_read grant requires exact files and read/command operations")
    if "verifier_commands" in contract or "verifier_artifact_root" in contract:
        verifier_root = (_post_batch_artifact_root(workspace, artifact_root, contract.get("verifier_commands"),
            contract.get("allowed_operations"), contract.get("write_scopes"), contract.get("declared_targets"))
            if post_batch_verifier else artifact_root)
        if ("verifier_commands" not in contract or artifact_root is None
                or contract.get("verifier_artifact_root") != str(verifier_root)
                or "command" not in contract.get("allowed_operations", [])):
            raise caller.EmbeddedNokiyError(CODE, "verifier artifact root or command grant changed")
        _verifier_commands(contract["verifier_commands"], workspace, verifier_root,
                           contract.get("read_scopes"), stale=True,
                           write_scopes=contract.get("write_scopes") or [])
    if "parent_shell" in contract:
        from .parent_shell import verify_policy
        verify_policy(contract, artifact_root)
    expected_names = set(contract["read_scopes"]) - {name + "/**" for name in directories}
    expected_names.update(contract["write_scopes"] + contract["declared_targets"])
    if (not isinstance(snapshots, dict) or len(snapshots) > 32 or (not snapshots and not directories)
            or set(snapshots) != expected_names):
        raise caller.EmbeddedNokiyError(CODE, "local source bindings missing")
    if snapshots != _snapshot(workspace, sorted(snapshots), max_bytes=_SOURCE_READ_FILE_BYTES):
        raise caller.EmbeddedNokiyError("NOKIY_LOCAL_CONTEXT_STALE", "workspace inputs changed; prepare a new bounded context")
    summary = context.get("context_summary")
    metadata = generation.get("source_sections")
    if not isinstance(summary, str) or len(summary) > _MAX_SUMMARY_CHARS:
        raise caller.EmbeddedNokiyError(CODE, "local context_summary exceeds existing budget")
    if metadata is None:
        if _SECTIONS_MARKER in summary:
            raise caller.EmbeddedNokiyError(CODE, "source section context is unbound")
        return
    if (not isinstance(metadata, list) or not 1 <= len(metadata) <= _MAX_SECTIONS
            or any(not isinstance(section, dict) or set(section) != {
                "path", "start_line", "end_line", "source_sha256", "section_sha256"}
                for section in metadata)):
        raise caller.EmbeddedNokiyError(CODE, "source section bindings are invalid")
    locators = [{k: section[k] for k in ("path", "start_line", "end_line")}
                for section in metadata]
    sections = _source_sections(workspace, locators, contract["read_scopes"], snapshots)
    if metadata != [{k: v for k, v in section.items() if k != "text"} for section in sections]:
        raise caller.EmbeddedNokiyError(CODE, "source section bindings differ from exact source")
    if (summary.count(_SECTIONS_MARKER) != 1
            or summary.split(_SECTIONS_MARKER, 1)[1] != json.dumps(
                sections, sort_keys=True, ensure_ascii=True, separators=(",", ":"))):
        raise caller.EmbeddedNokiyError(CODE, "source section context differs from exact source")
