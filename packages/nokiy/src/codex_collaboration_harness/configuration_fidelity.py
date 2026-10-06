# SPDX-License-Identifier: MIT
"""Diagnostic evidence about a configured TOML file, never effective settings.

Only the supplied file is read, once. Values are hashed using type-tagged
canonical JSON; tables are flattened into exact string-component key paths.
Lists (including arrays of tables) are fingerprinted as a whole at their key.
Empty tables, including an empty root at path [], have fingerprints too.
The canonical parsed-content digest hashes the sorted path/fingerprint
manifest, excluding file provenance and raw formatting.

Key paths and file names remain visible. Unsalted hashes are not encryption:
guessable values can be tested against them. These records are neither an
attestation nor permission to compare benchmark runs, and identify no writer.
"""

from __future__ import annotations

import hashlib
import json
import math
import tomllib
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

__all__ = [
    "ConfigurationFidelityError",
    "ConfigurationReadError",
    "ConfigurationParseError",
    "InvalidConfigurationSnapshot",
    "snapshot_configuration",
    "compare_configurations",
]

_SNAPSHOT_SCHEMA = "configuration_fidelity_snapshot_v1"
_COMPARISON_SCHEMA = "configuration_fidelity_comparison_v1"


class ConfigurationFidelityError(ValueError):
    """Base class for sanitized configuration-evidence errors."""


class ConfigurationReadError(ConfigurationFidelityError):
    """The supplied configured file could not be read."""


class ConfigurationParseError(ConfigurationFidelityError):
    """The configured file could not be parsed or fingerprinted."""


class InvalidConfigurationSnapshot(ConfigurationFidelityError):
    """Snapshot structure or comparison evidence is inconsistent."""


def _boundary() -> dict[str, Any]:
    return {
        "diagnostic_only": True,
        "permission_effect": "none",
        "comparison_eligibility": "not_granted",
        "writer": "unknown",
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _typed_value(value: Any) -> list[Any]:
    # These representations are transient hash inputs, never output evidence.
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["int", str(value)]
    if type(value) is float:
        token = value.hex()
        if math.isnan(value):
            token = "-nan" if math.copysign(1.0, value) < 0 else "nan"
        return ["float", token]
    if type(value) is str:
        return ["string", value]
    if type(value) is datetime:
        kind = "local_datetime" if value.utcoffset() is None else "offset_datetime"
        return [kind, value.isoformat()]
    if type(value) is date:
        return ["date", value.isoformat()]
    if type(value) is time:
        return ["time", value.isoformat()]
    if type(value) is list:
        return ["list", [_typed_value(item) for item in value]]
    if type(value) is dict:
        return ["table", [[key, _typed_value(value[key])] for key in sorted(value)]]
    raise ConfigurationParseError("Unsupported parsed TOML value type.")


def _fingerprints(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def visit(value: Any, path: tuple[str, ...]) -> None:
        if type(value) is dict and value:
            for key in sorted(value):
                visit(value[key], (*path, key))
        else:
            result.append({"path": list(path), "sha256": _digest(_typed_value(value))})

    visit(parsed, ())
    return result


def _manifest_digest(fingerprints: list[dict[str, Any]]) -> str:
    return _digest(["toml-path-fingerprints-v1", fingerprints])


def snapshot_configuration(path: Path) -> dict[str, Any]:
    """Read exactly ``path`` once and return value-free configured-file evidence.

    Table/key order and formatting are ignored by the parsed-content digest;
    list order, scalar types, signed zero/NaN and datetime offsets are retained.
    Read/parse exceptions contain fixed messages, not parser or OS diagnostics.
    """
    if not isinstance(path, Path):
        raise ConfigurationReadError("Configured path must be a pathlib.Path.")
    raw = None
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        pass
    if raw is None:
        # Raise outside the handler so the sensitive exception is not chained.
        raise ConfigurationReadError("Unable to read configured TOML file.")

    parsed = None
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        # ValueError includes TOMLDecodeError and UnicodeDecodeError.
        pass
    if parsed is None:
        raise ConfigurationParseError("Unable to parse configured TOML file.")

    fingerprints = None
    try:
        fingerprints = _fingerprints(parsed)
    except (ValueError, RecursionError):
        pass
    if fingerprints is None:
        raise ConfigurationParseError("Unable to fingerprint configured TOML file.")
    return {
        "schema": _SNAPSHOT_SCHEMA,
        "provenance": {
            "kind": "configured_file",
            "path": str(path),
            "native_effective_settings": False,
        },
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "canonical_parsed_sha256": _manifest_digest(fingerprints),
        "fingerprints": fingerprints,
        **_boundary(),
    }


def _require(condition: bool) -> None:
    if not condition:
        raise InvalidConfigurationSnapshot("Invalid configuration snapshot evidence.")


def _is_sha256(value: Any) -> bool:
    return (
        type(value) is str and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate(snapshot: Any) -> dict[tuple[str, ...], str]:
    boundary = _boundary()
    fields = {
        "schema", "provenance", "raw_sha256", "canonical_parsed_sha256",
        "fingerprints",
    } | set(boundary)
    _require(type(snapshot) is dict and set(snapshot) == fields)
    _require(type(snapshot["schema"]) is str and snapshot["schema"] == _SNAPSHOT_SCHEMA)
    for field, expected in boundary.items():
        _require(type(snapshot[field]) is type(expected) and snapshot[field] == expected)
    provenance = snapshot["provenance"]
    _require(
        type(provenance) is dict
        and set(provenance) == {"kind", "path", "native_effective_settings"}
    )
    _require(type(provenance["kind"]) is str and provenance["kind"] == "configured_file")
    _require(type(provenance["path"]) is str and bool(provenance["path"]))
    _require(provenance["native_effective_settings"] is False)
    _require(_is_sha256(snapshot["raw_sha256"]))
    _require(_is_sha256(snapshot["canonical_parsed_sha256"]))
    entries = snapshot["fingerprints"]
    _require(type(entries) is list and bool(entries))
    result: dict[tuple[str, ...], str] = {}
    previous = None
    for entry in entries:
        _require(type(entry) is dict and set(entry) == {"path", "sha256"})
        _require(type(entry["path"]) is list and all(type(key) is str for key in entry["path"]))
        _require(_is_sha256(entry["sha256"]))
        path = tuple(entry["path"])
        _require(previous is None or previous < path)
        _require(all(path[:length] not in result for length in range(len(path))))
        if not path:
            _require(len(entries) == 1 and entry["sha256"] == _digest(["table", []]))
        result[path] = entry["sha256"]
        previous = path
    _require(snapshot["canonical_parsed_sha256"] == _manifest_digest(entries))
    return result


def compare_configurations(before: dict, after: dict) -> dict[str, Any]:
    """Validate and compare evidence only; never read files or grant eligibility.

    All paths participate, without a model/provider/security-key allowlist.
    Validation checks internal consistency, not authenticity or who wrote a file.
    """
    old = _validate(before)
    new = _validate(after)
    raw_identical = before["raw_sha256"] == after["raw_sha256"]
    semantic_identical = old == new
    _require(semantic_identical == (
        before["canonical_parsed_sha256"] == after["canonical_parsed_sha256"]
    ))
    _require(not raw_identical or semantic_identical)
    return {
        "schema": _COMPARISON_SCHEMA,
        "provenance": {
            "kind": "configured_file",
            "before_path": before["provenance"]["path"],
            "after_path": after["provenance"]["path"],
            "native_effective_settings": False,
        },
        "raw_identical": raw_identical,
        "semantic_identical": semantic_identical,
        "changed_paths": [
            list(path) for path in sorted(old.keys() & new.keys())
            if old[path] != new[path]
        ],
        "added_paths": [list(path) for path in sorted(new.keys() - old.keys())],
        "removed_paths": [list(path) for path in sorted(old.keys() - new.keys())],
        **_boundary(),
    }
