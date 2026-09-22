# SPDX-License-Identifier: MIT
"""Verify public evidence-manifest structure and covered artifact integrity."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "evidence" / "provenance_manifest.json"
EXPECTED_SCHEMA = "public_provenance_manifest_v1"


class ProvenanceError(ValueError):
    """Raised when a provenance manifest or covered artifact is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProvenanceError(f"{label} must be an object")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ProvenanceError(f"{label} must be a lowercase SHA-256 hex digest")
    if any(character not in "0123456789abcdef" for character in value):
        raise ProvenanceError(f"{label} must be a lowercase SHA-256 hex digest")
    return value


def verify_manifest(manifest_path: Path = DEFAULT_MANIFEST) -> list[str]:
    """Return verified repo-relative artifact paths, or raise ProvenanceError."""

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvenanceError(f"cannot load provenance manifest: {error}") from error

    root = _require_mapping(manifest, "manifest")
    if root.get("schema_version") != EXPECTED_SCHEMA:
        raise ProvenanceError(f"schema_version must be {EXPECTED_SCHEMA!r}")

    source = _require_mapping(root.get("source"), "source")
    if source.get("source_kind") != "internal_allowlisted_aggregate_projection":
        raise ProvenanceError(
            "source.source_kind must describe the allowlisted projection"
        )
    _require_sha256(source.get("source_sha256"), "source.source_sha256")
    source_fields = source.get("fields")
    if (
        not isinstance(source_fields, list)
        or not source_fields
        or any(not isinstance(field, str) or not field for field in source_fields)
        or source_fields != sorted(set(source_fields))
    ):
        raise ProvenanceError(
            "source.fields must be a sorted, unique, non-empty string array"
        )
    if source.get("source_path_disclosed") is not False:
        raise ProvenanceError("source.source_path_disclosed must be false")
    if source.get("reproducible_from_public_bytes") is not False:
        raise ProvenanceError("source.reproducible_from_public_bytes must be false")

    entries = root.get("artifacts")
    if not isinstance(entries, list) or not entries:
        raise ProvenanceError("artifacts must be a non-empty array")

    verified: list[str] = []
    root_resolved = ROOT.resolve()
    for index, raw_entry in enumerate(entries):
        entry = _require_mapping(raw_entry, f"artifacts[{index}]")
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            raise ProvenanceError(f"artifacts[{index}].path must be a string")

        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ProvenanceError(f"artifacts[{index}].path is not repo-relative")

        candidate = ROOT.joinpath(*relative.parts)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as error:
            raise ProvenanceError(
                f"artifacts[{index}].path escapes the repository"
            ) from error

        if candidate.is_symlink() or not candidate.is_file():
            raise ProvenanceError(
                f"covered artifact is missing or a symlink: {raw_path}"
            )

        expected_digest = _require_sha256(
            entry.get("sha256"), f"artifacts[{index}].sha256"
        )
        actual_digest = _sha256(candidate)
        if actual_digest != expected_digest:
            raise ProvenanceError(
                f"digest mismatch for {raw_path}: expected {expected_digest}, got {actual_digest}"
            )

        expected_bytes = entry.get("bytes")
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise ProvenanceError(
                f"artifacts[{index}].bytes must be a non-negative integer"
            )
        actual_bytes = candidate.stat().st_size
        if actual_bytes != expected_bytes:
            raise ProvenanceError(
                f"size mismatch for {raw_path}: expected {expected_bytes}, got {actual_bytes}"
            )
        verified.append(raw_path)

    if len(verified) != len(set(verified)):
        raise ProvenanceError("artifacts contains duplicate paths")
    return verified


def main() -> int:
    try:
        verified = verify_manifest()
    except ProvenanceError as error:
        print(f"evidence manifest integrity check failed: {error}")
        return 1
    print(f"evidence manifest integrity check passed: {len(verified)} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
