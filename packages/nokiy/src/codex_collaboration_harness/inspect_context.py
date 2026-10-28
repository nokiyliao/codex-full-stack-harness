# SPDX-License-Identifier: MIT
"""Read-and-record inspection: an OS-sandboxed worker shell without repository write grants.

An inspect action lets one bounded worker run a chain of read-only checks whose only writes
are evidence files. The worker may run any shell line. full_core starts the whole engine under
a caller-written Seatbelt profile, bound into the J-Space contract by digest, that keeps its
signal boundary and confines every write to the request artifact root (run state, the action
evidence root and scratch). Reads and network stay those of the worker runtime, which needs
its credentials and provider; Seatbelt cannot nest, so shells inherit the same fence. The
profile is the boundary, so inspection needs no DCF surface and carries no exact grants.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess

from . import embedded_nokiy as caller

MODE = "inspect_workspace_jspace"
CODE = "NOKIY_INSPECT_CONTEXT_INVALID"
POLICY_SCHEMA = "nokiy_inspect_shell_v1"
PROFILE_ENV = "TURA_INSPECT_SANDBOX_PROFILE"
# Set only by the full_core branch that really started the engine under the profile.
FENCE_ENV = "NOKIY_INSPECT_FENCE_APPLIED"
_MAX_SUMMARY_CHARS = 12_000
_OPERATIONS = ["command", "read"]
# Exact grants belong to ordinary actions; mixing them with an open sandbox would be ambiguous.
_GRANT_FIELDS = frozenset({
    "read_scopes", "write_scopes", "target_paths", "read_directories", "read_search_scopes",
    "source_read", "source_sections", "command_templates", "verifier_commands",
    "verifier_artifact_root", "surface_targets", "navigation_targets", "mapped_source_reads",
})
NOTICE = ("\nInspect shell: one bounded read-and-record task. Any shell line runs under an OS sandbox; "
          "the repository is read-only and writes outside the evidence root and temporary directories fail, "
          "including apply_patch. Batch related checks into few command lines, write evidence files under the "
          "evidence root, open SQLite as file:<path>?mode=ro&immutable=1, and report findings with "
          "evidence paths. Headless browser checks may use playwright-cli; its snapshots and screenshots go "
          "to the evidence root.")
# Fields the fenced shell already subsumes: dropping them changes no authority.
_SUBSUMED_FIELDS = frozenset({"source_read", "include_source_excerpt"})


def _fail(message: str, path: str, reason: str):
    raise caller.EmbeddedNokiyError(CODE, message, issues=[{"path": path, "reason": reason}])


def _grants_nothing(value) -> bool:
    return value is None or value is False or (isinstance(value, (list, dict, str)) and not value)


def adapt_action(action: dict) -> tuple[dict, list[dict]]:
    """Resolve harmless field mismatches of an inspect:{} action instead of rejecting them.

    Action templates and the native batch host emit empty grant lists, the fenced shell subsumes
    source_read and source excerpts, and inspect implies exactly read and command. A missing
    context_summary is a format omission: the complete mission already states the task, so it
    supplies the summary. Each change is reported; a nonempty exact grant or a write operation
    still fails compile_context.
    """
    action, repairs = dict(action), []
    for key in sorted(set(action) & (_GRANT_FIELDS | _SUBSUMED_FIELDS)):
        if key in _SUBSUMED_FIELDS:
            rule = "inspect_shell_subsumes"
        elif _grants_nothing(action[key]):
            rule = "empty_grant_omitted"
        else:
            continue
        action.pop(key)
        repairs.append({"path": f"action.{key}", "rule": rule})
    operations = action.get("operations")
    if (isinstance(operations, list) and operations and all(isinstance(op, str) for op in operations)
            and set(operations) <= set(_OPERATIONS) and sorted(operations) != _OPERATIONS):
        action["operations"] = ["read", "command"]
        repairs.append({"path": "action.operations", "rule": "inspect_implies_read_command"})
    summary, mission = action.get("context_summary"), action.get("mission")
    if (summary is None or (isinstance(summary, str) and not summary.strip())) and isinstance(mission, dict):
        objective, predicate = mission.get("objective"), mission.get("current_predicate")
        if all(isinstance(value, str) and value.strip() for value in (objective, predicate)):
            text = f"{objective.strip()} Current predicate: {predicate.strip()}."
            action["context_summary"] = text[:_MAX_SUMMARY_CHARS - len(NOTICE) - 512]
            repairs.append({"path": "action.context_summary", "rule": "summary_from_mission"})
    return action, repairs


def _sbpl(text: str) -> str:
    return json.dumps(text)


def layout(root: Path) -> tuple[Path, Path, Path]:
    return root / "evidence", root / "scratch", root / "profile.sb"


def action_root(artifact_root: Path, action: dict) -> Path:
    # artifact_root is shared by a parent's requests; each action gets its own evidence root.
    return artifact_root / "inspect" / caller._canonical_sha256(action)[:16]


def profile_text(artifact_root: Path) -> str:
    """The engine fence for inspect runs: full_core's signal boundary plus write confinement.

    Temporary directories stay writable: zsh here-documents (TMPPREFIX) and many tools use /tmp or
    the per-user /var/folders temp/cache directories regardless of TMPDIR.
    The Playwright cache stays writable too: on macOS its browser registry and daemon state live
    there with no relocation setting, and headless browser checks are read-and-record work.
    """
    browsers = Path.home().resolve() / "Library" / "Caches" / "ms-playwright"
    return "\n".join([
        "(version 1)", "(allow default)",
        "(deny signal) (allow signal (target same-sandbox))",
        "(deny file-write*)",
        f"(allow file-write* (subpath {_sbpl(str(artifact_root))}) (subpath \"/private/tmp\") "
        '(subpath "/private/var/folders") (literal "/dev/null") '
        '(literal "/dev/zero") (literal "/dev/tty") (regex #"^/dev/fd/") (regex #"^/dev/ttys[0-9]+$"))',
        f"(allow file-write* (subpath {_sbpl(str(browsers))}))",
    ]) + "\n"


def tool_env(inspect_root: Path) -> dict[str, str]:
    """npm and Playwright keep state under $HOME by default; point it where the fence allows writes."""
    evidence, scratch, _ = layout(inspect_root)
    return {
        "npm_config_cache": str(_user_cache_dir(scratch) / "nokiy-inspect-npm"),
        "npm_config_logs_dir": str(scratch / "npm-logs"),
        "npm_config_update_notifier": "false",
        "PWTEST_DAEMON_SESSION_DIR": str(scratch / "playwright-daemon"),
        # macOS limits Unix socket paths to 104 bytes; the scratch path is far longer.
        "PWTEST_SOCKETS_DIR": "/private/tmp/nkpw-" + hashlib.sha256(str(scratch).encode()).hexdigest()[:12],
        "PLAYWRIGHT_MCP_OUTPUT_DIR": str(evidence / "playwright"),
        # Seatbelt cannot nest: Chromium's own sandbox crashes its renderers inside the fence.
        "PLAYWRIGHT_MCP_SANDBOX": "false",
    }


def _user_cache_dir(scratch: Path) -> Path:
    """The per-user cache under /private/var/folders persists across runs; scratch is the fallback."""
    try:
        value = subprocess.run(["/usr/bin/getconf", "DARWIN_USER_CACHE_DIR"], capture_output=True,
                               text=True, timeout=5, check=True).stdout.strip()
        path = Path(value).resolve() if value else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return scratch
    if path is None or not path.is_dir() or not path.is_relative_to("/private/var/folders"):
        return scratch
    return path


def profile_path(contract: dict) -> Path | None:
    policy = contract.get("inspect_shell") if isinstance(contract, dict) else None
    if not isinstance(policy, dict) or not isinstance(policy.get("evidence_root"), str):
        return None
    return Path(policy["evidence_root"]).parent / "profile.sb"


def _write_profile(path: Path, text: str) -> None:
    data = text.encode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        # A reused artifact root keeps one profile; a different policy needs a new root.
        if path.is_symlink() or path.read_bytes() != data:
            _fail("artifact root already holds a different inspect profile", "artifact_root", "profile_conflict")
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def compile_context(workspace: Path, action: dict, *, artifact_root: Path | None) -> tuple[dict, dict]:
    inspect = action.get("inspect")
    if not isinstance(inspect, dict) or inspect:
        _fail("inspect requires an empty object; the engine fence is the only boundary", "action.inspect",
              "invalid_shape")
    granted = sorted(set(action) & _GRANT_FIELDS)
    if granted:
        _fail("inspect actions carry no exact grants; the sandbox is the boundary", "action." + granted[0],
              "exact_grant_with_inspect")
    if artifact_root is None:
        _fail("inspect requires the request artifact_root for evidence", "artifact_root", "missing")
    mission = action.get("mission")
    if not isinstance(mission, dict) or any(not isinstance(mission.get(k), str) or not mission[k].strip()
                                            for k in ("mission_id", "task_id", "mode", "objective", "current_predicate")):
        _fail("complete parent mission required", "action.mission", "invalid_shape")
    summary = action.get("context_summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) + len(NOTICE) > _MAX_SUMMARY_CHARS:
        _fail("bounded task context_summary required", "action.context_summary", "invalid_shape")
    if sorted(set(action.get("operations") or [])) != _OPERATIONS or len(action["operations"]) != 2:
        _fail("inspect requires exactly the read and command operations", "action.operations", "scope_mismatch")
    forbidden = action.get("forbidden_effects", [])
    if (not isinstance(forbidden, list) or len(forbidden) > 64
            or any(not isinstance(v, str) or not v.strip() for v in forbidden) or set(forbidden) & set(_OPERATIONS)):
        _fail("forbidden_effects must be a bounded string list without read/command", "action.forbidden_effects",
              "scope_mismatch")
    evidence, scratch, profile = layout(action_root(artifact_root, action))
    for directory in (evidence, scratch):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            _fail("inspect evidence directories must be real directories", "artifact_root", "unsafe_path")
    evidence, scratch = evidence.resolve(), scratch.resolve()
    _write_profile(profile, profile_text(artifact_root.resolve()))
    policy = {"schema_version": POLICY_SCHEMA, "evidence_root": str(evidence), "network": "inherit",
              "profile_sha256": hashlib.sha256(profile.read_bytes()).hexdigest()}
    generation = {"context_mode": MODE, "dcf_available": False, "repo_root": str(workspace),
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  "required_domain_bindings": {}, "source_snapshot": {}}
    contract = {"schema_version": "jspace_contract_v2", "repo_root": str(workspace),
        "dcf_generation": generation, "provenance": {"context_mode": MODE, "authority": "parent_task"},
        "matched_surface_ids": [], "focused_verifiers": [], "declared_targets": [],
        "read_scopes": [], "write_scopes": [], "allowed_operations": list(_OPERATIONS),
        "denied_operations": ["create", "delete", "modify", "network"],
        "command_templates": [], "command_effect_policy": "trusted_argv_effects_v1",
        "expansion": {"mode": "exact_target_only", "error_code": "JSPACE_EXPANSION_REQUIRED",
                      "mutation_on_expansion": False},
        "inspect_shell": policy}
    auth = {k: contract[k] for k in ("repo_root", "matched_surface_ids", "declared_targets", "read_scopes",
        "write_scopes", "allowed_operations", "denied_operations", "command_templates",
        "command_effect_policy", "expansion", "inspect_shell")}
    auth.update(schema_version="jspace_authorization_v1", required_domain_bindings={})
    contract["authorization_semantic_sha256"] = caller._canonical_sha256(auth)
    contract["content_sha256"] = caller._canonical_sha256(contract)
    capsule = {"schema_version": "task_context_capsule_v1", "mission": mission,
        "context_summary": summary + NOTICE + f" Evidence root: {evidence}.", "dcf_generation": generation,
        "surface": {"repo_root": str(workspace), "matched_surface_ids": [], "declared_targets": []},
        "authority": {"source": "parent_task", "forbidden_effects": sorted(set(forbidden))},
        "evidence_refs": [], "focused_verifiers": [],
        "jspace_semantic_sha256": contract["authorization_semantic_sha256"]}
    capsule["semantic_sha256"] = caller._canonical_sha256(capsule)
    return capsule, contract


def verify_context(workspace: Path, context: dict, contract: dict, *, artifact_root: Path | None) -> None:
    generation = context.get("dcf_generation")
    policy = contract.get("inspect_shell")
    if (artifact_root is None or not isinstance(policy, dict) or generation != contract.get("dcf_generation")
            or not isinstance(generation, dict) or generation.get("context_mode") != MODE
            or generation.get("dcf_available") is not False or generation.get("required_domain_bindings") != {}
            or generation.get("source_snapshot") != {} or generation.get("repo_root") != str(workspace)
            or contract.get("schema_version") != "jspace_contract_v2"
            or contract.get("allowed_operations") != _OPERATIONS
            or any(contract.get(k) != [] for k in ("read_scopes", "write_scopes", "declared_targets",
                                                    "command_templates", "matched_surface_ids"))
            or any(k in contract for k in ("read_commands", "verifier_commands", "source_read"))):
        raise caller.EmbeddedNokiyError(CODE, "inspect context cannot claim exact grants or DCF authority")
    evidence = Path(policy.get("evidence_root", ""))
    root = evidence.parent
    _, scratch, profile = layout(root)
    if (not evidence.is_absolute() or root.parent != (artifact_root / "inspect").resolve()
            or evidence != evidence.resolve() or not evidence.is_dir() or not scratch.is_dir()):
        raise caller.EmbeddedNokiyError(CODE, "inspect evidence root differs from the request artifact root")
    expected = profile_text(artifact_root.resolve()).encode("utf-8")
    if (profile.is_symlink() or not profile.is_file() or profile.read_bytes() != expected
            or hashlib.sha256(expected).hexdigest() != policy.get("profile_sha256")
            or policy.get("network") != "inherit"):
        raise caller.EmbeddedNokiyError("NOKIY_INSPECT_PROFILE_CHANGED", "inspect sandbox profile is not the bound policy")

