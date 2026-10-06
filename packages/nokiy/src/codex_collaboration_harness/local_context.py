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

from . import embedded_nokiy as caller

MODE = "local_workspace_jspace"
CODE = "NOKIY_LOCAL_CONTEXT_INVALID"
_SHELLS = {"sh", "bash", "dash", "zsh", "fish", "csh", "tcsh", "ksh", "env", "osascript"}
_SHELL_META = frozenset(";&|<>$`\\\"'\r\n\x00*?[]{}()")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_MACHO = {bytes.fromhex(magic) for magic in (
    "feedface", "cefaedfe", "feedfacf", "cffaedfe",
    "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
)}
_SECTIONS_MARKER = "\nExact local source sections (context only; not a new grant):\n"
_MAX_SECTIONS = 4
_MAX_SECTION_LINES = 120
_MAX_SECTION_BYTES = 6_144
_MAX_SECTIONS_BYTES = 10_000
_MAX_SUMMARY_CHARS = 12_000
_SOURCE_READ_FILE_BYTES = 1_048_576


def dcf_root(workspace: Path) -> Path | None:
    # An absent interpreter, broken link or a subdirectory is not an opt-out.
    for root in (workspace, *workspace.parents):
        for name in ("scripts/ops/dcf.py", "config/contracts/dcf_v2_contract.json"):
            path = root / name
            if path.exists() or path.is_symlink():
                return root
    return None


def _strings(value, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 64 or any(
        not isinstance(v, str) or not v.strip() for v in value
    ):
        raise caller.EmbeddedNokiyError(CODE, f"{field} requires a bounded string list")
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
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise caller.EmbeddedNokiyError(CODE, "context inputs must be bounded regular files")
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


def _snapshot(workspace: Path, names: list[str], *, max_bytes: int = caller.MAX_REQUEST_BYTES) -> dict:
    snapshots = {}
    for name in names:
        _file(workspace, name)
        try:
            snapshots[name] = _snapshot_entry(workspace, name, max_bytes)
        except FileNotFoundError:
            snapshots[name] = None
        except OSError as error:
            raise caller.EmbeddedNokiyError(CODE, "context input unavailable") from error
    return snapshots


def _source_sections(workspace: Path, raw: object, reads: list[str], snapshots: dict) -> list[dict]:
    from .source_excerpt import _read_exact

    if not isinstance(raw, list) or not 1 <= len(raw) <= _MAX_SECTIONS:
        raise caller.EmbeddedNokiyError(CODE, "source_sections requires 1..4 exact line ranges")
    sections = []
    seen = set()
    total_bytes = 0
    for locator in raw:
        if not isinstance(locator, dict) or set(locator) != {"path", "start_line", "end_line"}:
            raise caller.EmbeddedNokiyError(CODE, "source section requires exact path/start_line/end_line")
        name, start, end = locator["path"], locator["start_line"], locator["end_line"]
        if (not isinstance(name, str) or name not in reads
                or type(start) is not int or type(end) is not int
                or start < 1 or end < start or end - start + 1 > _MAX_SECTION_LINES
                or (name, start, end) in seen):
            raise caller.EmbeddedNokiyError(CODE, "source section must be a distinct bounded exact scoped read")
        seen.add((name, start, end))
        _file(workspace, name)
        try:
            data = _read_exact(workspace, name, python_only=False)
            lines = data.splitlines(keepends=True)
            excerpt_bytes = b"".join(lines[start - 1:end])
            excerpt = excerpt_bytes.decode("utf-8")
        except (caller.EmbeddedNokiyError, UnicodeError) as error:
            raise caller.EmbeddedNokiyError(CODE, "exact source section read failed") from error
        source_sha256 = hashlib.sha256(data).hexdigest()
        if (end > len(lines) or len(excerpt_bytes) > _MAX_SECTION_BYTES
                or snapshots.get(name, {}).get("sha256") != source_sha256):
            raise caller.EmbeddedNokiyError(CODE, "source section exceeds budget or source snapshot changed")
        total_bytes += len(excerpt_bytes)
        if total_bytes > _MAX_SECTIONS_BYTES:
            raise caller.EmbeddedNokiyError(CODE, "source sections exceed aggregate byte budget")
        sections.append({"path": name, "start_line": start, "end_line": end,
                         "source_sha256": source_sha256,
                         "section_sha256": hashlib.sha256(excerpt_bytes).hexdigest(), "text": excerpt})
    return sections


def _directories(workspace: Path, names: list[str]) -> dict:
    if len(names) > 8:
        raise caller.EmbeddedNokiyError(CODE, "at most 8 discovery directories")
    result = {}
    for name in names:
        path = _file(workspace, name)
        if any(p.startswith(".") for p in Path(name).parts) or not path.is_dir():
            raise caller.EmbeddedNokiyError(CODE, "discovery roots must be non-hidden directories")
        info = path.stat()
        result[name] = {"device": info.st_dev, "inode": info.st_ino, "mode": stat.S_IMODE(info.st_mode)}
    return result


def _read_commands(roots: list[str]) -> dict:
    policy = {"roots": roots}
    for name in ("rg", "cat"):
        found = shutil.which(name)
        if not found:
            raise caller.EmbeddedNokiyError(CODE, f"required discovery executable missing: {name}")
        path = Path(found).resolve(strict=True)
        policy[name] = {"path": str(path), "sha256": caller._file_sha256(path)}
    return policy


def _verifier_file(raw: object, sha256: object, *, executable: bool, stale: bool) -> Path:
    if (not isinstance(raw, str) or not raw or not isinstance(sha256, str)
            or not _SHA256.fullmatch(sha256)):
        raise caller.EmbeddedNokiyError(CODE, "verifier file requires an absolute path and lowercase SHA-256")
    path = Path(raw)
    if (not path.is_absolute() or ".." in path.parts or str(path) != raw
            or any(node.is_symlink() for node in (path, *path.parents))):
        raise caller.EmbeddedNokiyError(CODE, "verifier files cannot traverse symlinks")
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or (executable and not info.st_mode & 0o111):
            raise caller.EmbeddedNokiyError(CODE, "verifier entry must be a regular executable file")
        with path.open("rb") as source:
            header = source.readline(256)
        if executable and header[:4] not in _MACHO:
            raise caller.EmbeddedNokiyError(CODE, "verifier executable must be a pinned Mach-O binary")
        if header.startswith(b"#!"):
            words = header[2:].decode("ascii", errors="ignore").strip().split()
            if words and (Path(words[0]).name in _SHELLS
                          or (Path(words[0]).name == "env" and len(words) > 1
                              and Path(words[1]).name in _SHELLS)):
                raise caller.EmbeddedNokiyError(CODE, "shell and env verifier entries are not admitted")
        actual = caller._file_sha256(path)
    except OSError as error:
        raise caller.EmbeddedNokiyError(CODE, "verifier file unavailable") from error
    if actual != sha256:
        code = "NOKIY_LOCAL_CONTEXT_STALE" if stale else CODE
        raise caller.EmbeddedNokiyError(code, "verifier file SHA-256 changed")
    return path


def _scratch_root(raw: object, workspace: Path, artifact_root: Path) -> str:
    if not isinstance(raw, str) or not raw:
        raise caller.EmbeddedNokiyError(CODE, "verifier scratch_root must be absolute")
    path = Path(raw)
    if (not path.is_absolute() or ".." in path.parts or str(path) != raw
            or path == artifact_root or not path.is_relative_to(artifact_root)
            or path.is_relative_to(workspace)):
        raise caller.EmbeddedNokiyError(CODE, "verifier scratch_root must be inside artifact_root and outside workspace")
    for node in (path, *path.parents):
        if node == artifact_root:
            break
        if node.is_symlink():
            raise caller.EmbeddedNokiyError(CODE, "verifier scratch_root cannot traverse symlinks")
        try:
            info = node.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise caller.EmbeddedNokiyError(CODE, "verifier scratch_root must be a directory")
    if not path.is_dir():
        raise caller.EmbeddedNokiyError(CODE, "verifier scratch_root must be allocated before preparation")
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
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        raise caller.EmbeddedNokiyError(CODE, "python_import_roots requires 1..4 directories")
    result = []
    for root in raw:
        if (not isinstance(root, str) or not root
                or any(char in root for char in ":\x00\r\n")):
            raise caller.EmbeddedNokiyError(CODE, "python_import_roots requires canonical absolute paths")
        path = Path(root)
        if (not path.is_absolute() or str(path) != root or ".." in path.parts
                or path == workspace or not path.is_relative_to(workspace)
                or any(part.startswith(".") for part in path.relative_to(workspace).parts)
                or root in result):
            raise caller.EmbeddedNokiyError(CODE, "python_import_roots requires distinct non-hidden workspace descendants")
        try:
            if (any(node.is_symlink() for node in (path, *path.parents))
                    or not path.is_dir() or path.resolve(strict=True) != path):
                raise caller.EmbeddedNokiyError(CODE, "python_import_roots cannot traverse symlinks or missing directories")
            contains_scope = any(
                (workspace / scope.removesuffix("/**")).is_relative_to(path)
                and _file(workspace, scope.removesuffix("/**")).exists()
                for scope in read_scopes
            )
        except (OSError, RuntimeError) as error:
            raise caller.EmbeddedNokiyError(CODE, "python_import_roots directory unavailable") from error
        if not contains_scope:
            raise caller.EmbeddedNokiyError(CODE, "python_import_roots must contain an existing admitted read scope")
        result.append(root)
    return result


def _verifier_commands(raw: object, workspace: Path, artifact_root: Path | None,
                       read_scopes: list[str], *, stale: bool = False) -> list[dict]:
    if (artifact_root is None or not artifact_root.is_absolute() or not artifact_root.is_dir()
            or artifact_root.is_symlink() or artifact_root.resolve(strict=True) != artifact_root):
        raise caller.EmbeddedNokiyError(CODE, "verifier grant requires request artifact_root")
    if (not isinstance(raw, list) or not 1 <= len(raw) <= 8
            or not isinstance(read_scopes, list) or any(not isinstance(scope, str) for scope in read_scopes)):
        raise caller.EmbeddedNokiyError(CODE, "verifier_commands requires 1..8 typed commands")
    result = []
    argv_seen = set()
    scratch_seen = set()
    for command in raw:
        if not isinstance(command, dict) or set(command) - {"python_import_roots"} != {
                "argv", "executable_sha256", "pinned_files", "timeout_seconds", "scratch_root", "network"}:
            raise caller.EmbeddedNokiyError(CODE, "verifier command fields differ")
        argv = command["argv"]
        executable_name = Path(argv[0]).name if isinstance(argv, list) and argv and isinstance(argv[0], str) else ""
        if (not isinstance(argv, list) or not 1 <= len(argv) <= 32
                or any(not isinstance(arg, str) or not arg or len(arg) > 1024
                       or any(char in _SHELL_META for char in arg) for arg in argv)
                or not Path(argv[0]).is_absolute()
                or executable_name in _SHELLS
                or (executable_name.startswith(("python", "node", "ruby", "perl", "php"))
                    and any(arg in {"-c", "-e", "--eval", "--execute"} for arg in argv[1:]))
                or tuple(argv) in argv_seen):
            raise caller.EmbeddedNokiyError(CODE, "verifier argv must be unique, exact and shell-free")
        argv_seen.add(tuple(argv))
        import_roots = None
        if "python_import_roots" in command:
            if (not re.fullmatch(r"python[0-9.]*", executable_name)
                    or any(re.match(r"-[bBdEhiIOqRsStuvVx]*[IE]", arg) for arg in argv[1:])):
                raise caller.EmbeddedNokiyError(CODE, "python_import_roots requires CPython without -I/-E")
            import_roots = _python_import_roots(command["python_import_roots"], workspace, read_scopes)
        _verifier_file(argv[0], command["executable_sha256"], executable=True, stale=stale)
        pinned = command["pinned_files"]
        if not isinstance(pinned, list) or not 1 <= len(pinned) <= 8:
            raise caller.EmbeddedNokiyError(CODE, "verifier requires 1..8 pinned entry files")
        paths = set()
        for entry in pinned:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256"}:
                raise caller.EmbeddedNokiyError(CODE, "pinned verifier entry fields differ")
            raw_path = entry["path"]
            if not isinstance(raw_path, str) or not _in_read_scope(Path(raw_path), workspace, read_scopes):
                raise caller.EmbeddedNokiyError(CODE, "pinned verifier entry is outside admitted read scopes")
            path = _verifier_file(raw_path, entry["sha256"], executable=False, stale=stale)
            if str(path) not in argv or str(path) in paths:
                raise caller.EmbeddedNokiyError(CODE, "pinned verifier entry must appear once in exact argv")
            paths.add(str(path))
        if (type(command["timeout_seconds"]) is not int
                or not 1 <= command["timeout_seconds"] <= 300
                or command["network"] is not False):
            raise caller.EmbeddedNokiyError(CODE, "verifier timeout/network policy invalid")
        scratch = _scratch_root(command["scratch_root"], workspace, artifact_root)
        if scratch in scratch_seen:
            raise caller.EmbeddedNokiyError(CODE, "verifier scratch_root duplicated")
        scratch_seen.add(scratch)
        result.append({"argv": list(argv), "executable_sha256": command["executable_sha256"],
                       "pinned_files": [dict(entry) for entry in pinned],
                       "timeout_seconds": command["timeout_seconds"],
                       "scratch_root": scratch, "network": False})
        if import_roots is not None:
            result[-1]["python_import_roots"] = import_roots
    return result


def compile_context(workspace: Path, action: dict, *, artifact_root: Path | None = None) -> tuple[dict, dict]:
    if dcf_root(workspace) is not None:
        raise caller.EmbeddedNokiyError(CODE, "DCF-managed workspace cannot use local preparation")
    if "read_search_scopes" in action:
        raise caller.EmbeddedNokiyError(CODE, "exact-file search is not an admitted local capability")
    mission = action["mission"]
    if any(not isinstance(mission.get(k), str) or not mission[k].strip()
           for k in ("mission_id", "task_id", "mode", "objective", "current_predicate")):
        raise caller.EmbeddedNokiyError(CODE, "complete parent mission required")
    summary = action.get("context_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > _MAX_SUMMARY_CHARS:
        raise caller.EmbeddedNokiyError(CODE, "bounded task context_summary required")
    if _SECTIONS_MARKER in summary:
        raise caller.EmbeddedNokiyError(CODE, "source section marker is reserved for verified context")
    operations = _strings(action.get("operations"), "operations")
    if not operations or set(operations) - {"read", "create", "modify", "command"}:
        raise caller.EmbeddedNokiyError(CODE, "local preparation supports read/create/modify/command only")
    reads = _strings(action.get("read_scopes", []), "read_scopes")
    if "source_read" in action and action["source_read"] is not True:
        raise caller.EmbeddedNokiyError(CODE, "source_read requires an explicit true opt-in")
    source_read = action.get("source_read") is True
    if source_read and any(name != name.strip() or "\\" in name for name in reads):
        raise caller.EmbeddedNokiyError(CODE, "source_read requires canonical exact file names")
    if source_read and (not reads or not {"read", "command"}.issubset(operations)):
        raise caller.EmbeddedNokiyError(CODE, "source_read requires exact files and read/command operations")
    directories = _strings(action.get("read_directories", []), "read_directories")
    directory_snapshot = _directories(workspace, directories)
    if directories and not {"read", "command"}.issubset(operations):
        raise caller.EmbeddedNokiyError(CODE, "discovery requires read and command operations")
    writes = _strings(action.get("write_scopes", []), "write_scopes")
    targets = _strings(action.get("target_paths", []), "target_paths")
    if (not set(writes).issubset(targets) or (writes and not {"create", "modify"}.intersection(operations))
            or (reads and "read" not in operations) or ("read" in operations and not reads and not directories)
            or ({"create", "modify"}.intersection(operations) and not writes)):
        raise caller.EmbeddedNokiyError(CODE, "explicit scopes and operations disagree")
    forbidden = _strings(action.get("forbidden_effects", []), "forbidden_effects")
    if set(forbidden).intersection(operations) or ("write" in forbidden and writes):
        raise caller.EmbeddedNokiyError(CODE, "action contradicts forbidden effects")
    names = sorted(set(reads + writes + targets))
    if (not names and not directories) or len(names) > 32:
        raise caller.EmbeddedNokiyError(CODE, "declare 1..32 exact context files")
    snapshot = _snapshot(workspace, names, max_bytes=(
        _SOURCE_READ_FILE_BYTES if source_read else caller.MAX_REQUEST_BYTES))
    if any(snapshot[n] is None for n in reads):
        raise caller.EmbeddedNokiyError(CODE, "read input missing",
            issues=[{"path": "action.read_scopes", "reason": "input_missing"}])
    sections = (_source_sections(workspace, action["source_sections"], reads, snapshot)
                if "source_sections" in action else [])
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
    if "verifier_commands" in action:
        verifier_reads = sorted(set(reads + [name + "/**" for name in directories]))
        verifiers = _verifier_commands(action["verifier_commands"], workspace, artifact_root,
                                       verifier_reads)
    if bool(templates or directories or verifiers or source_read) != ("command" in operations):
        raise caller.EmbeddedNokiyError(CODE, "command operation requires explicit templates")
    command_reads = {name for template in templates if template["argv"][0] == "cat"
                     for name in template["argv"][1:]}
    section_reads = {section["path"] for section in sections}
    if any(not source_read and name not in command_reads and name not in section_reads
           and not any(name.startswith(root + "/") for root in directories)
           for name in reads):
        raise caller.EmbeddedNokiyError(CODE, "exact read scope has no admitted read command")
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
    create_reads = []
    if source_read and {"read", "command", "create"}.issubset(operations):
        # Exactize existing directory read authority, retaining the absence binding.
        create_reads = [name for name in targets if name in writes and snapshot[name] is None
                        and name == name.strip() and "\\" not in name
                        and not any(part.startswith(".") for part in Path(name).parts)
                        and any(name.startswith(root + "/") for root in directories)]
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
        contract["verifier_artifact_root"] = str(artifact_root)
        contract["verifier_commands"] = verifiers
        auth["verifier_artifact_root"] = contract["verifier_artifact_root"]
        auth["verifier_commands"] = verifiers
    if directories:
        contract["read_commands"] = _read_commands(directories)
        auth["read_commands"] = contract["read_commands"]
        rg = contract["read_commands"]["rg"]["path"]
        cat = contract["read_commands"]["cat"]["path"]
        summary += (f"\nDiscovery: use pinned executable {rg} --no-config --max-filesize=1M "
                    "--files -- <root> to list; or add -n (optional -i/-F/-l/-g) then -- <pattern> <root> "
                    f"to search. Read discovered files with {cat} -- <file>. "
                    f"Granted roots: {directories}. No hidden files, symlinks, traversal, pipelines, --pre or --follow. "
                    "Search/read grants do not grant any new write targets.")
    if sections:
        summary += _SECTIONS_MARKER + json.dumps(sections, sort_keys=True, ensure_ascii=True,
                                                 separators=(",", ":"))
    if len(summary) > _MAX_SUMMARY_CHARS:
        raise caller.EmbeddedNokiyError(CODE, "local context_summary exceeds existing budget")
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
        raise caller.EmbeddedNokiyError(CODE, "local capsule exceeds existing byte budget")
    return capsule, contract


def verify_context(workspace: Path, context: dict, contract: dict,
                   *, artifact_root: Path | None = None) -> None:
    if dcf_root(workspace) is not None:
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
        if ("verifier_commands" not in contract or artifact_root is None
                or contract.get("verifier_artifact_root") != str(artifact_root)
                or "command" not in contract.get("allowed_operations", [])):
            raise caller.EmbeddedNokiyError(CODE, "verifier artifact root or command grant changed")
        _verifier_commands(contract["verifier_commands"], workspace, artifact_root,
                           contract.get("read_scopes"), stale=True)
    expected_names = set(contract["read_scopes"]) - {name + "/**" for name in directories}
    expected_names.update(contract["write_scopes"] + contract["declared_targets"])
    if (not isinstance(snapshots, dict) or len(snapshots) > 32 or (not snapshots and not directories)
            or set(snapshots) != expected_names):
        raise caller.EmbeddedNokiyError(CODE, "local source bindings missing")
    if snapshots != _snapshot(workspace, sorted(snapshots), max_bytes=(
            _SOURCE_READ_FILE_BYTES if source_read else caller.MAX_REQUEST_BYTES)):
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
