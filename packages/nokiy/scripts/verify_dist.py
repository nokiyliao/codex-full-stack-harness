# SPDX-License-Identifier: MIT
"""Verify one built wheel/sdist pair and write deterministic checksums."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
SOURCE_ROOT = ROOT / "src"
SOURCE_PACKAGE = SOURCE_ROOT / "codex_collaboration_harness"
REQUIRED_PACKAGE_MEMBERS = (
    "codex_collaboration_harness/embedded_nokiy.py",
    "codex_collaboration_harness/native_nokiy.py",
    "codex_collaboration_harness/agents/nokiy.toml",
    "codex_collaboration_harness/py.typed",
    "codex_collaboration_harness/protocol/nokiy_dispatch_request_v1.schema.json",
    "codex_collaboration_harness/protocol/nokiy_terminal_envelope_v1.schema.json",
    "codex_collaboration_harness/protocol/native_task_projection_v1.schema.json",
    "codex_collaboration_harness/protocol/native_nokiy_execution_profile_v1.schema.json",
    "codex_collaboration_harness/protocol/native_nokiy_terminal_v1.schema.json",
    "codex_collaboration_harness/protocol/golden/nokiy_dispatch_request_v1.json",
    "codex_collaboration_harness/protocol/golden/nokiy_result_v1.json",
    "codex_collaboration_harness/protocol/golden/nokiy_failure_v1.json",
)


def _single(pattern: str) -> Path:
    matches = sorted(DIST.glob(pattern))
    if len(matches) != 1:
        raise RuntimeError(f"expected exactly one {pattern}, found {matches}")
    return matches[0]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_package_members() -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    for path in sorted(SOURCE_PACKAGE.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"source package contains symlink: {path}")
        if not path.is_file() or path.suffix == ".pyc" or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(SOURCE_ROOT).as_posix()
        members[relative] = path.read_bytes()
    if not members:
        raise RuntimeError("source package has no members")
    return members


def _verify_role_bytes(role_bytes: bytes, source: str) -> None:
    try:
        role = tomllib.loads(role_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError(f"{source} Native Nokiy role is invalid: {error}") from error
    if role.get("name") != "nokiy":
        raise RuntimeError(f"{source} Native Nokiy role name differs")


def _verify_members(
    wheel: Path, sdist: Path, source_members: dict[str, bytes]
) -> None:
    role_member = "codex_collaboration_harness/agents/nokiy.toml"
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = {
            name
            for name in archive.namelist()
            if name.startswith("codex_collaboration_harness/")
            and not name.endswith("/")
        }
        if wheel_members != set(source_members):
            raise RuntimeError(
                "wheel package members differ from source: "
                f"missing={sorted(set(source_members) - wheel_members)}, "
                f"unknown={sorted(wheel_members - set(source_members))}"
            )
        for member, expected in source_members.items():
            if archive.read(member) != expected:
                raise RuntimeError(f"wheel {member} bytes differ from source")
        _verify_role_bytes(archive.read(role_member), "wheel")
    for member in REQUIRED_PACKAGE_MEMBERS:
        if member not in wheel_members:
            raise RuntimeError(f"wheel is missing {member}")

    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        if any(item.issym() or item.islnk() for item in members):
            raise RuntimeError("sdist must not contain symlink or hardlink members")
        names = {item.name for item in members}
        source_prefix = "/src/"
        sdist_package_members = {
            item.name.split(source_prefix, 1)[1]: item
            for item in members
            if item.isfile()
            and source_prefix in item.name
            and item.name.split(source_prefix, 1)[1].startswith(
                "codex_collaboration_harness/"
            )
        }
        if set(sdist_package_members) != set(source_members):
            raise RuntimeError(
                "sdist package members differ from source: "
                f"missing={sorted(set(source_members) - set(sdist_package_members))}, "
                f"unknown={sorted(set(sdist_package_members) - set(source_members))}"
            )
        for member, expected in source_members.items():
            stream = archive.extractfile(sdist_package_members[member])
            if stream is None or stream.read() != expected:
                raise RuntimeError(f"sdist {member} bytes differ from source")
        role_matches = [
            item
            for item in members
            if item.name.endswith(f"/src/{role_member}") and item.isfile()
        ]
        if len(role_matches) != 1:
            raise RuntimeError(
                f"sdist must contain one Native Nokiy role, found {role_matches}"
            )
        role_stream = archive.extractfile(role_matches[0])
        if role_stream is None:
            raise RuntimeError("sdist Native Nokiy role cannot be read")
        _verify_role_bytes(role_stream.read(), "sdist")
    for member in REQUIRED_PACKAGE_MEMBERS:
        suffix = f"/src/{member}"
        if not any(name.endswith(suffix) for name in names):
            raise RuntimeError(f"sdist is missing {member}")


def _verify_clean_install(
    wheel: Path, version: str, source_members: dict[str, bytes]
) -> None:
    with tempfile.TemporaryDirectory(prefix="harness-dist-") as temporary:
        root = Path(temporary)
        venv = root / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = venv / "bin" / "python"
        subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(wheel.resolve()),
            ],
            check=True,
        )
        role_sha256 = hashlib.sha256(
            source_members["codex_collaboration_harness/agents/nokiy.toml"]
        ).hexdigest()
        check = (
            "import hashlib, json, tomllib; "
            "from importlib.metadata import version; "
            "from importlib.resources import files; "
            "import codex_collaboration_harness as harness; "
            f"assert version('codex-collaboration-harness') == {version!r}; "
            "schema = files('codex_collaboration_harness').joinpath("
            "'protocol', 'nokiy_terminal_envelope_v1.schema.json'); "
            "assert json.loads(schema.read_text(encoding='utf-8'))"
            "['properties']['protocol_version']['const'] == "
            "'tura-collaboration/v1'; "
            "native_terminal = files('codex_collaboration_harness').joinpath("
            "'protocol', 'native_nokiy_terminal_v1.schema.json'); "
            "assert json.loads(native_terminal.read_text(encoding='utf-8'))"
            "['properties']['schema_version']['const'] == "
            "'tura_native_terminal_v1'; "
            "assert harness.NATIVE_NOKIY_TERMINAL_MARKER == "
            "'[TURA_NATIVE_TERMINAL_V1]'; "
            "role = files('codex_collaboration_harness').joinpath("
            "'agents', 'nokiy.toml'); "
            "role_bytes = role.read_bytes(); "
            f"assert hashlib.sha256(role_bytes).hexdigest() == "
            f"{role_sha256!r}; "
            "assert tomllib.loads(role_bytes.decode('utf-8'))['name'] == 'nokiy'; "
            "print(harness.__file__)"
        )
        subprocess.run([str(python), "-I", "-c", check], cwd=root, check=True)
        help_result = subprocess.run(
            [str(venv / "bin" / "nokiy-taskpacket"), "--help"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        required_commands = ("prepare-dispatch", "inspect-packets")
        if any(command not in help_result.stdout for command in required_commands):
            raise RuntimeError("installed Native Nokiy CLI is missing required commands")
        if "install-skill" in help_result.stdout:
            raise RuntimeError("retired Nokiy Skill installer is still exposed")
        embedded_help = subprocess.run(
            [str(venv / "bin" / "nokiy-embedded-run"), "--help"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        for command in ("preflight", "run", "read-result"):
            if command not in embedded_help.stdout:
                raise RuntimeError(
                    f"installed embedded Nokiy CLI is missing {command}"
                )
        packet_root = root / "packets"
        packet_root.mkdir()
        inspection = subprocess.run(
            [
                str(venv / "bin" / "nokiy-taskpacket"),
                "--root",
                str(packet_root),
                "inspect-packets",
            ],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        inventory = json.loads(inspection.stdout)
        if inventory["counts"] != {
            "CURRENT_PROFILED": 0,
            "LEGACY_READABLE": 0,
            "REJECTED": 0,
        }:
            raise RuntimeError("installed Native Nokiy packet inventory differs")
        graph_help = subprocess.run(
            [str(venv / "bin" / "nokiy-graph-tool"), "--help"],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout
        for command in ("serve-native", "read-result", "plan-retirement"):
            if command not in graph_help:
                raise RuntimeError(f"installed nokiy graph CLI is missing {command}")
        subprocess.run([str(python), "-m", "pip", "check"], cwd=root, check=True)


def main() -> int:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    wheel = _single("*.whl")
    sdist = _single("*.tar.gz")
    expected_dist_members = {wheel.name, sdist.name, "SHA256SUMS"}
    unexpected = sorted(
        path.name
        for path in DIST.iterdir()
        if path.name not in expected_dist_members
    )
    if unexpected:
        raise RuntimeError(f"dist contains unexpected members: {unexpected}")
    source_members = _source_package_members()
    _verify_members(wheel, sdist, source_members)
    _verify_clean_install(wheel, version, source_members)

    artifacts = sorted((wheel, sdist), key=lambda path: path.name)
    checksum_path = DIST / "SHA256SUMS"
    lines = [f"{_sha256(path)}  {path.name}" for path in artifacts]
    checksum_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    for line, path in zip(lines, artifacts, strict=True):
        expected = line.split()[0]
        if _sha256(path) != expected:
            raise RuntimeError(f"checksum changed after publication for {path.name}")
    print(
        json.dumps(
            {
                "version": version,
                "wheel": wheel.name,
                "sdist": sdist.name,
                "checksums": checksum_path.name,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
