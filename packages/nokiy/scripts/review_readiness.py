# SPDX-License-Identifier: MIT
"""Run dependency-free checks for a public review-ready repository."""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_LICENSE = "MIT"
REQUIRED_PATHS = (
    "README.md",
    "pyproject.toml",
    "Makefile",
    ".gitignore",
    ".github/workflows/ci.yml",
    ".github/workflows/component-conformance.yml",
    ".github/workflows/release.yml",
    ".github/dependabot.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/pull_request_template.md",
    "src/codex_collaboration_harness/__init__.py",
    "src/codex_collaboration_harness/core.py",
    "src/codex_collaboration_harness/native_nokiy.py",
    "src/codex_collaboration_harness/agents/nokiy.toml",
    "src/codex_collaboration_harness/py.typed",
    "tests/test_harness.py",
    "tests/test_nokiy_adapter.py",
    "tests/test_native_nokiy_role.py",
    "examples/synthetic_demo.py",
    "evidence/internal_benchmark_summary.json",
    "evidence/provenance_manifest.json",
    "scripts/check_provenance.py",
    "scripts/check_components.py",
    "scripts/clean_release_state.py",
    "scripts/normalize_sdist.py",
    "scripts/review_readiness.py",
    "scripts/verify_dist.py",
    "scripts/verify_reproducible_dist.py",
    "src/codex_collaboration_harness/adapters/__init__.py",
    "src/codex_collaboration_harness/adapters/nokiy.py",
    "docs/nokiy-integration.md",
    "docs/native-nokiy-role.md",
    "docs/full-stack-profile.md",
    "components/nokiy-runtime.json",
    "src/codex_collaboration_harness/protocol/nokiy_terminal_envelope_v1.schema.json",
    "src/codex_collaboration_harness/protocol/nokiy_dispatch_request_v1.schema.json",
    "src/codex_collaboration_harness/protocol/native_task_projection_v1.schema.json",
    "src/codex_collaboration_harness/protocol/native_nokiy_execution_profile_v1.schema.json",
    "src/codex_collaboration_harness/protocol/native_nokiy_terminal_v1.schema.json",
    "src/codex_collaboration_harness/protocol/golden/nokiy_dispatch_request_v1.json",
    "src/codex_collaboration_harness/protocol/golden/nokiy_result_v1.json",
    "src/codex_collaboration_harness/protocol/golden/nokiy_failure_v1.json",
)
EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
    "venv",
}
SECRET_PATTERNS = (
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,255}\b")),
    ("OpenAI-style token", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    (
        "private key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "assigned credential",
        re.compile(
            r"(?i)\b(?:api[_-]?key|client[_-]?secret|password|access[_-]?token)"
            r"\s*[:=]\s*['\"][^'\"\s]{12,}['\"]"
        ),
    ),
)
PRIVATE_PATH_PATTERNS = (
    re.compile("/" + "Users" + "/"),
    re.compile("/" + "home" + "/"),
    re.compile("/" + "Volumes" + "/"),
    re.compile(r"[A-Za-z]:\\" + "Users" + r"\\"),
)
GLOBAL_PRIVATE_PROJECT_MARKERS = ("unified_" + "trading_model",)
DOMAIN_PRIVATE_PROJECT_MARKERS = ("u" + "tm",)
DOMAIN_NEUTRAL_ROOTS = {"evidence", "examples", "src", "tests"}
RAW_ID_PATTERNS = (
    (
        "raw collaboration UUID",
        re.compile(
            r"\b019[0-9a-f]{5}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
    ),
    (
        "raw rollout identifier",
        re.compile(r"\brollout-[0-9]{4}-[0-9]{2}-[0-9]{2}[^\s'\"]+", re.IGNORECASE),
    ),
)
LOCAL_PAYLOAD_PATTERNS = (
    ("broker account identifier", re.compile(r"\b(?:DU|U)[0-9]{6,}\b")),
    (
        "account payload",
        re.compile(
            r"(?i)['\"]account(?:_id)?['\"]\s*:\s*['\"]"
            r"(?!synthetic|example|fake)[A-Za-z0-9._-]{6,}['\"]"
        ),
    ),
    (
        "local runtime payload",
        re.compile(r"(?i)['\"](?:account|broker|runtime)_payload['\"]\s*:"),
    ),
)


def _iter_public_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(ROOT)
        if _is_excluded(relative):
            continue
        files.append(path)
    return sorted(files)


def _is_excluded(relative: Path) -> bool:
    """Return whether a repo-relative path is non-versioned build state."""

    return any(
        part in EXCLUDED_DIRECTORIES
        or part.startswith(".venv")
        or part.endswith(".egg-info")
        for part in relative.parts
    )


def _read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _check_required_paths(errors: list[str]) -> None:
    for relative in REQUIRED_PATHS:
        if not (ROOT / relative).is_file():
            errors.append(f"missing required file: {relative}")


def _check_pyproject(errors: list[str]) -> None:
    try:
        data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        errors.append(f"invalid pyproject.toml: {error}")
        return

    project = data.get("project", {})
    if project.get("name") != "codex-collaboration-harness":
        errors.append("project.name must be codex-collaboration-harness")
    if project.get("requires-python") != ">=3.11":
        errors.append("project.requires-python must be >=3.11")
    if project.get("license") != EXPECTED_LICENSE:
        errors.append(
            f"project.license must use the SPDX expression {EXPECTED_LICENSE}"
        )
    if project.get("dependencies") not in (None, []):
        errors.append("runtime dependencies must remain empty")
    scripts = project.get("scripts", {})
    if scripts.get("nokiy-taskpacket") != "codex_collaboration_harness.native_nokiy:main":
        errors.append("project.scripts must expose the Native Nokiy task loader")


def _check_ci(errors: list[str]) -> None:
    path = ROOT / ".github" / "workflows" / "ci.yml"
    release_path = ROOT / ".github" / "workflows" / "release.yml"
    component_path = ROOT / ".github" / "workflows" / "component-conformance.yml"
    try:
        workflow = path.read_text(encoding="utf-8")
        release_workflow = release_path.read_text(encoding="utf-8")
        component_workflow = component_path.read_text(encoding="utf-8")
    except OSError as error:
        errors.append(f"cannot read CI workflow: {error}")
        return
    for required in (
        '"3.11"',
        '"3.12"',
        "make check",
        "make installed-smoke",
        "make release-check",
    ):
        if required not in workflow:
            errors.append(f"CI workflow is missing {required!r}")
    for required in (
        "git cat-file -t",
        "make release-check",
        "subject-checksums: dist/SHA256SUMS",
        "gh release create",
        "--verify-tag",
    ):
        if required not in release_workflow:
            errors.append(f"release workflow is missing {required!r}")
    if "--clobber" in release_workflow:
        errors.append("release workflow must not overwrite existing assets")
    if "make check-components" not in component_workflow:
        errors.append("component workflow is missing 'make check-components'")
    for workflow_name, workflow_text in (
        ("ci", workflow),
        ("release", release_workflow),
    ):
        if "make check-components" in workflow_text:
            errors.append(
                f"{workflow_name} workflow must not gate Native releases on optional components"
            )
    action_pattern = re.compile(r"uses:\s+[^\s#]+@([^\s#]+)")
    for name, text in (
        ("ci", workflow),
        ("release", release_workflow),
        ("component", component_workflow),
    ):
        for reference in action_pattern.findall(text):
            if not re.fullmatch(r"[0-9a-f]{40}", reference):
                errors.append(
                    f"{name} workflow action ref must be a full commit SHA: {reference}"
                )


def _check_spdx(errors: list[str]) -> None:
    for path in _iter_public_files():
        if path.suffix != ".py":
            continue
        text = _read_text(path)
        header = f"SPDX-License-Identifier: {EXPECTED_LICENSE}"
        if text is None or not any(header in line for line in text.splitlines()[:5]):
            errors.append(f"missing SPDX header: {path.relative_to(ROOT).as_posix()}")


def _check_benchmark_label(errors: list[str]) -> None:
    path = ROOT / "evidence" / "internal_benchmark_summary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"invalid benchmark summary: {error}")
        return

    interpretation = data.get("interpretation", {})
    required_false = (
        "causal_claim",
        "external_validity_claim",
        "production_performance_claim",
        "independently_reproduced",
    )
    if data.get("status") != "INTERNAL_ENGINEERING_BENCHMARK":
        errors.append("benchmark status must be INTERNAL_ENGINEERING_BENCHMARK")
    if interpretation.get("internal_only") is not True:
        errors.append("benchmark interpretation.internal_only must be true")
    for field in required_false:
        if interpretation.get(field) is not False:
            errors.append(f"benchmark interpretation.{field} must be false")


def _check_nokiy_component(errors: list[str]) -> None:
    path = ROOT / "components" / "nokiy-runtime.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"invalid Nokiy component manifest: {error}")
        return

    if data.get("schema_version") != "public_runtime_component_v1":
        errors.append(
            "Nokiy component schema_version must be public_runtime_component_v1"
        )
    if data.get("role") != "optional_legacy_external_executor_runtime":
        errors.append("Nokiy component must be classified as an optional legacy runtime")
    if data.get("license") != "AGPL-3.0-or-later":
        errors.append("Nokiy component license must be AGPL-3.0-or-later")
    repository = data.get("repository")
    if repository != "https://github.com/nokiyliao/tura":
        errors.append("Nokiy component repository must use the reviewed public fork")
    for field in (
        "public_ref_commit",
        "modified_source_parent",
        "benchmarked_candidate_commit",
        "upstream_base_commit",
    ):
        value = data.get(field)
        if (
            not isinstance(value, str)
            or len(value) != 40
            or any(character not in "0123456789abcdef" for character in value)
        ):
            errors.append(f"Nokiy component {field} must be a lowercase Git SHA")
    evidence = data.get("evidence_boundaries", {})
    if evidence.get("public_ref") != "SOURCE_PUBLICATION_ONLY":
        errors.append("Nokiy public ref must not imply installed verification")
    if evidence.get("benchmarked_candidate") != "ISOLATED_INTERNAL_BENCHMARK":
        errors.append("Nokiy benchmark candidate boundary is missing")
    if evidence.get("installed_or_running") != "NOT_CLAIMED":
        errors.append("Nokiy component must not claim installed or running adoption")


def _check_native_nokiy_role(errors: list[str]) -> None:
    path = ROOT / "src" / "codex_collaboration_harness" / "agents" / "nokiy.toml"
    try:
        role_bytes = path.read_bytes()
        role = tomllib.loads(role_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        errors.append(f"invalid Native Nokiy role: {error}")
        return

    if role.get("name") != "nokiy":
        errors.append("Native Nokiy role name must be nokiy")
    instructions = role.get("developer_instructions")
    if not isinstance(instructions, str):
        errors.append("Native Nokiy role developer_instructions must be a string")
        return
    for required in (
        "existing Codex session, tools, persistence, and parent callback",
        "FIRST_FALSE_PREDICATE",
        "SHORTEST_VALID_ROUTE",
        "EXPECTED_PREDICATE_DELTA",
        "ABANDON_IF",
        "Do not create another goal, database, lifecycle owner",
        "nokiy-taskpacket load",
    ):
        if required not in instructions:
            errors.append(f"Native Nokiy role is missing {required!r}")


def _check_public_content(errors: list[str]) -> None:
    for path in _iter_public_files():
        text = _read_text(path)
        if text is None:
            continue
        relative = path.relative_to(ROOT).as_posix()
        lowered = text.lower()
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                errors.append(f"possible {label} in {relative}")
        for pattern in PRIVATE_PATH_PATTERNS:
            if pattern.search(text):
                errors.append(f"private absolute path in {relative}")
        for marker in GLOBAL_PRIVATE_PROJECT_MARKERS:
            if re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered):
                errors.append(f"private project marker in {relative}")
        if path.relative_to(ROOT).parts[0] in DOMAIN_NEUTRAL_ROOTS:
            for marker in DOMAIN_PRIVATE_PROJECT_MARKERS:
                if re.search(rf"(?<![a-z0-9]){re.escape(marker)}(?![a-z0-9])", lowered):
                    errors.append(f"private project marker in {relative}")
        for label, pattern in RAW_ID_PATTERNS:
            if pattern.search(text):
                errors.append(f"{label} in {relative}")
        for label, pattern in LOCAL_PAYLOAD_PATTERNS:
            if pattern.search(text):
                errors.append(f"{label} in {relative}")


def main() -> int:
    errors: list[str] = []
    _check_required_paths(errors)
    _check_pyproject(errors)
    _check_ci(errors)
    _check_spdx(errors)
    _check_benchmark_label(errors)
    _check_nokiy_component(errors)
    _check_native_nokiy_role(errors)
    _check_public_content(errors)

    if errors:
        print("review readiness check failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(
        f"review readiness check passed: scanned {len(_iter_public_files())} public files"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
