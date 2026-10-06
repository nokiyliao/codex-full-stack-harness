"""Prepare and score a historical, offline coding-task pair; never run an arm.

The arm workspaces contain only a Git archive of the pre-fix source (no .git).
The post-fix verifier is fetched from a pinned Git object only after arm work
has ended; it is passed to an isolated Python test process over stdin and is
never written into either workspace. This is functional evidence, not model,
usage, billing, or causal-efficiency evidence.
"""

from __future__ import annotations

import ast
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import stat
import sys
import tarfile
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "benchmarks" / "recovery-invariants-v1" / "task.md"
BASE = "8c7411de47e8ec2456619b7251427d7ce8062d9f"
BASE_TREE = "6be68dc2d6f10763f98b271092f2cd4a79e87ae9"
FIX = "f906c7eab5cc1772cbe946574d599fd5631eee16"
CORE = "src/codex_collaboration_harness/core.py"
TEST = "tests/test_harness.py"
BASE_CORE_SHA256 = "8762432ec94d6d42237c21bbabec64c1b38cbfcdf4275ae6814ad1dff123dc95"
FIX_TEST_SHA256 = "301daced893d29f881fc202fd794a2610e6deef2acd46e670273c94e383b79e7"
SCHEMA = "paired-recovery-fixture/v1"
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_FILES = 200
MAX_ARM_ENTRIES = 1000
TIMEOUT_SECONDS = 60

TEST_RUNNER = r"""
import io
import json
import sys
import types
import unittest

sys.path.insert(0, sys.argv[1])
module = types.ModuleType("pinned_test_harness")
module.__file__ = "<pinned-test-harness>"
exec(compile(sys.stdin.read(), module.__file__, "exec"), module.__dict__)
suite = unittest.defaultTestLoader.loadTestsFromModule(module)
stream = io.StringIO()
result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
print(json.dumps({"tests_run": result.testsRun, "failures": len(result.failures),
                  "errors": len(result.errors), "skipped": len(result.skipped),
                  "passed": result.wasSuccessful(), "detail": stream.getvalue()[-16000:]}))
sys.exit(0 if result.wasSuccessful() else 1)
"""


class FixtureError(Exception):
    pass


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def bounded_file_digest(root: Path, name: str) -> str | None:
    parts = PurePosixPath(name).parts
    if not parts or PurePosixPath(name).is_absolute() or ".." in parts:
        return None
    directory = None
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_ARCHIVE_BYTES:
                return None
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
        return digest(raw) if len(raw) <= MAX_ARCHIVE_BYTES else None
    except OSError:
        return None
    finally:
        if directory is not None:
            os.close(directory)


def read_regular_file_no_follow(path: Path) -> bytes:
    """Read one bounded file through no-follow descriptors, including ancestors."""
    if not path.is_absolute() or not path.name:
        raise FixtureError("Source and protocol paths must be absolute files")
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_ARCHIVE_BYTES:
                raise FixtureError("Source or protocol is not a bounded regular file")
            raw = stream.read(MAX_ARCHIVE_BYTES + 1)
            after = os.fstat(stream.fileno())
        def identity(item):
            return (item.st_dev, item.st_ino, item.st_size,
                    item.st_mtime_ns, item.st_ctime_ns)
        if len(raw) > MAX_ARCHIVE_BYTES or identity(before) != identity(after):
            raise FixtureError("Source or protocol changed during its bounded read")
        return raw
    except OSError as exc:
        raise FixtureError("Source or protocol path is unavailable or contains a symlink") from exc
    finally:
        os.close(directory)


def unexpected_paths(root: Path, known_files: set[str]) -> list[str]:
    known_directories = {str(parent) for name in known_files
                         for parent in PurePosixPath(name).parents if str(parent) != "."}
    extra: list[str] = []
    stack = [root]
    seen = 0
    while stack:
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                if seen > MAX_ARM_ENTRIES:
                    raise FixtureError("Arm contains too many filesystem entries")
                relative = str(Path(entry.path).relative_to(root))
                if entry.is_symlink():
                    extra.append(relative)
                elif entry.is_dir(follow_symlinks=False):
                    if relative not in known_directories:
                        extra.append(relative + "/")
                    stack.append(Path(entry.path))
                elif relative not in known_files:
                    extra.append(relative)
    return sorted(extra)


def git(repo: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=False,
        timeout=30,
    )
    if completed.returncode:
        raise FixtureError(f"Git object unavailable: {' '.join(args)}: "
                           f"{completed.stderr.decode(errors='replace')[-500:]}")
    return completed.stdout


def pinned_blob(repo: Path, revision: str, path: str, expected: str | None = None) -> bytes:
    raw = git(repo, "show", f"{revision}:{path}")
    if expected is not None and digest(raw) != expected:
        raise FixtureError(f"Pinned blob SHA-256 mismatch: {revision}:{path}")
    return raw


def source_objects(repo: Path) -> tuple[bytes, bytes, bytes]:
    resolved = git(repo, "rev-parse", f"{BASE}^{{commit}}").decode().strip()
    tree = git(repo, "rev-parse", f"{BASE}^{{tree}}").decode().strip()
    if resolved != BASE or tree != BASE_TREE:
        raise FixtureError("Baseline commit/tree identity mismatch")
    core = pinned_blob(repo, BASE, CORE, BASE_CORE_SHA256)
    base_test = pinned_blob(repo, BASE, TEST)
    fixed_test = pinned_blob(repo, FIX, TEST, FIX_TEST_SHA256)
    return core, base_test, fixed_test


def archive_members(raw: bytes) -> list[tuple[str, bytes, int]]:
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise FixtureError("Baseline archive exceeds the bounded fixture size")
    files: list[tuple[str, bytes, int]] = []
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        members = archive.getmembers()
        if len(members) > MAX_FILES:
            raise FixtureError("Baseline archive has too many entries")
        for member in members:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise FixtureError("Unsafe archive path")
            if member.isdir():
                continue
            if not member.isfile():
                raise FixtureError("Special archive member is not allowed")
            stream = archive.extractfile(member)
            if stream is None:
                raise FixtureError("Archive member cannot be read")
            content = stream.read(MAX_ARCHIVE_BYTES + 1)
            if len(content) > MAX_ARCHIVE_BYTES:
                raise FixtureError("Oversized archive member")
            files.append((member.name, content, member.mode))
    return files


def materialize(root: Path, files: list[tuple[str, bytes, int]]) -> None:
    root.mkdir()
    for name, content, mode in files:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
        target.chmod(0o755 if mode & 0o111 else 0o644)


def write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def sandbox_profile(arm: Path, scratch: Path) -> str:
    """Keep candidate code away from other files and the network during tests."""
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise FixtureError("Darwin sandbox-exec is required for verifier execution")
    arm = arm.resolve(strict=True)
    scratch = scratch.resolve(strict=True)
    python = Path(sys.executable).resolve(strict=True)
    if not scratch.is_relative_to(arm) or not scratch.is_dir():
        raise FixtureError("Verifier scratch directory is outside its arm")
    if not python.is_file():
        raise FixtureError("Verifier Python is not a regular file")
    if python.is_relative_to(arm):
        raise FixtureError("Verifier Python cannot come from candidate source")
    python_roots = [str(Path(prefix).resolve(strict=True))
                    for prefix in (sys.prefix, sys.base_prefix)]
    read_roots = {"/System", "/usr", "/bin", "/sbin", "/Library/Apple",
                  str(arm), str(python.parent), *python_roots}
    read = " ".join(f"(subpath {json.dumps(root)})" for root in sorted(read_roots))
    return "\n".join([
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
        f"(allow file-read* {read} (literal \"/\") "
        "(literal \"/dev/null\") (literal \"/dev/urandom\"))",
        f"(allow file-write* (subpath {json.dumps(str(scratch))}) "
        "(literal \"/dev/null\"))",
        "(deny network*)",
    ])


def verifier_environment(scratch: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C",
            "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": str(scratch), "TMP": str(scratch), "TEMP": str(scratch)}


def declared_test_count(source: bytes) -> int:
    tree = ast.parse(source.decode("utf-8"))
    return sum(1 for class_node in tree.body if isinstance(class_node, ast.ClassDef)
               for member in class_node.body
               if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
               and member.name.startswith("test_"))


def prepare(repo: Path, output: Path, model: str, effort: str) -> dict[str, Any]:
    if not model.strip() or not effort.strip():
        raise FixtureError("Explicit identical model and effort are required")
    repo = repo.resolve(strict=True)
    task = TASK.read_bytes()
    core, base_test, _ = source_objects(repo)
    archive = git(repo, "archive", "--format=tar", BASE)
    files = archive_members(archive)
    hashes = {name: digest(content) for name, content, _ in files}
    if hashes.get(CORE) != digest(core) or hashes.get(TEST) != digest(base_test):
        raise FixtureError("Archive does not match the pinned source objects")
    if output.exists():
        raise FixtureError("Output must be a new directory; no pair identity is reused")
    output.mkdir(parents=True)
    for arm in ("native", "nokiy"):
        arm_root = output / arm
        materialize(arm_root, files)
        with (arm_root / "BENCHMARK_TASK.md").open("xb") as stream:
            stream.write(task)
    protocol = {
        "schema_version": SCHEMA,
        "task_id": "generic-harness-recovery-invariants-2026-09-03",
        "task_sha256": digest(task),
        "source_repo": str(repo),
        "base_commit": BASE,
        "base_tree": BASE_TREE,
        "heldout_test_sha256": FIX_TEST_SHA256,
        "original_test_sha256": digest(base_test),
        "tracked_sha256": hashes,
        "allowed_edit": CORE,
        "arms": {arm: str((output / arm).resolve()) for arm in ("native", "nokiy")},
        "requested_model": model,
        "requested_effort": effort,
        "run_limit": {"attempts_per_arm": 1, "wall_seconds_per_arm": 1200},
        "scope": "offline generic Python core repair; no Tura entrypoint, provider, broker, deployment or UTM",
        "evidence_boundary": "requested settings only; provider identity, usage, billing and elapsed time unobserved",
    }
    write_json(output / "protocol.json", protocol)
    return protocol


def run_suite(python: Path, arm: Path, source: bytes, profile: str,
              scratch: Path) -> dict[str, Any]:
    expected_count = declared_test_count(source)
    completed = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", profile, str(python), "-I", "-S", "-B", "-c",
         TEST_RUNNER, str(arm / "src")],
        input=source, text=False, capture_output=True, cwd=arm, timeout=TIMEOUT_SECONDS,
        env=verifier_environment(scratch),
    )
    if completed.returncode not in (0, 1):
        raise FixtureError(f"Verifier process failed: {completed.stderr.decode(errors='replace')[-1000:]}")
    try:
        result = json.loads(completed.stdout)
    except (ValueError, UnicodeDecodeError) as exc:
        raise FixtureError("Verifier returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("passed") != (completed.returncode == 0):
        raise FixtureError("Verifier status is inconsistent")
    if (type(result.get("tests_run")) is not int or result["tests_run"] != expected_count or
            type(result.get("failures")) is not int or result["failures"] < 0 or
            type(result.get("errors")) is not int or result["errors"] < 0 or
            type(result.get("skipped")) is not int or result["skipped"] < 0 or
            result["skipped"] != 0 or
            type(result.get("detail")) is not str or
            f"Ran {expected_count} tests" not in result["detail"] or
            result["passed"] != (result["failures"] == 0 and result["errors"] == 0)):
        raise FixtureError("Verifier test-count or result evidence is incomplete")
    return result


def score_arm(arm: Path, tracked: dict[str, str], python: Path,
              base_test: bytes, fixed_test: bytes, task_sha256: str,
              reviewed_core_sha256: str) -> dict[str, Any]:
    changed: list[str] = []
    missing: list[str] = []
    for name, expected in tracked.items():
        actual = bounded_file_digest(arm, name)
        if actual is None:
            missing.append(name)
        elif actual != expected:
            changed.append(name)
    extra = unexpected_paths(arm, set(tracked) | {"BENCHMARK_TASK.md"})
    prompt_intact = bounded_file_digest(arm, "BENCHMARK_TASK.md") == task_sha256
    scoped = prompt_intact and not missing and not extra and set(changed) <= {CORE}
    row = {
        "arm": arm.name,
        "scope_pass": scoped,
        "changed_tracked": sorted(changed),
        "missing_tracked": sorted(missing),
        "unexpected_paths": extra,
        "prompt_intact": prompt_intact,
        "core_sha256": bounded_file_digest(arm, CORE),
    }
    if not scoped:
        return {**row, "original": None, "heldout": None,
                "quality_pass": False, "verification_status": "REJECTED_SCOPE"}
    if (len(reviewed_core_sha256) != 64 or
            any(character not in "0123456789abcdef" for character in reviewed_core_sha256) or
            row["core_sha256"] != reviewed_core_sha256):
        return {**row, "original": None, "heldout": None,
                "quality_pass": False, "verification_status": "REJECTED_UNREVIEWED"}
    with tempfile.TemporaryDirectory(prefix=".verifier-", dir=arm) as directory:
        scratch = Path(directory)
        profile = sandbox_profile(arm, scratch)
        original = run_suite(python, arm, base_test, profile, scratch)
        heldout = run_suite(python, arm, fixed_test, profile, scratch)
    return {**row, "original": original, "heldout": heldout,
            "quality_pass": original["passed"] and heldout["passed"],
            "verification_status": "TESTED"}


def score(protocol_path: Path, protocol_sha256: str, python: Path,
          reviewed_core_sha256: dict[str, str]) -> dict[str, Any]:
    raw_protocol = protocol_path.read_bytes()
    if digest(raw_protocol) != protocol_sha256:
        raise FixtureError("Protocol SHA-256 differs from the frozen reference")
    try:
        protocol = json.loads(raw_protocol)
    except (ValueError, UnicodeDecodeError) as exc:
        raise FixtureError("Protocol is not valid UTF-8 JSON") from exc
    if not isinstance(protocol, dict):
        raise FixtureError("Protocol must be a JSON object")
    if set(reviewed_core_sha256) != {"native", "nokiy"}:
        raise FixtureError("Both arms require an operator-reviewed core digest")
    if python.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise FixtureError("Verifier Python must be the scorer's running interpreter")
    if python.resolve(strict=True).is_relative_to(protocol_path.parent.resolve(strict=True)):
        raise FixtureError("Verifier Python cannot come from the pair workspace")
    if protocol.get("schema_version") != SCHEMA or protocol.get("base_commit") != BASE:
        raise FixtureError("Unsupported or drifted protocol")
    repo = Path(protocol["source_repo"])
    _, base_test, fixed_test = source_objects(repo)
    if (digest(base_test) != protocol.get("original_test_sha256") or
            digest(fixed_test) != protocol.get("heldout_test_sha256")):
        raise FixtureError("Verifier identity mismatch")
    if digest(TASK.read_bytes()) != protocol.get("task_sha256"):
        raise FixtureError("Task prompt identity mismatch")
    archived = archive_members(git(repo, "archive", "--format=tar", BASE))
    archived_hashes = {name: digest(content) for name, content, _ in archived}
    if protocol.get("tracked_sha256") != archived_hashes:
        raise FixtureError("Tracked source inventory differs from the pinned baseline")
    arms = protocol.get("arms", {})
    expected = {name: str((protocol_path.parent / name).resolve())
                for name in ("native", "nokiy")}
    if arms != expected:
        raise FixtureError("Arm path identity mismatch")
    if any((protocol_path.parent / name).is_symlink() for name in expected):
        raise FixtureError("Arm workspace cannot be a symlink")
    results = {name: score_arm(Path(path), protocol["tracked_sha256"], python,
                               base_test, fixed_test, protocol["task_sha256"],
                               reviewed_core_sha256[name])
               for name, path in expected.items()}
    report = {
        "schema_version": "paired-recovery-score/v1",
        "task_id": protocol["task_id"],
        "task_sha256": protocol["task_sha256"],
        "requested_model": protocol["requested_model"],
        "requested_effort": protocol["requested_effort"],
        "python": str(python.resolve()),
        "verifier_sha256": digest(fixed_test),
        "results": results,
        "pair_quality_pass": all(row["quality_pass"] for row in results.values()),
        "arm_execution_observed": False,
        "effective_model_effort": "UNVERIFIED",
        "verifier_os_sandbox": "DARWIN_SEATBELT",
        "operator_review_authorship_authenticated": False,
        "provider_identity": "UNKNOWN",
        "usage": "UNKNOWN",
        "billing": "UNKNOWN",
        "elapsed_efficiency": "NOT_EVALUATED",
        "causal_gain": "NOT_ESTABLISHED",
    }
    return report


def supplemental_functional(protocol_path: Path, protocol_sha256: str, python: Path,
                            source_core: Path, reviewed_core_sha256: str) -> dict[str, Any]:
    """Replay reviewed core bytes in a new source-only workspace, not an arm."""
    protocol_path = Path(os.path.abspath(protocol_path))
    raw_protocol = read_regular_file_no_follow(protocol_path)
    if digest(raw_protocol) != protocol_sha256:
        raise FixtureError("Protocol SHA-256 differs from the frozen reference")
    try:
        protocol = json.loads(raw_protocol)
    except (ValueError, UnicodeDecodeError) as exc:
        raise FixtureError("Protocol is not valid UTF-8 JSON") from exc
    if not isinstance(protocol, dict):
        raise FixtureError("Protocol must be a JSON object")
    if (protocol.get("schema_version") != SCHEMA or protocol.get("base_commit") != BASE or
            protocol.get("base_tree") != BASE_TREE):
        raise FixtureError("Unsupported or drifted protocol")
    if python.resolve(strict=True) != Path(sys.executable).resolve(strict=True):
        raise FixtureError("Verifier Python must be the scorer's running interpreter")
    if python.resolve(strict=True).is_relative_to(protocol_path.parent):
        raise FixtureError("Verifier Python cannot come from the pair workspace")
    expected_arms = {name: str(protocol_path.parent / name)
                     for name in ("native", "nokiy")}
    if protocol.get("arms") != expected_arms:
        raise FixtureError("Arm path identity mismatch")
    expected_source = protocol_path.parent / "nokiy" / CORE
    if source_core != expected_source:
        raise FixtureError("Supplemental source must be the protocol's Nokiy core path")
    if (len(reviewed_core_sha256) != 64 or
            any(character not in "0123456789abcdef" for character in reviewed_core_sha256)):
        raise FixtureError("Reviewed core SHA-256 must be a lowercase digest")
    source = read_regular_file_no_follow(source_core)
    if digest(source) != reviewed_core_sha256:
        raise FixtureError("Supplemental source differs from the reviewed core SHA-256")

    repo = Path(protocol["source_repo"])
    baseline_core, base_test, fixed_test = source_objects(repo)
    if (digest(base_test) != protocol.get("original_test_sha256") or
            digest(fixed_test) != protocol.get("heldout_test_sha256") or
            digest(TASK.read_bytes()) != protocol.get("task_sha256")):
        raise FixtureError("Task or verifier identity mismatch")
    archived = archive_members(git(repo, "archive", "--format=tar", BASE))
    archived_hashes = {name: digest(content) for name, content, _ in archived}
    if (protocol.get("tracked_sha256") != archived_hashes or
            archived_hashes.get(CORE) != digest(baseline_core) or
            archived_hashes.get(TEST) != digest(base_test)):
        raise FixtureError("Tracked source inventory differs from the pinned baseline")

    with tempfile.TemporaryDirectory(prefix="paired-recovery-supplemental-") as directory:
        replay = Path(directory) / "source"
        materialize(replay, archived)
        (replay / CORE).write_bytes(source)
        if bounded_file_digest(replay, CORE) != reviewed_core_sha256:
            raise FixtureError("Materialized supplemental source identity mismatch")
        with tempfile.TemporaryDirectory(prefix=".verifier-", dir=replay) as scratch_dir:
            scratch = Path(scratch_dir)
            profile = sandbox_profile(replay, scratch)
            original = run_suite(python, replay, base_test, profile, scratch)
            heldout = run_suite(python, replay, fixed_test, profile, scratch)
    return {
        "schema_version": "paired-recovery-supplemental-functional/v1",
        "status": "SUPPLEMENTAL_FUNCTIONAL_ONLY",
        "protocol_sha256": protocol_sha256,
        "source_path": str(source_core),
        "source_core_sha256": reviewed_core_sha256,
        "replay_source": "PINNED_BASE_ARCHIVE_PLUS_REVIEWED_CORE_BYTES",
        "base_commit": BASE,
        "base_tree": BASE_TREE,
        "original_test_sha256": digest(base_test),
        "heldout_test_sha256": digest(fixed_test),
        "original": original,
        "heldout": heldout,
        "supplemental_functional_pass": original["passed"] and heldout["passed"],
        "original_pair_score": "UNCHANGED_NOT_REASSESSED",
        "arm_scope": "NOT_REASSESSED",
        "effect_audit": "NOT_REASSESSED",
        "verifier_os_sandbox": "DARWIN_SEATBELT",
        "acceptance": "NOT_ESTABLISHED",
        "causal_gain": "NOT_ESTABLISHED",
        "provider_identity": "UNKNOWN",
        "usage": "UNKNOWN",
        "billing": "UNKNOWN",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--repo", type=Path, default=ROOT)
    prep.add_argument("--output", type=Path, required=True)
    prep.add_argument("--model", required=True)
    prep.add_argument("--effort", required=True)
    verify = sub.add_parser("score")
    verify.add_argument("--protocol", type=Path, required=True)
    verify.add_argument("--protocol-sha256", required=True)
    verify.add_argument("--python", type=Path, default=Path(sys.executable))
    verify.add_argument("--native-reviewed-core-sha256", required=True)
    verify.add_argument("--nokiy-reviewed-core-sha256", required=True)
    supplemental = sub.add_parser("supplemental-functional")
    supplemental.add_argument("--protocol", type=Path, required=True)
    supplemental.add_argument("--protocol-sha256", required=True)
    supplemental.add_argument("--python", type=Path, default=Path(sys.executable))
    supplemental.add_argument("--source-core", type=Path, required=True)
    supplemental.add_argument("--reviewed-core-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            protocol = prepare(args.repo, args.output, args.model, args.effort)
            result = {"prepared": True, "protocol": str(args.output / "protocol.json"),
                      "protocol_sha256": digest((args.output / "protocol.json").read_bytes()),
                      "task_sha256": protocol["task_sha256"]}
        elif args.command == "score":
            result = score(args.protocol, args.protocol_sha256, args.python,
                           {"native": args.native_reviewed_core_sha256,
                            "nokiy": args.nokiy_reviewed_core_sha256})
        else:
            result = supplemental_functional(
                args.protocol, args.protocol_sha256, args.python,
                args.source_core, args.reviewed_core_sha256)
    except (FixtureError, OSError, subprocess.TimeoutExpired, KeyError, TypeError) as exc:
        print(json.dumps({"status": "REJECTED", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
