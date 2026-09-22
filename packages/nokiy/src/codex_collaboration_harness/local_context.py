# SPDX-License-Identifier: MIT
"""Bounded workspace context for projects without a DCF integration."""
from datetime import datetime, timezone
from pathlib import Path
import stat
import shutil

from . import embedded_nokiy as caller

MODE = "local_workspace_jspace"
CODE = "NOKIY_LOCAL_CONTEXT_INVALID"


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


def _snapshot(workspace: Path, names: list[str]) -> dict:
    snapshots = {}
    for name in names:
        path = _file(workspace, name)
        try:
            info = path.stat()
        except FileNotFoundError:
            snapshots[name] = None
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > caller.MAX_REQUEST_BYTES:
            raise caller.EmbeddedNokiyError(CODE, "context inputs must be bounded regular files")
        snapshots[name] = {"sha256": caller._file_sha256(path), "mode": stat.S_IMODE(info.st_mode)}
    return snapshots


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


def compile_context(workspace: Path, action: dict) -> tuple[dict, dict]:
    if dcf_root(workspace) is not None:
        raise caller.EmbeddedNokiyError(CODE, "DCF-managed workspace cannot use local preparation")
    mission = action["mission"]
    if any(not isinstance(mission.get(k), str) or not mission[k].strip()
           for k in ("mission_id", "task_id", "mode", "objective", "current_predicate")):
        raise caller.EmbeddedNokiyError(CODE, "complete parent mission required")
    summary = action.get("context_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 12000:
        raise caller.EmbeddedNokiyError(CODE, "bounded task context_summary required")
    operations = _strings(action.get("operations"), "operations")
    if not operations or set(operations) - {"read", "create", "modify", "command"}:
        raise caller.EmbeddedNokiyError(CODE, "local preparation supports read/create/modify/command only")
    reads = _strings(action.get("read_scopes", []), "read_scopes")
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
    snapshot = _snapshot(workspace, names)
    if any(snapshot[n] is None for n in reads):
        raise caller.EmbeddedNokiyError(CODE, "read input missing")
    raw_templates = action.get("command_templates", [])
    if not isinstance(raw_templates, list) or len(raw_templates) > 32:
        raise caller.EmbeddedNokiyError(CODE, "bounded command_templates required")
    templates = []
    for raw in raw_templates:
        if not isinstance(raw, dict) or set(raw) != {"argv", "effects", "targets"}:
            raise caller.EmbeddedNokiyError(CODE, "commands require exact typed argv/effects/targets")
        argv = raw["argv"]
        if (not isinstance(argv, list) or not argv or any(not isinstance(v, str) for v in argv)
                or raw["effects"] != ["read"]):
            raise caller.EmbeddedNokiyError(CODE, "local commands require verified read semantics")
        expected = []
        if argv != ["pwd"]:
            if len(argv) < 2 or argv[0] != "cat" or any(n not in reads or n.startswith("-") for n in argv[1:]):
                raise caller.EmbeddedNokiyError(CODE, "unsupported command; use file tools or parent native validation")
            expected = [{"operation": "read", "path": n, "argv_index": i} for i, n in enumerate(argv[1:], 1)]
        if raw["targets"] != expected or raw in templates:
            raise caller.EmbeddedNokiyError(CODE, "command operand bindings missing or duplicated")
        templates.append(raw)
    if bool(templates or directories) != ("command" in operations):
        raise caller.EmbeddedNokiyError(CODE, "command operation requires explicit templates")
    # Existing wire schemas name this slot dcf_generation. Mark it explicitly
    # non-DCF; no generation ID, domain authority or discovered surface is invented.
    generation = {"context_mode": MODE, "dcf_available": False, "repo_root": str(workspace),
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  "required_domain_bindings": {}, "source_snapshot": snapshot}
    if directories:
        generation["directory_snapshot"] = directory_snapshot
    reads = sorted(set(reads + [name + "/**" for name in directories]))
    contract = {"schema_version": "jspace_contract_v2", "repo_root": str(workspace),
        "dcf_generation": generation, "provenance": {"context_mode": MODE, "authority": "parent_task"},
        "matched_surface_ids": [], "focused_verifiers": [], "declared_targets": targets,
        "read_scopes": reads, "write_scopes": writes, "allowed_operations": operations,
        "denied_operations": sorted({"read", "create", "modify", "delete", "command"} - set(operations)),
        "command_templates": templates, "command_effect_policy": "trusted_argv_effects_v1",
        "expansion": {"mode": "exact_target_only", "error_code": "JSPACE_EXPANSION_REQUIRED", "mutation_on_expansion": False}}
    auth = {k: contract[k] for k in ("repo_root", "matched_surface_ids", "declared_targets", "read_scopes",
        "write_scopes", "allowed_operations", "denied_operations", "command_templates", "command_effect_policy", "expansion")}
    auth.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
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
    return capsule, contract


def verify_context(workspace: Path, context: dict, contract: dict) -> None:
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
    expected_names = set(contract["read_scopes"]) - {name + "/**" for name in directories}
    expected_names.update(contract["write_scopes"] + contract["declared_targets"])
    if (not isinstance(snapshots, dict) or len(snapshots) > 32 or (not snapshots and not directories)
            or set(snapshots) != expected_names):
        raise caller.EmbeddedNokiyError(CODE, "local source bindings missing")
    if snapshots != _snapshot(workspace, sorted(snapshots)):
        raise caller.EmbeddedNokiyError("NOKIY_LOCAL_CONTEXT_STALE", "workspace inputs changed; prepare a new bounded context")
