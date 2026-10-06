# SPDX-License-Identifier: MIT
"""Bounded closeout postimages and offline verification of typed file changes.

Tool completion and final file state are observations, not semantic acceptance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from collections.abc import Iterable
from typing import Any

from . import embedded_nokiy as caller

SCHEMA = "nokiy_file_change_evidence_v1"
MAX_EVENTS = 128
MAX_TARGETS = 128
MAX_FILE_BYTES = 64 * 1024 * 1024


class EffectError(ValueError):
    pass


def _sha(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _target(workspace: Path, contract: dict[str, Any], path: object, kind: object) -> tuple[str, Path]:
    if kind not in ("add", "create", "update") or not isinstance(path, str):
        raise EffectError("FILE_CHANGE_KIND_INVALID")
    if not workspace.is_absolute() or ".." in workspace.parts:
        raise EffectError("FILE_CHANGE_SCOPE_INVALID")
    target = Path(path)
    if (not target.is_absolute() or ".." in target.parts or "." in path.split("/")
            or "//" in path or str(target) != path):
        raise EffectError("FILE_CHANGE_PATH_INVALID")
    try:
        relative = target.relative_to(workspace).as_posix()
    except ValueError as exc:
        raise EffectError("FILE_CHANGE_SCOPE_INVALID") from exc
    operation = "modify" if kind == "update" else "create"
    if (relative in ("", ".") or relative not in contract.get("write_scopes", ())
            or relative not in contract.get("declared_targets", ())
            or operation not in contract.get("allowed_operations", ())
            or operation in contract.get("denied_operations", ())):
        raise EffectError("FILE_CHANGE_SCOPE_INVALID")
    return relative, target


def _postimage(workspace: Path, relative: str) -> dict[str, Any]:
    # Open from a verified directory descriptor, never re-resolve a mutable
    # ancestor by name between lstat and open.
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(workspace.anchor, flags)
    digest = hashlib.sha256()
    directories = []
    try:
        directory_path = Path(workspace.anchor)
        for part in workspace.parts[1:] + tuple(relative.split("/"))[:-1]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            directory_path /= part
            directories.append((directory_path, os.fstat(descriptor)))
        name = relative.split("/")[-1]
        before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
            raise EffectError("FILE_CHANGE_UNSAFE_FILE")
        with os.fdopen(os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                               dir_fd=descriptor), "rb") as stream:
            opened = os.fstat(stream.fileno())
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                                  s.st_ctime_ns, stat.S_IMODE(s.st_mode))
            if not stat.S_ISREG(opened.st_mode) or identity(before) != identity(opened):
                raise EffectError("FILE_CHANGE_DRIFT")
            size = 0
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                if size > MAX_FILE_BYTES:
                    raise EffectError("FILE_CHANGE_TOO_LARGE")
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if identity(before) != identity(after) or identity(after) != identity(final) or size != after.st_size:
            raise EffectError("FILE_CHANGE_DRIFT")
        for directory, opened_dir in directories:
            current = directory.lstat()
            if (not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (opened_dir.st_dev, opened_dir.st_ino)):
                raise EffectError("FILE_CHANGE_DRIFT")
        return {"sha256": digest.hexdigest(), "bytes": size, "mode": stat.S_IMODE(after.st_mode)}
    finally:
        os.close(descriptor)


def _events(events: Iterable[dict[str, Any]], workspace: Path, contract: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    records: list[dict[str, Any]] = []
    targets: dict[str, Path] = {}
    ids: set[str] = set()
    for index, event in enumerate(events):
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict) or item.get("type") != "file_change":
            continue
        changes = item.get("changes")
        output = item.get("aggregated_output")
        if isinstance(output, str):
            try:
                output = json.loads(output, object_pairs_hook=caller._unique_object,
                                    parse_constant=caller._invalid_constant)
            except ValueError as exc:
                raise EffectError("FILE_CHANGE_EVENT_INVALID") from exc
        if (event.get("type") != "item.completed" or item.get("status") != "completed"
                or item.get("success", True) is not True
                or item.get("isError") is True or not isinstance(item.get("id"), str)
                or not item["id"] or item["id"] in ids
                or ("exit_code" in item and (type(item["exit_code"]) is not int or item["exit_code"] != 0))
                or (output is not None and (not isinstance(output, dict)
                    or output.get("success", True) is not True or output.get("isError") is True
                    or ("exit_code" in output and (type(output["exit_code"]) is not int
                        or output["exit_code"] != 0))))
                or not isinstance(changes, list) or not changes):
            raise EffectError("FILE_CHANGE_EVENT_INVALID")
        ids.add(item["id"])
        if len(records) >= MAX_EVENTS or len(changes) > MAX_TARGETS:
            raise EffectError("FILE_CHANGE_INDEX_PARTIAL")
        normalized = []
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"kind", "path"}:
                raise EffectError("FILE_CHANGE_EVENT_INVALID")
            relative, target = _target(workspace, contract, change["path"], change["kind"])
            targets[relative] = target
            normalized.append({"kind": change["kind"], "path": relative})
        if len(targets) > MAX_TARGETS:
            raise EffectError("FILE_CHANGE_INDEX_PARTIAL")
        records.append({"event_index": index, "event_sha256": caller._canonical_sha256(event),
                        "id": item["id"], "changes": normalized})
    return records, targets


def produce(events: Iterable[dict[str, Any]], request: caller.EmbeddedNokiyRequest,
            contract: dict[str, Any]) -> dict[str, Any] | None:
    records, targets = _events(events, request.workspace, contract)
    if not records:
        return None
    return {"schema_version": SCHEMA, "request_id": request.request_id,
            "request_sha256": request.request_sha256, "native_thread_id": request.native_thread_id,
            "workspace": str(request.workspace), "contract_sha256": caller._canonical_sha256(contract),
            "total_count": len(records), "target_count": len(targets), "records": records,
            "postimages": [{"path": relative, **_postimage(request.workspace, relative)}
                           for relative in sorted(targets)]}


def verify(proof: dict[str, Any], events: Iterable[dict[str, Any]], terminal: dict[str, Any],
           original: dict[str, Any], contract: dict[str, Any]) -> dict[str, int]:
    payload = {key: value for key, value in original.items() if key not in ("request_id", "request_sha256")}
    digest = caller._canonical_sha256(payload)
    reference = original.get("jspace_contract")
    if (original.get("request_id") != terminal.get("request_id")
            or original.get("request_sha256") != digest or terminal.get("request_sha256") != digest
            or original.get("request_id") != "tura_embedded_" + digest
            or original.get("native_thread_id") != terminal.get("native_thread_id")
            or not isinstance(reference, dict) or not _sha(reference.get("sha256"))
            or proof.get("contract_sha256") != caller._canonical_sha256(contract)
            or contract.get("repo_root") != original.get("workspace")):
        raise EffectError("FILE_CHANGE_IDENTITY_MISMATCH")
    expected = {"schema_version": SCHEMA, "request_id": terminal["request_id"],
                "request_sha256": digest, "native_thread_id": terminal["native_thread_id"],
                "workspace": original["workspace"], "contract_sha256": caller._canonical_sha256(contract)}
    if any(proof.get(key) != value for key, value in expected.items()):
        raise EffectError("FILE_CHANGE_IDENTITY_MISMATCH")
    records, targets = _events(events, Path(original["workspace"]), contract)
    # Recovery validates paths lexically and against the copied contract; it must
    # never touch today's mutable workspace files.
    if (not records or caller._canonical_bytes(proof.get("records")) != caller._canonical_bytes(records)
            or type(proof.get("total_count")) is not int or proof["total_count"] != len(records)
            or type(proof.get("target_count")) is not int or proof["target_count"] != len(targets)):
        raise EffectError("FILE_CHANGE_EVENT_MISMATCH")
    postimages = proof.get("postimages")
    if (not isinstance(postimages, list) or len(postimages) != len(targets)
            or [p.get("path") for p in postimages if isinstance(p, dict)] != sorted(targets)):
        raise EffectError("FILE_CHANGE_POSTIMAGE_INVALID")
    if any(not isinstance(p, dict) or set(p) != {"path", "sha256", "bytes", "mode"}
           or not _sha(p["sha256"]) or type(p["bytes"]) is not int or not 0 <= p["bytes"] <= MAX_FILE_BYTES
           or type(p["mode"]) is not int or not 0 <= p["mode"] <= 0o7777 for p in postimages):
        raise EffectError("FILE_CHANGE_POSTIMAGE_INVALID")
    return {"events": len(records), "targets": len(targets)}
